import time
import json
from copy import copy, deepcopy
from pathlib import Path

import sqlglot

from .candidates import VARIANT_HINTS, check_candidates, choose_by_llm, choose_candidates, extract_sql
from .database import DatabaseError, connect
from .fewshot import load_fewshots, recall_fewshots, render_fewshots
from .guide import load_guide, render_guide_context
from .model import OpenAICompatibleModel
from .orm import apply_orm_rules, compile_orm, parse_orm_plan
from .prompts import direct_sql_messages, orm_messages
from .pipeline import (
    deterministic_sql_repair, difficulty, difficulty_top_k, render_data_hints,
    retrieve_guide_lines, trim_prompt_parts,
)
from .retrieval import select_columns, select_tables
from .schema import render_schema
from .context import database_context, render_descriptions
from .query_object import correct_vector_values, render_object_code
from .search_data import normalize_records
from .prompt_budget import pack_prompt_context


class Text2SQLEngine:
    def __init__(self, database, strategy):
        self.database = copy(connect(database))
        # 执行护栏跟着策略走：连接是构造时就传进来的，护栏参数在 tricks 里，
        # 这里同步一次，工作台、服务模式、寻优、search_demo 四个入口自动都带上。
        self.database.max_rows = strategy.tricks.max_rows
        self.database.statement_timeout_ms = strategy.tricks.statement_timeout_ms
        self.dialect = self.database.dialect
        self.strategy = strategy
        self.schema, self.default_guide = database_context(self.database, self.database.schema)
        self.model = OpenAICompatibleModel(strategy.model)
        self.fewshots = load_fewshots(strategy.tricks.fewshot_path, strategy.tricks.fewshot_dedup)
        if not strategy.tricks.fewshot_path and self.database.dialect == "sqlite":
            root = Path(__file__).resolve().parents[1] / "examples"
            path = Path(self.database.path).resolve()
            if path.parent == (root / "databases").resolve():
                self.fewshots = normalize_records(json.loads((root / "datasets" / f"{path.stem}.json").read_text(encoding="utf-8")), self.dialect, self.schema)

    def render_context(self, tables, columns):
        rendered = render_schema(self.schema, tables, columns)
        descriptions = render_descriptions(self.schema, tables, columns, self.strategy.tricks.field_description_count)
        if descriptions:
            rendered += "\n\n## 字段说明\n" + descriptions
        if not self.strategy.tricks.sample_rows:
            return rendered
        samples = [
            f"SAMPLES {table}: " + repr(self.database.sample_rows(table, columns[table], self.strategy.tricks.sample_rows))
            for table in tables
        ]
        return rendered + "\n\n" + "\n".join(samples)

    def generate(self, question, history=(), request_started=None):
        history = list(history)
        started = request_started if request_started is not None else time.perf_counter()
        self.model.begin_timing(started)
        tricks = deepcopy(self.strategy.tricks)
        executed_tricks = []
        effective_question = question
        if tricks.question_rewrite:
            effective_question = self.model.complete([{"role": "user", "content":
                "把下面问题改写成明确的数据库查询要求，保留全部条件，不解答，只输出改写结果：\n" + question}]).strip()
            executed_tricks.append("question_rewrite")
        recall_question = " ".join(
            [item["content"] for item in history if item["role"] == "user"][-2:] + [effective_question])
        level = difficulty(effective_question)
        if tricks.difficulty_routing:
            tricks.table_top_k = difficulty_top_k(level, tricks.table_top_k, len(self.schema))
            if tricks.table_recall == "all_schema" and level != "complex":
                tricks.table_recall = "bm25"
            executed_tricks.append("difficulty_routing")
        tables = select_tables(recall_question, self.schema, tricks, self.model, self.database)
        columns = select_columns(recall_question, self.schema, tables, tricks, self.model)
        executed_tricks += [f"table_recall:{tricks.table_recall}", f"column_recall:{tricks.column_recall}"]
        schema_text = self.render_context(tables, columns)
        if tricks.entity_sniffing or tricks.distinct_probe:
            assert self.database is not None, "实体值定位和高频值探测需要数据库连接"
            data_hints = render_data_hints(
                self.database, self.schema, tables, columns, effective_question,
                tricks.entity_sniffing, tricks.distinct_probe,
            )
            schema_text += "\n\n## 数据值提示\n" + data_hints
            executed_tricks += [name for name, active in [
                ("entity_sniffing", tricks.entity_sniffing), ("distinct_probe", tricks.distinct_probe)
            ] if active]
        guide_source = load_guide(tricks) or getattr(self, "default_guide", "")
        guide_text = render_guide_context(
            guide_source, tricks.guide_max_chars
        ) if tricks.guide_enabled else ""
        if tricks.guide_enabled:
            executed_tricks.append("guide")
        if tricks.rag_enabled:
            guide_text += "\n\n## RAG召回片段\n" + retrieve_guide_lines(
                guide_source or schema_text, effective_question, tricks.guide_max_chars
            )
            executed_tricks.append("rag")
        selected_fewshots = recall_fewshots(
            effective_question,
            self.fewshots,
            tricks.fewshot_top_k,
            tables if tricks.fewshot_same_table else [],
            tricks.fewshot_exclude_self,
            tricks.fewshot_method,
            self.model,
            tricks.fewshot_difficulty,
        ) if tricks.fewshot_enabled else []
        fewshot_text = render_fewshots(selected_fewshots)
        if tricks.fewshot_enabled:
            executed_tricks.append("fewshot")
        core = render_schema(self.schema, tables, columns) if self.database is not None else schema_text
        schema_text, guide_text, fewshot_text, context_budget = pack_prompt_context(
            core, schema_text, guide_text, fewshot_text, tricks.prompt_max_chars)
        if self.strategy.orm.enabled:
            messages = orm_messages(effective_question, schema_text, self.strategy.orm, history,
                                    guide_text=guide_text, fewshot_text=fewshot_text)
            raw = self.model.complete(messages)
            plan = parse_orm_plan(raw)
            source_plan = deepcopy(plan)
            plan, corrections = correct_vector_values(plan, self.strategy.orm.vector_fields, self.database)
            plan = apply_orm_rules(plan, self.strategy.orm)
            sql = compile_orm(plan, self.strategy.orm.table_prefix, self.strategy.orm.quote_order_by)
        else:
            candidates = []
            for index in range(tricks.candidate_count):
                variant = tricks.candidate_variants[index % len(tricks.candidate_variants)]
                temperature = tricks.candidate_temperatures[index % len(tricks.candidate_temperatures)]
                messages = direct_sql_messages(
                    effective_question, schema_text, guide_text, fewshot_text,
                    VARIANT_HINTS[variant], self.dialect, history,
                )
                raw_output = self.model.complete(messages, temperature=temperature)
                candidates.append({"sql": extract_sql(raw_output), "raw": raw_output, "variant": variant})
            if tricks.candidate_execution_check:
                assert self.database is not None, "候选执行验证需要数据库连接"
                candidates = check_candidates(candidates, self.database)
            if len(candidates) == 1:
                chosen, choose_method = candidates[0], "single"
            elif tricks.candidate_llm_selector:
                chosen, choose_method = choose_by_llm(effective_question, candidates, self.model)
            elif not tricks.candidate_vote:
                chosen, choose_method = min(candidates, key=lambda item: len(item["sql"])), "shortest"
            else:
                chosen, choose_method = choose_candidates(candidates)
            raw = chosen["raw"]
            plan = None
            sql = chosen["sql"]
        if tricks.semantic_refine:
            raw = self.model.complete([{"role": "user", "content":
                f"核对SQL是否完整满足问题且不增加条件。只输出修正后的{self.dialect} SQL。\n问题：{effective_question}\nSQL：{sql}"}])
            sql = extract_sql(raw)
            executed_tricks.append("semantic_refine")
        if tricks.dialect_conversion:
            sql = sqlglot.transpile(sql, write=self.dialect)[0]
            executed_tricks.append("dialect_conversion")
        if tricks.deterministic_repair:
            sql = deterministic_sql_repair(sql)
            executed_tricks.append("deterministic_repair")
        rows = []
        needs_execution = tricks.execution_check or tricks.execution_feedback or tricks.empty_result_refine
        if needs_execution:
            repair_count = 0
            while True:
                try:
                    rows = self.execute(sql)
                    break
                except DatabaseError as error:
                    if not tricks.execution_feedback or repair_count >= tricks.repair_rounds:
                        raise
                    raw = self.model.complete([{"role": "user", "content":
                        f"修复{self.dialect}执行错误，只输出SQL。\n问题：{effective_question}\nSQL：{sql}\n错误：{error}"}])
                    sql = extract_sql(raw)
                    repair_count += 1
            if repair_count:
                executed_tricks.append("execution_feedback")
            if not rows and tricks.empty_result_refine:
                raw = self.model.complete([{"role": "user", "content":
                    f"SQL执行无结果。检查字段、值与比较方式，不得删除问题条件。只输出修正后的{self.dialect} SQL。\n问题：{effective_question}\nSQL：{sql}"}])
                sql = extract_sql(raw)
                rows = self.execute(sql)
                executed_tricks.append("empty_result_refine")
        timing = self.model.timing()
        return {
            "sql": sql,
            "rows": rows,
            "tables": tables,
            "columns": columns,
            "orm_plan": plan,
            "orm_source_plan": source_plan if self.strategy.orm.enabled else None,
            "orm_code": render_object_code(source_plan) if self.strategy.orm.enabled else None,
            "raw": raw,
            "latency_s": round(time.perf_counter() - started, 3),
            "trace": {
                "guide_chars": len(guide_text),
                "fewshot_chars": len(fewshot_text),
                "context_budget": context_budget,
                "read_only": bool(getattr(self.database, "read_only", False)),
                "fewshot_count": len(selected_fewshots),
                "difficulty": level,
                "effective_question": effective_question,
                "history_messages": len(history),
                "executed_tricks": executed_tricks,
                "candidates": candidates if not self.strategy.orm.enabled else [],
                "choose_method": choose_method if not self.strategy.orm.enabled else "orm",
                "value_corrections": corrections if self.strategy.orm.enabled else [],
                **timing,
            },
        }

    def execute(self, sql):
        columns, rows = self.database.fetch(sql)
        return [dict(zip(columns, row)) for row in rows]
