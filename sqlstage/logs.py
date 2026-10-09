"""结构化日志：请求进出、模型耗时、数据库探活。

用标准库 logging，不引第三方——日志格式与落盘策略交给部署方（json-file、
Fluent Bit、 Loki 都认这个格式）。默认只输出到 stderr，不写文件。

调用方拿 root logger 的 handler 自行配置级别与输出，本模块只负责内容。
"""
import logging
import sys

logger = logging.getLogger("sqlstage")

if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def request_started(service, question):
    """请求上下文。耗时由 result 自带，这里不重复取时钟。"""
    return {"service": service, "question_chars": len(question)}


def request_finished(context, result):
    """记一次 /predict 的完成日志，延迟由 result 自带，不重复取时钟。"""
    logger.info("predict done service=%s sql_chars=%d rows=%d latency_s=%.3f model_s=%.3f "
                "model_calls=%d",
                context["service"], len(result["sql"]), len(result["rows"]),
                result["latency_s"], result["trace"]["model_s"], result["trace"]["model_calls"])


def request_failed(context, error):
    logger.warning("predict failed service=%s error=%s: %s",
                   context["service"], type(error).__name__, error)


def model_retry(attempt, delay_s, error):
    logger.warning("model retry attempt=%d delay_s=%.2f error=%s: %s",
                   attempt, delay_s, type(error).__name__, error)


def log_database_unavailable(service, error):
    logger.error("database unavailable service=%s error=%s: %s",
                 service, type(error).__name__, error)


def database_recovered(service):
    logger.info("database recovered service=%s", service)


def log_over_capacity(service, active, limit):
    logger.warning("over capacity service=%s active=%d limit=%d", service, active, limit)
