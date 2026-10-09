"""候选生成、择优与结果评判。

覆盖 sqlstage/candidates.py 的多路候选执行校验与投票择优，
sqlstage/judges.py 的四种判别口径，sqlstage/evaluation.py 的双口径打分。

用原生 assert 直接跑：python tests/test_candidates_and_judges.py
"""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage.candidates import (
    VARIANT_HINTS, check_candidates, choose_by_llm, choose_candidates, extract_sql, normalize_sql,
)
from sqlstage.evaluation import canonical_sql, exact_ast_score, load_jsonl, score_prediction
from sqlstage.judges import (
    JUDGE_LABELS, JUDGES, judge, judge_keyword, judge_llm_answer, judge_llm_sql,
    judge_sql_loose, judge_sql_strict,
)


class FakeModel:
    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages, temperature=None):
        return self.reply


def test_extract_sql():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("```sqlite\nSELECT 1;\n```") == "SELECT 1"
    assert extract_sql("SELECT 1;") == "SELECT 1"
    # 夹在正文里的说明文字不是代码块，原样返回给上层判别
    assert extract_sql("答案是 SELECT 1") == "答案是 SELECT 1"


def test_normalize_sql():
    assert normalize_sql("  SELECT  a\nFROM t; ") == "select a from t"


def test_check_candidates_marks_executable():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "goods.sqlite"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE goods(id INTEGER, name TEXT)")
        connection.close()
        rows = check_candidates([
            {"sql": "SELECT name FROM goods"},
            {"sql": "SELECT missing FROM goods"},
            {"sql": "这不是 SQL"},
            {"sql": "DELETE FROM goods"},
        ], str(path))
    assert rows[0]["executable"] is True and rows[0]["execution_error"] == ""
    assert rows[1]["executable"] is False and "no such column" in rows[1]["execution_error"]
    assert rows[2]["executable"] is False
    # 只读连接下写语句在 SQL 层就被拦，同样记成不可执行而不是崩掉
    assert rows[3]["executable"] is False


def test_choose_candidates_vote_wins():
    """两条候选语义一致时按投票定胜负，忽略空白与大小写差异。"""
    winner, method = choose_candidates([
        {"sql": "SELECT a FROM t"},
        {"sql": " select  a  from t; "},
        {"sql": "SELECT b FROM t"},
    ])
    assert normalize_sql(winner["sql"]) == "select a from t"
    assert method == "vote"


def test_choose_candidates_falls_back_to_shortest_executable():
    """没形成多数时优先选能执行的候选，再比长度——执行不了的 SQL 再短也没用。"""
    winner, method = choose_candidates([
        {"sql": "SELECT long_name FROM table_name", "executable": False},
        {"sql": "SELECT a FROM t", "executable": True},
        {"sql": "SELECT b, c FROM t", "executable": True},
    ])
    assert winner["sql"] == "SELECT a FROM t"
    assert method == "shortest"
    # 全都执行不了时退而在全部候选里挑最短的
    winner, method = choose_candidates([
        {"sql": "SELECT long_name FROM t", "executable": False},
        {"sql": "SELECT a FROM t", "executable": False},
    ])
    assert winner["sql"] == "SELECT a FROM t" and method == "shortest"


def test_choose_by_llm():
    candidates = [{"sql": "SELECT a FROM t"}, {"sql": "SELECT b FROM t"}]
    winner, method = choose_by_llm("哪个是 A", candidates, FakeModel('{"pick": 1}'))
    assert winner["sql"] == "SELECT a FROM t" and method == "llm_selector"


def test_variant_hints():
    assert set(VARIANT_HINTS) == {"direct", "join_first", "minimal"}
    assert all(VARIANT_HINTS.values())


def test_canonical_sql():
    assert canonical_sql("select name from products") == canonical_sql("SELECT name FROM products;")
    assert canonical_sql('SELECT "名称" FROM "商品"') == canonical_sql("SELECT 名称 FROM 商品")
    assert canonical_sql("SELECT a FROM t WHERE b = 1") != canonical_sql("SELECT a FROM t WHERE b = 2")
    try:
        canonical_sql("DELETE FROM products")
    except AssertionError as error:
        assert "不是只读查询" in str(error)
    else:
        raise AssertionError("写语句不该通过 canonical_sql")


def test_score_prediction():
    """双口径的严格 AST 分：不可解析的输出与写语句都判 0，并带原因。"""
    assert score_prediction("SELECT a FROM t", "select a from t") == (True, None)
    assert score_prediction("SELECT b FROM t", "select a from t") == (False, None)
    assert score_prediction("这里是答案", "SELECT a FROM t") == (False, "AssertionError: 不是只读查询")
    assert score_prediction("DELETE FROM t", "SELECT a FROM t") == (False, "AssertionError: 不是只读查询")
    assert exact_ast_score("SELECT a FROM t", " SELECT a FROM t ") is True


def test_load_jsonl():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "a.jsonl"
        path.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
        assert load_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_judge_sql_strict():
    assert judge_sql_strict("SELECT name FROM products", "select name from products")[0] == 1.0
    assert judge_sql_strict("SELECT name FROM products", "SELECT price FROM products")[0] == 0.0


def test_judge_sql_loose_tolerates_equivalent_forms():
    """宽松口径抹平等价写法：比较符容错、LIKE 与等号互认但不完全等价。"""
    assert judge_sql_loose("SELECT * FROM t WHERE a > 1", "SELECT * FROM t WHERE a >= 1")[0] == 1.0
    assert judge_sql_loose("SELECT * FROM t WHERE brand LIKE '%X%'", "SELECT * FROM t WHERE brand = 'X'")[0] == 0.8
    assert judge_sql_loose("SELECT * FROM t WHERE brand = 'X'", "SELECT * FROM t WHERE brand LIKE '%X%'")[0] == 0.9
    assert judge_sql_loose("SELECT a FROM t WHERE a > 1", "SELECT b FROM u WHERE b < 9")[0] == 0.0


def test_judge_keyword():
    score, reason = judge_keyword(
        "SELECT name FROM products WHERE price > 100",
        "SELECT name FROM products WHERE price > 100")
    assert score == 1.0 and "覆盖" in reason
    assert judge_keyword("SELECT name FROM products WHERE price > 100", "SELECT id FROM orders")[0] < 1.0


def test_judge_llm():
    same = FakeModel('{"same": true, "reason": "等价"}')
    different = FakeModel('{"same": false, "reason": "不同"}')
    assert judge_llm_sql("SELECT a FROM t", "SELECT a FROM t", "问题", same)[0] == 1.0
    assert judge_llm_answer("SELECT a FROM t", "SELECT b FROM t", "问题", different)[0] == 0.0


def test_judge_dispatch():
    """judge() 按策略名派发，评测侧因此只存名字不用存函数引用。"""
    assert set(JUDGES) == {"keyword", "sql_strict", "sql_loose", "llm_answer", "llm_sql"}
    assert set(JUDGE_LABELS) == set(JUDGES)
    assert judge("sql_strict", "SELECT a FROM t", "select a from t") == judge_sql_strict(
        "SELECT a FROM t", "select a from t")
    assert judge("keyword", "SELECT a FROM t", "SELECT a FROM t") == judge_keyword(
        "SELECT a FROM t", "SELECT a FROM t")
    try:
        judge("nonexistent", "SELECT 1", "SELECT 1")
    except KeyError:
        pass
    else:
        raise AssertionError("未知判别口径应当报错")


if __name__ == "__main__":
    for name, case in sorted(list(globals().items())):
        if name.startswith("test_"):
            case()
            print(f"  {name}: OK")
    print("test_candidates_and_judges: OK")
