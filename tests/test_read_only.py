"""数据库只读：连接层会话只读 + SQL 层 AST 校验，两层都要拦住写语句。

这是产品的安全底线，因此单独一个文件：任何一层失效都要能立刻看出来。

用原生 assert 直接跑：python tests/test_read_only.py
"""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage.database import (
    DatabaseError, ReadOnlyViolation, SQLiteDatabase, assert_read_only,
)
from sqlstage.schema import sqlite_uri

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from base import build_database


def build_sample(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE 商品 (商品名称 TEXT, 价格 INTEGER)")
    connection.executemany("INSERT INTO 商品 VALUES (?, ?)", [("A", 10), ("B", 20)])
    connection.commit()
    connection.close()


def test_read_only_guard():
    # 1. SQL 层：只读语句放行
    assert_read_only("SELECT * FROM 商品")
    assert_read_only("SELECT 商品名称 FROM 商品 WHERE 价格 > 5 ORDER BY 价格 DESC LIMIT 1")
    assert_read_only("SELECT COUNT(*) FROM 商品 UNION SELECT 1")
    assert_read_only("WITH t AS (SELECT * FROM 商品) SELECT * FROM t")

    # 2. SQL 层：写语句拦截
    for bad in [
        "DELETE FROM 商品",
        "UPDATE 商品 SET 价格 = 1",
        "INSERT INTO 商品 VALUES ('C', 30)",
        "DROP TABLE 商品",
        "CREATE TABLE x (a INT)",
        "ALTER TABLE 商品 ADD COLUMN y TEXT",
        "PRAGMA writable_schema = ON",
        "SELECT 1; DROP TABLE 商品",
        "WITH t AS (SELECT * FROM 商品) DELETE FROM 商品",
        "这不是 SQL",
        "",
    ]:
        try:
            assert_read_only(bad)
        except ReadOnlyViolation:
            pass
        else:
            raise AssertionError(f"未拦截非只读语句：{bad}")

    # 3. SQLite URI 生成（Windows 绝对路径也要能工作）
    assert sqlite_uri(Path("demo.db").resolve()).startswith("file:")
    assert sqlite_uri(Path("demo.db").resolve()).endswith("?mode=ro")
    assert sqlite_uri("demo.db", read_only=False) == "demo.db"

    with TemporaryDirectory() as directory:
        path = Path(directory) / "shop.db"
        build_sample(path)

        # 4. 只读连接：SELECT 正常执行
        readonly = SQLiteDatabase(path, read_only=True)
        assert readonly.read_only is True
        columns, rows = readonly.fetch("SELECT 商品名称 FROM 商品 ORDER BY 价格 DESC LIMIT 1")
        assert columns == ["商品名称"] and rows == [("B",)]

        # 5. 只读连接：Schema 正常读取
        assert readonly.schema["商品"]["columns"][0]["name"] == "商品名称"

        # 6. 只读连接：DELETE 被拦截，数据未被修改
        try:
            readonly.fetch("DELETE FROM 商品")
        except ReadOnlyViolation:
            pass
        else:
            raise AssertionError("只读连接未拦截 DELETE")
        remaining = readonly.fetch("SELECT COUNT(*) FROM 商品")[1][0][0]
        assert remaining == 2, f"数据被修改：{remaining}"

        # 7. 只读连接：探测接口仍然可用
        assert readonly.sample_rows("商品", ["商品名称", "价格"], 1) == [
            {"商品名称": "A", "价格": 10}]
        assert readonly.distinct_values("商品", "商品名称", 5) == ["A", "B"]
        assert readonly.top_values("商品", "商品名称", 5)[0][0] in ("A", "B")

        # 8. 关闭只读后写语句不再被拦截（证明开关真实生效，不是硬编码拒绝）
        writable = SQLiteDatabase(path, read_only=False)
        try:
            assert writable.fetch("DELETE FROM 商品") == ([], [])
        except ReadOnlyViolation as error:
            raise AssertionError(f"read_only=False 时不该拦截写操作：{error}") from error

        # 9. 只读模式下 SQLite 文件不存在时给出明确错误
        try:
            SQLiteDatabase(Path(directory) / "missing.db", read_only=True).schema
        except DatabaseError as error:
            assert "读取 SQLite Schema 失败" in str(error), str(error)
        else:
            raise AssertionError("只读模式下打开不存在的库应报错")

        # 10. 服务端参数解析与适配器构造：字段缺失按只读，显式布尔照原样传
        database = build_database("SQLite", {"database": str(path)})
        assert isinstance(database, SQLiteDatabase) and database.read_only is True

        database = build_database("SQLite", {"database": str(path), "read_only": False})
        assert database.read_only is False

        # 历史连接记录没有 read_only 字段时，默认只读
        database = build_database("SQLite", {"database": str(path)})
        assert database.read_only is True

        mysql = build_database("MySQL", {"host": "127.0.0.1", "port": 3306, "user": "u",
                                         "password": "p", "mysql_db": "d"})
        assert mysql.read_only is True
        postgres = build_database("PostgreSQL", {"host": "127.0.0.1", "port": 5432, "user": "u",
                                                 "password": "p", "mysql_db": "d",
                                                 "read_only": False})
        assert postgres.read_only is False

    # 11. 端到端：模型吐出 DELETE 时，开启执行校验也不会落到数据库
    with TemporaryDirectory() as directory:
        path = Path(directory) / "shop.db"
        build_sample(path)

        class FakeModel:
            def __init__(self, config):
                pass

            def begin_timing(self, started):
                pass

            def timing(self):
                return {"before_model_s": 0.0, "model_s": 0.0, "model_calls": 1}

            def complete(self, messages, temperature=None):
                return "```sql\nDELETE FROM 商品\n```"

        from sqlstage import StrategyConfig, Text2SQLEngine, TrickConfig
        from sqlstage import engine as engine_module

        original = engine_module.OpenAICompatibleModel
        engine_module.OpenAICompatibleModel = FakeModel
        try:
            strategy = StrategyConfig(
                tricks=TrickConfig(execution_check=True, execution_feedback=True, repair_rounds=1))
            engine = Text2SQLEngine(SQLiteDatabase(path, read_only=True), strategy)
            assert engine.database.read_only is True
            try:
                engine.generate("删除所有商品")
            except (ReadOnlyViolation, DatabaseError) as error:
                assert "只读" in str(error) or "READ ONLY" in str(error).upper(), str(error)
            else:
                raise AssertionError("端到端未拦截写语句")
        finally:
            engine_module.OpenAICompatibleModel = original

        # 数据完好无损
        assert SQLiteDatabase(path, read_only=True).fetch("SELECT COUNT(*) FROM 商品")[1][0][0] == 2


if __name__ == "__main__":
    test_read_only_guard()
    print("  test_read_only_guard: OK")
    print("test_read_only: OK")
