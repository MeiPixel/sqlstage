import json
import math
import re

from rank_bm25 import BM25Plus


CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_camel(text):
    return CAMEL_BOUNDARY.sub(" ", text)


def tokenize(text, camel_case_split=True):
    source = split_camel(text) if camel_case_split else text
    lowered = source.lower().replace("_", " ")
    words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index:index + 2] for index in range(len(chinese) - 1))
    return words


def lexical_score(question, text, camel_case_split=True):
    return len(tokenize(question, camel_case_split) & tokenize(text, camel_case_split))


def recall_by_lexical(question, documents, top_k, camel_case_split=True):
    ranked = sorted(
        documents,
        key=lambda name: (-lexical_score(question, documents[name], camel_case_split), name),
    )
    return ranked[:top_k]


def reciprocal_rank_fusion(rankings, top_k):
    scores = {}
    for ranking in rankings:
        for rank, name in enumerate(ranking, 1):
            scores[name] = scores.get(name, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda name: (-scores[name], str(name)))[:top_k]


NON_ENUM_TYPE = re.compile(r"INT|REAL|FLOAT|DOUBLE|DECIMAL|NUMERIC|DATE|TIME|BOOL")
ENUM_VALUE_LIMIT = 20
ENUM_VALUE_KEEP = 15


def enum_values(database, table, column, limit=ENUM_VALUE_LIMIT, keep=ENUM_VALUE_KEEP):
    """低基数字段的枚举取值；取值太多（非穷举字段）返回空。"""
    if NON_ENUM_TYPE.search(column["type"].upper()):
        return []
    values = database.distinct_values(table, column["name"], limit + 1)
    return [str(value) for value in values[:keep]] if len(values) <= limit else []


def bm25_field_scores(question, schema, descriptions, camel_case_split=True, database=None):
    query = sorted(tokenize(question, camel_case_split))
    scores = {}
    for table, meta in schema.items():
        documents = []
        for column in meta["columns"]:
            text = f"{table} {descriptions.get(table, '')} {column['name']}"
            if database is not None:
                values = enum_values(database, table, column)
                if values:
                    text += " " + " ".join(values)
            documents.append(sorted(tokenize(text, camel_case_split)))
        scores[table] = max(BM25Plus(documents).get_scores(query))
    return scores


def cosine(left, right):
    numerator = sum(a * b for a, b in zip(left, right))
    return numerator / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))


def recall_by_embedding(question, documents, top_k, model):
    names = list(documents)
    vectors = model.embed([question] + [documents[name] for name in names])
    ranked = sorted(zip(names, vectors[1:]), key=lambda item: -cosine(vectors[0], item[1]))
    return [name for name, _ in ranked[:top_k]]


def parse_name_list(text):
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    value = json.loads(match.group(1) if match else text)
    return value if isinstance(value, list) else value["names"]


def table_documents(schema, descriptions):
    return {
        table: " ".join(
            [table, descriptions.get(table, "")] + [column["name"] for column in meta["columns"]]
        )
        for table, meta in schema.items()
    }


def llm_select_names(question, documents, top_k, model, target):
    prompt = (
        f"你是Text2SQL的{target}选择器。根据问题从候选中选择最多{top_k}项。"
        "只输出JSON字符串数组，名称必须照抄。\n"
        + "\n".join(f"- {name}: {text}" for name, text in documents.items())
        + f"\n问题：{question}"
    )
    return parse_name_list(model.complete([{"role": "user", "content": prompt}]))[:top_k]


def expand_foreign_key_neighbors(selected, schema):
    expanded = list(selected)
    for table in list(selected):
        for foreign_key in schema[table]["foreign_keys"]:
            if foreign_key["target_table"] not in expanded:
                expanded.append(foreign_key["target_table"])
    for table, meta in schema.items():
        if any(item["target_table"] in selected for item in meta["foreign_keys"]) and table not in expanded:
            expanded.append(table)
    return expanded


def select_tables(question, schema, config, model, database=None):
    documents = table_documents(schema, config.table_descriptions)
    method = config.table_recall
    if method == "all_schema":
        selected = list(schema)
    elif method in {"keyword", "bm25"}:
        selected = recall_by_lexical(question, documents, config.table_top_k, config.camel_case_split)
    elif method == "bm25_field":
        scores = bm25_field_scores(question, schema, config.table_descriptions,
                                   config.camel_case_split, database)
        selected = sorted(scores, key=lambda name: (-scores[name], name))[:config.table_top_k]
    elif method == "embedding":
        selected = recall_by_embedding(question, documents, config.table_top_k, model)
    elif method == "llm":
        selected = llm_select_names(question, documents, config.table_top_k, model, "表")
        if config.llm_validate_names:
            selected = [name for name in selected if name in documents]
        if not selected and config.llm_lexical_fallback:
            selected = recall_by_lexical(question, documents, config.table_top_k, config.camel_case_split)
    elif method == "hybrid":
        lexical = recall_by_lexical(question, documents, config.table_top_k, config.camel_case_split)
        semantic = recall_by_embedding(question, documents, config.table_top_k, model)
        selected = reciprocal_rank_fusion([lexical, semantic], config.table_top_k)
    else:
        raise ValueError(f"未知表召回方法: {method}")
    return expand_foreign_key_neighbors(selected, schema) if config.join_expansion else selected


def select_columns(question, schema, tables, config, model):
    selected = {}
    for table in tables:
        meta = schema[table]
        documents = {item["name"]: f"{item['name']} {item['type']} " + " ".join(item.get("descriptions", [])) + item.get("comment", "") for item in meta["columns"]}
        if config.column_recall == "all_columns":
            names = list(documents)
            if config.column_description_recall and len(names) > config.column_description_threshold:
                names = recall_by_lexical(question, documents, config.column_top_k, config.camel_case_split)
        elif config.column_recall in {"keyword", "bm25"}:
            names = recall_by_lexical(question, documents, config.column_top_k, config.camel_case_split)
        elif config.column_recall == "embedding":
            names = recall_by_embedding(question, documents, config.column_top_k, model)
        elif config.column_recall == "llm":
            names = llm_select_names(question, documents, config.column_top_k, model, "字段")
            if config.llm_validate_names:
                names = [name for name in names if name in documents]
            if not names and config.llm_lexical_fallback:
                names = recall_by_lexical(question, documents, config.column_top_k, config.camel_case_split)
        elif config.column_recall == "hybrid":
            lexical = recall_by_lexical(question, documents, config.column_top_k, config.camel_case_split)
            semantic = recall_by_embedding(question, documents, config.column_top_k, model)
            names = reciprocal_rank_fusion([lexical, semantic], config.column_top_k)
        else:
            raise ValueError(f"未知字段召回方法: {config.column_recall}")
        if config.keep_primary_keys:
            names += [item["name"] for item in meta["columns"] if item["primary_key"]]
        if config.keep_foreign_keys:
            names += [item["column"] for item in meta["foreign_keys"]]
        selected[table] = list(dict.fromkeys(names))
    return selected
