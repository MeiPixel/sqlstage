"""生产护栏：只读拦危险函数、执行限行数与超时、评测认方言、模型重试、背压。

这四项都是「平时不出事、出事就是事故」的地方，任何一条失效都要能立刻看出来，
所以集中一个文件，逐条给出可复现的反证。

用原生 assert 直接跑：python tests/test_production_guards.py
"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage import StrategyConfig, Text2SQLEngine, TrickConfig
from sqlstage.config import ModelConfig
from sqlstage.database import (
    DatabaseError, ReadOnlyViolation, RowLimitExceeded, SQLiteDatabase, assert_read_only,
)
from sqlstage.evaluation import canonical_sql, score_prediction
from sqlstage.model import OpenAICompatibleModel, retryable_status

MYSQL_GOLD = "SELECT `name`, `GDP` FROM `country` WHERE `Population` > 1000000 LIMIT 5"
PG_GOLD = 'SELECT "name" FROM "country" WHERE "id" IN (SELECT "id" FROM "city")'


def blocked(sql, dialect):
    try:
        assert_read_only(sql, dialect)
    except ReadOnlyViolation:
        return True
    return False


def test_read_only_blocks_dangerous_functions():
    """只读不等于安全：读写文件、跨库执行、加载扩展的函数都以 SELECT 形式出现。

    连接层的只读事务拦不住它们——PG 的 pg_write_file 在只读事务里照样成功，
    所以 SQL 层必须自己按函数名拦。这一组每条都是真实攻击面。
    """
    for sql, dialect in [
        ("SELECT pg_read_file('/etc/passwd')", "postgres"),
        ("SELECT pg_read_binary_file('/etc/shadow')", "postgres"),
        ("SELECT pg_write_file('/tmp/evil.txt', 'x')", "postgres"),
        ("SELECT pg_ls_dir('/')", "postgres"),
        ("SELECT lo_import('/etc/passwd')", "postgres"),
        ("SELECT dblink_exec('host=x dbname=y', 'DROP TABLE t')", "postgres"),
        ("SELECT load_extension('evil.so')", "sqlite"),
        ("SELECT readfile('/etc/passwd')", "mysql"),
        ("SELECT load_file('/etc/passwd')", "mysql"),
        # 藏在子查询与 CTE 里同样要拦，不能只看顶层
        ("SELECT * FROM t WHERE id IN (SELECT pg_read_file('/etc/passwd'))", "postgres"),
        ("WITH x AS (SELECT pg_read_file(chr(47))) SELECT * FROM x", "postgres"),
        # 行锁属于写语义
        ("SELECT * FROM t FOR UPDATE", "mysql"),
        ("SELECT * FROM t FOR SHARE", "postgres"),
    ]:
        assert blocked(sql, dialect), f"未拦截危险语句：{dialect} {sql}"

    # 名字大小写不同也要拦，避免用大小写绕过
    assert blocked("SELECT PG_READ_FILE('/etc/passwd')", "postgres")
    assert blocked("SELECT Load_Extension('x.so')", "sqlite")


def test_read_only_still_allows_normal_queries():
    """加拦截不能误杀正常分析 SQL，否则功能等于废掉。"""
    for sql, dialect in [
        ("SELECT * FROM t", "sqlite"),
        ("SELECT COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 1", "mysql"),
        ("SELECT a FROM t UNION SELECT b FROM u", "postgres"),
        ("WITH t AS (SELECT 1 AS a) SELECT * FROM t", "sqlite"),
        ("SELECT t1.a FROM t1 JOIN t2 ON t1.id = t2.id WHERE t2.b LIKE '%x%'", "postgres"),
        ("SELECT MAX(price) FROM orders", "mysql"),
        ("SELECT coalesce(a, 0), cast(b AS TEXT) FROM t", "postgres"),
        ("SELECT row_number() OVER (PARTITION BY a ORDER BY b) FROM t", "mysql"),
        ("WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 5) "
         "SELECT * FROM t", "sqlite"),
    ]:
        assert_read_only(sql, dialect)


def build_rows_db(path, rows=500):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t(a INTEGER)")
    connection.executemany("INSERT INTO t VALUES(?)", [(index,) for index in range(rows)])
    connection.commit()
    connection.close()


def test_row_limit_and_statement_timeout():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "big.db"
        build_rows_db(path)

        # 默认 5000 上限，500 行不该触发
        database = SQLiteDatabase(path)
        assert database.max_rows == 5000
        assert len(database.fetch("SELECT a FROM t")[1]) == 500

        # 超过上限直接报错，不静默截断：半截的结果会被下游当成完整答案
        limited = SQLiteDatabase(path, max_rows=100)
        try:
            limited.fetch("SELECT a FROM t")
        except RowLimitExceeded as error:
            assert "超过上限" in str(error), str(error)
        else:
            raise AssertionError("超过 max_rows 未报错")

        # 语句超时：progress_handler 到点打断
        tight = SQLiteDatabase(path, statement_timeout_ms=1)
        started = time.perf_counter()
        try:
            tight.fetch("SELECT count(*) FROM t a, t b, t c, t d")
        except DatabaseError:
            assert time.perf_counter() - started < 5, "超时未在预期时间内生效"
        else:
            raise AssertionError("重查询未触发语句超时")

        # 超时不能影响正常查询
        assert len(SQLiteDatabase(path, statement_timeout_ms=30000)
                   .fetch("SELECT a FROM t LIMIT 10")[1]) == 10


def test_engine_applies_guards_to_database():
    """护栏参数在 tricks 里，连接是构造时传进来的，engine 要把它们同步过去。

    四个入口（工作台、服务模式、寻优、search_demo）都经由 Text2SQLEngine，
    同步一次就全覆盖，不必在每个调用点重复传。
    """
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "big.db"
        build_rows_db(path)
        engine = Text2SQLEngine(SQLiteDatabase(path), StrategyConfig(
            tricks=TrickConfig(max_rows=10, statement_timeout_ms=5000)))
        assert engine.database.max_rows == 10
        assert engine.database.statement_timeout_ms == 5000


def test_evaluation_respects_dialect():
    """canonical_sql 写死 sqlite 时，MySQL 反引号语法会解析失败。

    这会让非 SQLite 方言的评测准确率静默变成 0——不报错，只是算错，
    拿着这份数据做技术选型就完了。因此三个方言都必须自比得 1。
    """
    assert score_prediction(MYSQL_GOLD, MYSQL_GOLD, "mysql") == (True, None)
    assert score_prediction(PG_GOLD, PG_GOLD, "postgres") == (True, None)
    # 去掉引号差异后仍应等价
    assert score_prediction("SELECT name, GDP FROM country WHERE Population > 1000000 LIMIT 5",
                            MYSQL_GOLD, "mysql")[0]
    # sqlite 默认行为不变
    assert score_prediction("select a from t", "SELECT a FROM t;") == (True, None)
    # 不同 SQL 仍然判不等
    assert score_prediction("SELECT b FROM t", "SELECT a FROM t")[0] is False


def test_evaluation_ignores_table_aliases():
    """表别名是写法差异不是语义差异，必须双向等价。

    Spider 的 gold 习惯 `country AS T1`，模型习惯写表名或 c/cl。
    不归一化的话同一份语义会因别名判错——实测这一项曾让 MySQL 方言的
    基线准确率从 33%掉到 7%，而 SQL 本身完全正确。

    列的限定名挂在 Column.table 上（字符串），SELECT / ON / WHERE 三处都要换，
    漏一处就前功尽弃，所以三处都验。
    """
    gold = ("SELECT T1.Region FROM country AS T1 JOIN city AS T2 "
            "ON T1.Code = T2.CountryCode WHERE T2.Name = 'Kabul'")
    for pred in [
        # 表名限定
        "SELECT country.Region FROM country JOIN city "
        "ON country.Code = city.CountryCode WHERE city.Name = 'Kabul'",
        # 换一套别名
        "SELECT c.Region FROM country AS c JOIN city AS ci "
        "ON c.Code = ci.CountryCode WHERE ci.Name = 'Kabul'",
    ]:
        assert score_prediction(pred, gold, "mysql")[0], pred

    # gold 本身用无限定列名时，AS 省略与换别名同样要等价
    plain_gold = ("SELECT Region FROM country AS T1 JOIN city AS T2 "
                  "ON T1.Code = T2.CountryCode WHERE T2.Name = 'Kabul'")
    for pred in [
        "SELECT Region FROM country T1 JOIN city T2 "
        "ON T1.Code = T2.CountryCode WHERE T2.Name = 'Kabul'",
        "SELECT Region FROM country AS c JOIN city AS ci "
        "ON c.Code = ci.CountryCode WHERE ci.Name = 'Kabul'",
    ]:
        assert score_prediction(pred, plain_gold, "mysql")[0], pred

    # 三表 JOIN 里的别名也要换全
    triple_gold = ("SELECT T2.Language FROM country AS T1 JOIN countrylanguage AS T2 "
                   "ON T1.Code = T2.CountryCode WHERE T1.Name = 'Aruba'")
    triple_pred = ("SELECT cl.Language FROM country AS c JOIN countrylanguage AS cl "
                   "ON cl.CountryCode = c.Code WHERE c.Name = 'Aruba'")
    assert score_prediction(triple_pred, triple_gold, "mysql")[0]

    # JOIN 条件里等号左右互换也该等价：条件对称，写的顺序无关
    assert score_prediction("SELECT a FROM x JOIN y ON y.id = x.id",
                            "SELECT a FROM x JOIN y ON x.id = y.id", "mysql")[0]
    assert score_prediction("SELECT a FROM x WHERE y.v <> x.v",
                            "SELECT a FROM x WHERE x.v <> y.v", "mysql")[0]

    # 但大小比较不能互换：`x.v > 3` 与 `3 > x.v` 语义相反，
    # 归一化若碰了这类运算就是「制造出错误的等价」，比漏判更危险
    assert score_prediction("SELECT a FROM x WHERE 3 > x.v",
                            "SELECT a FROM x WHERE x.v > 3", "mysql")[0] is False
    assert score_prediction("SELECT a FROM x WHERE y.v < x.v",
                            "SELECT a FROM x WHERE x.v < y.v", "mysql")[0] is False

    # 但别名不同**不能**掩盖真实差异：换的是列不是表
    assert score_prediction(
        "SELECT T1.Population FROM country AS T1 JOIN city AS T2 "
        "ON T1.Code = T2.CountryCode WHERE T2.Name = 'Kabul'",
        gold, "mysql")[0] is False


def test_evaluation_keeps_meaningful_differences():
    """归一化不能把语义不同的 SQL 判成等价——比漏判更危险，会虚高准确率。"""
    gold = "SELECT COUNT(*) FROM country WHERE GovernmentForm = 'Republic'"
    for pred in [
        # 用 LIKE 代替 =：范围不同
        "SELECT COUNT(*) FROM country WHERE GovernmentForm LIKE '%Republic%'",
        # 聚合方式不同
        "SELECT SUM(Population) FROM country WHERE GovernmentForm = 'Republic'",
        # 反引号在 MySQL 里是标识符引号，与单引号语义不同
        "SELECT COUNT(*) FROM country WHERE GovernmentForm = `GovernmentForm`",
        # 列顺序不同（严格 AST 口径判错，符合预期）
        "SELECT Region, Population FROM country WHERE Name = 'Angola'",
    ]:
        assert score_prediction(pred, gold, "mysql")[0] is False, pred

    gold_multi = "SELECT Population, Region FROM country WHERE Name = 'Angola'"
    assert score_prediction("SELECT Region, Population FROM country WHERE Name = 'Angola'",
                            gold_multi, "mysql")[0] is False
    # JOIN 少一张表必须判错
    assert score_prediction("SELECT a FROM x JOIN y ON x.id = y.id",
                            "SELECT a FROM x JOIN y ON x.id = y.id JOIN z ON z.id = y.id",
                            "mysql")[0] is False
    # 反引号与单引号在 MySQL 下不可互换
    assert score_prediction("SELECT a FROM t WHERE g = `X`", "SELECT a FROM t WHERE g = 'X'",
                            "mysql")[0] is False
    # MySQL 下双引号也是字符串引号，与单引号等价
    assert score_prediction('SELECT a FROM t WHERE g = "X"', "SELECT a FROM t WHERE g = 'X'",
                            "mysql")[0]
    # SQLite 相反：双引号是标识符引号（沿用 SQL 标准），
    # 这就是 tools/build_mysql_benchmark.py 要把 gold 的双引号字符串纠正成单引号的原因
    assert score_prediction('SELECT a FROM t WHERE g = "X"', "SELECT a FROM t WHERE g = 'X'",
                            "sqlite")[0] is False


def test_evaluation_normalizes_identifier_case_per_dialect():
    """SQLite 折叠 ASCII；PG 保留带引号大写；MySQL 表名不盲目折叠。

    Spider 的 gold 里大小写并不统一（`SUM(population)` 与 `SUM(Population)` 混用），
    不归一就会把完全正确的 SQL 判错。字符串字面量则始终敏感，不能一起小写。
    """
    # MySQL 与 PG：列名大小写不敏感
    for dialect in ("mysql", "postgres"):
        assert score_prediction(
            "SELECT AVG(GNP), SUM(Population) FROM country WHERE GovernmentForm = 'US Territory'",
            "SELECT AVG(GNP), SUM(population) FROM country WHERE GovernmentForm = 'US Territory'",
            dialect)[0], dialect
    assert score_prediction("SELECT a FROM Country", "SELECT a FROM country", "postgres")[0]
    assert score_prediction("SELECT a FROM Country", "SELECT a FROM country", "mysql")[0] is False
    assert score_prediction('SELECT "Name" FROM country', "SELECT name FROM country", "postgres")[0] is False

    # SQLite：ASCII 标识符不区分大小写。
    assert score_prediction("SELECT a FROM Country", "SELECT a FROM country",
                            "sqlite")[0]

    # 字符串字面量在所有方言下都敏感：'US Territory' 与 'us territory' 查不到同一批行
    for dialect in ("mysql", "postgres", "sqlite"):
        assert score_prediction(
            "SELECT a FROM t WHERE g = 'us territory'",
            "SELECT a FROM t WHERE g = 'US Territory'", dialect)[0] is False, dialect


def model_request():
    return httpx.Request("POST", "http://model.invalid")


def status_error(code):
    return openai.APIStatusError("upstream error",
                                 response=httpx.Response(code, request=model_request()),
                                 body=None)


def fake_client(sequence):
    """sequence 里每个元素是异常或 'ok'，按顺序返回；返回 (计数器, 客户端)。"""
    calls = {"n": 0}

    class Message:
        content = "SELECT 1"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    class Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            step = sequence[min(calls["n"] - 1, len(sequence) - 1)]
            if isinstance(step, Exception):
                raise step
            return Response()

    return calls, type("Client", (), {"chat": type("Chat", (), {"completions": Completions()})()})()


def connect_error():
    return openai.APIConnectionError(request=model_request())


def test_model_retry_policy():
    # 状态码判定：429 与 5xx 可重试，其余 4xx 是请求本身的问题
    for code, want in [(429, True), (500, True), (503, True),
                       (400, False), (401, False), (404, False)]:
        assert retryable_status(status_error(code)) is want, f"状态码 {code} 判定错"
    assert retryable_status(connect_error()) is True

    # 抖动后成功：调用 1 + max_retries 次
    calls, client = fake_client([status_error(500), status_error(500), "ok"])
    model = OpenAICompatibleModel(ModelConfig(api_key="x", max_retries=2))
    model.client = client
    assert model.complete([{"role": "user", "content": "q"}]) == "SELECT 1"
    assert calls["n"] == 3, f"应为 3 次调用，实际 {calls['n']}"

    # 耗尽后抛原始异常，不吞也不换成模糊错误
    calls, client = fake_client([status_error(503)])
    model = OpenAICompatibleModel(ModelConfig(api_key="x", max_retries=2))
    model.client = client
    try:
        model.complete([{"role": "user", "content": "q"}])
    except openai.APIStatusError as error:
        assert error.status_code == 503, error
        assert calls["n"] == 3, f"应为 3 次调用，实际 {calls['n']}"
    else:
        raise AssertionError("重试耗尽后应抛原始异常")

    # 4xx 不重试：key 错、参数错，重试只是白花钱
    calls, client = fake_client([status_error(401)])
    model = OpenAICompatibleModel(ModelConfig(api_key="x", max_retries=2))
    model.client = client
    try:
        model.complete([{"role": "user", "content": "q"}])
    except openai.APIStatusError:
        assert calls["n"] == 1, f"401 不该重试，实际调用 {calls['n']} 次"
    else:
        raise AssertionError("401 应抛出")

    # 关掉重试时就是直接失败
    calls, client = fake_client([connect_error()])
    model = OpenAICompatibleModel(ModelConfig(api_key="x", max_retries=0))
    model.client = client
    try:
        model.complete([{"role": "user", "content": "q"}])
    except openai.APIConnectionError:
        assert calls["n"] == 1, f"max_retries=0 不该重试，实际 {calls['n']} 次"
    else:
        raise AssertionError("连接失败应抛出")


def test_concurrency_backpressure():
    """并发闸门：满了直接 429，让调用方自己退避，而不是排队等到超时。"""
    from fastapi import HTTPException

    from server.serve import DEFAULT_MAX_CONCURRENCY, ServiceRuntime

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shop.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE t(a)")
        connection.commit()
        connection.close()

        entry = {"name": "demo"}
        runtime = ServiceRuntime(entry, SQLiteDatabase(path),
                                 {"connected": True, "tables": 1}, max_concurrency=2)
        runtime.acquire()
        runtime.acquire()
        try:
            runtime.acquire()
        except HTTPException as error:
            assert error.status_code == 429, error.status_code
        else:
            raise AssertionError("并发超限未返回 429")

        # 释放后槽位必须能再抢到，否则会永久性死锁
        runtime.release()
        runtime.acquire()
        assert runtime.active == 2, runtime.active
        # 默认上限来自常量，命令行 --max-concurrency 可覆盖
        assert ServiceRuntime(entry, SQLiteDatabase(path), {}).max_concurrency \
            == DEFAULT_MAX_CONCURRENCY


def test_workbench_token_guard():
    """工作台能连任意数据库，token 存在时所有 /api/* 都要校验。"""
    import importlib
    import os

    from fastapi.testclient import TestClient

    os.environ["SQLSTAGE_WORKBENCH_TOKEN"] = "sk-guard-test"
    try:
        import server.main as main_module
        importlib.reload(main_module)
        client = TestClient(main_module.app)
        assert client.get("/api/health").json()["auth_required"] is True
        assert client.get("/api/guide/tables").status_code == 401
        assert client.get("/api/guide/tables",
                           headers={"Authorization": "Bearer wrong"}).status_code == 403
        assert client.get("/api/guide/tables",
                           headers={"Authorization": "Bearer sk-guard-test"}).status_code == 200
    finally:
        os.environ.pop("SQLSTAGE_WORKBENCH_TOKEN", None)
        importlib.reload(main_module)

    # 没设环境变量时不鉴权，本机开发不用每次配 token
    client = TestClient(main_module.app)
    assert client.get("/api/health").json()["auth_required"] is False
    assert client.get("/api/guide/tables").status_code == 200


def test_guide_endpoint_blocks_path_traversal():
    """guide 端点的 name 来自 URL，必须 resolve 后比对白名单。"""
    import server.connections as connections_module
    from fastapi import HTTPException

    original = connections_module.BUNDLE_DIR
    try:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "guides"
            (bundle / "products").mkdir(parents=True)
            (bundle / "products/guide.md").write_text("Test-only guide", encoding="utf-8")
            connections_module.BUNDLE_DIR = bundle
            assert connections_module.view_table_guide("products")["name"] == "products"
            for evil in ["..", "../..", "../../../../../../项目"]:
                try:
                    connections_module.view_table_guide(evil)
                except HTTPException as error:
                    assert error.status_code in (403, 404), (evil, error.status_code)
                else:
                    raise AssertionError(f"路径穿越未被拦：{evil}")
    finally:
        connections_module.BUNDLE_DIR = original


def test_connection_pool_reuses_and_bounds():
    """池的核心义务两条：复用连接（不重复握手）、池满就关掉多余的。"""
    from sqlstage.database import ConnectionPool

    created = []

    class FakeConnection:
        def __init__(self, index):
            self.index = index
            self.closed = False
            self.rolled_back = 0
            self.prepared = 0

        def rollback(self):
            self.rolled_back += 1

        def close(self):
            self.closed = True

    def build():
        connection = FakeConnection(len(created))
        created.append(connection)
        return connection

    def prepare(connection):
        connection.prepared += 1

    pool = ConnectionPool(build, size=2)
    # 连续借还同一个：应该只建一次连接
    for _ in range(5):
        pool.release(pool.acquire(prepare))
    assert len(created) == 1, f"5 次借还应只建 1 条连接，实际 {len(created)}"
    assert created[0].rolled_back == 5, "每次归还都必须 rollback 一次"
    assert created[0].prepared == 5, "每次借出都要设置会话参数"

    # 借出超过容量时每条都是新连接，归还后多余的被关掉
    held = [pool.acquire(prepare) for _ in range(4)]
    assert len({id(item) for item in held}) == 4, "同时在途的连接必须互不相同"
    for connection in held:
        pool.release(connection)
    assert pool._idle.qsize() == 2, f"池满 2，多余的应被关掉，实际剩 {pool._idle.qsize()}"
    assert sum(1 for item in created if item.closed) == len(created) - 2, "多余连接应已关闭"

    # 池关闭后归还的连接直接关掉，不会留在池里
    pool.close()
    late = FakeConnection(99)
    pool.release(late)
    assert late.closed is True


def test_connection_pool_discards_dead_connection():
    """服务端掐掉的空闲连接不能被复用：设置会话参数失败即就地重建。"""
    from sqlstage.database import ConnectionPool

    class FakeConnection:
        def __init__(self, alive):
            self.alive = alive
            self.closed = False

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    dead = FakeConnection(alive=False)
    fresh = FakeConnection(alive=True)
    # 新建连接只会产出 fresh；dead 是之前遗留、已经被服务端掐掉的
    handed = [fresh]

    def prepare(connection):
        if not connection.alive:
            raise RuntimeError("server has gone away")

    pool = ConnectionPool(lambda: handed.pop(0), size=3)
    pool.release(dead)
    connection = pool.acquire(prepare)
    assert connection is fresh, "死连接应被丢弃，换成新连接"
    assert dead.closed is True, "死连接必须关闭，不能留在池里"


def test_new_connection_failure_propagates():
    """新建连接就失败说明服务端真有问题，必须原样抛出而不是重试到池空。"""
    from sqlstage.database import ConnectionPool

    def refuse():
        raise ConnectionRefusedError("服务端不可达")

    def prepare(connection):
        pass

    pool = ConnectionPool(refuse, size=2)
    try:
        pool.acquire(prepare)
    except ConnectionRefusedError as error:
        assert "服务端不可达" in str(error), error
    else:
        raise AssertionError("新建连接失败应原样抛出")


def test_pool_size_zero_disables_pooling():
    """pool_size=0 退化成每次新建，SQLite 走的就是这条路径。"""
    from sqlstage.database import SQLiteDatabase

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shop.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE t(a)")
        connection.commit()
        connection.close()

        database = SQLiteDatabase(path)
        assert database.pool._size == 0, "SQLite 不该池化"
        database.fetch("SELECT COUNT(*) FROM t")
        assert database.pool._idle.qsize() == 0, "不池化时不该留下空闲连接"
        database.pool.close()


def test_workbench_reuses_database_per_connection_params():
    """工作台每个接口都 build_database，适配器有池，必须按参数复用而不是每次新建。

    否则每次请求都新建一批连接，请求结束后没人关，一路堆到服务端 wait_timeout。
    """
    from server.base import _DATABASES, build_database

    _DATABASES.clear()
    params = {"host": "127.0.0.1", "port": 3306, "user": "u", "password": "p",
              "mysql_db": "d", "read_only": True, "pool_size": 3}
    first = build_database("MySQL", params)
    second = build_database("MySQL", dict(params))
    assert first is second, "相同连接参数应复用同一个适配器（含连接池）"
    assert first.pool._size == 3, first.pool._size

    # 改只读开关必须拿到新实例，不能沿用旧的只读策略
    writable = dict(params, read_only=False)
    assert build_database("MySQL", writable) is not first
    assert build_database("MySQL", writable).read_only is False
    # 改库名同理
    assert build_database("MySQL", dict(params, mysql_db="other")) is not first

    first.pool.close()
    _DATABASES.clear()


if __name__ == "__main__":
    test_read_only_blocks_dangerous_functions()
    print("  test_read_only_blocks_dangerous_functions: OK")
    test_read_only_still_allows_normal_queries()
    print("  test_read_only_still_allows_normal_queries: OK")
    test_row_limit_and_statement_timeout()
    print("  test_row_limit_and_statement_timeout: OK")
    test_engine_applies_guards_to_database()
    print("  test_engine_applies_guards_to_database: OK")
    test_evaluation_respects_dialect()
    print("  test_evaluation_respects_dialect: OK")
    test_evaluation_ignores_table_aliases()
    print("  test_evaluation_ignores_table_aliases: OK")
    test_evaluation_keeps_meaningful_differences()
    print("  test_evaluation_keeps_meaningful_differences: OK")
    test_evaluation_normalizes_identifier_case_per_dialect()
    print("  test_evaluation_normalizes_identifier_case_per_dialect: OK")
    test_model_retry_policy()
    print("  test_model_retry_policy: OK")
    test_concurrency_backpressure()
    print("  test_concurrency_backpressure: OK")
    test_workbench_token_guard()
    print("  test_workbench_token_guard: OK")
    test_guide_endpoint_blocks_path_traversal()
    print("  test_guide_endpoint_blocks_path_traversal: OK")
    test_connection_pool_reuses_and_bounds()
    print("  test_connection_pool_reuses_and_bounds: OK")
    test_connection_pool_discards_dead_connection()
    print("  test_connection_pool_discards_dead_connection: OK")
    test_new_connection_failure_propagates()
    print("  test_new_connection_failure_propagates: OK")
    test_pool_size_zero_disables_pooling()
    print("  test_pool_size_zero_disables_pooling: OK")
    test_workbench_reuses_database_per_connection_params()
    print("  test_workbench_reuses_database_per_connection_params: OK")
    print("test_production_guards: OK")
