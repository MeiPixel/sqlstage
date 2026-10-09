"""ORM 约束：确定性规则编译，不经过大模型。

覆盖 sqlstage/orm.py 的 sql_value、compile_orm、apply_orm_rules、apply_link_rules、
convert_unit_value、split_order、expand_named_expressions，以及 OrmConfig 的
字段匹配、取值映射、数值转换、字段别名、派生字段、指标、表前缀与排序引用。

用原生 assert 直接跑：python tests/test_orm.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlstage.engine as engine_module
from sqlstage import ModelConfig, StrategyConfig, Text2SQLEngine, TrickConfig
from sqlstage.config import OrmConfig
from sqlstage.orm import (
    apply_link_rules, apply_orm_rules, compile_orm, convert_unit_value,
    expand_named_expressions, parse_orm_plan, split_order, sql_value,
)

from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import create_database


def preset_config():
    """仅测试通用字段规则，不读任何赛事预置。"""
    return OrmConfig(enabled=True, field_match={"products.name": "contains"},
                     value_maps={"products.brand": {"test maker": "Acme"}},
                     numeric_cast=["products.price"], field_alias={"products.name": ["title"]})


def test_sql_value():
    assert sql_value(None) == "NULL"
    assert sql_value("NULL") == "'NULL'"
    assert sql_value(5000) == "5000"
    assert sql_value(3.5) == "3.5"
    assert sql_value("O'Reilly") == "'O''Reilly'"
    assert sql_value("CURRENT_DATE") == "CURRENT_DATE"


def test_compile_orm_shape():
    assert compile_orm({"select": [], "from": "products"}) == "SELECT * FROM products"
    assert compile_orm({
        "select": ["name", "price"],
        "from": "products",
        "filters": [{"field": "price", "op": "<", "value": 5000}],
        "order_by": ["price ASC"],
        "limit": 5,
    }) == "SELECT name, price FROM products WHERE price < 5000 ORDER BY price ASC LIMIT 5"
    assert compile_orm({
        "select": ["o.id", "c.name"],
        "from": "orders o",
        "joins": [{"table": "customers c", "on": "o.customer_id = c.id"}],
        "filters": [{"field": "c.name", "op": "=", "value": "O'Reilly"}],
        "group_by": ["c.name"],
    }) == ("SELECT o.id, c.name FROM orders o JOIN customers c "
           "ON o.customer_id = c.id WHERE c.name = 'O''Reilly' GROUP BY c.name")


def test_split_order():
    # 只有一个词时方向留空，调用方原样输出，不擅自补 ASC
    assert split_order("price") == ("price", "")
    assert split_order("price ASC") == ("price", "ASC")
    assert split_order("price desc") == ("price", "DESC")
    assert split_order({"field": "price", "desc": True}) == ("price", "DESC")
    assert split_order({"field": "price", "direction": "asc"}) == ("price", "ASC")
    # 方向只能来自 direction / order / desc 之一。
    # desc=False 是「显式关闭」而非方向，所以落到缺失分支报错——前端 Switch 关掉时
    # 本就不该下发这个键，宁可炸出来也不要静默编一个方向。
    for incomplete in ({"field": "price"}, {"field": "price", "desc": False}):
        try:
            split_order(incomplete)
        except ValueError as error:
            assert "排序项" in str(error)
        else:
            raise AssertionError(f"缺少方向时应当报错：{incomplete}")


def test_convert_unit_value():
    rules = {"内存容量_GB": {"TB": 1024, "GB": 1}, "重量_kg": {"克": 0.001}}
    assert convert_unit_value("内存容量_GB", "1TB", rules) == 1024
    assert convert_unit_value("内存容量_GB", "16 GB", rules) == 16
    assert convert_unit_value("重量_kg", "500克", rules) == 0.5
    assert convert_unit_value("名称", "1TB", rules) == "1TB"
    assert convert_unit_value("内存容量_GB", 16, rules) == 16


def test_apply_link_rules():
    rules = [
        {
            "when": {"field": "独立显卡型号", "op": "!=", "value": "无"},
            "then": [{"field": "显卡类型", "op": "=", "value": "独立显卡"}],
        },
        {
            "when": {"field": "配置状态", "op": "=", "value": "当前有效"},
            "then": [
                {"field": "配置状态", "op": "=", "value": "有效"},
                {"field": "配置生效日期", "op": "<=", "value": "CURRENT_DATE"},
            ],
        },
    ]
    gpu = [{"field": "独立显卡型号", "op": "!=", "value": "无"}]
    assert apply_link_rules(gpu, rules) == gpu + [
        {"field": "显卡类型", "op": "=", "value": "独立显卡"}
    ]
    assert apply_link_rules([], rules) == []
    active = [{"field": "配置状态", "op": "=", "value": "当前有效"}]
    assert len(apply_link_rules(active, rules)) == 3


def test_apply_orm_rules_value_map_and_required():
    config = OrmConfig(
        value_maps={"WiFi控制": {"支持": "是"}},
        required_fields={"价格.成交均价": ["商品.型号"]},
        join_rules=[{
            "trigger_field": "价格.成交均价",
            "join": {"table": "价格", "on": "商品.id = 价格.product_id"},
        }],
    )
    plan = {
        "select": ["价格.成交均价"],
        "from": "商品",
        "filters": [{"field": "WiFi控制", "op": "=", "value": "支持"}],
    }
    result = apply_orm_rules(plan, config)
    assert result["filters"][0]["value"] == "是"
    assert "商品.型号" in result["select"]
    assert result["joins"] == [{"table": "价格", "on": "商品.id = 价格.product_id"}]


def test_mutex_fields_raise():
    config = OrmConfig(mutex_fields=[["上架", "下架"]])
    invalid = {"select": [], "from": "商品", "filters": [
        {"field": "上架", "op": "=", "value": 1},
        {"field": "下架", "op": "=", "value": 1},
    ]}
    try:
        apply_orm_rules(invalid, config)
    except AssertionError as error:
        assert "互斥" in str(error)
    else:
        raise AssertionError("互斥字段同时出现时应当报错")


def test_field_match_value_map_and_alias():
    config = OrmConfig(
        field_match={
            "air_conditioner.系列": "contains",
            "air_conditioner.产品认证": "startswith",
        },
        value_maps={"air_conditioner.安装方式": {"柜式": "立柜式"}},
        field_alias={"headphones.耳机类型": ["佩戴方式"]},
    )
    cases = [
        ({"select": ["系列"], "from": "air_conditioner",
          "filters": [{"field": "系列", "op": "=", "value": "变频"}]},
         "SELECT 系列 FROM air_conditioner WHERE 系列 LIKE '%变频%'"),
        ({"select": [], "from": "air_conditioner",
          "filters": [{"field": "产品认证", "op": "=", "value": "CCC"}]},
         "产品认证 LIKE 'CCC%'"),
        ({"select": [], "from": "air_conditioner",
          "filters": [{"field": "安装方式", "op": "=", "value": "柜式"}]},
         "安装方式 = '立柜式'"),
        ({"select": [], "from": "headphones",
          "filters": [{"field": "佩戴方式", "op": "=", "value": "入耳式"}]},
         "耳机类型 = '入耳式'"),
    ]
    for plan, expected in cases:
        sql = compile_orm(apply_orm_rules(plan, config))
        assert expected in sql, sql


def test_numeric_cast_applies_to_any_operator():
    """数值字段统一包 CAST，避免 TEXT 列上按字符串比较大小。"""
    plan = {"select": [], "from": "air_conditioner",
            "filters": [{"field": "室内机重量_kg", "op": "<", "value": 30}]}
    sql = compile_orm(apply_orm_rules(plan, OrmConfig(
        numeric_cast=["air_conditioner.室内机重量_kg"])))
    assert sql == "SELECT * FROM air_conditioner WHERE CAST(室内机重量_kg AS REAL) < 30", sql

    # 与 field_match 叠加：等值改 LIKE 之后仍然要套 CAST，否则 30 变成 '30' 匹配不到数字
    sql = compile_orm(apply_orm_rules(
        {"select": [], "from": "air_conditioner",
         "filters": [{"field": "室内机重量_kg", "op": "=", "value": 30}]},
        OrmConfig(field_match={"air_conditioner.室内机重量_kg": "contains"},
                  numeric_cast=["air_conditioner.室内机重量_kg"])))
    assert sql == ("SELECT * FROM air_conditioner "
                   "WHERE CAST(室内机重量_kg AS REAL) LIKE '%30%'"), sql


def test_field_match_uses_table_qualifier():
    plan = {"select": [], "from": "computer_join_main",
            "filters": [{"field": "硬盘接口类型", "op": "=", "value": "NVMe"}]}
    sql = compile_orm(apply_orm_rules(plan, OrmConfig(
        field_match={"computer_join_config.硬盘接口类型": "contains"})))
    assert "硬盘接口类型 LIKE '%NVMe%'" in sql, sql


def test_named_expressions():
    """命名表达式是 select 项的宏，编译后列名仍是原名，聚合结果才能被外层引用。"""
    config = OrmConfig(
        derived_fields={"毛利率": "(收入 - 成本) / 收入"},
        metrics={"客单价": "SUM(金额) / COUNT(DISTINCT 用户)"},
    )
    plan = apply_orm_rules({"select": ["毛利率", "客单价"], "from": "orders"}, config)
    sql = compile_orm(plan)
    assert sql == ("SELECT (收入 - 成本) / 收入 AS 毛利率, "
                   "SUM(金额) / COUNT(DISTINCT 用户) AS 客单价 FROM orders"), sql


def test_table_prefix_and_quoted_order_by():
    """多 schema 同名表时给 FROM / JOIN 加库名前缀；ORDER BY 默认不转义。"""
    plan = {"select": ["name"], "from": "orders o",
            "joins": [{"table": "users u", "on": "o.user_id = u.id"}]}
    assert compile_orm(plan, table_prefix="shop.") == (
        "SELECT name FROM shop.orders o JOIN shop.users u ON o.user_id = u.id")
    assert compile_orm(plan) == "SELECT name FROM orders o JOIN users u ON o.user_id = u.id"
    assert compile_orm({"select": [], "from": "t", "order_by": ["score"]},
                       quote_order_by=True).endswith("ORDER BY `score`")


def test_expand_named_expressions():
    assert expand_named_expressions(["a", "b"], {}) == ["a", "b"]
    # 命名表达式带 AS 别名，编译后列名仍是原名，聚合结果才能被外层引用
    assert expand_named_expressions(["毛利率"], {"毛利率": "(收入 - 成本) / 收入"}) == [
        "(收入 - 成本) / 收入 AS 毛利率"
    ]


def test_parse_orm_plan():
    assert parse_orm_plan('{"select": ["name"], "from": "products"}')["from"] == "products"
    assert parse_orm_plan('```json\n{"from": "products"}\n```')["from"] == "products"


def test_preset_rules_rewrite_plan():
    config = preset_config()
    plan = apply_orm_rules({"select": ["name"], "from": "products",
                           "filters": [{"field": "name", "op": "=", "value": "Test"}]}, config)
    assert "name LIKE '%Test%'" in compile_orm(plan)
    plan = apply_orm_rules({"select": ["name"], "from": "products",
                           "filters": [{"field": "brand", "op": "=", "value": "test maker"}]}, config)
    assert "'Acme'" in compile_orm(plan)
    plan = apply_orm_rules({"select": ["name"], "from": "products",
                           "filters": [{"field": "price", "op": "<", "value": 30}]}, config)
    assert "CAST(price AS REAL)" in compile_orm(plan)
    plan = apply_orm_rules({"select": [], "from": "products",
                           "filters": [{"field": "title", "op": "=", "value": "Test"}]}, config)
    assert "name" in compile_orm(plan)


def test_preset_end_to_end():
    class PlanModel:
        def __init__(self, config):
            pass

        def begin_timing(self, started):
            pass

        def timing(self):
            return {"before_model_s": 0.0, "model_s": 0.0, "model_calls": 1}

        def complete(self, messages, temperature=None):
            return json.dumps({"select": ["name", "price"], "from": "products",
                               "filters": [{"field": "name", "op": "=", "value": "Test One"}]})

    original = engine_module.OpenAICompatibleModel
    engine_module.OpenAICompatibleModel = PlanModel
    try:
        with TemporaryDirectory() as directory:
            path = create_database(directory)
            engine = Text2SQLEngine(path, StrategyConfig(model=ModelConfig(api_key="test-key"),
                                    tricks=TrickConfig(table_recall="all_schema", execution_check=True),
                                    orm=preset_config()))
            result = engine.generate("Find Test One")
            assert result["orm_plan"] is not None
            assert "LIKE" in result["sql"] and "products" in result["sql"]
            assert result["rows"] and set(result["rows"][0]) >= {"name", "price"}
    finally:
        engine_module.OpenAICompatibleModel = original


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_orm: OK")
