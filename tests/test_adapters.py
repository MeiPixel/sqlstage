"""MySQL 与 PostgreSQL 适配器：真库上的 Schema 解析、执行、探针、只读保护与引擎联动。

SQLite 覆盖不到的部分有两个，都在这里用真库验：连接层那句
SET SESSION TRANSACTION READ ONLY / SET SESSION CHARACTERISTICS，以及各家的
类型名与 information_schema 差异。

本机没有 MySQL / PostgreSQL 服务时打印 ENV_SKIP 并 exit 0，汇总入口据此记为「跳过」。
需要服务时先起：net start MySQLStudioTest、net start pgsqlstudio。

用原生 assert 直接跑：python tests/test_adapters.py
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pg8000.dbapi as pg
import pymysql

from sqlstage import (
    DEFAULT_POOL_SIZE, ModelConfig, MySQLDatabase, PostgreSQLDatabase, StrategyConfig,
    Text2SQLEngine, TrickConfig,
)
from sqlstage.database import DatabaseError, ReadOnlyViolation

TABLE = "adapter_probe"
MARK = "adapter_probe_written"

# 各家建库与建表语法不同，参数化时分开处理
MYSQL = dict(host="127.0.0.1", port=3306, user="root", password="", database="sqlstage_test",
             id_type="int", brand_type="varchar(64)", price_type="decimal(10,2)",
             mark_count="SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema='{database}' AND table_name='{mark}'")
POSTGRES = dict(host="127.0.0.1", port=5432, user="postgres", password="",
                # 维护连接的目标库（不能是待删的那个），测试库另建
                maintenance_db="postgres", database="sqlstage_test_pg",
                id_type="integer", brand_type="character varying(64)", price_type="numeric(10,2)",
                mark_count="SELECT COUNT(*) FROM pg_tables WHERE tablename='{mark}'")


class FakeModel:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []
        self.calls = 0

    def begin_timing(self, request_started):
        pass

    def complete(self, messages, temperature=None):
        self.messages.append(messages)
        self.calls += 1
        return next(self.outputs)

    def timing(self):
        return {"before_model_s": 0.01, "model_s": 0.02, "model_calls": self.calls}


def server_up(host, port, timeout=1.5):
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def prepare_mysql(spec):
    connection = pymysql.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                                 password=spec["password"], charset="utf8mb4")
    with connection.cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS {spec['database']}")
        cursor.execute(f"CREATE DATABASE {spec['database']} CHARACTER SET utf8mb4")
    connection.commit()
    connection.close()
    prepare_rows(lambda **kw: pymysql.connect(charset="utf8mb4", **kw), spec)


def prepare_postgres(spec):
    import pg8000.dbapi as pg
    connection = pg.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                            password=spec["password"], database=spec["maintenance_db"])
    connection.autocommit = True
    cursor = connection.cursor()
    cursor.execute(f"DROP DATABASE IF EXISTS {spec['database']}")
    cursor.execute(f"CREATE DATABASE {spec['database']}")
    cursor.close()
    connection.close()
    prepare_rows(lambda **kw: pg.connect(**kw), spec)


def prepare_rows(connect, spec):
    connection = connect(host=spec["host"], port=spec["port"], user=spec["user"],
                         password=spec["password"], database=spec["database"])
    cursor = connection.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {MARK}")
    cursor.execute(f"DROP TABLE IF EXISTS {TABLE}")
    cursor.execute(f"CREATE TABLE {TABLE}(id {spec['id_type']} PRIMARY KEY, name VARCHAR(32))")
    cursor.execute("CREATE TABLE products(id INT PRIMARY KEY, brand VARCHAR(64), "
                   f"name VARCHAR(64), price {spec['price_type']})")
    # MySQL 要单列 product_id 再补 FOREIGN KEY；PG 的 REFERENCES 语法自带列名
    if spec is MYSQL:
        cursor.execute("CREATE TABLE orders(id INT PRIMARY KEY, product_id INT, quantity INT, "
                       "FOREIGN KEY (product_id) REFERENCES products(id))")
    else:
        cursor.execute("CREATE TABLE orders(id INT PRIMARY KEY, quantity INT, "
                       "product_id INT REFERENCES products(id))")
    cursor.execute("INSERT INTO products VALUES (1,'ThinkPad','X1',9999),"
                   "(2,'MateBook','X Pro',7999),(3,'ThinkPad','T14',6999)")
    cursor.execute("INSERT INTO orders(id, product_id, quantity) VALUES "
                   "(1,1,2),(2,2,1),(3,1,5)")
    cursor.execute(f"INSERT INTO {TABLE} VALUES (1,'a'),(2,'b')")
    connection.commit()
    cursor.close()
    connection.close()


def has_mark(spec, database):
    _, rows = database.fetch(spec["mark_count"].format(database=spec["database"], mark=MARK))
    return rows[0][0] == 1


def open_database(spec, kind, read_only=True, pool_size=DEFAULT_POOL_SIZE):
    if kind == "mysql":
        return MySQLDatabase(spec["host"], spec["port"], spec["user"], spec["password"],
                             spec["database"], read_only=read_only, pool_size=pool_size)
    return PostgreSQLDatabase(spec["host"], spec["port"], spec["user"], spec["password"],
                              spec["database"], read_only=read_only, pool_size=pool_size)


def assert_adapter_contract(spec, kind, label):
    """三种能力都要在真库上验：Schema 归一化、执行与探针、只读保护。"""
    database = open_database(spec, kind)
    assert database.read_only is True, f"{label}: 默认必须是只读"

    schema = database.schema
    # 表名按字母序返回，保证提示里的顺序稳定可复现
    assert list(schema) == sorted(["orders", "products", TABLE]), list(schema)
    assert schema["products"]["columns"][0] == {
        "name": "id", "type": spec["id_type"], "primary_key": True}
    assert schema["products"]["columns"][1] == {
        "name": "brand", "type": spec["brand_type"], "primary_key": False}
    assert schema["orders"]["foreign_keys"] == [
        {"column": "product_id", "target_table": "products", "target_column": "id"}]

    ok, error = database.try_execute("SELECT missing FROM products")
    assert ok is False and "missing" in error
    assert database.try_execute("SELECT id FROM products")[0] is True
    assert set(database.distinct_values("products", "brand", 10)) == {"ThinkPad", "MateBook"}
    assert database.top_values("products", "brand", 5) == [("ThinkPad", 2), ("MateBook", 1)]
    assert database.sample_rows("products", ["id", "brand", "name"], 2) == [
        {"id": 1, "brand": "ThinkPad", "name": "X1"},
        {"id": 2, "brand": "MateBook", "name": "X Pro"},
    ]


def assert_read_only_guarded(spec, kind, label, verify_landing=True):
    """只读连接挡住写语句；关掉只读后写语句能真正落库。

    反证用 CREATE TABLE 而不是 DELETE：两个适配器都不提交事务，DML 会在连接关闭
    时回滚（只读定位下这其实是第三层保护）。MySQL 的 DDL 隐式提交，所以能留下
    可查证据；PostgreSQL 的 DDL 同样在事务里、连接关闭即回滚，因此 PG 侧
    verify_landing=False，只验证语句确实到达了数据库。
    """
    database = open_database(spec, kind)
    try:
        database.fetch(f"DELETE FROM {TABLE}")
    except ReadOnlyViolation:
        pass
    else:
        raise AssertionError(f"{label}: 只读连接未拦截 DELETE")

    # ReadOnlyViolation 必须仍是 DatabaseError，engine 的修复循环才会处理它
    assert issubclass(ReadOnlyViolation, DatabaseError)

    # 只读连接上的读要正常——这同时证明连接层那句 SET ... READ ONLY 没把会话搞坏
    _, rows = database.fetch(f"SELECT COUNT(*) FROM {TABLE}")
    assert rows[0][0] == 2, f"{label}: 只读连接读到 {rows}，应为 2 行"
    assert not has_mark(spec, database), f"{label}: 反证表应在此前清掉"

    # 反证：关掉只读后写语句能真正到达数据库，说明不是无条件拒绝。
    # DDL 没有结果集，顺带验证 fetch 不再假设 cursor.description 非空
    columns, rows = open_database(spec, kind, read_only=False).fetch(f"CREATE TABLE {MARK}(id INT)")
    assert columns == [] and rows == [], f"{label}: DDL 应返回空结果集，实际 {columns} {rows}"
    if verify_landing:
        assert has_mark(spec, database), f"{label}: read_only=False 时 DDL 没落库，拦截没真的接通"


def assert_engine_linkage(spec, kind, label):
    """探针与多轮执行都要能在真库上跑通。"""
    database = open_database(spec, kind)
    engine = Text2SQLEngine(database, StrategyConfig(
        model=ModelConfig(), tricks=TrickConfig(execution_check=True)))
    engine.model = FakeModel(["SELECT name, price FROM products WHERE brand = 'ThinkPad'"])
    result = engine.generate("列出 ThinkPad 的商品名和价格")
    assert result["sql"] == "SELECT name, price FROM products WHERE brand = 'ThinkPad'"
    assert [row["name"] for row in result["rows"]] == ["X1", "T14"], result["rows"]

    engine = Text2SQLEngine(open_database(spec, kind), StrategyConfig(
        model=ModelConfig(), tricks=TrickConfig(
            execution_check=False, entity_sniffing=True, distinct_probe=True)))
    engine.model = FakeModel(["SELECT id FROM products"])
    engine.generate("ThinkPad 有哪些")
    prompt = str(engine.model.messages[-1])
    assert "实体命中：products.brand=ThinkPad" in prompt, prompt
    assert "高频值：products.brand" in prompt, prompt


def assert_pool_reuses_connections(spec, kind, label):
    """真库上验池化：反复取还必须是同一条会话，且池满后多余的会被关掉。

    MySQL 侧能直接数服务端的 processlist，PG 只能靠连接 backend_pid 判断。
    """
    database = open_database(spec, kind, pool_size=2)
    try:
        assert database.pool._size == 2, database.pool._size
        # 反复取还：拿到的是同一条连接，不会每次都新建
        first = database.pool.acquire(database.session_setup)
        first_id = connection_identity(first, kind)
        database.pool.release(first)
        for _ in range(4):
            again = database.pool.acquire(database.session_setup)
            assert connection_identity(again, kind) == first_id, f"{label}: 连接未被复用"
            database.pool.release(again)

        # schema 读取也走池，不能另开连接
        database.pool._idle.queue.clear()
        database.schema
        assert database.pool._idle.qsize() == 1, f"{label}: schema 读取未走池"

        # 借出超过容量后全部归还，池里只留 2 条，多余的当场关掉
        held = [database.pool.acquire(database.session_setup) for _ in range(4)]
        identities = {connection_identity(item, kind) for item in held}
        assert len(identities) == 4, f"{label}: 同时在途的连接应互不相同，实际 {identities}"
        for connection in held:
            database.pool.release(connection)
        assert database.pool._idle.qsize() == 2, \
            f"{label}: 池满 2，多余的应被关掉，实际剩 {database.pool._idle.qsize()}"

        # 服务端掐掉空闲连接后，借出时应自动重建而不是把错误抛给用户
        stale = database.pool.acquire(database.session_setup)
        stale_id = connection_identity(stale, kind)
        terminate_connection(spec, kind, stale_id)
        assert database.fetch("SELECT 1")[1][0][0] == 1, f"{label}: 死连接未被重建"
        assert connection_identity(database.pool.acquire(database.session_setup), kind) != stale_id, \
            f"{label}: 掐掉的连接仍被复用"
    finally:
        database.pool.close()


def connection_identity(connection, kind):
    """连接的服务端标识：MySQL 用会话 id，PG 用 backend pid。"""
    if kind == "mysql":
        return connection.thread_id()
    cursor = connection.cursor()
    cursor.execute("SELECT pg_backend_pid()")
    return cursor.fetchone()[0]


def terminate_connection(spec, kind, identity):
    """从另一条连接把目标连接踢掉，模拟服务端掐空闲连接。"""
    if kind == "mysql":
        killer = pymysql.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                                 password=spec["password"], database=spec["database"])
        with killer.cursor() as cursor:
            cursor.execute(f"KILL {identity}")
        killer.close()
        return
    killer = pg.connect(host=spec["host"], port=spec["port"], user=spec["user"],
                        password=spec["password"], database=spec["maintenance_db"])
    cursor = killer.cursor()
    cursor.execute("SELECT pg_terminate_backend(%s)", (identity,))
    killer.commit()
    killer.close()


def test_mysql():
    if not server_up(MYSQL["host"], MYSQL["port"]):
        print(f"ENV_SKIP 本机 MySQL 未启动（{MYSQL['host']}:{MYSQL['port']}）")
        return False
    prepare_mysql(MYSQL)
    assert_adapter_contract(MYSQL, "mysql", "mysql")
    assert_read_only_guarded(MYSQL, "mysql", "mysql")
    assert_engine_linkage(MYSQL, "mysql", "mysql")
    assert_pool_reuses_connections(MYSQL, "mysql", "mysql")
    return True


def test_postgres():
    if not server_up(POSTGRES["host"], POSTGRES["port"]):
        print(f"ENV_SKIP 本机 PostgreSQL 未启动（{POSTGRES['host']}:{POSTGRES['port']}）")
        return False
    prepare_postgres(POSTGRES)
    assert_adapter_contract(POSTGRES, "postgres", "postgres")
    assert_read_only_guarded(POSTGRES, "postgres", "postgres", verify_landing=False)
    assert_engine_linkage(POSTGRES, "postgres", "postgres")
    assert_pool_reuses_connections(POSTGRES, "postgres", "postgres")
    return True


if __name__ == "__main__":
    ran = []
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            if case():
                ran.append(name[5:])
    if not ran:
        sys.exit(0)
    print(f"test_adapters: {' 与 '.join(ran)} 在真库上通过")
