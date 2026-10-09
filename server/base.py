"""FastAPI 服务共享依赖：Store、连接与策略构造。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlstage import (
    DEFAULT_POOL_SIZE, MySQLDatabase, ModelConfig, OrmConfig, PostgreSQLDatabase,
    SQLiteDatabase, StrategyConfig, TrickConfig,
)
from sqlstage.store import Store

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "examples" / "guides"

store = Store()

# 工作台的每个接口都会 build_database 一次，而适配器现在持有连接池：
# 每次新建适配器就等于新建一批连接，请求结束后没人负责关，连接会一路堆到
# 服务端的 wait_timeout 才被回收。按连接参数缓存适配器，同一条连接在全进程
# 复用同一个池；params 变了（改了库、关了只读）key 就变，自然拿到新实例。
_DATABASES = {}


def build_database(connection_type, params):
    # 参数来自我们自己的连接表单，read_only 是布尔值；老记录没有这个字段时按只读处理
    read_only = params.get("read_only", True)
    # 同样按老记录兼容：pool_size 是后加的，缺省用适配器默认值
    pool_size = params.get("pool_size", DEFAULT_POOL_SIZE)
    key = (connection_type, read_only, pool_size, tuple(sorted(params.items())))
    if key in _DATABASES:
        return _DATABASES[key]
    database = _create_database(connection_type, params, read_only, pool_size)
    _DATABASES[key] = database
    return database


def _create_database(connection_type, params, read_only, pool_size):
    if connection_type == "MySQL":
        return MySQLDatabase(params["host"], params["port"], params["user"],
                             params["password"], params["mysql_db"],
                             read_only=read_only, pool_size=pool_size)
    if connection_type == "PostgreSQL":
        return PostgreSQLDatabase(params["host"], params["port"], params["user"],
                                  params["password"], params["mysql_db"],
                                  read_only=read_only, pool_size=pool_size)
    return SQLiteDatabase(params["database"], read_only=read_only)


def split_messages(messages):
    question = messages[-1]["content"]
    history = [{"role": item["role"], "content": item["content"]} for item in messages[:-1]]
    return question, history


def build_strategy(raw):
    if not raw:
        return StrategyConfig()
    model_raw = dict(raw.get("model") or {})
    if not model_raw.get("api_key"):
        model_raw.pop("api_key", None)
    return StrategyConfig(
        name=raw.get("name", "default"),
        model=ModelConfig(**model_raw),
        tricks=TrickConfig(**(raw.get("tricks") or {})),
        orm=OrmConfig(**(raw.get("orm") or {})),
    )
