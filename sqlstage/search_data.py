"""搜索数据准备：保留外部知识、按真实 Schema 标注表、隔离整批测试题。"""
import random
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def question_key(text):
    return re.sub(r"[\W_]+", "", text).casefold()


def normalize_records(rows, dialect="sqlite", schema=None):
    result = []
    for index, row in enumerate(rows):
        query = row.get("query") or row.get("question")
        sql = row.get("sql") or row.get("query_sql")
        if not query or not sql:
            raise ValueError(f"第{index + 1}条需要问题与标准 SQL")
        trees = sqlglot.parse(sql, read=dialect)
        if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ValueError(f"第{index + 1}条标准答案不是单条查询")
        tables = referenced_tables(trees[0], schema, dialect)
        result.append({**row, "id": row.get("id", index), "query": query, "sql": sql, "tables": tables})
    return result


def fixed_sample(rows, seed=42, count=10):
    if not 1 <= count <= 50:
        raise ValueError("抽样数量必须在 1—50 之间")
    unique = {question_key(row["query"]): row for row in reversed(rows)}
    if len(unique) < count:
        raise ValueError(f"测试集仅 {len(unique)} 道不重复题，无法抽取 {count} 道")
    return random.Random(int(seed)).sample(list(reversed(list(unique.values()))), count)


def prepare_pool(rows, records, dialect, schema, dedup=False):
    excluded = {question_key(row["query"]) for row in records}
    seen, pool = set(), []
    for row in normalize_records(rows, dialect, schema):
        key = question_key(row["query"])
        if key in excluded or (dedup and key in seen):
            continue
        seen.add(key)
        pool.append(row)
    return pool


def question_with_evidence(question, evidence):
    return question + ("\n\n题目提供的外部知识：" + evidence if evidence else "")

def referenced_tables(tree, schema, dialect):
    tables = []
    ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if not isinstance(source, exp.Table):
                continue
            name = source.name
            if schema is not None:
                matches = [table for table in schema if (table.translate(ascii_lower) == name.translate(ascii_lower) if dialect == "sqlite" else table == name)]
                if not matches:
                    raise ValueError(f"标准 SQL 引用了当前连接中不存在的表：{name}；请确认测试集绑定的数据库")
                name = matches[0]
            if name not in tables:
                tables.append(name)
    return tables
