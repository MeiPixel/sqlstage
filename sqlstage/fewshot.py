import json
from pathlib import Path

from .retrieval import lexical_score, recall_by_embedding, reciprocal_rank_fusion
from .pipeline import difficulty
from .search_data import question_key


def load_fewshots(path, dedup=False):
    if not path:
        return []
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not dedup:
        return rows
    seen, unique = set(), []
    for row in rows:
        key = (_question(row), row.get("sql") or row.get("query_sql"))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _question(row):
    return row.get("question") or row.get("query")


def recall_fewshots(question, rows, top_k, same_tables, exclude_self, method="bm25", model=None, match_difficulty=False):
    own_key = question_key(question.split("\n\n题目提供的外部知识：", 1)[0])
    candidates = [row for row in rows if not exclude_self or question_key(_question(row)) != own_key]
    if match_difficulty:
        candidates = [row for row in candidates if difficulty(_question(row)) == difficulty(question)]
    if same_tables:
        candidates = [row for row in candidates if set(row.get("tables", [row.get("table", "")])) & set(same_tables)]
    if method == "fixed":
        return candidates[:top_k]
    if method in {"bm25", "keyword", "hybrid"}:
        candidates.sort(key=lambda row: -lexical_score(question, _question(row)))
        lexical = candidates[:top_k]
        if method != "hybrid":
            return lexical
    documents = {str(index): _question(row) for index, row in enumerate(candidates)}
    indexes = recall_by_embedding(question, documents, top_k, model)
    semantic = [candidates[int(index)] for index in indexes]
    if method != "hybrid":
        return semantic
    lexical_ids = [str(candidates.index(row)) for row in lexical]
    return [candidates[int(index)] for index in reciprocal_rank_fusion([lexical_ids, indexes], top_k)]


def render_fewshots(rows):
    if not rows:
        return ""
    return "\n\n## Few-shot\n" + "\n\n".join(
        f"问题：{_question(row)}\nSQL：{row.get('sql') or row.get('query_sql') or row.get('pred_sql')}"
        for row in rows
    )
