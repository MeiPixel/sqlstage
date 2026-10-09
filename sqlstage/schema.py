import sqlite3
from pathlib import Path
from urllib.parse import quote


def sqlite_uri(path, read_only=True):
    """把文件系统路径转成 SQLite URI；只读模式使用 file:...?mode=ro。"""
    if not read_only:
        return str(path)
    target = quote(Path(path).resolve().as_posix(), safe="/:")
    return f"file:{target}?mode=ro"


def read_sqlite_schema(path, read_only=True):
    connection = sqlite3.connect(sqlite_uri(path, read_only), uri=read_only)
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    result = {}
    for table in tables:
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        result[table] = {
            "columns": [{"name": row[1], "type": row[2], "primary_key": bool(row[5])} for row in columns],
            "foreign_keys": [
                {"column": row[3], "target_table": row[2], "target_column": row[4]}
                for row in foreign_keys
            ],
        }
    connection.close()
    return result


def render_schema(schema, tables=None, selected_columns=None):
    blocks = []
    table_names = list(schema) if tables is None else tables
    for table in table_names:
        meta = schema[table]
        allowed = set(selected_columns.get(table, [])) if selected_columns else None
        columns = ", ".join(
            f"{item['name']} {item['type']}{' PRIMARY KEY' if item['primary_key'] else ''}"
            for item in meta["columns"] if allowed is None or item["name"] in allowed
        )
        foreign_keys = ", ".join(
            f"{item['column']} -> {item['target_table']}.{item['target_column']}"
            for item in meta["foreign_keys"]
        )
        blocks.append(f"TABLE {table}\nCOLUMNS: {columns}\nFOREIGN KEYS: {foreign_keys or 'none'}")
    return "\n\n".join(blocks)
