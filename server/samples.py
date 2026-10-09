"""可选示例资源读取与注册。公开版本不分发数据，未安装资源时返回空清单。"""
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .base import store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
MANIFEST = EXAMPLES / "manifest.json"

router = APIRouter()


def load_manifest():
    if not MANIFEST.exists():
        return {"version": 1, "databases": []}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def find_entry(name):
    entry = next((row for row in load_manifest()["databases"] if row["name"] == name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="未找到该示例库")
    return entry


@router.get("/api/samples")
def list_samples():
    """列出内置示例库。file 缺失时标记 available=false，前端据此禁用入口。"""
    databases = []
    for entry in load_manifest()["databases"]:
        path = ROOT / entry["file"]
        databases.append({**entry, "available": path.exists(), "path": str(path)})
    return {"databases": databases}


@router.get("/api/samples/{name}/questions")
def sample_questions(name: str):
    entry = find_entry(name)
    path = ROOT / "examples" / "datasets" / f"{name}.json"
    if not path.exists():
        return {"questions": []}
    return {"questions": json.loads(path.read_text(encoding="utf-8"))}


@router.post("/api/samples/{name}/load")
def load_sample(name: str):
    """把示例库注册成一条 SQLite 连接，已存在则复用，返回连接记录与示例问题。"""
    entry = find_entry(name)
    path = ROOT / entry["file"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="可选示例库文件缺失，请配置自己的数据库连接")

    connection_name = f"示例 · {entry['title']}"
    existing = next((row for row in store.list_connections()
                     if row["name"] == connection_name and row["type"] == "SQLite"), None)
    if existing:
        connection = existing
    else:
        connection_id = store.save_connection(connection_name, "SQLite",
                                              {"database": str(path), "read_only": True})
        connection = {"id": connection_id, "name": connection_name, "type": "SQLite",
                      "params": json.dumps({"database": str(path), "read_only": True},
                                           ensure_ascii=False)}

    questions_path = ROOT / "examples" / "datasets" / f"{name}.json"
    questions = []
    if questions_path.exists():
        questions = json.loads(questions_path.read_text(encoding="utf-8"))
    return {
        "connection": connection,
        "database": entry,
        "questions": questions,
        "orm_preset": entry.get("orm_preset"),
    }
