"""固定抽样、真实参数搜索和可审计的演示结果。

公开版本不附带数据库、题库或 Guide。资源存在时可使用可选演示入口；
普通搜索须显式绑定用户自己的数据库与数据集，入口见 server/search.py。
"""
import csv
import hashlib
import json
import random
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .config import StrategyConfig
from .engine import Text2SQLEngine
from .evaluation import canonical_sql
from .model import OpenAICompatibleModel
from .optimization import apply_strategy_overrides, evaluate_candidate, expand_search_space
from .schema import read_sqlite_schema
from .search_data import fixed_sample, normalize_records, prepare_pool, question_key
from .search_runner import HEADERS, run_bound_search
from .search_variants import search_variants

STUDIO = Path(__file__).resolve().parents[1]
EXAMPLES = STUDIO / "examples"
GUIDES = EXAMPLES / "guides"
KNOWLEDGE = EXAMPLES / "knowledge"

# 历史可选资源的路径约定；发布包不附带数据，API 仅展示已安装资源。
DATABASE = EXAMPLES / "databases" / "ecommerce_9t.sqlite"
DATASETS = {
    "电商九表（推荐演示）": EXAMPLES / "datasets" / "ecommerce_9t.json",
    "Spider world_1": EXAMPLES / "datasets" / "world_1.json",
    "BIRD superhero": EXAMPLES / "datasets" / "superhero.json",
}


def load_questions(path):
    """题库是 JSON 数组（与 examples/manifest.json 配套分发），不是 JSONL。"""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path} 必须是题目数组，实际是 {type(rows).__name__}")
    return rows


def sample_dataset(path, seed, count=10, dialect="sqlite", schema=None):
    return fixed_sample(normalize_records(load_questions(path), dialect, schema), seed, count)


def demo_space(fewshots, candidates, guides, descriptions, samples, table_ks,
               table_methods, joins, column_methods, column_ks,
               same_tables, votes, selectors, generation_modes,
               camel_splits=("开启",), validates=("开启",), fallbacks=("关闭",),
               desc_recalls=("关闭",), dedups=("关闭",)):
    values = [fewshots, candidates, guides, descriptions, samples, table_ks,
              table_methods, joins, column_methods, column_ks,
              same_tables, votes, selectors, generation_modes,
              camel_splits, validates, fallbacks, desc_recalls, dedups]
    if any(not items for items in values):
        raise ValueError("每个搜索维度至少选择一个值")
    return {
        "tricks.fewshot_top_k": sorted({int(x) for x in fewshots}),
        "tricks.candidate_count": sorted({int(x) for x in candidates}),
        "tricks.guide_enabled": [x == "开启" for x in dict.fromkeys(guides)],
        "tricks.field_description_count": sorted({int(x) for x in descriptions}),
        "tricks.sample_rows": sorted({int(x) for x in samples}),
        "tricks.table_top_k": sorted({int(x) for x in table_ks}),
        "tricks.table_recall": [{"BM25": "bm25", "BM25字段级": "bm25_field", "全部表": "all_schema", "大模型选表": "llm"}[x] for x in dict.fromkeys(table_methods)],
        "tricks.join_expansion": [x == "开启" for x in dict.fromkeys(joins)],
        "tricks.column_recall": [{"全部字段": "all_columns", "BM25": "bm25", "大模型选字段": "llm"}[x] for x in dict.fromkeys(column_methods)],
        "tricks.column_top_k": sorted({int(x) for x in column_ks}),
        "tricks.fewshot_same_table": [x == "开启" for x in dict.fromkeys(same_tables)],
        "tricks.candidate_vote": [x == "开启" for x in dict.fromkeys(votes)],
        "tricks.candidate_llm_selector": [x == "开启" for x in dict.fromkeys(selectors)],
        "orm.enabled": [x == "ORM" for x in dict.fromkeys(generation_modes)],
        "tricks.camel_case_split": [x == "开启" for x in dict.fromkeys(camel_splits)],
        "tricks.llm_validate_names": [x == "开启" for x in dict.fromkeys(validates)],
        "tricks.llm_lexical_fallback": [x == "开启" for x in dict.fromkeys(fallbacks)],
        "tricks.column_description_recall": [x == "开启" for x in dict.fromkeys(desc_recalls)],
        "tricks.fewshot_dedup": [x == "开启" for x in dict.fromkeys(dedups)],
    }


def expand_demo_space(space, base=None):
    return [overrides for overrides, _ in search_variants(base or StrategyConfig(), space)]


def search_budget(space):
    combinations = expand_demo_space(space)
    calls = 0
    has_dynamic_join = False
    for item in combinations:
        per_question = item["tricks.candidate_count"]
        per_question += item["tricks.candidate_count"] > 1 and item["tricks.candidate_llm_selector"]
        per_question += item["tricks.table_recall"] == "llm"
        if item["tricks.column_recall"] == "llm":
            per_question += 13 if item["tricks.table_recall"] == "all_schema" else item["tricks.table_top_k"]
            has_dynamic_join |= item["tricks.join_expansion"]
        calls += per_question * 10
    suffix = "；JOIN扩展后的逐表字段召回可能使实际调用略高" if has_dynamic_join else ""
    return f"**{len(combinations)}种有效组合 × 固定10题**；预计至少调用模型 **{calls}次**（含大模型选表、选字段和LLM裁判{suffix}）。"


def render_context(schema, tables, columns, description_count, sample_count):
    blocks = []
    for table in tables:
        blocks.append(f"TABLE {table}")
        for column in schema[table]["columns"]:
            if column["name"] not in columns[table]:
                continue
            descriptions = column.get("descriptions", [])[:description_count]
            samples = column.get("samples", [])[:sample_count]
            line = f"{column['name']} {column['type']}"
            if descriptions:
                line += " | 说明：" + "、".join(descriptions)
            if samples:
                line += " | 样例值：" + json.dumps(samples, ensure_ascii=False)
            blocks.append(line)
        blocks.extend(f"JOIN {table}.{key['column']} = {key['target_table']}.{key['target_column']}"
                      for key in schema[table]["foreign_keys"])
    return "\n".join(blocks)


def load_knowledge(table):
    """读 examples/knowledge/<table>/knowledge.jsonl，返回 字段名 -> 记录。"""
    path = KNOWLEDGE / table / "knowledge.jsonl"
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["field"]: row for row in rows}


def load_demo_schema(database=DATABASE):
    """从真实 SQLite 读表结构，再用 knowledge 补上字段说明、样例值与表关键词。

    第二个返回值是每张表的关键词（表名 + 字段名 + 各字段的 sql_note），
    供 BM25 表召回打分用——这正是知识注入对召回的作用点。
    """
    schema = read_sqlite_schema(database)
    profiles = {}
    for table, meta in schema.items():
        knowledge = load_knowledge(table)
        for column in meta["columns"]:
            record = knowledge.get(column["name"], {})
            column["descriptions"] = [record["sql_note"]] if record.get("sql_note") else []
            column["samples"] = record.get("sample_values", [])
        profiles[table] = {
            "table_keywords": [table] + [column["name"] for column in meta["columns"]]
            + [column["descriptions"][0] for column in meta["columns"] if column["descriptions"]],
        }
    return schema, profiles


class DemoEngine(Text2SQLEngine):
    def __init__(self, strategy, schema, pool):
        self.database = None
        self.dialect = "sqlite"
        self.strategy = strategy
        self.schema = schema
        self.model = OpenAICompatibleModel(strategy.model)
        self.fewshots = pool

    def render_context(self, tables, columns):
        return render_context(self.schema, tables, columns,
                              self.strategy.tricks.field_description_count,
                              self.strategy.tricks.sample_rows)


def demo_base(model, profiles):
    base = StrategyConfig(model=deepcopy(model))
    base.tricks.table_recall = "bm25"
    base.tricks.table_descriptions = {name: " ".join(p["table_keywords"]) for name, p in profiles.items()}
    base.tricks.fewshot_enabled = True
    base.tricks.fewshot_method = "bm25"
    base.tricks.fewshot_same_table = False
    base.tricks.execution_check = False
    guide_paths = sorted(GUIDES.glob("*/guide.md"))
    base.tricks.guide_text = "\n\n".join(p.read_text(encoding="utf-8") for p in guide_paths)
    if not base.tricks.guide_text:
        raise ValueError("未找到 examples/guides/*/guide.md，无法演示Guide开关")
    base.tricks.guide_max_chars = 4000
    return base


def leaderboard(results):
    ranked = sorted(results, key=lambda r: (-r["accuracy"], r["latency_s"], r["index"]))
    return [[rank, r["index"], f"{round(r['accuracy'] * 10)}/10", round(r["accuracy"] * 100, 2),
             round(r["latency_s"], 3), r["errors"],
             r["overrides"]["tricks.fewshot_top_k"], r["overrides"]["tricks.candidate_count"],
             "开启" if r["overrides"]["tricks.guide_enabled"] else "关闭",
             strategy_summary(r["overrides"])] for rank, r in enumerate(ranked, 1)]


def strategy_summary(o):
    enabled = [
        f"表={o['tricks.table_recall']}/Top{o['tricks.table_top_k']}",
        f"字段={o['tricks.column_recall']}/Top{o['tricks.column_top_k']}",
        f"说明{o['tricks.field_description_count']}", f"样例{o['tricks.sample_rows']}",
        f"Few-shot{o['tricks.fewshot_top_k']}", f"Pick{o['tricks.candidate_count']}",
        "ORM" if o["orm.enabled"] else "直出SQL",
    ]
    enabled += [name for name, active in [
        ("JOIN扩展", o["tricks.join_expansion"]), ("Guide", o["tricks.guide_enabled"]),
        ("同表Few-shot", o["tricks.fewshot_same_table"]), ("投票", o["tricks.candidate_vote"]),
        ("LLM裁判", o["tricks.candidate_llm_selector"]),
    ] if active]
    return "；".join(enabled)


def run_search(strategy, records, space, database, **kwargs):
    """兼容模块入口；数据库必须显式传入，不再暗中选商品库。"""
    return run_bound_search(strategy, records, space, database, **kwargs)
