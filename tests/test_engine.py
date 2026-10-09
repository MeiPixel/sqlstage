"""引擎端到端：Trick 开关是否真的生效、执行反馈自愈、空结果重生、语义精修。

Trace 里的 executed_tricks 记的是「实际跑过的组件」而不是配置声明，
所以这里同时断言行为与 trace，防止出现「界面显示开启、实际没跑」。

用原生 assert 直接跑：python tests/test_engine.py
"""
import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage import ModelConfig, OrmConfig, StrategyConfig, Text2SQLEngine, TrickConfig

SCHEMA_SQL = (
    "CREATE TABLE products(id INTEGER PRIMARY KEY, brand TEXT, color TEXT, price REAL);"
    "INSERT INTO products VALUES(1,'ThinkPad','黑色',99),(2,'MateBook','银色',199);"
)


class SequenceModel:
    """按预设顺序返回答案，用来观察多轮组件各自改了什么。"""

    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    def begin_timing(self, request_started):
        self.calls = 0

    def complete(self, messages, temperature=None):
        self.messages.append(messages)
        self.calls += 1
        return next(self.outputs)

    def timing(self):
        return {"before_model_s": 0.01, "model_s": 0.02, "model_calls": self.calls}


def make_engine(path, tricks, outputs):
    value = Text2SQLEngine(path, StrategyConfig(model=ModelConfig(), tricks=tricks))
    value.model = SequenceModel(outputs)
    return value


def make_products_db(directory):
    path = Path(directory) / "demo.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.close()
    return path


def make_fewshot_file(directory):
    path = Path(directory) / "fewshot.jsonl"
    path.write_text("\n".join([
        json.dumps({"question": "列出商品", "sql": "SELECT * FROM products", "table": "products"},
                   ensure_ascii=False),
        json.dumps({"question": "每个品牌平均价格", "sql": "SELECT brand, AVG(price) FROM products GROUP BY brand",
                    "table": "products"}, ensure_ascii=False),
    ]), encoding="utf-8")
    return path


def test_tricks_reach_the_prompt():
    """开启的组件必须真的改变提示内容，且都记进 trace。"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database = make_products_db(root)
        tricks = TrickConfig(
            table_recall="all_schema", column_recall="all_columns", execution_check=False,
            question_rewrite=True, difficulty_routing=True, entity_sniffing=True,
            distinct_probe=True, guide_text="价格单位是元\n颜色是枚举值", rag_enabled=True,
            fewshot_enabled=True, fewshot_path=str(make_fewshot_file(root)),
            fewshot_method="bm25", fewshot_top_k=2, fewshot_difficulty=True,
            prompt_max_chars=10000,
        )
        item = make_engine(database, tricks, [
            "列出ThinkPad商品",
            "SELECT * FROM products WHERE brand='ThinkPad'",
        ])
        result = item.generate("帮我看ThinkPad")
        prompt = str(item.model.messages[-1])

        assert result["tables"] == ["products"]
        assert result["trace"]["fewshot_count"] == 1
        assert "实体命中：products.brand=ThinkPad" in prompt
        assert "高频值：products.color" in prompt
        assert "价格单位是元" in prompt
        assert {"question_rewrite", "difficulty_routing", "entity_sniffing",
                "distinct_probe", "rag"} <= set(result["trace"]["executed_tricks"])


def test_execution_feedback_repairs():
    """列名写错时把报错回喂，让模型自己修好。"""
    with TemporaryDirectory() as directory:
        database = make_products_db(Path(directory))
        item = make_engine(database, TrickConfig(
            execution_check=True, execution_feedback=True, repair_rounds=1),
            ["SELECT missing FROM products", "SELECT brand FROM products"])
        result = item.generate("列出品牌")
        assert result["rows"] and "execution_feedback" in result["trace"]["executed_tricks"]


def test_empty_result_refine():
    """查空结果时不该直接返回空表，应让模型重写条件。"""
    with TemporaryDirectory() as directory:
        database = make_products_db(Path(directory))
        item = make_engine(database, TrickConfig(execution_check=True, empty_result_refine=True),
                           ["SELECT * FROM products WHERE price < 0", "SELECT * FROM products"])
        result = item.generate("列出商品")
        assert len(result["rows"]) == 2
        assert "empty_result_refine" in result["trace"]["executed_tricks"]


def test_semantic_refine_and_deterministic_repair():
    """模型带围栏、用了 == 时，语义精修 + 确定性修复应产出规范 SQL。"""
    with TemporaryDirectory() as directory:
        database = make_products_db(Path(directory))
        item = make_engine(database, TrickConfig(
            execution_check=False, semantic_refine=True, deterministic_repair=True,
            dialect_conversion=True),
            ["SELECT * FROM products", "```sql\nSELECT * FROM products WHERE price == 99;\n```"])
        result = item.generate("价格为99")
        assert result["sql"] == "SELECT * FROM products WHERE price = 99"
        assert {"semantic_refine", "deterministic_repair",
                "dialect_conversion"} <= set(result["trace"]["executed_tricks"])


def test_orm_disabled_leaves_plan_alone():
    """ORM 关掉时引擎走直接生成，SQL 原样返回不做规则编译。"""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "demo.sqlite"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL);"
            "INSERT INTO products VALUES(1, 'keyboard', 99);")
        connection.close()
        item = Text2SQLEngine(database, StrategyConfig(
            model=ModelConfig(), orm=OrmConfig(enabled=False)))
        item.model = SequenceModel(["SELECT name, price FROM products WHERE price < 100"])
        result = item.generate("100元以下的商品")
        assert result["sql"] == "SELECT name, price FROM products WHERE price < 100"
        assert result["rows"] == [{"name": "keyboard", "price": 99.0}]
        assert result["trace"]["model_calls"] == 1


def test_engine_accepts_database_object():
    """适配器对象与路径都该能用——服务模式传对象，脚本传路径。"""
    from sqlstage.database import SQLiteDatabase
    with TemporaryDirectory() as directory:
        path = make_products_db(Path(directory))
        for source in (str(path), SQLiteDatabase(path, read_only=True)):
            item = make_engine(source, TrickConfig(execution_check=True),
                               ["SELECT brand FROM products"])
            assert item.generate("列出品牌")["rows"] == [
                {"brand": "ThinkPad"}, {"brand": "MateBook"}]


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_engine: OK")
