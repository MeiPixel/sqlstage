import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .evaluation import score_prediction
from .search_data import question_with_evidence


def split_batches(rows, batch_size):
    assert 1 <= batch_size <= 10
    return [rows[index:index + batch_size] for index in range(0, len(rows), batch_size)]


def run_benchmark_item(generator, source):
    started = time.perf_counter()
    try:
        result = generator(question_with_evidence(source["query"], source.get("evidence", "")))
        return {"source": source, "result": result, "error": None, "elapsed_s": time.perf_counter() - started}
    except Exception as error:
        return {"source": source, "result": None, "error": f"{type(error).__name__}: {error}",
                "elapsed_s": time.perf_counter() - started}


def run_benchmark(generator, records, output_path, batch_size=10, workers=10, dialect="sqlite"):
    output = Path(output_path)
    rows = []
    started = time.perf_counter()
    for batch_index, batch in enumerate(split_batches(records, batch_size), 1):
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            generated = list(executor.map(lambda row: run_benchmark_item(generator, row), batch))
        for item in generated:
            source = item["source"]
            result = item["result"]
            if result:
                matched, score_error = score_prediction(result["sql"], source["sql"], dialect)
            else:
                matched, score_error = False, None
            error = item["error"] or score_error
            row = {
                "id": source.get("id"),
                "query": source["query"],
                "gold_sql": source["sql"],
                "pred_sql": result["sql"] if result else "",
                "exact_ast_match": matched,
                "latency_s": result["latency_s"] if result else item["elapsed_s"],
                "trace": result["trace"] if result else {},
                "raw": result["raw"] if result else "",
                "error": error,
            }
            rows.append(row)
            print(json.dumps({"batch": batch_index, "done": len(rows), "match": matched}, ensure_ascii=False))
        with output.open("a", encoding="utf-8") as stream:
            for row in rows[-len(batch):]:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "count": len(rows),
        "correct": sum(row["exact_ast_match"] for row in rows),
        "exact_ast_match": sum(row["exact_ast_match"] for row in rows) / len(rows),
        "latency_mean_s": sum(row["latency_s"] for row in rows) / len(rows),
        "elapsed_s": time.perf_counter() - started,
        "batch_size": batch_size,
        "errors": sum(bool(row["error"]) for row in rows),
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
