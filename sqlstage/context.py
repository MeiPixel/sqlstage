"""数据库绑定的离线知识；自建库只使用自身 Schema 与注释。"""
import json
from copy import deepcopy
from pathlib import Path

from .schema import render_schema

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def database_context(database, schema, examples=EXAMPLES):
    result = deepcopy(schema)
    builtin = database.dialect == "sqlite" and Path(database.path).resolve() == (examples / "databases/ecommerce_9t.sqlite").resolve()
    if builtin:
        for table, meta in result.items():
            path = examples / "knowledge" / table / "knowledge.jsonl"
            records = {row["field"]: row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())}
            for column in meta["columns"]:
                record = records[column["name"]]
                column["descriptions"] = [record["sql_note"]] if record.get("sql_note") else []
        guide = "\n\n".join((examples / "guides" / table / "guide.md").read_text(encoding="utf-8") for table in result)
    else:
        guide = "仅按真实外键连接，不猜测无声明关系。保留问题条件和返回字段。\n" + render_schema(result)
    return result, guide


def render_descriptions(schema, tables, columns, count):
    return "\n".join(f"{table}.{column['name']} | 说明：" + "；".join(column.get("descriptions", [column["comment"]] if column.get("comment") else [])[:count])
                     for table in tables for column in schema[table]["columns"]
                     if column["name"] in columns[table] and count > 0 and (column.get("descriptions") or column.get("comment")))
