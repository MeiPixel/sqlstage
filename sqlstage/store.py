"""SQLite 持久化层：连接、策略方案、数据集、运行记录与生成历史。"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "runs" / "studio.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'medium',
    query TEXT NOT NULL,
    sql TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    judge_score TEXT NOT NULL DEFAULT '',
    judge_reason TEXT NOT NULL DEFAULT '',
    ord INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dataset_id INTEGER,
    strategy_id INTEGER,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    gold_sql TEXT NOT NULL DEFAULT '',
    pred_sql TEXT NOT NULL DEFAULT '',
    score REAL,
    reason TEXT NOT NULL DEFAULT '',
    latency_s REAL,
    trace TEXT NOT NULL DEFAULT '{}',
    overrides TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    sql TEXT NOT NULL,
    connection_id INTEGER,
    strategy_id INTEGER,
    latency_s REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    strategy_id INTEGER NOT NULL,
    connection_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path=DEFAULT_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _execute(self, sql, params=()):
        with self._connect() as connection:
            return connection.execute(sql, params).lastrowid

    def _rows(self, sql, params=()):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---------- connections ----------
    def list_connections(self):
        return self._rows("SELECT * FROM connections ORDER BY id")

    def save_connection(self, name, type, params, connection_id=None):
        payload = (name, type, json.dumps(params, ensure_ascii=False))
        if connection_id:
            self._execute("UPDATE connections SET name=?, type=?, params=? WHERE id=?",
                          payload + (connection_id,))
            return connection_id
        return self._execute(
            "INSERT INTO connections (name, type, params, created_at) VALUES (?, ?, ?, ?)",
            payload + (_now(),))

    def delete_connection(self, connection_id):
        self._execute("DELETE FROM connections WHERE id=?", (connection_id,))

    def get_connection(self, connection_id):
        return self._row("SELECT * FROM connections WHERE id=?", (connection_id,))

    # ---------- strategies ----------
    def list_strategies(self):
        return self._rows("SELECT * FROM strategies ORDER BY id")

    def save_strategy(self, name, description, config, strategy_id=None):
        payload = (name, description, json.dumps(config, ensure_ascii=False), _now())
        if strategy_id:
            self._execute("UPDATE strategies SET name=?, description=?, config=?, updated_at=? WHERE id=?",
                          payload + (strategy_id,))
            return strategy_id
        return self._execute(
            "INSERT INTO strategies (name, description, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            payload + (_now(),))

    def delete_strategy(self, strategy_id):
        self._execute("DELETE FROM strategies WHERE id=?", (strategy_id,))

    def get_strategy(self, strategy_id):
        return self._row("SELECT * FROM strategies WHERE id=?", (strategy_id,))

    # ---------- publishes ----------
    def list_publishes(self):
        return self._rows("SELECT * FROM publishes ORDER BY id")

    def save_publish(self, name, strategy_id, connection_id):
        existing = self.get_publish(name)
        if existing:
            self._execute("UPDATE publishes SET strategy_id=?, connection_id=? WHERE name=?",
                          (strategy_id, connection_id, name))
            return existing["id"]
        return self._execute(
            "INSERT INTO publishes (name, strategy_id, connection_id, created_at) VALUES (?, ?, ?, ?)",
            (name, strategy_id, connection_id, _now()))

    def get_publish(self, name):
        return self._row("SELECT * FROM publishes WHERE name=?", (name,))

    def delete_publish(self, name):
        self._execute("DELETE FROM publishes WHERE name=?", (name,))

    # ---------- datasets ----------
    def list_datasets(self):
        return self._rows(
            "SELECT d.*, (SELECT COUNT(*) FROM dataset_items i WHERE i.dataset_id = d.id) AS item_count "
            "FROM datasets d ORDER BY d.id")

    def create_dataset(self, name, description=""):
        return self._execute("INSERT INTO datasets (name, description, created_at) VALUES (?, ?, ?)",
                             (name, description, _now()))

    def delete_dataset(self, dataset_id):
        self._execute("DELETE FROM dataset_items WHERE dataset_id=?", (dataset_id,))
        self._execute("DELETE FROM datasets WHERE id=?", (dataset_id,))

    def list_items(self, dataset_id):
        return self._rows("SELECT * FROM dataset_items WHERE dataset_id=? ORDER BY ord, id", (dataset_id,))

    def _item_rows(self, dataset_id, items, offset=0):
        return [(dataset_id, item.get("difficulty", "medium"), item["query"], item["sql"],
                 item.get("tags", ""), str(item.get("judge_score", "")),
                 item.get("judge_reason", ""), offset + index)
                for index, item in enumerate(items)]

    def replace_items(self, dataset_id, items):
        with self._connect() as connection:
            connection.execute("DELETE FROM dataset_items WHERE dataset_id=?", (dataset_id,))
            connection.executemany(
                "INSERT INTO dataset_items "
                "(dataset_id, difficulty, query, sql, tags, judge_score, judge_reason, ord) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._item_rows(dataset_id, items))

    def append_items(self, dataset_id, items):
        """在已有题目后面追加，ord 接着当前最大值排，避免整表读改写。"""
        with self._connect() as connection:
            start = connection.execute(
                "SELECT COALESCE(MAX(ord) + 1, 0) FROM dataset_items WHERE dataset_id=?",
                (dataset_id,)).fetchone()[0]
            connection.executemany(
                "INSERT INTO dataset_items "
                "(dataset_id, difficulty, query, sql, tags, judge_score, judge_reason, ord) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                self._item_rows(dataset_id, items, start))

    # ---------- runs ----------
    def create_run(self, kind, dataset_id=None, strategy_id=None):
        run_id = self._execute(
            "INSERT INTO runs (kind, dataset_id, strategy_id, status, created_at) VALUES (?, ?, ?, 'running', ?)",
            (kind, dataset_id, strategy_id, _now()))
        return run_id

    def finish_run(self, run_id, status, summary):
        self._execute("UPDATE runs SET status=?, summary=?, finished_at=? WHERE id=?",
                      (status, json.dumps(summary, ensure_ascii=False), _now(), run_id))

    def add_run_items(self, run_id, items):
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO run_items (run_id, query, gold_sql, pred_sql, score, reason, latency_s, trace, overrides) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, item["query"], item.get("gold_sql", ""), item.get("pred_sql", ""),
                  item.get("score"), item.get("reason", ""), item.get("latency_s"),
                  json.dumps(item.get("trace", {}), ensure_ascii=False),
                  json.dumps(item.get("overrides", {}), ensure_ascii=False))
                 for item in items])

    def list_runs(self, kind=None):
        sql = "SELECT * FROM runs"
        params = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        return self._rows(sql + " ORDER BY id DESC", params)

    def get_run(self, run_id):
        return self._row("SELECT * FROM runs WHERE id=?", (run_id,))

    def list_run_items(self, run_id):
        return self._rows("SELECT * FROM run_items WHERE run_id=? ORDER BY id", (run_id,))

    # ---------- generations ----------
    def add_generation(self, question, sql, connection_id=None, strategy_id=None, latency_s=None):
        return self._execute(
            "INSERT INTO generations (question, sql, connection_id, strategy_id, latency_s, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (question, sql, connection_id, strategy_id, latency_s, _now()))

    def list_generations(self, limit=50):
        return self._rows("SELECT * FROM generations ORDER BY id DESC LIMIT ?", (limit,))
