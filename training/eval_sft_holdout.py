"""SFT保留集SQL评估：严格AST规范序列化匹配，不等同官方EM。"""
import json
import time
import urllib.error
import urllib.request
import sqlglot
from sqlglot import exp


def canonical_sql(sql):
    trees = sqlglot.parse(sql, read="sqlite")
    assert len(trees) == 1 and isinstance(trees[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)), "不是单条只读查询"
    tree = trees[0]
    assert not any(tree.find(t) for t in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Into)), "存在写入语句"
    return tree.sql(dialect="sqlite", comments=False)


def evaluate_one(index, record, url, model, timeout=120, opener=urllib.request.urlopen):
    assert record["messages"][-1]["role"] == "assistant"
    gold = record["messages"][-1]["content"]
    gold_ast = canonical_sql(gold)
    payload = {"model": model, "messages": record["messages"][:-1], "temperature": 0,
               "max_tokens": 512, "chat_template_kwargs": {"enable_thinking": False}}
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"})
    result = {"index": index, "gold_sql": gold, "gold_ast_sql": gold_ast,
              "raw_sql": None, "pred_ast_sql": None, "syntax_valid": False,
              "exact_ast_match": False, "usage": None, "finish_reason": None, "error": None}
    started = time.perf_counter()
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        result["latency_s"] = time.perf_counter() - started
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        result["latency_s"] = time.perf_counter() - started
        result["error"] = {"kind": "network", "detail": str(error)}
        if isinstance(error, urllib.error.HTTPError):
            result["error"]["body"] = error.read().decode("utf-8", errors="replace")
        return result
    try:
        data = json.loads(body)
        result["raw_sql"] = data["choices"][0]["message"]["content"]
        result["finish_reason"] = data["choices"][0]["finish_reason"]
        result["usage"] = data.get("usage")
        assert isinstance(result["raw_sql"], str), "响应content不是字符串"
    except (ValueError, KeyError, IndexError, TypeError, AssertionError) as error:
        result["error"] = {"kind": "response", "detail": str(error), "body": body}
        return result
    try:
        result["pred_ast_sql"] = canonical_sql(result["raw_sql"])
        result["syntax_valid"] = True
        result["exact_ast_match"] = result["pred_ast_sql"] == gold_ast
    except (sqlglot.errors.SqlglotError, AssertionError) as error:
        result["error"] = {"kind": "sql", "detail": str(error)}
    return result


def summarize(results, elapsed):
    count = len(results)
    assert count > 0, "评估集为空"
    latencies = sorted(row["latency_s"] for row in results)
    return {"count": count, "syntax_valid_rate": sum(x["syntax_valid"] for x in results) / count,
        "exact_ast_match": sum(x["exact_ast_match"] for x in results) / count,
        "latency_mean_s": sum(latencies) / count,
        "latency_p50_s": latencies[(count - 1) // 2],
        "latency_p95_s": latencies[min(count - 1, int(count * .95))],
        "elapsed_s": elapsed, "error_count": sum(x["error"] is not None for x in results),
        "usage": {key: sum((x["usage"] or {}).get(key, 0) for x in results)
                  for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "metric_note": "sqlglot SQLite AST规范序列化严格匹配；非官方EM，未执行数据库语义校验"}



if __name__ == "__main__":
    import argparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True, help="逐题JSONL；旁边自动生成 .summary.json")
    parser.add_argument("--model", default="qwen3-0.6b")
    parser.add_argument("--url", default="http://127.0.0.1:6006/v1/chat/completions")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    target = Path(args.output)
    summary_path = target.with_suffix(".summary.json")
    assert not target.exists() and not summary_path.exists(), "输出已存在，拒绝覆盖"
    assert args.workers >= 1
    records = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines()]
    assert records
    target.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    results = []
    with target.open("x", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate_one, i, row, args.url, args.model, args.timeout)
                   for i, row in enumerate(records)]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps({"done": len(results), "total": len(records), "index": result["index"],
                "latency_s": round(result["latency_s"], 3), "syntax_valid": result["syntax_valid"],
                "exact_ast_match": result["exact_ast_match"], "error": result["error"]},
                ensure_ascii=False), flush=True)
    summary = summarize(results, time.perf_counter() - start)
    summary.update({"input": args.input, "model": args.model, "url": args.url,
                    "workers": args.workers, "sqlglot_version": sqlglot.__version__})
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)

