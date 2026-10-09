"""数据集生成/判分/导入、评测任务与运行历史。"""
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sqlstage import Text2SQLEngine, connect
from sqlstage.dataset import build_generation_messages, parse_generated_items
from sqlstage.judges import JUDGES, JUDGE_LABELS, judge
from sqlstage.model import OpenAICompatibleModel
from sqlstage.schema import render_schema

from .base import build_database, build_strategy, store

router = APIRouter()


class DatasetPayload(BaseModel):
    name: str
    description: str = ""
    items: list[dict[str, Any]]


class GenerateItemsPayload(BaseModel):
    connection_type: str
    connection: dict[str, Any]
    instruction: str = ""
    count: int = 10
    easy: float = 1
    medium: float = 1
    hard: float = 1
    strategy: dict[str, Any] | None = None


class JudgePayload(BaseModel):
    items: list[dict[str, Any]]
    judge_name: str = "sql_strict"
    strategy: dict[str, Any] | None = None


class ImportPayload(BaseModel):
    name: str
    content: str


class AppendPayload(BaseModel):
    items: list[dict[str, Any]]


class EvaluatePayload(BaseModel):
    dataset_id: int
    judge_name: str = "sql_strict"
    workers: int = 5
    connection_type: str
    connection: dict[str, Any]
    strategy: dict[str, Any] | None = None


@router.get("/api/judges")
def judges():
    return [{"value": key, "label": JUDGE_LABELS[key]} for key in JUDGES]


@router.get("/api/datasets")
def list_datasets():
    return store.list_datasets()


@router.get("/api/datasets/{dataset_id}/items")
def list_items(dataset_id: int):
    return store.list_items(dataset_id)


@router.post("/api/datasets")
def save_dataset(payload: DatasetPayload):
    existing = next((row for row in store.list_datasets() if row["name"] == payload.name), None)
    if existing:
        store.delete_dataset(existing["id"])
    dataset_id = store.create_dataset(payload.name, payload.description)
    store.replace_items(dataset_id, payload.items)
    return {"id": dataset_id}


@router.post("/api/datasets/{dataset_id}/items")
def append_items(dataset_id: int, payload: AppendPayload):
    """把生成页挑中的题目追加进已有数据集，避免整表读改写带来的竞态。"""
    if not payload.items:
        raise HTTPException(status_code=400, detail="没有要追加的题目")
    store.append_items(dataset_id, payload.items)
    return {"dataset_id": dataset_id, "added": len(payload.items),
            "total": len(store.list_items(dataset_id))}


@router.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: int):
    store.delete_dataset(dataset_id)
    return {"deleted": dataset_id}


@router.post("/api/datasets/generate")
def generate_items(payload: GenerateItemsPayload):
    strategy = build_strategy(payload.strategy)
    schema = connect(build_database(payload.connection_type, payload.connection)).schema
    if not schema:
        raise HTTPException(status_code=400, detail="当前连接没有读到任何表")
    messages = build_generation_messages(render_schema(schema), payload.instruction,
                                         payload.count, payload.easy, payload.medium, payload.hard)
    raw = OpenAICompatibleModel(strategy.model).complete(messages)
    return {"items": parse_generated_items(raw)}


@router.post("/api/datasets/judge")
def judge_items(payload: JudgePayload):
    model = OpenAICompatibleModel(build_strategy(payload.strategy).model)
    results = []
    for item in payload.items:
        reference, candidate = item.get("sql", ""), item.get("candidate", "")
        if not reference or not candidate:
            results.append({**item, "score": None, "judge_reason": "缺少参考 SQL 或当前 SQL"})
            continue
        score, reason = judge(payload.judge_name, reference, candidate, item.get("query", ""), model)
        results.append({**item, "score": score, "judge_reason": reason})
    return {"items": results}


@router.post("/api/datasets/import")
def import_items(payload: ImportPayload):
    items = []
    for line in payload.content.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        query = row.get("query") or row.get("question")
        sql = row.get("sql") or row.get("query_sql")
        if not query or not sql:
            raise HTTPException(status_code=400, detail="每行需要 query/question 和 sql/query_sql")
        items.append({"difficulty": row.get("difficulty", "medium"), "query": query, "sql": sql})
    return {"name": payload.name, "items": items}


@router.post("/api/evaluate")
def evaluate(payload: EvaluatePayload):
    items = store.list_items(payload.dataset_id)
    if not items:
        raise HTTPException(status_code=400, detail="数据集为空")
    engine = Text2SQLEngine(build_database(payload.connection_type, payload.connection),
                             build_strategy(payload.strategy))
    run_id = store.create_run("evaluate", payload.dataset_id)

    def work(item):
        result = engine.generate(item["query"])
        score, reason = judge(payload.judge_name, item["sql"], result["sql"], item["query"], engine.model)
        return {"query": item["query"], "gold_sql": item["sql"], "pred_sql": result["sql"],
                "score": score, "reason": reason, "latency_s": result["latency_s"],
                "trace": result["trace"]}

    with ThreadPoolExecutor(max_workers=int(payload.workers)) as executor:
        rows = list(executor.map(work, items))
    summary = {"count": len(rows),
               "accurate": sum(1 for row in rows if row["score"] == 1.0),
               "mean_score": sum(row["score"] for row in rows) / len(rows),
               "empty": sum(1 for row in rows if not row["pred_sql"])}
    store.add_run_items(run_id, rows)
    store.finish_run(run_id, "done", summary)
    return {"run_id": run_id, "rows": rows, "summary": summary}


@router.get("/api/runs")
def list_runs(kind: str | None = None):
    return store.list_runs(kind)


@router.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行不存在")
    return {"run": run, "items": store.list_run_items(run_id)}
