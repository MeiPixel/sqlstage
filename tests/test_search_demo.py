"""搜索组合、排序和提示参数的离线测试，不依赖私有题库。"""
import json
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from support import create_database, question_pool
from sqlstage.config import ModelConfig, StrategyConfig
from sqlstage.schema import read_sqlite_schema
from sqlstage.search_demo import (DemoEngine, demo_space, expand_demo_space,
                                 leaderboard, question_key, render_context,
                                 sample_dataset, search_budget)


class RecordingModel:
    def __init__(self, outputs=()):
        self.calls = []
        self.outputs = outputs

    def complete(self, messages, temperature=None):
        self.calls.append(messages)
        return self.outputs[len(self.calls) - 1] if self.outputs else "SELECT 1"

    def begin_timing(self, request_started):
        self.request_started = request_started

    def timing(self):
        return {"before_model_s": 0.1, "model_s": 0.2, "model_calls": len(self.calls)}


def test_sampling_grid_and_ranking():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "gold.json"
        path.write_text(json.dumps(question_pool()), encoding="utf-8")
        first = sample_dataset(path, 42)
        assert first == sample_dataset(path, 42) and first != sample_dataset(path, 43)
        assert len(first) == len({row["query"] for row in first}) == 10
    space = demo_space([0, 3, 3], [1, 3, 5], ["关闭", "开启"], [0, 3], [0], [3],
                       ["BM25"], ["开启"], ["全部字段"], [30], ["关闭"],
                       ["开启"], ["关闭"], ["直出SQL"])
    assert len(expand_demo_space(space)) == 24 and "720" in search_budget(space)
    override = expand_demo_space(space)[0]
    rows = [{"index": i, "accuracy": a, "latency_s": t, "errors": 0, "overrides": override}
            for i, a, t in [(1, .8, 9), (2, .9, 10000), (3, .8, 1)]]
    assert [row[1] for row in leaderboard(rows)] == [2, 3, 1]
    assert question_key("你 好？") == question_key("你好")
    full = demo_space([0, 3], [1, 3], ["关闭", "开启"], [3], [0], [3, 5],
                      ["BM25", "全部表"], ["关闭", "开启"], ["全部字段", "BM25"], [10, 30],
                      ["关闭", "开启"], ["关闭", "开启"], ["关闭", "开启"], ["直出SQL", "ORM"])
    combos = expand_demo_space(full)
    assert len(combos) >= 10
    assert any(x["orm.enabled"] for x in combos)
    assert any(x["tricks.join_expansion"] for x in combos)
    assert any(x["tricks.candidate_llm_selector"] for x in combos)
    assert len(combos) == len({json.dumps(x, sort_keys=True) for x in combos})
    llm_space = demo_space([3], [1], ["关闭"], [3], [0], [3], ["大模型选表"],
                          ["关闭"], ["大模型选字段"], [30], ["关闭"], ["关闭"], ["关闭"], ["直出SQL"])
    assert expand_demo_space(llm_space)[0]["tricks.column_recall"] == "llm"
    assert "50次" in search_budget(llm_space)


def test_parameters_reach_the_prompt():
    with TemporaryDirectory() as directory:
        schema = read_sqlite_schema(create_database(directory))
        for meta in schema.values():
            for column in meta["columns"]:
                column["descriptions"] = ["Test field description"]
                column["samples"] = ["Test sample"]
        columns = {"products": [column["name"] for column in schema["products"]["columns"]]}
        bare = render_context(schema, ["products"], columns, 0, 0)
        rich = render_context(schema, ["products"], columns, 3, 2)
        assert "说明：" not in bare and "样例值：" not in bare
        assert "说明：" in rich and "样例值：" in rich and len(rich) > len(bare)
        base = StrategyConfig(model=ModelConfig(api_key="fake-key"))
        base.tricks.fewshot_enabled = True
        base.tricks.fewshot_same_table = False
        base.tricks.execution_check = False
        base.tricks.guide_text = "products.price is the numeric product price."
        pool = question_pool()
        for count in [1, 3, 5]:
            strategy = deepcopy(base)
            strategy.tricks.candidate_count = count
            strategy.tricks.guide_enabled = count != 1
            strategy.tricks.field_description_count = 0 if count == 1 else 3
            strategy.tricks.sample_rows = 0
            engine = DemoEngine(strategy, schema, pool)
            engine.model = RecordingModel()
            result = engine.generate("Find products priced below 55")
            assert len(engine.model.calls) == count
            prompt = str(engine.model.calls[0])
            assert ("Database Guide" in prompt) == (count != 1)
            assert ("说明：" in prompt) == (count != 1)
            assert result["trace"]["fewshot_count"] == 3
        base.tricks.candidate_count = 3
        base.tricks.candidate_vote = False
        engine = DemoEngine(base, schema, pool)
        engine.model = RecordingModel(["SELECT very_long_column_name FROM a", "SELECT 1", "SELECT 2"])
        assert engine.generate("Find products")["sql"] == "SELECT 1"


if __name__ == "__main__":
    test_sampling_grid_and_ranking()
    test_parameters_reach_the_prompt()
    print("test_search_demo: OK")
