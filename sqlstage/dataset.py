"""评测数据集生成：按数量与难度比例让大模型产出候选题。"""
import json
import re

DIFFICULTY_NAMES = {"easy": "简单", "medium": "中等", "hard": "困难"}


def allocate_counts(total, easy, medium, hard):
    """按易/中/难比例把 total 分成整数数量，余数按小数部分从大到小补。"""
    weights = {"easy": easy, "medium": medium, "hard": hard}
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        raise ValueError("难度比例之和必须大于 0")
    raw = {name: total * weight / weight_sum for name, weight in weights.items()}
    counts = {name: int(value) for name, value in raw.items()}
    order = sorted(weights, key=lambda name: -(raw[name] - counts[name]))
    for name in order:
        if sum(counts.values()) >= total:
            break
        counts[name] += 1
    return counts


def build_generation_messages(schema_text, instruction, total, easy, medium, hard):
    counts = allocate_counts(total, easy, medium, hard)
    requirement = "；".join(
        f"{DIFFICULTY_NAMES[name]} {counts[name]} 题" for name in ("easy", "medium", "hard")
    )
    prompt = (
        "你是 Text2SQL 评测集出题专家。根据数据库结构和出题要求，生成全新的查询题及其标准 SQL。\n"
        f"出题要求：{instruction or '围绕数据库核心字段设计常见查询'}\n"
        f"数量与难度：共 {total} 题，其中 {requirement}。\n"
        "难度定义：简单=单表条件查询；中等=聚合/排序/分组；困难=多表 JOIN 或复合条件。\n"
        "标准 SQL 必须能在该数据库直接执行，字段名与表名严格来自数据库结构。\n"
        "只输出 JSON 数组，每项含 difficulty(easy/medium/hard)、query(中文问题)、sql(标准 SQL)，不要输出解释。\n\n"
        f"数据库结构：\n{schema_text}"
    )
    return [{"role": "user", "content": prompt}]


def parse_generated_items(text):
    match = re.search(r"\[.*\]", text, re.DOTALL)
    items = json.loads(match.group(0))
    return [
        {
            "difficulty": item.get("difficulty", "medium"),
            "query": item["query"],
            "sql": item["sql"],
        }
        for item in items
    ]
