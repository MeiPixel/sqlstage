import json
import re
from collections import Counter

from .database import connect


VARIANT_HINTS = {
    "direct": "直接生成最符合问题的SQL。",
    "join_first": "先确认最小JOIN路径和连接键，再生成SQL。",
    "minimal": "使用最少的表、字段和条件，不增加问题未要求的逻辑。",
}


def check_candidates(candidates, database):
    database = connect(database)
    checked = []
    for candidate in candidates:
        item = dict(candidate)
        executable, error = database.try_execute(item["sql"])
        item.update(executable=executable, execution_error=error)
        checked.append(item)
    return checked


def extract_sql(text):
    stripped = text.strip()
    match = re.fullmatch(r"```(?:sqlite|sql)?\s*(.*?)\s*```", stripped, re.IGNORECASE | re.DOTALL)
    return (match.group(1) if match else stripped).strip().rstrip(";")


def normalize_sql(sql):
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


def choose_candidates(candidates):
    normalized = [normalize_sql(item["sql"]) for item in candidates]
    winner, votes = Counter(normalized).most_common(1)[0]
    if votes >= 2:
        return next(item for item, key in zip(candidates, normalized) if key == winner), "vote"
    executable = [item for item in candidates if item.get("executable", False)]
    pool = executable or candidates
    return min(pool, key=lambda item: len(item["sql"])), "shortest"


def choose_by_llm(question, candidates, model):
    prompt = (
        "根据问题选择语义最准确的SQL。只输出JSON，如 {\"pick\": 1}。\n"
        f"问题：{question}\n" + "\n".join(
            f"候选{index}: {item['sql']}" for index, item in enumerate(candidates, 1)
        )
    )
    picked = json.loads(model.complete([{"role": "user", "content": prompt}]))["pick"]
    return candidates[int(picked) - 1], "llm_selector"
