"""自动寻优与 Trick 地图。

寻优一轮可能跑几百次模型调用，因此走任务化端点：start 返回任务号，前端轮询
status，cancel 请求停止。同步的 /api/search/run 保留给脚本调用。
"""
from typing import Any
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sqlstage.search_demo import (
    DATASETS, demo_space, load_questions,
)
from sqlstage.search_data import fixed_sample, normalize_records
from sqlstage.search_dimensions import dimensions_to_space
from sqlstage.search_runner import HEADERS, run_bound_search
from sqlstage.search_variants import estimate_predictions, search_variants
from sqlstage.tricks import render_trick_markdown

from .base import ROOT, build_database, build_strategy, store
from .tasks import manager

router = APIRouter()

DIMENSIONS = ("fewshots", "candidates", "guides", "descriptions", "samples", "table_ks",
              "table_methods", "joins", "column_methods", "column_ks", "same_tables", "votes",
              "selectors", "generation_modes", "camel_splits", "validates", "fallbacks",
              "desc_recalls", "dedups")


class PreviewPayload(BaseModel):
    dataset: str
    seed: int = 42
    sample_count: int = Field(10, ge=1, le=50)
    dimensions: dict[str, list[Any]] | None = None
    search_space: dict[str, list[Any]] | None = None
    connection_type: str | None = None
    connection: dict[str, Any] | None = None
    strategy: dict[str, Any] | None = None
    max_predictions: int = Field(50, ge=1, le=50)
    use_cache: bool = True


class RunPayload(PreviewPayload):
    workers: int = Field(5, ge=1, le=10)


def build_space(dimensions, base=None):
    if dimensions is None:
        return {"tricks.fewshot_top_k": [0, 3], "tricks.guide_enabled": [False, True],
                "tricks.candidate_count": [1], "orm.enabled": [False]}
    return dimensions_to_space(dimensions, base or build_strategy(None))


def check_dataset(name):
    datasets = available_datasets(DATASETS, ROOT / "examples/databases")
    if name not in datasets:
        raise HTTPException(status_code=400, detail="未知数据集")
    return datasets[name]


def available_datasets(datasets, database_dir):
    """仅显示题库和对应 SQLite 同时存在的可选示例。"""
    return {name: path for name, path in datasets.items()
            if path.is_file() and (Path(database_dir) / f"{path.stem}.sqlite").is_file()}


def resolve_search_data(payload):
    if payload.dataset in DATASETS:
        path = check_dataset(payload.dataset)
        database = build_database("SQLite", {"database": str(ROOT / "examples/databases" / f"{path.stem}.sqlite"), "read_only": True})
        rows = load_questions(path)
    elif payload.dataset.startswith("saved:"):
        dataset_id = int(payload.dataset.split(":", 1)[1])
        if not any(row["id"] == dataset_id for row in store.list_datasets()):
            raise HTTPException(404, "自建数据集不存在")
        if payload.connection_type is None or payload.connection is None:
            raise HTTPException(400, "自建数据集必须选择对应数据库连接，不会回落商品库")
        database = build_database(payload.connection_type, payload.connection)
        rows = store.list_items(dataset_id)
    else:
        raise HTTPException(400, "未知数据集")
    records = fixed_sample(normalize_records(rows, database.dialect, database.schema), payload.seed, payload.sample_count)
    return database, records, rows


def make_runner(payload: RunPayload):
    """把请求参数固化成 runner，任务线程里再取数据，避免并发读写请求对象。"""
    database, records, pool = resolve_search_data(payload)
    strategy = build_strategy(payload.strategy)
    space = payload.search_space if payload.search_space is not None else build_space(payload.dimensions, strategy)
    predictions = estimate_predictions(search_variants(strategy, space), len(records))
    if predictions > payload.max_predictions:
        raise HTTPException(400, f"当前组合需要 {predictions} 条 SQL，超过 {payload.max_predictions} 条预算，请收窄搜索")
    return lambda should_stop: run_bound_search(strategy, records, space, database, workers=payload.workers,
                                               should_stop=should_stop, pool_rows=pool,
                                               use_cache=payload.use_cache, max_predictions=payload.max_predictions)


@router.get("/api/search/datasets")
def search_datasets():
    saved = store.list_datasets()
    bindings = {name: {"database": path.stem, "sample_name": path.stem, "kind": "builtin"}
                for name, path in available_datasets(DATASETS, ROOT / "examples/databases").items()}
    bindings.update({f"saved:{row['id']}": {"database": "当前选择的连接", "kind": "saved", "title": row["name"]}
                     for row in saved})
    return {"datasets": list(bindings), "bindings": bindings, "headers": HEADERS}


@router.post("/api/search/preview")
def preview(payload: PreviewPayload):
    database, records, _ = resolve_search_data(payload)
    strategy = build_strategy(payload.strategy)
    space = payload.search_space if payload.search_space is not None else build_space(payload.dimensions, strategy)
    variants = search_variants(strategy, space)
    predictions = estimate_predictions(variants, len(records))
    return {"records": records, "database": {"dialect": database.dialect, "tables": list(database.schema)},
            "combinations": len(variants), "predictions": predictions,
            "within_budget": predictions <= payload.max_predictions,
            "budget": f"{len(variants)}组 × 固定{len(records)}题，基础生成{predictions}条候选SQL；所有模型请求合计硬上限{payload.max_predictions}次（选表、选字段、修复、Embedding及重试也计入）。额外Trick可能提前耗尽额度并明确报错。缓存命中不重新调用模型。"}


@router.post("/api/search/start")
def start(payload: RunPayload):
    try:
        task = manager.start("search", make_runner(payload), exclusive=True)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return {"task_id": task.id}


@router.get("/api/search/status/{task_id}")
def status(task_id: str):
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.snapshot()


@router.get("/api/search/results/{task_id}")
def results(task_id: str):
    task = manager.get(task_id)
    if task is None or task.csv_path is None:
        raise HTTPException(404, "尚无已完成的组合")
    rows = json.loads(Path(task.csv_path).with_name("results.json").read_text(encoding="utf-8"))
    return sorted(rows, key=lambda row: (-row["accuracy"], row["errors"], row["latency_s"], row["index"]))


@router.post("/api/search/cancel/{task_id}")
def cancel(task_id: str):
    task = manager.stop(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task_id": task_id, "status": task.status}


@router.post("/api/search/run")
def run(payload: RunPayload):
    """同步跑完整轮，供脚本使用；界面请用 start + status 组合。"""
    final = {"status": "", "table": [], "csv_path": None}
    for status_text, table, csv_path in make_runner(payload)(lambda: False):
        final = {"status": status_text, "table": table, "csv_path": csv_path}
    return final


@router.get("/api/tricks")
def tricks():
    return {"markdown": render_trick_markdown()}
