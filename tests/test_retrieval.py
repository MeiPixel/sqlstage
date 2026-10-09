"""Schema 解析与召回：表/字段召回策略、Few-shot 检索、Guide 注入、样本值匹配。

覆盖 sqlstage/schema.py、retrieval.py、fewshot.py、guide.py、dataset.py。

用原生 assert 直接跑：python tests/test_retrieval.py
"""
import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage.config import TrickConfig
from sqlstage.database import SQLiteDatabase
from sqlstage.dataset import allocate_counts, build_generation_messages, parse_generated_items
from sqlstage.fewshot import load_fewshots, recall_fewshots, render_fewshots
from sqlstage.guide import render_guide_context
from sqlstage.retrieval import (
    bm25_field_scores, enum_values, expand_foreign_key_neighbors, parse_name_list,
    recall_by_lexical, select_columns, select_tables, split_camel, tokenize,
)
from sqlstage.schema import read_sqlite_schema, render_schema, sqlite_uri

SCHEMA = {
    "products": {
        "columns": [
            {"name": "productId", "type": "INTEGER", "primary_key": True},
            {"name": "brandName", "type": "TEXT", "primary_key": False},
            {"name": "salePrice", "type": "REAL", "primary_key": False},
        ],
        "foreign_keys": [],
    },
    "orders": {
        "columns": [
            {"name": "orderId", "type": "INTEGER", "primary_key": True},
            {"name": "productId", "type": "INTEGER", "primary_key": False},
            {"name": "orderDate", "type": "TEXT", "primary_key": False},
        ],
        "foreign_keys": [{"column": "productId", "target_table": "products", "target_column": "productId"}],
    },
}
DESCRIPTIONS = {"products": "商品 品牌 价格", "orders": "订单"}


class FakeModel:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages, temperature=None):
        return self.reply


def make_shop_db(directory):
    path = Path(directory) / "shop.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE 商品(商品ID INTEGER PRIMARY KEY, 品牌 TEXT, 型号 TEXT, 价格 REAL)")
    connection.executemany(
        "INSERT INTO 商品(品牌, 型号, 价格) VALUES (?, ?, ?)",
        [(["联想", "戴尔", "惠普"][index % 3], f"型号{index}", index) for index in range(25)])
    connection.commit()
    connection.close()
    return path


def test_tokenize():
    assert split_camel("productIdName") == "product Id Name"
    assert {"product", "id", "name"} <= tokenize("productIdName")
    assert tokenize("productIdName", camel_case_split=False) == {"productidname"}
    # 中文按二元切分，「商品价格」能命中「商品」
    assert "商品" in tokenize("商品价格")


def test_read_sqlite_schema():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "shop.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), amount REAL);")
        connection.close()
        schema = read_sqlite_schema(str(path))
        assert list(schema) == ["orders", "users"]
        assert schema["users"]["columns"][0]["primary_key"] is True
        assert schema["orders"]["foreign_keys"] == [
            {"column": "user_id", "target_table": "users", "target_column": "id"}
        ]


def test_sqlite_uri_is_read_only():
    with TemporaryDirectory() as directory:
        path = make_shop_db(directory)
        assert sqlite_uri(str(path), read_only=True).startswith("file:")
        assert "mode=ro" in sqlite_uri(str(path), read_only=True)
        assert "mode=ro" not in sqlite_uri(str(path), read_only=False)


def test_render_schema():
    text = render_schema(SCHEMA, ["products"], {"products": ["brandName"]})
    assert "products" in text
    assert "brandName" in text


def test_guide_context():
    assert render_guide_context("", 10) == ""
    assert render_guide_context("join: a.id=b.id", 6) == "\n\n## Database Guide\njoin: "
    assert len(render_guide_context("x" * 100, 20).splitlines()[-1]) == 20


def test_recall_by_lexical():
    documents = {"products": "products 商品 brandName salePrice", "orders": "orders 订单 orderDate"}
    assert recall_by_lexical("品牌", documents, 1, True) == ["products"]


def test_bm25_field_scores():
    scores = bm25_field_scores("品牌价格", SCHEMA, DESCRIPTIONS, True)
    assert scores["products"] > scores["orders"] == 0.0


def test_enum_values_skips_high_cardinality():
    with TemporaryDirectory() as directory:
        path = make_shop_db(directory)
        database = SQLiteDatabase(path)
        columns = {column["name"]: column for column in read_sqlite_schema(str(path))["商品"]["columns"]}
        # 品牌只有 3 个取值，可当枚举提示；型号 25 个、ID 是数值，都不该枚举
        assert sorted(enum_values(database, "商品", columns["品牌"])) == ["惠普", "戴尔", "联想"]
        assert enum_values(database, "商品", columns["型号"]) == []
        assert enum_values(database, "商品", columns["商品ID"]) == []


def test_bm25_uses_sample_values():
    """真实数据里的枚举值应当进入匹配，否则「买联想的」这类问题召不回表。"""
    with TemporaryDirectory() as directory:
        path = make_shop_db(directory)
        database = SQLiteDatabase(path)
        schema = read_sqlite_schema(str(path))
        assert bm25_field_scores("联想", schema, {})["商品"] == 0
        assert bm25_field_scores("联想", schema, {}, database=database)["商品"] > 0


def test_select_tables_strategies():
    config = TrickConfig(table_recall="bm25_field", table_top_k=1, join_expansion=False,
                         table_descriptions=DESCRIPTIONS)
    assert select_tables("品牌名称", SCHEMA, config, None) == ["products"]

    config = TrickConfig(table_recall="all_schema", join_expansion=False)
    assert select_tables("任意", SCHEMA, config, None) == ["products", "orders"]

    # LLM 报了不存在的表，校验开关必须把它剔掉
    config = TrickConfig(table_recall="llm", table_top_k=3, join_expansion=False, llm_validate_names=True)
    assert select_tables("品牌", SCHEMA, config, FakeModel('["products", "not_a_table"]')) == ["products"]

    # 全是幻觉表时降级到词法召回
    config = TrickConfig(table_recall="llm", table_top_k=1, join_expansion=False,
                         llm_validate_names=True, llm_lexical_fallback=True,
                         table_descriptions=DESCRIPTIONS)
    assert select_tables("订单", SCHEMA, config, FakeModel('["ghost"]')) == ["orders"]

    # 非法策略直接报错，不静默退回全量
    config = TrickConfig(table_recall="nonexistent")
    try:
        select_tables("x", SCHEMA, config, None)
    except ValueError as error:
        assert "未知表召回方法" in str(error)
    else:
        raise AssertionError("未知表召回方法应当报错")


def test_join_expansion_pulls_neighbours():
    """选了 orders 就该把被外键指向的 products 一起带上，否则 JOIN 缺表；
    反向也一样——选了 products，引用它的 orders 也要进来。"""
    config = TrickConfig(table_recall="keyword", table_top_k=1, join_expansion=True,
                         table_descriptions=DESCRIPTIONS)
    assert select_tables("订单日期", SCHEMA, config, None) == ["orders", "products"]
    assert expand_foreign_key_neighbors(["orders"], SCHEMA) == ["orders", "products"]
    assert expand_foreign_key_neighbors(["products"], SCHEMA) == ["products", "orders"]

    # 关闭扩展就只给召回结果，不做联动
    config = TrickConfig(table_recall="keyword", table_top_k=1, join_expansion=False,
                         table_descriptions=DESCRIPTIONS)
    assert select_tables("订单日期", SCHEMA, config, None) == ["orders"]


def test_select_columns():
    config = TrickConfig(column_recall="all_columns", column_description_recall=True,
                         column_description_threshold=1, column_top_k=1,
                         keep_primary_keys=True, keep_foreign_keys=True)
    columns = select_columns("品牌", SCHEMA, ["products"], config, None)
    assert "brandName" in columns["products"] and "productId" in columns["products"]


def test_load_fewshots_dedup():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "pool.jsonl"
        path.write_text("\n".join([
            json.dumps({"question": "列出商品", "sql": "SELECT * FROM products"}),
            json.dumps({"question": "列出商品", "sql": "SELECT * FROM products"}),
            json.dumps({"question": "订单数", "sql": "SELECT COUNT(*) FROM orders"}),
        ]), encoding="utf-8")
        assert len(load_fewshots(str(path), dedup=True)) == 2
        assert len(load_fewshots(str(path), dedup=False)) == 3
    assert load_fewshots("") == []


def test_recall_fewshots():
    rows = [
        {"question": "查询商品价格", "sql": "SELECT price FROM products", "tables": ["products"]},
        {"question": "查询订单金额", "sql": "SELECT amount FROM orders", "tables": ["orders"]},
        {"question": "商品价格是多少", "sql": "SELECT price FROM products", "tables": ["products"]},
    ]
    # 排除自身，避免把正在回答的题当示例
    assert recall_fewshots("查询商品价格", rows, 2, ["products"], True)[0]["question"] == "商品价格是多少"
    # 同表过滤后只剩一条命中的
    assert len(recall_fewshots("查询商品价格", rows, 3, ["products"], True)) == 1
    assert recall_fewshots("查询订单金额", rows, 1, ["orders"], False)[0]["question"] == "查询订单金额"


def test_render_fewshots():
    assert render_fewshots([]) == ""
    text = render_fewshots([{"question": "q", "sql": "SELECT 1"}])
    assert text.startswith("\n\n## Few-shot\n") and "SELECT 1" in text


def test_parse_name_list():
    assert parse_name_list('["a", "b"]') == ["a", "b"]
    assert parse_name_list('```json\n["a"]\n```') == ["a"]
    assert parse_name_list('{"names": ["a"]}') == ["a"]


def test_dataset_helpers():
    assert allocate_counts(10, 1, 1, 1) == {"easy": 4, "medium": 3, "hard": 3}
    assert allocate_counts(10, 1, 1, 0) == {"easy": 5, "medium": 5, "hard": 0}
    assert allocate_counts(5, 2, 1, 1) == {"easy": 3, "medium": 1, "hard": 1}
    assert sum(allocate_counts(7, 0, 1, 0).values()) == 7

    content = build_generation_messages("TABLE products\nCOLUMNS: id, name", "覆盖品牌", 10, 1, 1, 1)[0]["content"]
    assert "共 10 题" in content and "简单 4 题" in content and "覆盖品牌" in content

    items = parse_generated_items(
        '前缀说明 [{"difficulty":"easy","query":"列出商品","sql":"SELECT name FROM products"}] 后缀')
    assert items == [{"difficulty": "easy", "query": "列出商品", "sql": "SELECT name FROM products"}]
    assert parse_generated_items('[{"query":"q","sql":"SELECT 1"}]')[0]["difficulty"] == "medium"


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_retrieval: OK")
