"""评测判分器：keyword / SQL 严格 / SQL 宽松 / LLM 判答案 / LLM 判 SQL。

统一接口 judge(reference, candidate, question="", model=None, database=None) -> (score, reason)。
严格判分复刻 eval/evaluator_sqlite_ast.py 的 AST 归一化规则；连接数据库时可追加结果集比对。
"""
import json
import re

import sqlglot
from sqlglot import exp

from .evaluation import canonical_sql


def _json_reply(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group(0))


def _sql_tokens(sql, dialect="sqlite"):
    tree = sqlglot.parse_one(sql, read=dialect)
    identifiers = {node.name for node in tree.find_all(exp.Identifier)}
    literals = {node.this for node in tree.find_all(exp.Literal)}
    return identifiers, literals


def judge_keyword(reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    ref_ids, ref_lits = _sql_tokens(reference, dialect)
    cand_ids, cand_lits = _sql_tokens(candidate, dialect)
    keywords = ref_ids | ref_lits
    if not keywords:
        return 1.0, "参考 SQL 无关键词"
    hit = keywords & (cand_ids | cand_lits)
    coverage = len(hit) / len(keywords)
    return coverage, f"关键词覆盖 {len(hit)}/{len(keywords)}={coverage:.2f}"


def judge_sql_strict(reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    if canonical_sql(reference, dialect) == canonical_sql(candidate, dialect):
        return 1.0, "SQL 规范化后完全相同"
    return 0.0, "SQL 不同"


def _and_terms(node):
    if isinstance(node, exp.And):
        return _and_terms(node.this) + _and_terms(node.expression)
    return [node]


def _combine_and(terms):
    if not terms:
        return None
    combined = terms[0]
    for term in terms[1:]:
        combined = exp.And(this=combined, expression=term)
    return combined


def _column_name(node):
    return node.name if isinstance(node, exp.Column) else ""


def _literal_text(node):
    return node.this if isinstance(node, exp.Literal) else None


def _is_count_field(name):
    return bool(re.search(r"数量|个数|接口|插槽|端口|麦克风数|扬声器数|档位数", name))


def _normalize_literals(tree):
    for node in list(tree.walk()):
        if isinstance(node, exp.Literal) and node.is_number:
            number = float(node.this)
            if number == int(number):
                node.replace(exp.Literal.number(str(int(number))))


def _normalize_comparators(tree):
    for node in list(tree.walk()):
        if isinstance(node, exp.GT):
            node.replace(exp.GTE(this=node.this.copy(), expression=node.expression.copy()))
        elif isinstance(node, exp.LT):
            node.replace(exp.LTE(this=node.this.copy(), expression=node.expression.copy()))


def _normalize_count_comparators(tree):
    for node in list(tree.walk()):
        if not isinstance(node, (exp.GT, exp.GTE)):
            continue
        name = _column_name(node.this)
        if not _is_count_field(name):
            continue
        value = _literal_text(node.expression)
        if (isinstance(node, exp.GT) and value == "0") or (isinstance(node, exp.GTE) and value == "1"):
            node.replace(exp.GTE(this=node.this.copy(), expression=exp.Literal.number("1")))


def _normalize_like_to_eq(tree):
    for node in list(tree.walk()):
        if not isinstance(node, exp.Like):
            continue
        value = _literal_text(node.expression)
        if value is None or not (value.startswith("%") and value.endswith("%")):
            continue
        exact_value = value[1:-1].replace("%", "").replace("_", "")
        node.replace(exp.EQ(this=node.this.copy(), expression=exp.Literal.string(exact_value)))


def _remove_redundant_identity_filters(tree):
    where = tree.args.get("where")
    if where is None:
        return
    terms = _and_terms(where.this)
    model_terms = [term for term in terms
                   if isinstance(term, exp.EQ) and _column_name(term.this) in {"型号", "型号名称"}]
    if not model_terms:
        return
    kept = [term for term in terms if _column_name(term.this) not in {"品牌", "品牌名称", "系列"}]
    tree.set("where", exp.Where(this=_combine_and(kept))) if kept else tree.set("where", None)


def _sort_and_terms(tree):
    where = tree.args.get("where")
    if where is None:
        return
    terms = _and_terms(where.this)
    terms.sort(key=lambda term: term.sql(dialect="sqlite"))
    tree.set("where", exp.Where(this=_combine_and(terms)))


def body_signature(tree, mode="plain", dialect="sqlite"):
    normalized = tree.copy()
    normalized.find(exp.Select).set("expressions", [exp.Star()])
    _normalize_literals(normalized)
    if mode in {"boundary", "count"}:
        _normalize_comparators(normalized)
    if mode == "count":
        _normalize_count_comparators(normalized)
    if mode == "like_to_eq":
        _normalize_like_to_eq(normalized)
    if mode == "identity":
        _remove_redundant_identity_filters(normalized)
    _sort_and_terms(normalized)
    return normalized.sql(dialect=dialect)


def has_like(tree):
    return any(isinstance(node, exp.Like) for node in tree.walk())


def judge_sql_loose(reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    ref_tree = sqlglot.parse_one(reference, read=dialect)
    cand_tree = sqlglot.parse_one(candidate, read=dialect)
    if has_like(cand_tree) != has_like(ref_tree):
        if body_signature(cand_tree, "like_to_eq", dialect) == body_signature(ref_tree, "like_to_eq", dialect):
            return (0.9 if has_like(cand_tree) else 0.8), "LIKE/= 归一化等价"
    if body_signature(cand_tree, "boundary", dialect) == body_signature(ref_tree, "boundary", dialect):
        return 1.0, "边界归一化等价（>/>=、</<=）"
    if body_signature(cand_tree, "count", dialect) == body_signature(ref_tree, "count", dialect):
        return 1.0, "数量字段归一化等价（>0 / >=1）"
    if body_signature(cand_tree, "identity", dialect) == body_signature(ref_tree, "identity", dialect):
        return 1.0, "型号唯一性归一化等价"
    return 0.0, "宽松比对不等价"


def judge_llm_sql(reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    prompt = (
        "你是SQL评审。判断两个SQL是否语义等价：查询目标、过滤条件、聚合口径一致即视为等价，"
        "忽略大小写、字段顺序和等价写法。只输出JSON：{\"same\": true/false, \"reason\": \"一句中文理由\"}。\n"
        f"问题：{question}\nSQL A：{reference}\nSQL B：{candidate}"
    )
    data = _json_reply(model.complete([{"role": "user", "content": prompt}]))
    return (1.0 if data["same"] else 0.0), data.get("reason", "")


def judge_llm_answer(reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    prompt = (
        "你是SQL结果评审。根据问题推断两个SQL分别会返回什么答案，判断二者答案是否一致。"
        "只输出JSON：{\"same\": true/false, \"reason\": \"一句中文理由\"}。\n"
        f"问题：{question}\nSQL A：{reference}\nSQL B：{candidate}"
    )
    data = _json_reply(model.complete([{"role": "user", "content": prompt}]))
    return (1.0 if data["same"] else 0.0), data.get("reason", "")


JUDGES = {
    "keyword": judge_keyword,
    "sql_strict": judge_sql_strict,
    "sql_loose": judge_sql_loose,
    "llm_answer": judge_llm_answer,
    "llm_sql": judge_llm_sql,
}

JUDGE_LABELS = {
    "keyword": "关键词判断",
    "sql_strict": "SQL 严格相同",
    "sql_loose": "SQL 宽松相同",
    "llm_answer": "LLM 判答案",
    "llm_sql": "LLM 判 SQL",
}


def judge(name, reference, candidate, question="", model=None, database=None, dialect="sqlite"):
    return JUDGES[name](reference, candidate, question, model, database, dialect)
