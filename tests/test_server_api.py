"""开发模式后端的 HTTP 契约：配置、连接、生成、Guide、数据集、评测、寻优、下载。

模型输出全部由 FakeModel 顶替，不碰外部服务；sqlite 库建在临时目录，跑完即弃。

用原生 assert 直接跑：python tests/test_server_api.py
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlstage.engine as engine_module
import sqlstage.search_demo as search_demo
import sqlstage.search_runner as search_runner
from sqlstage.database import SQLiteDatabase
from sqlstage.config import StrategyConfig
import server.connections as connections_module
import server.datasets as server_datasets_module
from fastapi.testclient import TestClient

from server.main import app
from server.tasks import TaskManager
from sqlstage.config import ModelConfig

client = TestClient(app)
CONNECTION = "api沙盒连接"
DATASET = "api沙盒数据集"
SAMPLE_CONNECTION_PREFIX = "示例 · "
GOLD_SQL = "SELECT 商品名称 FROM 商品 ORDER BY 价格 DESC LIMIT 1"


class FakeModel:
    """按提示内容分支返回，覆盖合成题、判别、Guide 生成三条不同请求。"""

    def __init__(self, config):
        pass

    def begin_timing(self, started):
        pass

    def timing(self):
        return {"before_model_s": 0.0, "model_s": 0.0, "model_calls": 1}

    def complete(self, messages, temperature=None):
        content = messages[0]["content"]
        if "只输出 JSON 数组" in content:
            return f'[{{"difficulty":"easy","query":"价格最高的商品","sql":"{GOLD_SQL}"}}]'
        if '"same"' in content:
            return '{"same": true, "reason": "mock"}'
        if "Database Guide" in content:
            return "## 1 数据库用途\nmock guide"
        return GOLD_SQL


def make_db():
    path = Path(tempfile.mkdtemp()) / "shop.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE 商品(商品ID INTEGER PRIMARY KEY, 商品名称 TEXT, 价格 REAL);"
        "INSERT INTO 商品 VALUES (1, 'A', 10), (2, 'B', 25);")
    connection.commit()
    connection.close()
    return path


def cleanup():
    for row in client.get("/api/connections").json():
        if row["name"] == CONNECTION:
            client.delete(f"/api/connections/{row['id']}")
    for row in client.get("/api/datasets").json():
        if row["name"] == DATASET:
            client.delete(f"/api/datasets/{row['id']}")
    for row in client.get("/api/strategies").json():
        if row["name"] == "api沙盒方案":
            client.delete(f"/api/strategies/{row['id']}")

def test_health_config_and_database():
    # health 会带上 auth_required，调用方据此知道要不要带令牌
    assert client.get("/api/health").json() == {"status": "ok", "auth_required": False}
    defaults = client.get("/api/config/defaults").json()
    assert defaults["model"]["provider"] == "deepseek"
    assert defaults["tricks"]["table_recall"] == "all_schema"
    assert {item["value"] for item in client.get("/api/judges").json()} == {
        "keyword", "sql_strict", "sql_loose", "llm_answer", "llm_sql"}


def test_connection_and_strategy_crud():
    cleanup()
    created = client.post("/api/connections", json={
        "name": CONNECTION, "type": "SQLite", "params": {"database": "test.db"}}).json()
    assert client.get(f"/api/connections/{created['id']}").json()["name"] == CONNECTION
    assert client.get("/api/connections/999999").status_code == 404
    client.delete(f"/api/connections/{created['id']}")

    saved = client.post("/api/strategies", json={
        "name": "api沙盒方案", "description": "描述", "config": {"tricks": {"table_recall": "bm25"}}}).json()
    assert any(row["name"] == "api沙盒方案" for row in client.get("/api/strategies").json())
    client.delete(f"/api/strategies/{saved['id']}")


def test_generate_and_history():
    engine_module.OpenAICompatibleModel = FakeModel
    path = make_db()
    body = client.post("/api/generate", json={
        "messages": [{"role": "user", "content": "价格最高的商品叫什么"}],
        "connection_type": "SQLite",
        "connection": {"database": str(path)}}).json()
    assert body["sql"] == GOLD_SQL, body["sql"]
    assert body["rows"] == [{"商品名称": "B"}], body["rows"]
    assert body["trace"]["executed_tricks"]
    assert body["trace"]["history_messages"] == 0
    assert any(row["sql"] == GOLD_SQL for row in client.get("/api/generations").json())

    multi = client.post("/api/generate", json={
        "messages": [{"role": "user", "content": "价格最高的商品叫什么"},
                     {"role": "assistant", "content": GOLD_SQL},
                     {"role": "user", "content": "那最便宜的呢"}],
        "connection_type": "SQLite", "connection": {"database": str(path)}}).json()
    assert multi["trace"]["history_messages"] == 2


def test_guide_endpoints():
    connections_module.OpenAICompatibleModel = FakeModel
    path = make_db()
    tables = client.get("/api/guide/tables").json()
    assert tables == [], "公开版本不分发 Guide，空列表是正确初始状态"
    assert client.get("/api/guide/table/不存在").status_code == 404
    body = client.post("/api/guide/generate", json={
        "connection_type": "SQLite", "connection": {"database": str(path)}}).json()
    assert "mock guide" in body["text"]


def test_dataset_generate_save_judge_and_evaluate():
    server_datasets_module.OpenAICompatibleModel = FakeModel
    engine_module.OpenAICompatibleModel = FakeModel
    cleanup()
    path = make_db()

    generated = client.post("/api/datasets/generate", json={
        "connection_type": "SQLite", "connection": {"database": str(path)},
        "instruction": "围绕商品", "count": 1, "easy": 1, "medium": 0, "hard": 0}).json()
    assert generated["items"][0]["sql"] == GOLD_SQL

    items = [{**generated["items"][0], "candidate": GOLD_SQL}]
    judged = client.post("/api/datasets/judge", json={"items": items, "judge_name": "sql_strict"}).json()
    assert judged["items"][0]["score"] == 1.0, judged["items"][0]

    imported = client.post("/api/datasets/import", json={
        "name": DATASET, "content": f'{{"query":"价格最高","sql":"{GOLD_SQL}"}}'}).json()
    dataset_id = client.post("/api/datasets", json={
        "name": DATASET, "description": "沙盒", "items": imported["items"]}).json()["id"]
    assert client.get(f"/api/datasets/{dataset_id}/items").json()[0]["sql"] == GOLD_SQL

    evaluated = client.post("/api/evaluate", json={
        "dataset_id": dataset_id, "judge_name": "sql_strict", "workers": 2,
        "connection_type": "SQLite", "connection": {"database": str(path)}}).json()
    assert evaluated["summary"]["accurate"] == 1, evaluated["summary"]

    run = client.get(f"/api/runs/{evaluated['run_id']}").json()
    assert run["run"]["status"] == "done"
    assert run["items"][0]["pred_sql"] == GOLD_SQL
    assert any(item["id"] == evaluated["run_id"] for item in client.get("/api/runs").json())
    cleanup()


def test_search_and_tricks_meta():
    meta = client.get("/api/search/datasets").json()
    assert "电商九表（推荐演示）" not in meta["datasets"]
    assert meta["headers"][0] == "排名"
    assert "问题理解" in client.get("/api/tricks").json()["markdown"]


# 以下为示例库、产物下载与寻优任务




def test_samples():
    """代码-only 版本没有内置数据，但资源接口正常，未知示例明确报 404。"""
    response = client.get("/api/samples")
    assert response.status_code == 200, response.text
    assert response.json()["databases"] == []
    assert client.post("/api/samples/not_exists/load").status_code == 404
    assert client.get("/api/samples/not_exists/questions").status_code == 404


def test_download_whitelist():
    """白名单下载只操作自己创建的临时目录，不覆盖用户的 runs/。"""
    from tempfile import TemporaryDirectory
    import server.files as files_module
    original = files_module.ALLOWED
    try:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            allowed = folder / "runs"
            allowed.mkdir()
            files_module.ALLOWED = (allowed.resolve(), (folder / "examples").resolve())
            target = allowed / "leaderboard.csv"
            target.write_text("a,b\n1,2\n", encoding="utf-8")
            response = client.get("/api/files/download", params={"path": str(target)})
            assert response.status_code == 200 and "1,2" in response.text
            outside = folder / "secret.csv"
            outside.write_text("x", encoding="utf-8")
            assert client.get("/api/files/download", params={"path": str(outside)}).status_code == 403
            traversal = allowed / ".." / "secret.csv"
            assert client.get("/api/files/download", params={"path": str(traversal)}).status_code == 403
            gone = allowed / "missing.csv"
            assert client.get("/api/files/download", params={"path": str(gone)}).status_code == 404
    finally:
        files_module.ALLOWED = original


def test_append_items():
    """追加题目时 ord 接着最大值，不覆盖已有内容。"""
    name = "追加沙盒数据集"
    dataset_id = client.post("/api/datasets", json={
        "name": name, "description": "",
        "items": [{"difficulty": "easy", "query": "第一题", "sql": "SELECT 1"}],
    }).json()["id"]
    try:
        first = client.post(f"/api/datasets/{dataset_id}/items", json={
            "items": [{"difficulty": "medium", "query": "第二题", "sql": "SELECT 2"}]}).json()
        assert first["added"] == 1 and first["total"] == 2, first

        items = client.get(f"/api/datasets/{dataset_id}/items").json()
        assert [row["query"] for row in items] == ["第一题", "第二题"], [row["query"] for row in items]
        assert [row["ord"] for row in items] == [0, 1], [row["ord"] for row in items]

        empty = client.post(f"/api/datasets/{dataset_id}/items", json={"items": []})
        assert empty.status_code == 400, empty.status_code
    finally:
        client.delete(f"/api/datasets/{dataset_id}")


def test_task_manager_cancel():
    """任务中途取消：状态变 stopped，已产出的部分结果保留。"""
    manager = TaskManager()

    def runner(should_stop):
        for index in range(50):
            if should_stop():
                yield f"已停止：完成 {index} 组", [[index]], None
                return
            yield f"完成 {index}", [[index]], None
            time.sleep(0.01)

    task = manager.start("unit", runner)
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(task.id).message:
            break
        time.sleep(0.01)
    manager.stop(task.id)

    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(task.id).status in ("stopped", "done", "error"):
            break
        time.sleep(0.02)

    snapshot = manager.get(task.id).snapshot()
    assert snapshot["status"] == "stopped", snapshot
    assert "已停止" in snapshot["message"], snapshot["message"]
    assert snapshot["table"], "停止后应保留已完成部分"
    assert manager.stop("not-exists") is None

    failing = manager.start("unit", lambda should_stop: (_ for _ in ()).throw(ValueError("boom")))
    deadline = time.time() + 5
    while time.time() < deadline:
        if manager.get(failing.id).status == "error":
            break
        time.sleep(0.02)
    assert manager.get(failing.id).snapshot()["error"] == "ValueError: boom"
    assert manager.get(failing.id).snapshot()["detail"], "失败任务应保留 traceback"


def test_search_stop_integration():
    """run_search 真的响应 should_stop，并把已完成部分写成 CSV。"""
    database_path = make_db()

    original = search_runner.evaluate_candidate
    search_runner.evaluate_candidate = lambda generate, records, **kwargs: {
        "count": len(records), "details": [], "accuracy": 0.5, "latency_s": 0.01, "errors": 0}
    stop_after = []

    def should_stop():
        stop_after.append(1)
        return len(stop_after) >= 2

    try:
        records = [{"query": "Find the most expensive item", "sql": GOLD_SQL},
                   {"query": "List items", "sql": "SELECT 商品名称 FROM 商品"}]
        space = search_demo.demo_space(
            fewshots=["0", "3"], candidates=["1"], guides=["关闭"], descriptions=["0"],
            samples=["0"], table_ks=["3"], table_methods=["BM25"], joins=["开启"],
            column_methods=["全部字段"], column_ks=["30"], same_tables=["关闭"],
            votes=["开启"], selectors=["关闭"], generation_modes=["直出SQL"],
            camel_splits=["开启"], validates=["开启"], fallbacks=["关闭"],
            desc_recalls=["关闭"], dedups=["关闭"])
        assert len(search_demo.expand_demo_space(space)) == 2, "本用例假设只有 2 个组合"

        model = ModelConfig(api_key="sandbox-key")
        final = None
        for message, table, csv_path in search_demo.run_search(
                StrategyConfig(model=model), records, space, SQLiteDatabase(database_path),
                workers=1, should_stop=should_stop, output_root=Path(tempfile.mkdtemp()), use_cache=False):
            final = (message, table, csv_path)

        message, table, csv_path = final
        assert "已停止" in message, message
        assert len(table) == 1, f"应只保留 1 组结果，实际 {len(table)}"
        assert Path(csv_path).is_file(), csv_path
        content = Path(csv_path).read_text(encoding="utf-8-sig")
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines[0].startswith("排名"), lines
        assert len(lines) == 2, f"CSV 应为表头 + 1 行已完成组合，实际 {len(lines)} 行"
    finally:
        search_runner.evaluate_candidate = original


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_server_api: OK")
