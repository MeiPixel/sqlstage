"""数据库适配层：SQLite / MySQL / PostgreSQL 统一 Schema 读取、执行与探测接口。"""
import os
import queue
import sqlite3
import time

import pg8000.dbapi as pg
import pymysql
import sqlglot
from sqlglot import exp

from .schema import read_sqlite_schema, sqlite_uri

# 连接池默认保留的空闲连接数。常驻连接约等于这个数，并发请求各额外持有一个；
# 服务模式的并发背压（--max-concurrency）已经把总连接数卡在可接受范围内，
# 池的作用是不重复建连、不无限累积。0 表示每次都新建连接（回到无池行为）。
DEFAULT_POOL_SIZE = 5


class DatabaseError(Exception):
    """适配层统一异常，屏蔽底层驱动的类型差异。"""


class ReadOnlyViolation(DatabaseError):
    """尝试执行非只读语句（INSERT / UPDATE / DELETE / DDL 等），或调用危险函数。"""


READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# 只读不等于安全：这些函数以 SELECT 的形式出现，实际却读写文件、跨库执行或加载扩展。
# 连接层的只读事务拦不住它们（PG 的 pg_write_file 在只读事务里照样成功），
# 所以必须在 AST 层按函数名拦掉。名单按「做什么」分三类，不做穷举。
DANGEROUS_FUNCTIONS = {
    # 读写服务器本地文件
    "pg_read_file", "pg_read_binary_file", "pg_write_file", "pg_ls_dir",
    "lo_import", "lo_export",
    "readfile", "load_file", "dumpfile",
    # 跨库 / 跨服务器执行 SQL，可绕过当前连接的权限边界
    "dblink", "dblink_exec", "dblink_open", "postgres_fdw_disconnect",
    # 加载本地扩展，等于任意代码执行
    "load_extension",
    # 发起外部请求（SSRF），MySQL 与 SQLite 扩展里都有
    "sys_exec", "sys_eval",
}


def _function_name(node):
    """取函数调用的名字。

    ``exp.Anonymous`` 解析出的自定义函数，``this`` 就是函数名字符串本身
    （``pg_read_file(...)`` → ``this == "pg_read_file"``）；
    标准函数走 ``exp.Dot`` / ``exp.Identifier``，一并处理掉。
    """
    this = node.this
    if isinstance(this, str):
        return this
    if isinstance(this, exp.Identifier):
        return this.name
    if isinstance(this, exp.Dot):
        return _function_name(this)
    return None


def assert_read_only(sql, dialect="sqlite"):
    """校验 SQL 的每一条语句均为只读查询，否则抛 ReadOnlyViolation。

    三道检查：
    1. WITH ... SELECT 会下钻到 CTE 主体判断，因此 `WITH x AS (...) DELETE`
       这类写法同样会被拦截。
    2. 危险函数（读文件、写文件、跨库执行、加载扩展）一律拒绝——
       它们以 SELECT 形式出现，只判根节点拦不住。
    3. SELECT ... FOR UPDATE 会加行锁，属于写语义。

    无法解析的 SQL 一律拒绝。
    """
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as error:
        raise ReadOnlyViolation(f"SQL 无法解析，拒绝执行：{error}") from error
    if not trees or any(tree is None for tree in trees):
        raise ReadOnlyViolation("SQL 为空或无法解析，拒绝执行")
    for tree in trees:
        root = tree
        while isinstance(root, exp.With):
            root = root.this
        if not isinstance(root, READ_ONLY_ROOTS):
            raise ReadOnlyViolation(
                f"只读模式下只允许 SELECT 查询，收到 {type(root).__name__}：{sql[:200]}"
            )
        for call in tree.find_all(exp.Anonymous):
            name = _function_name(call)
            if name and name.lower() in DANGEROUS_FUNCTIONS:
                raise ReadOnlyViolation(
                    f"只读模式禁止调用函数 {name}()：它能读写文件、跨库执行或加载扩展。"
                    f"{sql[:200]}"
                )
        for lock in tree.find_all(exp.Lock):
            raise ReadOnlyViolation(
                f"只读模式禁止行锁 {sql[:200]}（FOR UPDATE 会修改数据）"
            )


def quote_identifier(name, dialect):
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def read_mysql_schema(connection, database):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, column_name, column_type, column_key "
            "FROM information_schema.columns WHERE table_schema = %s "
            "ORDER BY table_name, ordinal_position",
            (database,),
        )
        columns = cursor.fetchall()
        cursor.execute(
            "SELECT table_name, column_name, referenced_table_name, referenced_column_name "
            "FROM information_schema.key_column_usage "
            "WHERE table_schema = %s AND referenced_table_name IS NOT NULL",
            (database,),
        )
        foreign_keys = cursor.fetchall()
    schema = {}
    for table, column, column_type, column_key in columns:
        schema.setdefault(table, {"columns": [], "foreign_keys": []})
        schema[table]["columns"].append(
            {"name": column, "type": column_type, "primary_key": column_key == "PRI"}
        )
    for table, column, target_table, target_column in foreign_keys:
        schema[table]["foreign_keys"].append(
            {"column": column, "target_table": target_table, "target_column": target_column}
        )
    return schema


def read_postgres_schema(connection):
    cursor = connection.cursor()
    cursor.execute(
        "SELECT table_name, column_name, data_type, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position"
    )
    columns = cursor.fetchall()
    cursor.execute(
        "SELECT kcu.table_name, kcu.column_name FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'"
    )
    primary_keys = {(table, column) for table, column in cursor.fetchall()}
    cursor.execute(
        "SELECT kcu.table_name, kcu.column_name, ccu.table_name, ccu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'"
    )
    foreign_keys = cursor.fetchall()
    schema = {}
    for table, column, data_type, length in columns:
        schema.setdefault(table, {"columns": [], "foreign_keys": []})
        schema[table]["columns"].append({
            "name": column,
            "type": f"{data_type}({length})" if length else data_type,
            "primary_key": (table, column) in primary_keys,
        })
    for table, column, target_table, target_column in foreign_keys:
        schema[table]["foreign_keys"].append(
            {"column": column, "target_table": target_table, "target_column": target_column}
        )
    return schema


class RowLimitExceeded(DatabaseError):
    """结果集超过 max_rows。不静默截断——截断出来的答案会被当成完整的用。"""


class ConnectionPool:
    """线程安全的空闲连接池。

    只解决两件事：避免每次操作重复握手建连，避免空闲连接无限累积。

    复用的连接交给 ``prepare`` 准备一次——它同时起两个作用：设置会话参数
    （只读事务 + 语句超时），以及确认连接还通着。服务端把连接掐掉时
    prepare 会直接报错，池据此丢弃重建，不会把「同样的请求时好时坏」
    这种难排查的现象留给用户。prepare 只在复用时调用，新建连接由调用方自己准备。

    ``pool_size`` 限的是**空闲**连接数，不是在途总数：并发请求各持有一个连接，
    忙完归还。总连接数由服务模式的并发背压（``--max-concurrency``）约束。
    取 0 时退化成每次新建连接。
    """

    def __init__(self, connect_fn, size):
        self._connect_fn = connect_fn
        self._size = size
        self._idle = queue.LifoQueue(maxsize=max(size, 1))
        self._closed = False

    def acquire(self, prepare):
        """取一个已准备好的连接。

        复用的连接先 prepare；不通就丢掉换一个。新建连接直接返回，
        由调用方负责准备——它失败说明服务端真有问题，应该原样抛出去。
        """
        if self._size <= 0:
            connection = self._connect_fn()
            prepare(connection)
            return connection
        while True:
            try:
                reused = self._idle.get_nowait()
            except queue.Empty:
                connection = self._connect_fn()
                prepare(connection)
                return connection
            try:
                prepare(reused)
            except Exception:
                self.discard(reused)
                continue
            return reused

    def release(self, connection):
        """归还连接。池满则直接关掉，多余的空闲连接没有保留价值。"""
        # 只读事务不该有未提交内容，但语句失败时连接可能停在失败事务里，
        # 不清掉的话下一个使用者会直接吃到报错（PG 尤其明显）。
        try:
            connection.rollback()
        except Exception:
            self.discard(connection)
            return
        if self._size <= 0 or self._closed:
            self.discard(connection)
            return
        try:
            self._idle.put_nowait(connection)
        except queue.Full:
            self.discard(connection)

    def discard(self, connection):
        """丢弃一个不能再用的连接。关不关得掉都不影响后续，所以吞掉异常。"""
        try:
            connection.close()
        except Exception:
            pass

    def close(self):
        """关掉全部空闲连接。在途连接归还时会被直接关掉。"""
        self._closed = True
        while True:
            try:
                self.discard(self._idle.get_nowait())
            except queue.Empty:
                return


class _AdapterMixin:
    """SQLite/MySQL/PostgreSQL 共享的探测接口，依赖子类的 fetch 与 dialect。"""

    def __init__(self, read_only=True, statement_timeout_ms=30000, max_rows=5000,
                 pool_size=DEFAULT_POOL_SIZE):
        self.read_only = read_only
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows
        self.pool = ConnectionPool(self.open, pool_size)

    def open(self):
        """新建一个裸连接，不含任何会话设置。由子类实现。"""
        raise NotImplementedError

    def session_setup(self, connection):
        """建连接后统一设置会话参数：只读事务 + 语句超时。

        三个数据库机制不同（SQLite 走 connection.set_progress_handler、
        MySQL 用 max_execution_time、PG 用 statement_timeout），
        因此由子类各自实现。收 connection 而不是 cursor，
        因为 SQLite 的超时只能挂在 connection 上。
        """
        raise NotImplementedError

    def guard(self, sql):
        """只读模式下在 SQL 层拦截写语句，作为连接层只读的纵深防御。"""
        if self.read_only:
            assert_read_only(sql, self.dialect)

    def run(self, sql):
        """借连接执行 SQL 并归还，供 fetch 与 schema 读取共用。

        session_setup 同时是准备与探活：复用连接走它准备，挂了就地重建。
        """
        connection = self.pool.acquire(self.session_setup)
        try:
            return self.execute_on(connection, sql)
        finally:
            self.pool.release(connection)

    def execute_on(self, connection, sql):
        """在已建好会话的连接上执行一条 SQL，返回 (列名, 行)。子类实现。"""
        raise NotImplementedError

    def try_execute(self, sql):
        try:
            self.fetch(sql)
        except DatabaseError as error:
            return False, str(error)
        return True, ""

    def limit_rows(self, rows):
        """结果集超过 max_rows 就报错。

        不截断：半截的结果看起来是完整答案，下游无从发现数据被丢了。
        真要取样交给 SQL 里的 LIMIT，这里只做兜底告警。
        """
        if len(rows) > self.max_rows:
            raise RowLimitExceeded(
                f"结果集 {len(rows)} 行，超过上限 {self.max_rows}。"
                f"请在 SQL 里加聚合或 LIMIT，不要拉明细全表。"
            )
        return rows

    def probe(self):
        """启动探活：真连一次库读 schema，返回表数量；连不上抛 DatabaseError。

        探活通过不代表之后永远可用（库随时可能重启、连接随时可能被掐），
        但能在服务启动时把「账号密码不对、库不存在、主机不可达」直接暴露出来。
        """
        return len(self.schema)

    def sample_rows(self, table, columns, limit):
        names = ", ".join(quote_identifier(name, self.dialect) for name in columns)
        _, rows = self.fetch(
            f"SELECT {names} FROM {quote_identifier(table, self.dialect)} LIMIT {int(limit)}"
        )
        return [dict(zip(columns, row)) for row in rows]

    def distinct_values(self, table, column, limit):
        target = quote_identifier(column, self.dialect)
        _, rows = self.fetch(
            f"SELECT DISTINCT {target} FROM {quote_identifier(table, self.dialect)} "
            f"WHERE {target} IS NOT NULL LIMIT {int(limit)}"
        )
        return [row[0] for row in rows]

    def top_values(self, table, column, limit):
        target = quote_identifier(column, self.dialect)
        _, rows = self.fetch(
            f"SELECT {target}, COUNT(*) FROM {quote_identifier(table, self.dialect)} "
            f"WHERE {target} IS NOT NULL GROUP BY {target} ORDER BY COUNT(*) DESC LIMIT {int(limit)}"
        )
        return [(row[0], row[1]) for row in rows]


class SQLiteDatabase(_AdapterMixin):
    """SQLite 连的是本地文件，建连接几乎零成本，因此不池化（pool_size 固定 0）。"""

    dialect = "sqlite"

    def __init__(self, path, read_only=True, statement_timeout_ms=30000, max_rows=5000):
        super().__init__(read_only, statement_timeout_ms, max_rows, pool_size=0)
        self.path = str(path)

    @property
    def schema(self):
        try:
            return read_sqlite_schema(self.path, self.read_only)
        except sqlite3.Error as error:
            raise DatabaseError(
                f"读取 SQLite Schema 失败（只读模式下文件必须已存在）：{error}"
            ) from error

    def session_setup(self, connection):
        """SQLite 没有语句超时参数，只能靠 progress_handler 定期让出。

        handler 返回非零即中断语句，所以按「是否已超时」决定返回值：
        没到时间返回 0 继续跑，到点返回 1 被打断。n=1000 是约 1000 个虚拟指令
        调一次，兼顾反应速度与开销。
        """
        deadline = time.perf_counter() + self.statement_timeout_ms / 1000

        def expired():
            return 1 if time.perf_counter() > deadline else 0

        connection.set_progress_handler(expired, 1000)

    def open(self):
        return sqlite3.connect(sqlite_uri(self.path, self.read_only), uri=self.read_only)

    def execute_on(self, connection, sql):
        try:
            cursor = connection.execute(sql)
            columns = [item[0] for item in cursor.description] if cursor.description else []
            return columns, self.limit_rows(cursor.fetchall())
        except sqlite3.Error as error:
            raise DatabaseError(str(error)) from error

    def fetch(self, sql):
        self.guard(sql)
        return self.run(sql)


class MySQLDatabase(_AdapterMixin):
    dialect = "mysql"

    def __init__(self, host, port, user, password, database, read_only=True,
                 statement_timeout_ms=30000, max_rows=5000, pool_size=DEFAULT_POOL_SIZE):
        super().__init__(read_only, statement_timeout_ms, max_rows, pool_size)
        self.database = database
        self.connection_args = dict(
            host=host, port=int(port), user=user, password=password,
            database=database, charset="utf8mb4",
        )

    @property
    def schema(self):
        connection = self.pool.acquire(self.session_setup)
        try:
            return read_mysql_schema(connection, self.database)
        except pymysql.Error as error:
            raise DatabaseError(f"读取 MySQL Schema 失败：{error}") from error
        finally:
            self.pool.release(connection)

    def session_setup(self, connection):
        """max_execution_time 只对 SELECT 生效，单位毫秒，配合只读事务一起设。

        变量名是 max_execution_time（没有 MAX_EXECUTIONTIME 这种写法，设了会报
        Unknown system variable）。
        """
        with connection.cursor() as cursor:
            if self.read_only:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION max_execution_time={int(self.statement_timeout_ms)}")

    def open(self):
        return pymysql.connect(**self.connection_args)

    def execute_on(self, connection, sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [item[0] for item in cursor.description] if cursor.description else []
            return columns, self.limit_rows(cursor.fetchall())

    def fetch(self, sql):
        self.guard(sql)
        try:
            return self.run(sql)
        except pymysql.Error as error:
            raise DatabaseError(str(error)) from error


class PostgreSQLDatabase(_AdapterMixin):
    dialect = "postgres"

    def __init__(self, host, port, user, password, database, read_only=True,
                 statement_timeout_ms=30000, max_rows=5000, pool_size=DEFAULT_POOL_SIZE):
        super().__init__(read_only, statement_timeout_ms, max_rows, pool_size)
        self.database = database
        self.connection_args = dict(
            host=host, port=int(port), user=user, password=password, database=database
        )

    @property
    def schema(self):
        connection = self.pool.acquire(self.session_setup)
        try:
            return read_postgres_schema(connection)
        except pg.Error as error:
            raise DatabaseError(f"读取 PostgreSQL Schema 失败：{error}") from error
        finally:
            self.pool.release(connection)

    def session_setup(self, connection):
        """statement_timeout 由数据库自己计时，超时直接报 57014 query_canceled。

        pg8000 的 connection 虽然支持 with，但 __enter__ 返回的是 cursor 而不是
        自己（与 DB-API 规范相反），所以这里显式取 cursor，不用 with。
        """
        cursor = connection.cursor()
        if self.read_only:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cursor.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")

    def open(self):
        return pg.connect(**self.connection_args)

    def execute_on(self, connection, sql):
        cursor = connection.cursor()
        cursor.execute(sql)
        # pg8000 在没有结果集时连 fetchall() 都会报 "unexecuted cursor"，
        # 所以必须先看 description，不能像 MySQL/SQLite 那样无条件 fetchall
        if not cursor.description:
            return [], []
        return [item[0] for item in cursor.description], self.limit_rows(cursor.fetchall())

    def fetch(self, sql):
        self.guard(sql)
        try:
            return self.run(sql)
        except pg.Error as error:
            raise DatabaseError(str(error)) from error


def connect(database, read_only=True):
    """路径（str/PathLike）按 SQLite 处理，其余视为已构造的适配器对象。

    read_only 默认为 True：连接层与 SQL 层双重只读，模型生成的写语句不会被执行。
    """
    if isinstance(database, (str, os.PathLike)):
        return SQLiteDatabase(database, read_only=read_only)
    return database