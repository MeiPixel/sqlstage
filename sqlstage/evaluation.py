import json
from collections import Counter
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope
from .database import DatabaseError, assert_read_only


def execution_score(predicted, gold, database):
    """只证明当前数据上的 EX，不用其替代严格 AST 指标。"""
    try:
        assert_read_only(predicted, database.dialect)
        assert_read_only(gold, database.dialect)
        pred_columns, pred_rows = database.fetch(predicted)
        gold_columns, gold_rows = database.fetch(gold)
    except DatabaseError as error:
        return False, f"{type(error).__name__}: {error}", ""
    if len(pred_columns) != len(gold_columns):
        return False, None, "返回列数不同"
    ordered = sqlglot.parse_one(gold, read=database.dialect).args.get("order") is not None
    matched = pred_rows == gold_rows if ordered else Counter(tuple(row) for row in pred_rows) == Counter(tuple(row) for row in gold_rows)
    return matched, None, "两者均为空集，不能据此证明语义等价" if not pred_rows and not gold_rows else ""


def canonical_sql(sql, dialect="sqlite"):
    """严格 AST 指纹，不等同执行语义证明。

    SQLite 标识符只折叠 ASCII；MySQL 只折叠列名（表名可能大小写敏感）；
    PostgreSQL 保留带引号的大写标识符。字符串字面量、SELECT 列、比较边界不改。
    别名按查询作用域处理，自连接保留两个独立来源；不重排 JOIN。
    """
    trees = sqlglot.parse(sql, read=dialect)
    assert len(trees) == 1, "评测只允许单条查询"
    tree = trees[0]
    assert isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)), "不是只读查询"
    assert not any(tree.find(kind) for kind in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Into)), "查询包含写操作"
    if dialect == "sqlite":
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        for identifier in tree.find_all(exp.Identifier):
            identifier.set("this", identifier.this.translate(ascii_lower))
    elif dialect == "mysql":
        for identifier in tree.find_all(exp.Identifier):
            if (isinstance(identifier.parent, exp.Column) and identifier.arg_key == "this") or isinstance(identifier.parent, exp.Alias):
                identifier.set("this", identifier.this.lower())
    else:
        normalize_identifiers(tree, dialect=dialect)
    _replace_aliases(tree)
    _normalize_equality(tree)
    for identifier in tree.find_all(exp.Identifier):
        if dialect != "postgres" or not identifier.args.get("quoted") or identifier.this == identifier.this.lower():
            identifier.set("quoted", False)
    return tree.sql(dialect=dialect, comments=False)


def _replace_aliases(tree):
    scopes = list(traverse_scope(tree))
    sources = {id(scope): dict(scope.selected_sources) for scope in scopes}
    replacements = {}
    for scope in scopes:
        physical = [(name, node, source) for name, (node, source) in sources[id(scope)].items()
                    if isinstance(source, exp.Table)]
        counts = Counter(source.name for _, _, source in physical)
        mapping = {}
        for index, (name, node, source) in enumerate(physical):
            if counts[source.name] > 1:
                canonical = f"__sqlstage_self_{index}"
                node.set("alias", exp.TableAlias(this=exp.to_identifier(canonical)))
            else:
                canonical = source.name
                node.set("alias", None)
            mapping[name] = canonical
        replacements[id(scope)] = mapping
    for scope in scopes:
        for column in scope.columns:
            owner = scope
            while owner is not None and column.table not in sources[id(owner)]:
                owner = owner.parent
            if owner is not None and column.table in replacements[id(owner)]:
                column.set("table", replacements[id(owner)][column.table])


def _normalize_equality(tree):
    """把相等比较的两侧按字典序排，让 ``a.id = b.id`` 与 ``b.id = a.id`` 等价。

    只处理相等与不等（``=`` / ``<>`` / ``!=``）——它们对称。
    大小比较不能碰：``y.v > 3`` 换成 ``3 > y.v`` 语义反转了，
    那是「归一化制造出错误等价」，比漏判更危险。

    JOIN 条件里左右写的顺序经常与 gold 相反，这是纯粹的书写顺序问题。
    """
    symmetric = (exp.EQ, exp.NEQ)
    for node in tree.walk():
        if isinstance(node, symmetric):
            left, right = node.this, node.expression
            if left.sql() > right.sql():
                node.set("this", right)
                node.set("expression", left)


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def exact_ast_score(pred_sql, gold_sql, dialect="sqlite"):
    return canonical_sql(pred_sql, dialect) == canonical_sql(gold_sql, dialect)


def score_prediction(pred_sql, gold_sql, dialect="sqlite"):
    try:
        return exact_ast_score(pred_sql, gold_sql, dialect), None
    except (sqlglot.errors.SqlglotError, AssertionError) as error:
        return False, f"{type(error).__name__}: {error}"
