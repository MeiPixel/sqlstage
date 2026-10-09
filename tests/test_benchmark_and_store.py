"""批量评测、自动寻优与本地存储。

覆盖 sqlstage/benchmark.py 的分批与并发跑分、optimization.py 的搜索空间展开与策略覆盖，
以及 sqlstage/store.py 的连接、策略、数据集、运行记录与生成历史。

用原生 assert 直接跑：python tests/test_benchmark_and_store.py
"""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage import ModelConfig, StrategyConfig, TrickConfig
from sqlstage.benchmark import run_benchmark, run_benchmark_item, split_batches
from sqlstage.optimization import (
    apply_strategy_overrides, evaluate_candidate, expand_search_space, grid_search, rank_results,
)
from sqlstage.store import Store


def generator_for(answers):
    def generate(question):
        return {"sql": answers[question], "latency_s": 1.0, "trace": {"model_calls": 1}, "raw": ""}
    return generate


def test_split_batches():
    assert split_batches([], 10) == []
    assert split_batches(list(range(10)), 10) == [list(range(10))]
    batches = split_batches(list(range(23)), 10)
    assert [len(batch) for batch in batches] == [10, 10, 3]
    # 分批不能丢题也不能重题
    assert [item for batch in batches for item in batch] == list(range(23))
    # 并发度有上限，超过就说明调用方算错了规模
    try:
        split_batches([1], 11)
    except AssertionError as error:
        assert str(error) == ""
    else:
        raise AssertionError("batch_size 越界应当报错")


def test_run_benchmark_item_records_error():
    """单题失败不中断整批：把异常记进 error 字段，result 留空。"""
    source = {"query": "商品有哪些", "sql": "SELECT * FROM products"}
    ok = run_benchmark_item(generator_for({source["query"]: source["sql"]}), source)
    assert ok["result"]["sql"] == source["sql"] and ok["error"] is None

    def broken(question):
        raise ValueError("SQL被截断")

    failed = run_benchmark_item(broken, source)
    assert failed["result"] is None and failed["error"] == "ValueError: SQL被截断"


def test_run_benchmark_writes_jsonl_and_summary():
    records = [
        {"id": 1, "query": "q1", "sql": "SELECT 1"},
        {"id": 2, "query": "q2", "sql": "SELECT 2"},
        {"id": 3, "query": "q3", "sql": "SELECT 3"},
    ]
    with TemporaryDirectory() as directory:
        output = Path(directory) / "run.jsonl"
        summary = run_benchmark(
            generator_for({"q1": "SELECT 1", "q2": "SELECT 9", "q3": "这不是 SQL"}),
            records, output, batch_size=2, workers=2)
        rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

    assert summary["count"] == 3
    assert summary["correct"] == 1
    assert summary["exact_ast_match"] == 1 / 3
    assert summary["errors"] == 1, summary
    assert len(rows) == 3
    assert [row["id"] for row in rows] == [1, 2, 3]
    # 不可解析的输出原样留档（评测要看清模型到底吐了什么），失败原因记在 error
    assert rows[2]["pred_sql"] == "这不是 SQL"
    assert rows[2]["error"] == "AssertionError: 不是只读查询"
    assert rows[2]["exact_ast_match"] is False


def test_expand_search_space():
    assert expand_search_space({}) == [{}]
    assert expand_search_space({"top_k": [1, 3]}) == [{"top_k": 1}, {"top_k": 3}]
    assert expand_search_space({"orm": [False, True], "fewshot_k": [0, 3]}) == [
        {"orm": False, "fewshot_k": 0},
        {"orm": False, "fewshot_k": 3},
        {"orm": True, "fewshot_k": 0},
        {"orm": True, "fewshot_k": 3},
    ]
    assert len(expand_search_space({"a": [1, 2], "b": [1, 2, 3], "c": [True, False]})) == 12


def test_apply_strategy_overrides_is_non_destructive():
    """寻优会反复在同一份基准配置上套不同组合，绝不能改坏原对象。"""
    base = StrategyConfig(model=ModelConfig(), tricks=TrickConfig())
    changed = apply_strategy_overrides(
        base, {"tricks.fewshot_top_k": 9, "tricks.rag_enabled": True, "name": "候选"})
    assert changed.tricks.fewshot_top_k == 9
    assert changed.tricks.rag_enabled is True
    assert changed.name == "候选"
    assert base.tricks.fewshot_top_k != 9 and base.tricks.rag_enabled is False


def test_evaluate_candidate_and_ranking():
    records = [{"query": "q1", "sql": "SELECT 1"}, {"query": "q2", "sql": "SELECT 2"}]
    result = evaluate_candidate(
        generator_for({"q1": "SELECT 1", "q2": "SELECT 3"}), records, batch_size=2, workers=2)
    assert result["count"] == 2
    assert result["accuracy"] == 0.5
    assert result["latency_s"] == 1.0

    # 默认只看准确率；给了时延权重才让快的排前面
    rows = [{"accuracy": 0.5, "latency_s": 3.0}, {"accuracy": 0.5, "latency_s": 1.0}]
    assert rank_results(rows)[0]["latency_s"] == 3.0
    assert rank_results(rows, latency_weight=1.0)[0]["latency_s"] == 1.0


def test_store_crud():
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "studio.db")

        connection_id = store.save_connection("本地商品库", "mysql", {"host": "127.0.0.1", "port": 3306})
        assert store.list_connections()[0]["type"] == "mysql"
        store.save_connection("本地商品库", "sqlite", {"path": "demo.sqlite"}, connection_id)
        assert store.list_connections()[0]["params"] == '{"path": "demo.sqlite"}'
        store.delete_connection(connection_id)
        assert store.list_connections() == []

        strategy_id = store.save_strategy("默认方案", "起点", {"tricks": {"table_recall": "bm25"}})
        store.save_strategy("默认方案", "改过", {"tricks": {"table_recall": "llm"}}, strategy_id)
        saved = store.list_strategies()[0]
        assert saved["description"] == "改过" and '"llm"' in saved["config"]
        store.delete_strategy(strategy_id)
        assert store.list_strategies() == []


def test_store_datasets_append_and_replace():
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "studio.db")
        dataset_id = store.create_dataset("商品评测集", "内部")
        assert store.list_datasets()[0]["item_count"] == 0

        store.replace_items(dataset_id, [
            {"difficulty": "easy", "query": "列出商品", "sql": "SELECT name FROM products"},
            {"difficulty": "hard", "query": "统计", "sql": "SELECT COUNT(*) FROM orders",
             "judge_score": "1.0", "judge_reason": "ok"},
        ])
        items = store.list_items(dataset_id)
        assert items[0]["ord"] == 0 and items[1]["judge_reason"] == "ok"
        assert store.list_datasets()[0]["item_count"] == 2

        # 生成页「加入数据集」是追加，不能覆盖已有标注
        store.append_items(dataset_id, [{"query": "追加一题", "sql": "SELECT 2"}])
        assert store.list_datasets()[0]["item_count"] == 3
        assert store.list_items(dataset_id)[-1]["query"] == "追加一题"

        store.replace_items(dataset_id, [{"query": "只留一题", "sql": "SELECT 1"}])
        assert store.list_datasets()[0]["item_count"] == 1


def test_store_runs_and_generations():
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "studio.db")
        dataset_id = store.create_dataset("集")
        run_id = store.create_run("evaluate", dataset_id=dataset_id, strategy_id=None)
        store.add_run_items(run_id, [{"query": "q", "gold_sql": "SELECT 1", "pred_sql": "SELECT 1",
                                      "score": 1.0, "reason": "ok", "latency_s": 0.5,
                                      "trace": {"model_calls": 1}}])
        store.finish_run(run_id, "done", {"accuracy": 1.0})
        run = store.get_run(run_id)
        assert run["status"] == "done" and '"accuracy": 1.0' in run["summary"]
        assert store.list_run_items(run_id)[0]["trace"] == '{"model_calls": 1}'
        assert store.list_runs("evaluate")[0]["id"] == run_id

        store.add_generation("问题", "SELECT 1", connection_id=None, strategy_id=None, latency_s=1.2)
        assert store.list_generations()[0]["question"] == "问题"


def test_grid_search_finds_the_better_combo():
    """网格寻优：注入的参数要真的改变行为，最优组合必须排在第一个。"""
    records = [{"query": "q1", "sql": "SELECT a FROM t"}, {"query": "q2", "sql": "SELECT b FROM t"}]

    def factory(strategy):
        def generate(question):
            # fewshot_top_k=5 时答得对，否则故意答错，让寻优有可分辨的信号
            if strategy.tricks.fewshot_top_k == 5:
                sql = {"q1": "SELECT a FROM t", "q2": "SELECT b FROM t"}[question]
            else:
                sql = "SELECT x FROM t"
            return {"sql": sql, "latency_s": 0.1, "raw": sql}
        return generate

    ranked = grid_search(
        StrategyConfig(),
        {"tricks.fewshot_top_k": [3, 5], "tricks.rag_enabled": [False, True]},
        records, factory, batch_size=2, workers=2)
    assert len(ranked) == 4
    assert ranked[0]["accuracy"] == 1.0
    assert ranked[0]["overrides"]["tricks.fewshot_top_k"] == 5
    assert ranked[-1]["accuracy"] == 0.0


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_benchmark_and_store: OK")
