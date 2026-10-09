"""只覆盖显式搜索参数；按真实有效策略去重，不修改继承来的配置。"""
import json
from dataclasses import asdict

from .optimization import apply_strategy_overrides, expand_search_space


def search_variants(base, space):
    variants, seen = [], set()
    for overrides in expand_search_space(space):
        for path in overrides:
            target = base
            for name in path.split("."):
                target = getattr(target, name)
        strategy = apply_strategy_overrides(base, overrides)
        if "tricks.fewshot_top_k" in overrides and "tricks.fewshot_enabled" not in overrides:
            strategy.tricks.fewshot_enabled = strategy.tricks.fewshot_top_k > 0
        effective = asdict(strategy)
        tricks = effective["tricks"]
        if effective["orm"]["enabled"]:
            tricks["candidate_count"] = 1
        if tricks["candidate_count"] == 1:
            for field in ("candidate_vote", "candidate_llm_selector", "candidate_execution_check"):
                tricks[field] = False
            tricks["candidate_variants"] = tricks["candidate_variants"][:1]
            tricks["candidate_temperatures"] = tricks["candidate_temperatures"][:1]
        if not tricks["fewshot_enabled"] or tricks["fewshot_top_k"] == 0:
            for name in list(tricks):
                if name.startswith("fewshot_"):
                    del tricks[name]
        if tricks["table_recall"] == "all_schema" and not tricks["difficulty_routing"]:
            tricks.pop("table_top_k")
            tricks.pop("join_expansion")
        if tricks["column_recall"] == "all_columns" and not tricks["column_description_recall"]:
            tricks.pop("column_top_k")
        key = json.dumps(effective, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            variants.append((overrides, strategy))
    return variants


def estimate_predictions(variants, count):
    return sum(count * (1 if strategy.orm.enabled else strategy.tricks.candidate_count)
               for _, strategy in variants)
