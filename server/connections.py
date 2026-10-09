"""连接、策略方案、生成、Guide 与生成历史。"""
from dataclasses import asdict
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sqlstage import Text2SQLEngine, connect
from sqlstage.guide import generate_database_guide
from sqlstage.model import OpenAICompatibleModel
from sqlstage.schema import render_schema

from .base import BUNDLE_DIR, build_database, build_strategy, split_messages, store

router = APIRouter()


class ConnectionPayload(BaseModel):
    name: str
    type: str
    params: dict[str, Any]


class StrategyPayload(BaseModel):
    name: str
    description: str = ""
    config: dict[str, Any]


class GeneratePayload(BaseModel):
    messages: list[dict[str, Any]]
    connection_type: str
    connection: dict[str, Any]
    strategy: dict[str, Any] | None = None


class GuidePayload(BaseModel):
    connection_type: str
    connection: dict[str, Any]
    strategy: dict[str, Any] | None = None


@router.get("/api/config/defaults")
def defaults():
    return asdict(build_strategy(None))


@router.get("/api/connections")
def list_connections():
    return store.list_connections()


@router.get("/api/connections/{connection_id}")
def get_connection(connection_id: int):
    row = next((item for item in store.list_connections() if item["id"] == connection_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="连接不存在")
    return row


@router.post("/api/connections")
def save_connection(payload: ConnectionPayload):
    return {"id": store.save_connection(payload.name, payload.type, payload.params)}


@router.delete("/api/connections/{connection_id}")
def delete_connection(connection_id: int):
    store.delete_connection(connection_id)
    return {"deleted": connection_id}


@router.get("/api/strategies")
def list_strategies():
    return store.list_strategies()


@router.post("/api/strategies")
def save_strategy(payload: StrategyPayload):
    return {"id": store.save_strategy(payload.name, payload.description, payload.config)}


@router.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: int):
    store.delete_strategy(strategy_id)
    return {"deleted": strategy_id}


@router.post("/api/generate")
def generate(payload: GeneratePayload):
    started = perf_counter()
    question, history = split_messages(payload.messages)
    engine = Text2SQLEngine(build_database(payload.connection_type, payload.connection),
                             build_strategy(payload.strategy))
    result = engine.generate(question, history, request_started=started)
    store.add_generation(question, result["sql"], latency_s=result["latency_s"])
    return result


@router.get("/api/generations")
def list_generations(limit: int = 50):
    return store.list_generations(limit)


@router.get("/api/guide/tables")
def guide_tables():
    return [path.parent.name for path in sorted(BUNDLE_DIR.glob("*/guide.md"))]


@router.get("/api/guide/table/{name}")
def view_table_guide(name: str):
    # name 来自 URL，必须 resolve 后比对白名单，否则 ../ 能读到 examples/ 之外。
    # 与 files.py 的下载端点用同一套校验。
    try:
        resolved = (BUNDLE_DIR / name / "guide.md").resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="表名无法解析")
    if not resolved.is_relative_to(BUNDLE_DIR.resolve()):
        raise HTTPException(status_code=403, detail="表名含非法路径")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="未找到该表 Guide")
    return {"name": resolved.parent.name, "text": resolved.read_text(encoding="utf-8")}


@router.post("/api/guide/generate")
def generate_guide(payload: GuidePayload):
    database = build_database(payload.connection_type, payload.connection)
    schema = connect(database).schema
    if not schema:
        raise HTTPException(status_code=400, detail="当前连接没有读到任何表，请检查连接参数与数据库名")
    model = OpenAICompatibleModel(build_strategy(payload.strategy).model)
    return {"text": generate_database_guide(render_schema(schema), model)}
