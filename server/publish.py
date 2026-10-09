"""把策略方案发布成固定的 Text2SQL 接口，并导出成服务模式可加载的配置。"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from sqlstage import Text2SQLEngine

from .base import build_database, build_strategy, split_messages, store
from .service_config import (
    build_service_config, dump_service_config, generate_token, token_sha256,
)

router = APIRouter()


class PublishPayload(BaseModel):
    name: str
    strategy_id: int
    connection_id: int


class PredictPayload(BaseModel):
    messages: list[dict]


class TokenInput(BaseModel):
    name: str
    token: str


class ExportPayload(BaseModel):
    names: list[str] = Field(default_factory=list, description="要导出的服务名，留空表示全部")
    format: str = "yaml"
    inline_secrets: bool = False
    tokens: list[TokenInput] = Field(default_factory=list)


@router.get("/api/publish")
def list_publishes():
    return store.list_publishes()


@router.post("/api/publish")
def save_publish(payload: PublishPayload):
    return {"id": store.save_publish(payload.name, payload.strategy_id, payload.connection_id)}


@router.delete("/api/publish/{name}")
def delete_publish(name: str):
    store.delete_publish(name)
    return {"deleted": name}


@router.post("/api/publish/token")
def new_token():
    token = generate_token()
    return {"token": token, "sha256": token_sha256(token)}


def export_services(names, fmt, inline_secrets, tokens):
    """一次导出一个或多个服务，全部塞进同一份配置。"""
    rows = store.list_publishes()
    if not rows:
        raise HTTPException(status_code=400, detail="还没有发布任何服务")
    known = {row["name"] for row in rows}
    missing = sorted(set(names) - known)
    if missing:
        raise HTTPException(status_code=404, detail=f"未发布的服务：{', '.join(missing)}")
    chosen = [row for row in rows if not names or row["name"] in names]

    services = [{
        "name": row["name"],
        "strategy": store.get_strategy(row["strategy_id"]),
        "connection": store.get_connection(row["connection_id"]),
        "tokens": tokens,
    } for row in chosen]
    config = build_service_config(services, inline_secrets)
    filename = (f"{chosen[0]['name']}.{fmt}" if len(chosen) == 1
                else f"sqlstage-services.{fmt}")
    return {
        "filename": filename,
        "format": fmt,
        "content": dump_service_config(config, fmt),
        "warnings": config["warnings"],
        "services": [row["name"] for row in chosen],
    }


@router.post("/api/publish/export")
def export_all(payload: ExportPayload):
    return export_services(payload.names, payload.format, payload.inline_secrets,
                           [item.model_dump() for item in payload.tokens])


@router.post("/api/publish/{name}/export")
def export_publish(name: str, payload: ExportPayload):
    return export_services([name], payload.format, payload.inline_secrets,
                           [item.model_dump() for item in payload.tokens])


@router.post("/api/publish/{name}/predict")
def predict(name: str, payload: PredictPayload):
    published = store.get_publish(name)
    if published is None:
        raise HTTPException(status_code=404, detail="该名称尚未发布")
    question, history = split_messages(payload.messages)
    connection = store.get_connection(published["connection_id"])
    strategy = store.get_strategy(published["strategy_id"])
    engine = Text2SQLEngine(build_database(connection["type"], json.loads(connection["params"])),
                             build_strategy(json.loads(strategy["config"])))
    result = engine.generate(question, history)
    store.add_generation(question, result["sql"],
                         connection["id"], strategy["id"], result["latency_s"])
    return result
