import time
from threading import Lock, local

from openai import APIConnectionError, APIStatusError, OpenAI

from .logs import model_retry

# 上游抖动重试的异常：连接失败/超时（含 429 限流与 5xx）。
# 4xx 里除 429 外都是请求本身的问题（key 错、参数错），重试没有意义。
RETRYABLE = (APIConnectionError, APIStatusError)
RETRY_BASE_DELAY_S = 0.5
RETRY_MAX_DELAY_S = 8.0


def retryable_status(error):
    """APIStatusError 只在 429 与 5xx 时重试；其余状态码是请求本身的问题。"""
    if not isinstance(error, APIStatusError):
        return True
    return error.status_code == 429 or error.status_code >= 500


class RequestTimer:
    def __init__(self):
        self.state = local()

    def begin(self, request_started):
        self.state.request_started = request_started
        self.state.first_model_started = None
        self.state.model_s = 0.0
        self.state.model_calls = 0
        self.state.tokens = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        self.state.usage_complete = True

    def record(self, model_started, model_finished, usage=None):
        if self.state.first_model_started is None:
            self.state.first_model_started = model_started
        self.state.model_s += model_finished - model_started
        self.state.model_calls += 1
        self.state.usage_complete &= usage is not None
        if usage is not None:
            for key in self.state.tokens:
                self.state.tokens[key] += getattr(usage, key, 0)

    def snapshot(self):
        before = self.state.first_model_started
        return {
            "before_model_s": round((before - self.state.request_started) if before is not None else 0, 3),
            "model_s": round(self.state.model_s, 3),
            "model_calls": self.state.model_calls,
            "token_usage": dict(self.state.tokens) if self.state.usage_complete else None,
        }


class CallBudget:
    """并发共享硬预算，所有模型/Embedding 请求（含重试）发送前计数。"""
    def __init__(self, limit):
        self.limit, self.used, self.lock = limit, 0, Lock()

    def reserve(self):
        with self.lock:
            if self.used >= self.limit:
                raise RuntimeError(f"模型请求预算 {self.limit} 次已用完；停止新请求")
            self.used += 1


def build_reasoning_extra_body(provider, thinking, custom):
    body = dict(custom)
    if provider == "deepseek" and thinking != "default":
        body["thinking"] = {"type": "enabled" if thinking == "enabled" else "disabled"}
    if provider in {"qwen", "vllm", "sglang"} and thinking != "default":
        kwargs = dict(body.get("chat_template_kwargs", {}))
        kwargs["enable_thinking"] = thinking == "enabled"
        body["chat_template_kwargs"] = kwargs
    return body


class OpenAICompatibleModel:
    def __init__(self, config):
        self.config = config
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            # SDK 自带重试会与我们的退避叠在一起，层数不可控，关掉自己来。
            max_retries=0,
        )
        self.request_timer = RequestTimer()
        self.request_timer.begin(time.perf_counter())
        self.budget = None

    def begin_timing(self, request_started):
        self.request_timer.begin(request_started)

    def timing(self):
        return self.request_timer.snapshot()

    def call_with_retry(self, send):
        """有界指数退避重试。

        只对连接失败、超时、429、5xx 重试；重试次数耗尽后把原始异常抛出去，
        不吞、不换成模糊的错误。最后一次失败会把异常原样抛出，调用方拿到的是真实原因。
        """
        for attempt in range(self.config.max_retries + 1):
            try:
                return send()
            except RETRYABLE as error:
                if attempt == self.config.max_retries or not retryable_status(error):
                    raise
                delay = min(RETRY_BASE_DELAY_S * 2 ** attempt, RETRY_MAX_DELAY_S)
                model_retry(attempt + 1, delay, error)
                time.sleep(delay)

    def complete(self, messages, temperature=None):
        extra_body = build_reasoning_extra_body(
            self.config.provider, self.config.thinking, self.config.extra_body
        )

        def send():
            if self.budget is not None:
                self.budget.reserve()
            model_started = time.perf_counter()
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature if temperature is None else temperature,
                max_tokens=self.config.max_tokens,
                extra_body=extra_body,
            )
            self.request_timer.record(model_started, time.perf_counter(), getattr(response, "usage", None))
            return response.choices[0].message.content

        return self.call_with_retry(send)

    def embed(self, texts):
        def send():
            if self.budget is not None:
                self.budget.reserve()
            model_started = time.perf_counter()
            response = self.client.embeddings.create(
                model=self.config.embedding_model,
                input=texts,
            )
            self.request_timer.record(model_started, time.perf_counter(), getattr(response, "usage", None))
            return [item.embedding for item in response.data]

        return self.call_with_retry(send)
