"""公开版端到端回归：自建数据集、搜索缓存、请求预算、ORM对象。完全离线。"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from support import create_database, question_pool
from fastapi.testclient import TestClient
from sqlstage.config import ModelConfig, StrategyConfig, TrickConfig
from sqlstage.database import SQLiteDatabase
from sqlstage.engine import Text2SQLEngine
from sqlstage.model import CallBudget, OpenAICompatibleModel
from sqlstage.search_data import fixed_sample, normalize_records
from sqlstage.search_runner import run_bound_search
from server.main import app
import sqlstage.engine as engine_module

CALLS = []


def fake_create(**kwargs):
    messages = kwargs["messages"]
    CALLS.append(messages)
    if "现有查询" in messages[-1]["content"]:
        text, completion = 'query = query.filter(brand="Acme")', 9
    elif "JSON查询计划" in messages[0]["content"]:
        text = json.dumps({"from": "products", "select": ["name", "price"],
                           "filters": [{"field": "price", "op": "<", "value": 50}],
                           "order_by": [{"field": "price", "direction": "DESC"}]})
        completion = 61
    else:
        question = messages[-1]["content"]
        text = next((row["sql"] for row in question_pool() if row["query"] == question),
                    "SELECT name, price FROM products WHERE price < 50")
        completion = 28
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                           usage=SimpleNamespace(prompt_tokens=120, completion_tokens=completion,
                                                 total_tokens=120 + completion))


def model(config):
    value = OpenAICompatibleModel(config)
    value.client.chat.completions.create = fake_create
    return value


def test_production():
    original = engine_module.OpenAICompatibleModel
    engine_module.OpenAICompatibleModel = model
    try:
        with TemporaryDirectory() as directory:
            path = create_database(directory)
            database = SQLiteDatabase(path)
            config = StrategyConfig(model=ModelConfig(api_key="fake-key"),
                                    tricks=TrickConfig(sample_rows=0, execution_check=False,
                                                       guide_text="products.price is numeric."))
            rows = normalize_records(question_pool(), schema=database.schema)
            records = fixed_sample(rows, count=2)
            output = Path(directory) / "search"
            space = {"tricks.fewshot_top_k": [0, 3], "tricks.guide_enabled": [False, True]}
            list(run_bound_search(config, records, space, database, pool_rows=rows,
                                  output_root=output, max_predictions=8))
            results = json.loads(next(output.glob("search_*/results.json")).read_text(encoding="utf-8"))
            assert len(results) == 4 and all(row["accuracy"] == 1 for row in results)
            assert all(row["execution_accuracy"] == 1 for row in results)
            assert all(row["details"][0]["trace"]["token_usage"]["completion_tokens"] == 28 for row in results)
            assert results[-1]["actual_model_requests"] == 8
            saved = results[-1]["strategy"]
            assert saved["model"]["api_key"] == ""
            saved["model"]["api_key"] = "fake-key"
            from server.base import build_strategy
            restored = Text2SQLEngine(database, build_strategy(saved))
            assert len(restored.fewshots) == len(rows) - 2
            before = len(CALLS)
            list(run_bound_search(config, records, space, database, pool_rows=rows,
                                  output_root=output, max_predictions=8))
            assert len(CALLS) == before, "缓存不应重复调用模型"
            stop = iter([False, True])
            messages = list(run_bound_search(config, records, space, database, pool_rows=rows,
                            output_root=output, max_predictions=8, should_stop=lambda: next(stop)))
            assert "已停止" in messages[-1][0] and len(messages[-1][1]) == 1
            limited = model(config.model)
            limited.budget = CallBudget(1)
            limited.complete([{"role": "user", "content": "q"}])
            before = len(CALLS)
            try:
                limited.complete([{"role": "user", "content": "q"}])
            except RuntimeError as error:
                assert "预算" in str(error)
            else:
                raise AssertionError("模型请求硬上限失效")
            assert len(CALLS) == before

            client = TestClient(app)
            payload = {"connection_type": "SQLite", "connection": {"database": str(path)},
                       "strategy": {"model": {"api_key": "fake-key"},
                                    "tricks": {"sample_rows": 0, "execution_check": False}},
                       "question": "Products below 50"}
            response = client.post("/api/orm/object", json=payload)
            assert response.status_code == 200, response.text
            first = response.json()
            assert "query = table" in first["orm_code"] and "50" in first["sql"]
            assert first["tables"] and first["columns"]
            second = client.post("/api/orm/object", json={**payload, "plan": first["orm_source_plan"],
                                                        "question": "Only Acme"}).json()
            assert "Acme" in second["sql"] and "50" in second["sql"]
            assert second["trace"]["token_usage"]["completion_tokens"] == 9
            template = client.post("/api/orm/template", json={"plan": second["orm_source_plan"],
                                   "filter_index": 0, "parameter": "budget"}).json()
            before = len(CALLS)
            third = client.post("/api/orm/object", json={**payload, "plan": template["plan"],
                                                       "parameters": {"budget": 70}}).json()
            assert "70" in third["sql"] and "Acme" in third["sql"] and len(CALLS) == before
            assert third["trace"]["token_usage"]["completion_tokens"] == 0
            home = client.post("/api/generate", json={**payload, "messages": [
                {"role": "user", "content": payload["question"]}]}).json()
            assert home["tables"] and home["columns"]
            assert home["trace"]["before_model_s"] >= 0 and home["trace"]["model_s"] >= 0
            assert home["trace"]["token_usage"]["completion_tokens"] > 0
            dataset = client.post("/api/datasets", json={"name": "Public integration dataset",
                                                       "items": question_pool()}).json()
            preview = client.post("/api/search/preview", json={"dataset": f"saved:{dataset['id']}",
                       "connection_type": "SQLite", "connection": {"database": str(path)}})
            assert preview.status_code == 200, preview.text
            assert preview.json()["combinations"] == 4 and preview.json()["predictions"] == 40
            assert "products" in preview.json()["database"]["tables"]
            missing_connection = client.post("/api/search/preview", json={"dataset": f"saved:{dataset['id']}"})
            assert missing_connection.status_code == 400
    finally:
        engine_module.OpenAICompatibleModel = original


if __name__ == "__main__":
    test_production()
    print("test_search_integration: PASS (Mock，不是模型准确率测评)")
