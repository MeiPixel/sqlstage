"""可视化取值转成真实策略参数；未搜索的参数完整继承。"""
import json

LEGACY_PATHS = dict(zip(
    ("fewshots", "candidates", "guides", "descriptions", "samples", "table_ks", "table_methods", "joins", "column_methods", "column_ks", "same_tables", "votes", "selectors", "generation_modes", "camel_splits", "validates", "fallbacks", "desc_recalls", "dedups"),
    ("tricks.fewshot_top_k", "tricks.candidate_count", "tricks.guide_enabled", "tricks.field_description_count", "tricks.sample_rows", "tricks.table_top_k", "tricks.table_recall", "tricks.join_expansion", "tricks.column_recall", "tricks.column_top_k", "tricks.fewshot_same_table", "tricks.candidate_vote", "tricks.candidate_llm_selector", "orm.enabled", "tricks.camel_case_split", "tricks.llm_validate_names", "tricks.llm_lexical_fallback", "tricks.column_description_recall", "tricks.fewshot_dedup")))
ENUMS = {"BM25": "bm25", "BM25字段级": "bm25_field", "全部表": "all_schema", "大模型选表": "llm", "全部字段": "all_columns", "关键词": "keyword", "Embedding": "embedding", "Hybrid": "hybrid", "大模型选字段": "llm", "固定": "fixed"}


def dimensions_to_space(dimensions, base):
    space = {}
    for name, values in dimensions.items():
        if not values:
            raise ValueError(f"{name} 未选择任何取值")
        if values == ["继承"]:
            continue
        path = LEGACY_PATHS.get(name, name)
        current = base
        for part in path.split("."):
            current = getattr(current, part)
        converted = []
        for value in values:
            if value == "继承":
                choice = current
            elif path == "orm.enabled":
                choice = value in ("ORM", "开启", True)
            elif isinstance(current, bool):
                if value not in ("开启", "关闭", True, False):
                    raise ValueError(f"{name} 必须选择开启或关闭")
                choice = value in ("开启", True)
            elif isinstance(current, (int, float)):
                choice = type(current)(value)
            elif isinstance(current, list) and isinstance(value, str):
                choice = [float(item) for item in value.split("+")] if path.endswith("candidate_temperatures") else value.split("+")
            else:
                choice = ENUMS.get(value, value) if isinstance(value, str) else value
            if not any(json.dumps(choice, sort_keys=True) == json.dumps(item, sort_keys=True) for item in converted):
                converted.append(choice)
        space[path] = converted
    return space
