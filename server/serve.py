"""服务模式：拿开发模式导出的一份配置，在服务器起一个只做 Text2SQL 的最小服务。

    python -m server.serve --config service.yaml
    python -m server.serve --config service.yaml --token sk-xxx --host 0.0.0.0 --port 8021

一份配置可以有多个服务，按名字路由：/v1/{服务名}/predict；只有一个服务时 /v1/predict 也能用。
只暴露 /health 与 /v1/*，工作台那套管理接口（改配置、读文件、跑寻优）一个都不挂。

启动时会对每个服务真连一次数据库：连上就报告读到几张表，连不上就打印原因和连接参数
（密码打码）后照常启动——适配器每次请求独立建连接，数据库修好后不用重启服务。
要把它变成硬门槛就加 --strict：有服务连不上直接退出，返回码 1。
"""
import argparse
import secrets
import sys
import tempfile
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from threading import BoundedSemaphore

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from sqlstage import Text2SQLEngine
from sqlstage.database import DatabaseError
from sqlstage.logs import (
    database_recovered, log_database_unavailable, log_over_capacity,
    request_failed, request_finished, request_started,
)

from server.base import build_database, build_strategy
from server.service_config import (
    expand_env_value, generate_token, load_service_config, token_sha256,
)

bearer_scheme = HTTPBearer(auto_error=True)
# 这几个 Trick 要在真实库上跑 SQL，数据库连不上时 /predict 一定失败
EXECUTION_TRICKS = ("execution_check", "execution_feedback", "empty_result_refine")
DEFAULT_MAX_CONCURRENCY = 16


class PredictPayload(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list)


class BearerAuth:
    """校验 Bearer token，比对用常量时间的 compare_digest。"""

    def __init__(self, digests):
        self.digests = digests

    def __call__(self, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
        digest = token_sha256(credentials.credentials)
        if not any(secrets.compare_digest(digest, item) for item in self.digests):
            raise HTTPException(status_code=403, detail="token 无效")
        return credentials.credentials


def collect_token_digests(entry, extra_digests=()):
    """配置里每个 token 的 sha256，加上命令行 --token 传进来的（同样已是摘要）。

    有 sha256 就以 sha256 为准；没有则按明文处理，明文可以是真 token 或 ${ENV:变量名}。
    """
    digests = []
    for item in entry["auth"]["tokens"]:
        if "sha256" in item:
            digests.append(item["sha256"])
        else:
            digests.append(token_sha256(expand_env_value(item["token"])))
    digests.extend(extra_digests)
    return digests


def materialize_files(entry, workdir):
    """把 ``files`` 段里内联的文件写回磁盘，再把 tricks 的路径指向它们。

    现在只有 few-shot 走这条路——guide 在导出时就已经是正文（写进 ``guide_text``），
    配置里不带路径。早先版本导出的配置里可能还有 ``files.guide_path``，这里照样能处理。
    """
    for field, item in entry.get("files", {}).items():
        target = Path(workdir) / Path(item["path"]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["text"], encoding="utf-8")
        entry["tricks"][field] = str(target)


def resolve_database(entry, base_dir):
    """SQLite 的相对路径按配置文件所在目录解析，配置文件和库一起搬走就能用。"""
    connection = entry["connection"]
    if connection["type"] != "SQLite":
        return
    raw = connection["params"].get("database")
    if raw and not Path(raw).is_absolute():
        connection["params"]["database"] = str(Path(base_dir) / raw)


def describe_connection(connection):
    """给启动报告用的连接摘要，密码一律打码。"""
    params = dict(connection["params"])
    if "password" in params:
        params["password"] = "******"
    detail = ", ".join(f"{key}={value}" for key, value in params.items())
    return f"{connection['type']}（{detail}）"


def probe_database(entry, database):
    """启动探活：真连一次库。连不上只记录原因，服务照常起来。"""
    try:
        return {"service": entry["name"], "connected": True, "tables": database.probe()}
    except DatabaseError as error:
        return {"service": entry["name"], "connected": False, "error": str(error)}


def print_database_report(statuses, entries, will_continue=True):
    """把探活结果讲清楚：连上几个表、连不上是什么原因、会影响到什么、接下来会怎样。"""
    by_name = {entry["name"]: entry for entry in entries}
    for status in statuses:
        entry = by_name[status["service"]]
        if status["connected"]:
            print(f"[OK] {status['service']}：数据库已连接，读到 {status['tables']} 张表", flush=True)
            continue
        print(f"[警告] {status['service']}：连不上数据库 —— {status['error']}", flush=True)
        print(f"       连接参数：{describe_connection(entry['connection'])}", flush=True)
        if any(entry["tricks"][name] for name in EXECUTION_TRICKS):
            print("       该服务开启了执行校验/修复，数据库恢复前 /predict 会直接失败", flush=True)
        if will_continue:
            print("       服务照常启动：每次请求独立建连接，数据库恢复后不用重启", flush=True)
        else:
            print("       --strict 已开启，服务不会启动", flush=True)


def database_unavailable(error):
    return HTTPException(status_code=503, detail=f"数据库不可用：{error}")


class ServiceRuntime:
    """一个服务的运行态：库连接、引擎、以及启动时探活的结果。

    引擎延迟到第一次请求才建——``Text2SQLEngine`` 构造时会读一次 schema，
    数据库临时不可用时这样才起得来，等库恢复后首个请求自动补建，不用重启服务。

    ``slots`` 是并发闸门：一次 /predict 会连打多次模型、若干次数据库，
    不限流的话并发一上来就把上游和数据库同时打爆。满了直接回 429，
    让调用方自己退避，而不是排在这里等到超时。
    """

    def __init__(self, entry, database, status, max_concurrency=DEFAULT_MAX_CONCURRENCY):
        self.entry = entry
        self.database = database
        self.status = status
        self.engine = None
        self.slots = BoundedSemaphore(max_concurrency)
        self.max_concurrency = max_concurrency
        self.active = 0

    def acquire(self):
        if not self.slots.acquire(blocking=False):
            log_over_capacity(self.entry["name"], self.active, self.max_concurrency)
            raise HTTPException(status_code=429, detail="服务并发已满，请稍后重试")
        self.active += 1

    def release(self):
        self.active -= 1
        self.slots.release()

    def require_engine(self):
        if self.engine is None:
            try:
                self.engine = Text2SQLEngine(self.database, build_strategy({
                    "name": self.entry["name"], "model": self.entry["model"],
                    "tricks": self.entry["tricks"], "orm": self.entry["orm"],
                }))
            except DatabaseError as error:
                log_database_unavailable(self.entry["name"], error)
                raise database_unavailable(error) from error
            self.status["connected"] = True
            self.status.pop("error", None)
            database_recovered(self.entry["name"])
        return self.engine

    def mark_down(self, error):
        self.status["connected"] = False
        self.status["error"] = str(error)
        log_database_unavailable(self.entry["name"], error)


def register_service_routes(app, runtime, auth_required, digests, prefix=""):
    entry = runtime.entry
    guard = BearerAuth(digests) if auth_required else (lambda: None)

    @app.get(prefix + "/info")
    def info(_token=Depends(guard)):
        tricks = entry["tricks"]
        return {
            "service": entry["name"],
            "dialect": entry["connection"]["type"],
            "source": entry.get("source", {}),
            "orm_enabled": bool(entry["orm"]["enabled"]),
            "database_connected": runtime.status["connected"],
            "execution_check": bool(tricks["execution_check"]),
            "table_recall": tricks["table_recall"],
            "column_recall": tricks["column_recall"],
            "guide_enabled": bool(tricks["guide_enabled"]),
            "fewshot_enabled": bool(tricks["fewshot_enabled"]),
        }

    @app.get(prefix + "/schema")
    def schema(_token=Depends(guard)):
        try:
            return {"tables": runtime.database.schema}
        except DatabaseError as error:
            runtime.mark_down(error)
            raise database_unavailable(error) from error

    @app.post(prefix + "/predict")
    def predict(payload: PredictPayload, _token=Depends(guard)):
        engine = runtime.require_engine()
        context = request_started(entry["name"], payload.question)
        runtime.acquire()
        try:
            result = engine.generate(payload.question, payload.history)
        except DatabaseError as error:
            runtime.mark_down(error)
            request_failed(context, error)
            raise database_unavailable(error) from error
        except Exception as error:
            # 模型侧的失败（解析不了、上下文超限）不该记成数据库故障，
            # 但要留下请求级的日志，否则这条请求在链路上就没痕迹了。
            request_failed(context, error)
            raise
        finally:
            runtime.release()
        request_finished(context, result)
        return result


def create_app(config, auth_required=True, extra_digests=(), base_dir=".",
               max_concurrency=DEFAULT_MAX_CONCURRENCY):
    workdir = Path(tempfile.mkdtemp(prefix="t2sql-serve-"))
    services = []
    for raw in config["services"]:
        entry = deepcopy(raw)
        materialize_files(entry, workdir)
        resolve_database(entry, base_dir)
        connection = entry["connection"]
        database = build_database(connection["type"], dict(connection["params"]))
        status = probe_database(entry, database)
        services.append({
            "runtime": ServiceRuntime(entry, database, status, max_concurrency),
            "digests": collect_token_digests(entry, extra_digests),
        })

    # 启动时每个服务都真连一次库，连不上也照样起，只把原因留给启动报告和 /health
    statuses = [item["runtime"].status for item in services]
    names = [item["runtime"].entry["name"] for item in services]
    runtimes = {item["runtime"].entry["name"]: item["runtime"] for item in services}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        # 池里的空闲连接是真实持有的数据库会话，进程退出前必须归还，
        # 否则服务端要等到 wait_timeout 才回收。
        for runtime in runtimes.values():
            runtime.database.pool.close()

    app = FastAPI(title="SqlStage Service", lifespan=lifespan)
    app.state.database_status = statuses
    app.state.service_entries = [item["runtime"].entry for item in services]

    @app.get("/health")
    def health():
        return {"status": "ok", "services": names, "auth_required": auth_required,
                "databases": statuses,
                "load": {name: {"active": runtime.active, "limit": runtime.max_concurrency}
                         for name, runtime in runtimes.items()}}

    all_digests = [digest for item in services for digest in item["digests"]]
    any_guard = BearerAuth(all_digests) if auth_required else (lambda: None)

    @app.get("/v1/services")
    def list_services(_token=Depends(any_guard)):
        return {"services": [
            {"name": item["runtime"].entry["name"],
             "dialect": item["runtime"].entry["connection"]["type"],
             "source": item["runtime"].entry.get("source", {})}
            for item in services
        ]}

    for item in services:
        sub_app = FastAPI(title=f"SqlStage · {item['runtime'].entry['name']}")
        register_service_routes(sub_app, item["runtime"], auth_required, item["digests"])
        app.mount(f"/v1/{item['runtime'].entry['name']}", sub_app)

    # 只有一个服务时保留不带名字的写法，方便单服务部署
    if len(services) == 1:
        register_service_routes(app, services[0]["runtime"], auth_required,
                                services[0]["digests"], "/v1")

    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="SqlStage 服务模式")
    parser.add_argument("--config", required=True, help="开发模式导出的 service.yaml / service.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--token", action="append", default=[],
                        help="访问 token，可重复传入；不传且配置里也没有时自动生成")
    parser.add_argument("--no-auth", action="store_true", help="关闭鉴权，仅限可信内网")
    parser.add_argument("--strict", action="store_true",
                        help="有服务连不上数据库就退出（默认只警告并继续启动）")
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY,
                        help=f"单服务并发上限，超出返回 429（默认 {DEFAULT_MAX_CONCURRENCY}）")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = load_service_config(args.config)
    base_dir = str(Path(args.config).resolve().parent)
    extra_digests = [token_sha256(token) for token in args.token]

    for warning in config["warnings"]:
        print(f"提示：{warning}")

    # 服务模式默认带鉴权：有 token 就用，没有就现场生成一个，只有显式 --no-auth 才开放
    auth_required = not args.no_auth
    configured = [digest for entry in config["services"]
                  for digest in collect_token_digests(entry)]

    if auth_required and not configured and not extra_digests:
        token = generate_token()
        extra_digests = [token_sha256(token)]
        print("=" * 60)
        print(f"未指定 token，已自动生成：{token}")
        print("请保存好，它不会再次显示。下次启动用 --token 指定同一个值。")
        print("=" * 60, flush=True)
    if not auth_required:
        print("警告：鉴权已关闭，任何能访问该端口的人都能查你的数据库", flush=True)

    app = create_app(config, auth_required, extra_digests, base_dir, args.max_concurrency)
    # 启动即探活：账号密码不对、库不存在、主机不可达，在这里就能看见
    failed = [item for item in app.state.database_status if not item["connected"]]
    print_database_report(app.state.database_status, app.state.service_entries,
                          will_continue=not (failed and args.strict))
    if failed and args.strict:
        print(f"启动中止：{len(failed)}/{len(app.state.database_status)} 个服务连不上数据库"
              "（--strict 已开启）", flush=True)
        return 1

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
