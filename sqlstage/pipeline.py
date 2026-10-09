import re

from .candidates import extract_sql
from .database import connect
from .retrieval import lexical_score


def difficulty(question):
    if re.search(r"连接|关联|跨表|join|按.+(?:统计|分组)", question, re.I):
        return "complex"
    if re.search(r"平均|总和|合计|统计|数量|多少|最大|最小|排名|每个|分组", question):
        return "medium"
    return "simple"


def difficulty_top_k(level, configured, table_count):
    if level == "simple":
        return min(1, table_count)
    if level == "complex":
        return min(max(3, configured), table_count)
    return min(configured, table_count)


def deterministic_sql_repair(sql):
    cleaned = extract_sql(sql)
    return re.sub(r"(?<![<>!=])==(?!=)", "=", cleaned).strip().rstrip(";")


def retrieve_guide_lines(text, question, max_chars):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    ranked = sorted(lines, key=lambda line: -lexical_score(question, line))
    selected = []
    for line in ranked:
        if sum(map(len, selected)) + len(line) > max_chars:
            continue
        selected.append(line)
    return "\n".join(selected)


def trim_prompt_parts(parts, max_chars):
    remaining = int(max_chars)
    result = []
    for part in parts:
        value = part[:remaining]
        result.append(value)
        remaining -= len(value)
    return result


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def render_data_hints(database, schema, tables, columns, question, entity_sniffing, distinct_probe):
    database = connect(database)
    lines = []
    for table in tables:
        for column in columns[table]:
            if entity_sniffing:
                values = database.distinct_values(table, column, 100)
                lines += [f"实体命中：{table}.{column}={value}" for value in values if str(value) in question]
            if distinct_probe:
                values = database.top_values(table, column, 5)
                rendered = "、".join(f"{value}({count})" for value, count in values)
                lines.append(f"高频值：{table}.{column}={rendered}")
    return "\n".join(lines)
