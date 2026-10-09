"""生成流水线：难度分级、预算裁剪、Guide 检索、确定性修复、提示拼装、计时与模型协议。

覆盖 sqlstage/pipeline.py、prompts.py、model.py，以及 server/base.py 的历史消息切分。

用原生 assert 直接跑：python tests/test_pipeline.py
"""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.base import split_messages
from sqlstage.model import RequestTimer, build_reasoning_extra_body
from sqlstage.pipeline import (
    difficulty, difficulty_top_k, deterministic_sql_repair, quote, render_data_hints,
    retrieve_guide_lines, trim_prompt_parts,
)
from sqlstage.prompts import direct_sql_messages, orm_messages
from sqlstage.schema import read_sqlite_schema


class FakeOrmConfig:
    link_rules = []
    value_maps = {}
    unit_rules = {}
    required_fields = {}
    mutex_fields = []
    derived_fields = {}
    metrics = []
    join_rules = []


def make_products_db(directory):
    path = Path(directory) / "demo.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE products(brand TEXT, color TEXT);"
        "INSERT INTO products VALUES('ThinkPad','黑色'),('ThinkPad','黑色'),('MateBook','银色');")
    connection.close()
    return path


def test_difficulty_grading():
    assert difficulty("列出所有商品") == "simple"
    assert difficulty("每个品牌的平均价格是多少") == "medium"
    assert difficulty("连接订单与用户，按地区统计总金额") == "complex"


def test_difficulty_top_k_widens_on_complex():
    """问题越难，允许进提示的表越多：简单 1 张、复杂至少 3 张，但都不超过库里的表数。"""
    assert difficulty_top_k("simple", 5, 13) == 1
    assert difficulty_top_k("complex", 1, 13) == 3
    assert difficulty_top_k("complex", 8, 13) == 8
    assert difficulty_top_k("medium", 4, 13) == 4
    # 表少时不能凭空造出表来
    assert difficulty_top_k("simple", 5, 2) == 1
    assert difficulty_top_k("complex", 1, 2) == 2
    assert difficulty_top_k("medium", 4, 2) == 2


def test_deterministic_sql_repair():
    """无需模型的确定性修复：剥围栏、去分号、把 == 规范成 =。"""
    assert deterministic_sql_repair("```sql\nSELECT * FROM x WHERE a == 1;\n```") == (
        "SELECT * FROM x WHERE a = 1")
    assert deterministic_sql_repair("SELECT 1") == "SELECT 1"


def test_retrieve_guide_lines():
    """Guide 是长文档，按问题里的关键词重排后按预算装填，超预算的行整行丢弃。"""
    assert "价格" in retrieve_guide_lines("品牌说明\n价格单位是元\n日期格式", "价格低于100元", 20)
    # 排序只是「更相关的排前面」，没有相关度门槛：匹配不上的行只要装得下也会入选
    picked = retrieve_guide_lines("价格单位是元\n日期格式", "完全不相关的问题", 100)
    assert set(picked.splitlines()) == {"价格单位是元", "日期格式"}
    # 预算装不下的整行丢掉，不会被截断成半句误导模型
    assert retrieve_guide_lines("价格单位是元", "价格", 3) == ""


def test_trim_prompt_parts_keeps_budget():
    parts = trim_prompt_parts(["a" * 10, "b" * 10, "c" * 10], 21)
    assert sum(map(len, parts)) <= 21
    assert parts[0] == "a" * 10
    assert trim_prompt_parts([], 10) == []


def test_quote_identifier():
    assert quote("a") == '"a"'
    assert quote("we ird") == '"we ird"'


def test_render_data_hints_probes_real_data():
    """实体嗅探与高频值探针：把库里真实存在的取值提示给模型，减少幻觉值。"""
    with TemporaryDirectory() as directory:
        path = make_products_db(directory)
        schema = read_sqlite_schema(path)
        columns = {"products": ["brand", "color"]}
        entity = render_data_hints(path, schema, ["products"], columns, "ThinkPad有哪些", True, False)
        frequent = render_data_hints(path, schema, ["products"], columns, "有哪些商品", False, True)
        assert "products.brand=ThinkPad" in entity, entity
        assert "黑色(2)" in frequent, frequent
        # 两个探针都关时不该产生任何提示
        assert render_data_hints(path, schema, ["products"], columns, "x", False, False) == ""


def test_direct_sql_messages_multiturn():
    history = [
        {"role": "user", "content": "价格最高的商品"},
        {"role": "assistant", "content": "SELECT * FROM products ORDER BY price DESC LIMIT 1"},
    ]
    messages = direct_sql_messages("那最便宜的呢", "SCHEMA", history=history)
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[3]["content"] == "那最便宜的呢"
    assert direct_sql_messages("价格最高的商品", "SCHEMA") == [
        direct_sql_messages("价格最高的商品", "SCHEMA")[0],
        {"role": "user", "content": "价格最高的商品"},
    ]


def test_orm_messages_multiturn():
    messages = orm_messages("那最便宜的呢", "SCHEMA", FakeOrmConfig(),
                            history=[{"role": "user", "content": "最贵的"}])
    assert [item["role"] for item in messages] == ["system", "user", "user"]
    assert messages[-1]["content"] == "那最便宜的呢"


def test_split_messages_takes_last_question():
    """多轮里当前问题是最后一条 user，之前的历史原样保留。"""
    question, past = split_messages([
        {"role": "user", "content": "最贵的商品"},
        {"role": "assistant", "content": "SELECT 1"},
        {"role": "user", "content": "那最便宜的"},
    ])
    assert question == "那最便宜的"
    assert past == [{"role": "user", "content": "最贵的商品"},
                    {"role": "assistant", "content": "SELECT 1"}]


def test_request_timer_splits_model_overhead():
    """把「组装提示的耗时」与「模型耗时」分开，Trace 才能定位是哪一段慢。"""
    timer = RequestTimer()
    timer.begin(10.0)
    timer.record(12.0, 14.5)
    timer.record(15.0, 16.0)
    assert timer.snapshot() == {"before_model_s": 2.0, "model_s": 3.5, "model_calls": 2, "token_usage": None}

    # begin 会重置：一次请求不该继承上一次的统计
    timer.begin(20.0)
    timer.record(20.25, 21.0)
    assert timer.snapshot() == {"before_model_s": 0.25, "model_s": 0.75, "model_calls": 1, "token_usage": None}


def test_build_reasoning_extra_body_per_provider():
    """不同厂商开关思考模式的参数名不一样，统一在这里翻译。"""
    assert build_reasoning_extra_body("deepseek", "disabled", {}) == {
        "thinking": {"type": "disabled"}}
    assert build_reasoning_extra_body("deepseek", "enabled", {}) == {
        "thinking": {"type": "enabled"}}
    assert build_reasoning_extra_body("qwen", "disabled", {}) == {
        "chat_template_kwargs": {"enable_thinking": False}}
    assert build_reasoning_extra_body("vllm", "enabled", {"top_k": 20}) == {
        "top_k": 20, "chat_template_kwargs": {"enable_thinking": True}}
    # gemma 没有对应开关，default 都不该注入任何字段
    assert build_reasoning_extra_body("gemma", "disabled", {"x": 1}) == {"x": 1}
    assert build_reasoning_extra_body("deepseek", "default", {"x": 1}) == {"x": 1}


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_pipeline: OK")
