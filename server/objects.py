"""查询对象接口：自然语言增量真实调用模型；参数绑定只在本地编译。"""
from time import perf_counter
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from sqlstage.engine import Text2SQLEngine
from sqlstage.database import assert_read_only
from sqlstage.query_object import (QueryObject, bind_template, correct_vector_values,
                                  describe_query, follow_object, parameterize)
from sqlstage.schema import render_schema
from .base import build_database, build_strategy

router = APIRouter()


class ObjectPayload(BaseModel):
    connection_type: str
    connection: dict[str, Any]
    strategy: dict[str, Any] | None = None
    question: str = ""
    plan: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None


class TemplatePayload(BaseModel):
    plan: dict[str, Any]
    filter_index: int
    parameter: str


@router.post("/api/orm/template")
def make_template(payload: TemplatePayload):
    return {"plan": parameterize(payload.plan, payload.filter_index, payload.parameter)}


@router.post("/api/orm/object")
def operate_object(payload: ObjectPayload):
    started = perf_counter()
    strategy = build_strategy(payload.strategy)
    strategy.orm.enabled = True
    engine = Text2SQLEngine(build_database(payload.connection_type, payload.connection), strategy)
    if payload.plan is None:
        result = engine.generate(payload.question, request_started=started)
        return result
    if payload.parameters is not None:
        query = bind_template(payload.plan, payload.parameters)
        raw = "query = 已保存模板(参数)"
        engine.model.begin_timing(started)
        timing = engine.model.timing()
    else:
        tables, _ = describe_query(QueryObject(payload.plan).sql(), engine.schema, engine.dialect)
        context = render_schema(engine.schema, tables) + "\n可用表：" + ", ".join(engine.schema)
        query, raw, timing = follow_object(QueryObject(payload.plan), payload.question, engine.model, context, started)
    corrected, corrections = correct_vector_values(query.plan, strategy.orm.vector_fields, engine.database)
    sql = QueryObject(corrected).sql(strategy.orm)
    assert_read_only(sql, engine.dialect)
    tables, columns = describe_query(sql, engine.schema, engine.dialect)
    rows = engine.execute(sql) if strategy.tricks.execution_check else []
    return {"sql": sql, "rows": rows, "tables": tables, "columns": columns,
            "orm_plan": corrected, "orm_source_plan": query.plan, "orm_code": raw, "raw": raw,
            "latency_s": round(perf_counter() - started, 3),
            "trace": {**timing, "value_corrections": corrections, "executed_tricks": ["template_bind" if payload.parameters is not None else "object_increment"]}}
