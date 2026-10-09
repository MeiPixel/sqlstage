"""搜索和首页使用同一引擎；显式绑定数据库，固定预算，完整保存策略。"""
import csv
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .engine import Text2SQLEngine
from .model import CallBudget
from .optimization import evaluate_candidate
from .search_data import prepare_pool
from .search_variants import estimate_predictions, search_variants

ROOT = Path(__file__).resolve().parents[1]
HEADERS = ["排名", "组合ID", "正确数", "AST准确率%", "平均秒", "错误数", "Few-shot", "候选数", "Guide", "完整 Trick 组合"]


def public_strategy(strategy):
    result = asdict(strategy)
    result["model"]["api_key"] = ""
    return result


def search_fingerprint(strategy, records, schema, pool, database):
    path = Path(database.path).resolve()
    stat = path.stat()
    code = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((ROOT / "sqlstage").glob("*.py")))).hexdigest()
    identity = {"version": 2, "strategy": public_strategy(strategy), "records": records, "pool": pool,
                "schema": schema, "database": [str(path), stat.st_size, stat.st_mtime_ns], "code": code}
    wal = Path(str(path) + "-wal")
    if wal.exists():
        identity["wal"] = [wal.stat().st_size, wal.stat().st_mtime_ns]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def strategy_summary(overrides):
    return "；".join(f"{key}={value}" for key, value in overrides.items()) or "完整继承当前策略"


def leaderboard(results):
    ranked = sorted(results, key=lambda row: (-row["accuracy"], row["errors"], row["latency_s"], row["index"]))
    return [[rank, row["index"], f"{round(row['accuracy'] * row['count'])}/{row['count']}",
             round(row["accuracy"] * 100, 2), round(row["latency_s"], 3), row["errors"],
             row["strategy"]["tricks"]["fewshot_top_k"] if row["strategy"]["tricks"]["fewshot_enabled"] else 0,
             1 if row["strategy"]["orm"]["enabled"] else row["strategy"]["tricks"]["candidate_count"],
             "开启" if row["strategy"]["tricks"]["guide_enabled"] else "关闭",
             strategy_summary(row["overrides"])] for rank, row in enumerate(ranked, 1)]


def run_bound_search(base, records, space, database, workers=5, should_stop=None,
                     pool_rows=(), output_root=ROOT / "runs", use_cache=True, max_predictions=50):
    variants = search_variants(base, space)
    if not records or len(records) > 50:
        raise ValueError("搜索固定测试题数量必须在 1—50 之间")
    predictions = estimate_predictions(variants, len(records))
    if predictions > max_predictions:
        raise ValueError(f"需要生成 {predictions} 条 SQL，超过预算 {max_predictions}；请收窄组合或候选数")
    output = Path(output_root) / datetime.now().strftime("search_%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True)
    cache_dir = Path(output_root) / "search_cache"
    cache_dir.mkdir(exist_ok=True)
    schema = database.schema
    manifest = {"records": records, "space": space, "base_strategy": public_strategy(base),
                "dialect": database.dialect, "tables": list(schema), "workers": workers,
                "predictions_budget": max_predictions, "predictions_estimate": predictions}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    results = []
    budget = CallBudget(max_predictions)
    yield f"开始搜索：{len(variants)}组，共 {predictions} 条候选 SQL；绑定 {database.dialect} 数据库。", [], None
    for index, (overrides, strategy) in enumerate(variants, 1):
        if should_stop is not None and should_stop():
            yield f"已停止：保留 {len(results)} 组结果。", leaderboard(results), str(output / "leaderboard.csv") if results else None
            return
        engine = Text2SQLEngine(database, strategy)
        engine.model.budget = budget
        source = engine.fewshots if strategy.tricks.fewshot_path else pool_rows
        engine.fewshots = prepare_pool(source, records, database.dialect, engine.schema, strategy.tricks.fewshot_dedup)
        # SQLite 文件可确定版本；动态 MySQL/PG 不盲目复用旧数据上下文。
        cacheable = use_cache and database.dialect == "sqlite"
        fingerprint = search_fingerprint(strategy, records, engine.schema, engine.fewshots, database) if cacheable else None
        cache_file = cache_dir / f"{fingerprint}.json" if cacheable else None
        if cacheable and cache_file.exists():
            metrics = json.loads(cache_file.read_text(encoding="utf-8"))
            cached = True
        else:
            metrics = evaluate_candidate(engine.generate, records, workers=workers, dialect=database.dialect, database=engine.database)
            cached = False
            if cacheable and metrics["errors"] == 0:
                cache_file.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        reusable = public_strategy(strategy)
        if strategy.tricks.fewshot_enabled:
            pool_path = output / f"fewshot_pool_{index}.jsonl"
            pool_path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in engine.fewshots), encoding="utf-8")
            # 应用最佳方案时必须继承同一份隔离示例池，不能只复制开关。
            reusable["tricks"]["fewshot_path"] = str(pool_path)
        results.append({"index": index, "overrides": overrides, "strategy": reusable,
                        "cached": cached, "fingerprint": fingerprint, "actual_model_requests": budget.used, **metrics})
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        table = leaderboard(results)
        csv_path = output / "leaderboard.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADERS)
            writer.writerows(table)
        yield f"完成 {index}/{len(variants)} 组；{'复用缓存，无模型费用' if cached else '新推理'}；最佳 {table[0][2]}。", table, str(csv_path)
    yield f"搜索完成：{len(results)}组。最佳 #{table[0][1]}，AST准确率 {table[0][3]}%。", table, str(csv_path)
