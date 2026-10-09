from copy import deepcopy
from dataclasses import asdict
from itertools import product

from .benchmark import run_benchmark_item, split_batches
from .evaluation import execution_score, score_prediction


def expand_search_space(space):
    names = list(space)
    return [dict(zip(names, values)) for values in product(*(space[name] for name in names))]


def rank_results(results, accuracy_weight=1.0, latency_weight=0.0):
    return sorted(
        results,
        key=lambda item: accuracy_weight * item["accuracy"] - latency_weight * item["latency_s"],
        reverse=True,
    )


def apply_strategy_overrides(strategy, overrides):
    candidate = deepcopy(strategy)
    for path, value in overrides.items():
        target = candidate
        names = path.split(".")
        for name in names[:-1]:
            target = getattr(target, name)
        setattr(target, names[-1], value)
    return candidate


def evaluate_candidate(generator, records, batch_size=10, workers=10, dialect="sqlite", database=None):
    from concurrent.futures import ThreadPoolExecutor

    rows = []
    for batch in split_batches(records, batch_size):
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
            items = list(executor.map(lambda row: run_benchmark_item(generator, row), batch))
        for item in items:
            source, result = item["source"], item["result"]
            if result:
                correct, score_error = score_prediction(result["sql"], source["sql"], dialect)
            else:
                correct, score_error = False, None
            ex, ex_error, ex_note = execution_score(result["sql"], source["sql"], database) if result and database is not None else (None, None, "未执行结果评测")
            rows.append({
                "query": source["query"],
                "gold_sql": source["sql"],
                "pred_sql": result["sql"] if result else "",
                "trace": result.get("trace", {}) if result else {},
                "correct": correct,
                "execution_match": ex,
                "execution_note": ex_note,
                "latency_s": result["latency_s"] if result else item["elapsed_s"],
                "error": item["error"] or score_error or ex_error,
            })
    return {
        "details": rows,
        "count": len(rows),
        "accuracy": sum(row["correct"] for row in rows) / len(rows),
        "execution_accuracy": sum(row["execution_match"] is True for row in rows) / len(rows) if database is not None else None,
        "latency_s": sum(row["latency_s"] for row in rows) / len(rows),
        "errors": sum(bool(row["error"]) for row in rows),
    }


def grid_search(base_strategy, search_space, records, generator_factory, batch_size=10, workers=10,
                dialect="sqlite"):
    results = []
    for index, overrides in enumerate(expand_search_space(search_space), 1):
        strategy = apply_strategy_overrides(base_strategy, overrides)
        metrics = evaluate_candidate(
            generator_factory(strategy), records,
            batch_size=batch_size, workers=workers, dialect=dialect,
        )
        results.append({
            "index": index,
            "overrides": overrides,
            "strategy": asdict(strategy),
            **metrics,
        })
    return rank_results(results, accuracy_weight=1.0, latency_weight=0.0001)
