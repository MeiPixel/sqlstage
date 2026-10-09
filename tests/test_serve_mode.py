"""验证服务模式：配置导出（多服务 / 文件内联 / 路径相对化）→ 加载 → token 鉴权 → 预测。

用原生 assert 直接跑：python python_sandbox/test_serve_mode.py
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlstage.engine as engine_module
from fastapi.testclient import TestClient

from server.base import build_database
from server.service_config import (
    YAML_HEADER, build_service_config, dump_service_config, expand_config, generate_token,
    load_service_config, relative_to_project, token_sha256,
)
from server.serve import (
    collect_token_digests, create_app, describe_connection, materialize_files,
    print_database_report, resolve_database,
)

GOLD_SQL = "SELECT name FROM city ORDER BY population DESC LIMIT 1"
TOKEN = "test-service-token-not-a-secret"


class FakeModel:
    def __init__(self, config):
        self.config = config

    def begin_timing(self, started):
        pass

    def timing(self):
        return {"before_model_s": 0.0, "model_s": 0.0, "model_calls": 1}

    def complete(self, messages, temperature=None):
        return GOLD_SQL


@contextlib.contextmanager
def fake_model():
    """引擎是延迟到第一次请求才建的，补丁必须一直打到请求结束。"""
    original = engine_module.OpenAICompatibleModel
    engine_module.OpenAICompatibleModel = FakeModel
    try:
        yield
    finally:
        engine_module.OpenAICompatibleModel = original


def make_db():
    path = Path(tempfile.mkdtemp()) / "city.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE city(id INTEGER PRIMARY KEY, name TEXT, population INTEGER);"
        "INSERT INTO city VALUES (1, '上海', 2400), (2, '苏州', 1200);")
    connection.commit()
    connection.close()
    return str(path)


def strategy_config(**trick_overrides):
    from sqlstage import TrickConfig
    tricks = asdict(TrickConfig())
    tricks["llm_validate_names"] = False
    tricks["candidate_count"] = 1
    tricks.update(trick_overrides)
    return {
        "model": {"provider": "deepseek", "base_url": "https://api.deepseek.com",
                  "api_key": "sk-real-secret", "model": "deepseek-chat"},
        "tricks": tricks,
        "orm": {"enabled": False},
    }


def strategy_row(name="默认策略", **trick_overrides):
    return {"id": 1, "name": name, "config": json.dumps(strategy_config(**trick_overrides),
                                                        ensure_ascii=False)}


def connection_row(db_path=None, kind="SQLite", **params):
    payload = {"database": db_path, "read_only": True} if kind == "SQLite" else {
        "host": "127.0.0.1", "port": 3306, "user": "root", "password": "db-secret",
        "mysql_db": "shop", "read_only": True}
    payload.update(params)
    return {"id": 2, "name": "本地库", "type": kind, "params": json.dumps(payload, ensure_ascii=False)}


def test_export_replaces_secrets():
    """导出默认把 api_key 与数据库密码换成环境变量占位符，不落明文。"""
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(kind="MySQL"),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    entry = config["services"][0]
    assert config["version"] == 2, config["version"]
    assert entry["model"]["api_key"] == "${ENV:DEEPSEEK_API_KEY}", entry["model"]["api_key"]
    assert entry["connection"]["params"]["password"] == "${ENV:SQLSTAGE_DB_PASSWORD}"
    assert entry["auth"]["tokens"][0]["token"] == "${ENV:SQLSTAGE_SERVICE_TOKEN}"
    assert entry["auth"]["tokens"][0]["sha256"] == token_sha256(TOKEN)
    assert entry["source"]["strategy"] == "默认策略"

    inline = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(kind="MySQL"),
        "tokens": [{"name": "default", "token": TOKEN}],
    }], inline_secrets=True)
    entry = inline["services"][0]
    assert entry["model"]["api_key"] == "sk-real-secret"
    assert entry["connection"]["params"]["password"] == "db-secret"
    assert entry["auth"]["tokens"][0]["token"] == TOKEN


def test_multiple_services_in_one_config():
    """一份配置装多个服务，各自带自己的策略、连接与 token。"""
    config = build_service_config([
        {"name": "城市问答", "strategy": strategy_row(), "connection": connection_row(make_db()),
         "tokens": [{"name": "a", "token": TOKEN}]},
        {"name": "订单问答", "strategy": strategy_row("订单策略"), "connection": connection_row(make_db()),
         "tokens": [{"name": "b", "token": "sk-second"}]},
    ])
    assert [entry["name"] for entry in config["services"]] == ["城市问答", "订单问答"]
    assert config["services"][1]["auth"]["tokens"][0]["sha256"] == token_sha256("sk-second")
    assert config["services"][1]["source"]["strategy"] == "订单策略"


def test_guide_is_exported_as_text_only():
    """guide 只导出正文：文件内容进 guide_text，配置里不留路径、也不进 files 段。"""
    directory = Path(tempfile.mkdtemp())
    guide = directory / "guide.md"
    guide.write_text("## 1 数据库用途\n用于城市人口查询", encoding="utf-8")

    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(guide_enabled=True, guide_path=str(guide)),
        "connection": connection_row(make_db()),
    }])
    entry = config["services"][0]
    assert "数据库用途" in entry["tricks"]["guide_text"], entry["tricks"]["guide_text"]
    assert "guide_path" not in entry["tricks"], entry["tricks"]
    assert entry["files"] == {}, entry["files"]
    assert str(guide) not in dump_service_config(config, "yaml"), "导出的配置里不该出现本机路径"


def test_guide_text_wins_over_path():
    """正文与路径都填时按引擎取值规则用正文，路径不导出，只给一条提示。"""
    directory = Path(tempfile.mkdtemp())
    guide = directory / "guide.md"
    guide.write_text("文件里的正文", encoding="utf-8")

    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(guide_enabled=True, guide_path=str(guide),
                                                 guide_text="策略里的正文"),
        "connection": connection_row(make_db()),
    }])
    entry = config["services"][0]
    assert entry["tricks"]["guide_text"] == "策略里的正文", entry["tricks"]["guide_text"]
    assert "guide_path" not in entry["tricks"]
    assert any("guide" in warning for warning in config["warnings"]), config["warnings"]


def test_fewshot_file_is_inlined():
    """few-shot 是结构化列表，内容仍然内联进 files 段，启动时物化。"""
    directory = Path(tempfile.mkdtemp())
    fewshot = directory / "fewshot.json"
    fewshot.write_text('[{"query": "人口最多的城市", "sql": "%s"}]' % GOLD_SQL, encoding="utf-8")

    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(fewshot_enabled=True, fewshot_path=str(fewshot)),
        "connection": connection_row(make_db()),
    }])
    files = config["services"][0]["files"]
    assert GOLD_SQL in files["fewshot_path"]["text"], files
    assert files["fewshot_path"]["path"] == str(fewshot)


def test_missing_external_file_raises():
    """引用的外部文件读不到就直接报错，不能导出一份残缺的配置。"""
    for field, path in (("guide_path", "不存在/guide.md"), ("fewshot_path", "不存在/shot.json")):
        try:
            build_service_config([{
                "name": "svc",
                "strategy": strategy_row(**{field: path}),
                "connection": connection_row(make_db()),
            }])
        except FileNotFoundError as error:
            assert Path(path).name in str(error), str(error)
        else:
            raise AssertionError(f"{field} 指向的文件缺失必须报错")


def test_sqlite_path_is_made_portable():
    """项目内的 SQLite 库写成相对路径；项目外的照旧但给 warning。"""
    root = Path(__file__).resolve().parents[1]
    inside = root / "examples" / "databases" / "world_1.sqlite"
    portable, ok = relative_to_project(str(inside))
    assert ok and Path(portable).is_relative_to("examples"), portable
    assert "\\" not in portable, f"相对路径要用正斜杠，否则搬到 Linux 打不开：{portable}"

    outside = Path(tempfile.mkdtemp()) / "elsewhere.db"
    sqlite3.connect(outside).close()
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(str(outside)),
    }])
    assert config["warnings"], "项目外的绝对路径应该给出提示"
    assert "SQLite" in config["warnings"][0]


def test_deploy_block_sits_on_top():
    """部署时常改的（库账号密码、模型 Key、token）要排在文件最上面，且字段全量导出。"""
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(kind="MySQL"),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    assert list(config) == ["version", "deploy", "services", "warnings"], list(config)
    deploy = config["deploy"]["svc"]
    assert deploy["database"]["password"] == "${ENV:SQLSTAGE_DB_PASSWORD}"
    assert deploy["database"]["mysql_db"] == "shop" and deploy["database"]["host"] == "127.0.0.1"
    assert deploy["model"]["api_key"] == "${ENV:DEEPSEEK_API_KEY}"
    assert deploy["tokens"][0]["sha256"] == token_sha256(TOKEN)

    text = dump_service_config(config, "yaml")
    assert text.index("deploy:") < text.index("services:"), "deploy 必须在 services 之前"
    assert "deploy 段" in YAML_HEADER, "yaml 顶部注释要讲清 deploy 段怎么改"
    assert text.startswith("# SqlStage 服务配置"), text[:40]


def test_deploy_overrides_win_on_load():
    """在 deploy 段改的库账号密码/模型 Key/token，加载后要盖住 services 段里的开发值。"""
    directory = Path(tempfile.mkdtemp())
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(kind="MySQL"),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    config["deploy"]["svc"]["database"].update(
        {"host": "10.0.0.5", "user": "readonly", "password": "pwd-on-server", "mysql_db": "prod"})
    config["deploy"]["svc"]["model"]["api_key"] = "sk-on-server"
    config["deploy"]["svc"]["tokens"] = [{"name": "prod", "token": "sk-deploy-token"}]
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")

    entry = load_service_config(str(path))["services"][0]
    assert entry["connection"]["params"]["host"] == "10.0.0.5"
    assert entry["connection"]["params"]["user"] == "readonly"
    assert entry["connection"]["params"]["password"] == "pwd-on-server"
    assert entry["connection"]["params"]["mysql_db"] == "prod"
    assert entry["model"]["api_key"] == "sk-on-server"
    assert collect_token_digests(entry) == [token_sha256("sk-deploy-token")]


def test_deploy_token_without_sha256():
    """deploy 里写明文的 token（或 ${ENV:变量}）时按明文算摘要，sha256 优先。"""
    os.environ["SQLSTAGE_SERVICE_TOKEN"] = "sk-env-token"
    plain = {"auth": {"tokens": [{"name": "t", "token": "sk-plain"}]}}
    assert collect_token_digests(plain) == [token_sha256("sk-plain")]
    from_env = {"auth": {"tokens": [{"name": "t", "token": "${ENV:SQLSTAGE_SERVICE_TOKEN}"}]}}
    assert collect_token_digests(from_env) == [token_sha256("sk-env-token")]
    both = {"auth": {"tokens": [{"name": "t", "token": "sk-plain", "sha256": token_sha256(TOKEN)}]}}
    assert collect_token_digests(both) == [token_sha256(TOKEN)], "有 sha256 就以 sha256 为准"


def test_dump_and_load_roundtrip():
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(make_db()),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    directory = Path(tempfile.mkdtemp())
    for fmt, suffix in (("yaml", ".yaml"), ("json", ".json")):
        path = directory / f"service{suffix}"
        path.write_text(dump_service_config(config, fmt), encoding="utf-8")
        os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
        os.environ["SQLSTAGE_SERVICE_TOKEN"] = TOKEN
        loaded = load_service_config(str(path))
        entry = loaded["services"][0]
        assert entry["model"]["api_key"] == "sk-from-env", entry["model"]["api_key"]
        # token 明文保留占位符，鉴权只认 sha256
        assert entry["auth"]["tokens"][0]["token"] == "${ENV:SQLSTAGE_SERVICE_TOKEN}"
        assert entry["auth"]["tokens"][0]["sha256"] == token_sha256(TOKEN)

    try:
        dump_service_config(config, "toml")
    except ValueError as error:
        assert "只支持 yaml / json" in str(error)
    else:
        raise AssertionError("不支持的格式应该报错")


def test_v1_config_still_loads():
    """v1 的单服务平铺格式要能自动升级成 services 列表。"""
    v1 = {
        "version": 1, "name": "老服务",
        "model": {"api_key": "sk-from-env"}, "tricks": strategy_config()["tricks"],
        "orm": {"enabled": False},
        "connection": {"type": "SQLite", "params": {"database": make_db(), "read_only": True}},
        "auth": {"required": True, "tokens": [{"name": "t", "sha256": token_sha256(TOKEN)}]},
    }
    path = Path(tempfile.mkdtemp()) / "old.yaml"
    path.write_text(dump_service_config(v1, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    loaded = load_service_config(str(path))
    assert loaded["version"] == 2 and len(loaded["services"]) == 1
    assert loaded["services"][0]["name"] == "老服务"


def test_missing_env_raises():
    os.environ.pop("MISSING_FOR_TEST", None)
    try:
        expand_config({"api_key": "${ENV:MISSING_FOR_TEST}"})
    except KeyError as error:
        assert "MISSING_FOR_TEST" in str(error)
    else:
        raise AssertionError("环境变量缺失必须报错，不能静默留空")


def test_token_placeholder_needs_no_env():
    """token 用占位符时不必设置环境变量，--token 另给一个就能起来。"""
    os.environ.pop("SQLSTAGE_SERVICE_TOKEN", None)
    directory = Path(tempfile.mkdtemp())
    config = build_service_config([{
        "name": "svc", "strategy": strategy_row(), "connection": connection_row(make_db()),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    loaded = load_service_config(str(path))
    digests = collect_token_digests(loaded["services"][0]) + [token_sha256("sk-another")]
    client = TestClient(create_app(loaded, True, digests, str(directory)))
    assert client.get("/v1/info", headers={"Authorization": "Bearer sk-another"}).status_code == 200


def test_single_service_endpoints():
    """单服务：/v1/predict 与 /v1/{name}/predict 都能用。"""
    directory = Path(tempfile.mkdtemp())
    guide = directory / "guide.md"
    guide.write_text("## 1 数据库用途\n城市人口", encoding="utf-8")
    config = build_service_config([{
        "name": "城市服务", "strategy": strategy_row(guide_enabled=True, guide_path=str(guide)),
        "connection": connection_row(make_db()),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    os.environ["SQLSTAGE_SERVICE_TOKEN"] = TOKEN
    loaded = load_service_config(str(path))
    digests = collect_token_digests(loaded["services"][0])

    with fake_model():
        client = TestClient(create_app(loaded, True, digests, str(directory)))
        headers = {"Authorization": f"Bearer {TOKEN}"}

        health = client.get("/health")
        assert health.status_code == 200 and health.json()["services"] == ["城市服务"]

        assert client.get("/v1/info").status_code == 401, "缺 token 必须 401"
        assert client.get("/v1/info",
                          headers={"Authorization": "Bearer sk-wrong"}).status_code == 403

        info = client.get("/v1/info", headers=headers)
        assert info.json()["service"] == "城市服务" and info.json()["guide_enabled"] is True
        assert info.json()["database_connected"] is True
        assert "api_key" not in json.dumps(info.json()), "info 不能泄露 api_key"

        assert "city" in client.get("/v1/schema", headers=headers).json()["tables"]

        for url in ("/v1/predict", "/v1/城市服务/predict"):
            response = client.post(url, json={"question": "人口最多的城市"}, headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["sql"].strip() == GOLD_SQL, body["sql"]
            assert body["rows"][0]["name"] == "上海", body["rows"]


def test_multi_service_routing():
    """多服务按名字路由，各用各的库，token 不串。"""
    directory = Path(tempfile.mkdtemp())
    first_db, second_db = make_db(), make_db()
    config = build_service_config([
        {"name": "alpha", "strategy": strategy_row(), "connection": connection_row(first_db),
         "tokens": [{"name": "a", "token": TOKEN}]},
        {"name": "beta", "strategy": strategy_row("第二套"), "connection": connection_row(second_db),
         "tokens": [{"name": "b", "token": "sk-beta"}]},
    ])
    path = directory / "services.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    loaded = load_service_config(str(path))

    with fake_model():
        client = TestClient(create_app(loaded, True, [], str(directory)))

        assert client.get("/health").json()["services"] == ["alpha", "beta"]
        listing = client.get("/v1/services", headers={"Authorization": f"Bearer {TOKEN}"})
        assert listing.status_code == 200
        assert [item["name"] for item in listing.json()["services"]] == ["alpha", "beta"]

        assert client.post("/v1/beta/predict", json={"question": "x"},
                           headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 403
        assert client.post("/v1/beta/predict", json={"question": "x"},
                           headers={"Authorization": "Bearer sk-beta"}).status_code == 200
        # 多服务时不再提供不带名字的写法
        assert client.post("/v1/predict", json={"question": "x"},
                           headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_materialize_and_relative_path():
    """内联文件写回磁盘，SQLite 相对路径按配置文件目录解析。"""
    directory = Path(tempfile.mkdtemp())
    (directory / "data.db").write_bytes(b"")
    entry = {
        "name": "svc",
        "tricks": {"fewshot_path": ""},
        "connection": {"type": "SQLite", "params": {"database": "data.db", "read_only": True}},
        "files": {"fewshot_path": {"path": "bundle/x/fewshot.json", "text": '[{"query": "x"}]'}},
    }
    workdir = Path(tempfile.mkdtemp())
    materialize_files(entry, workdir)
    written = Path(entry["tricks"]["fewshot_path"])
    assert written.exists() and written.read_text(encoding="utf-8") == '[{"query": "x"}]'

    resolve_database(entry, str(directory))
    assert entry["connection"]["params"]["database"] == str(directory / "data.db")


def test_legacy_config_with_guide_path_still_works():
    """早先版本导出的配置里 guide 还是 files.guide_path，加载后照样能取到正文。"""
    from sqlstage.config import TrickConfig
    from sqlstage.guide import load_guide

    entry = {
        "name": "svc",
        "tricks": {"guide_path": "", "guide_text": ""},
        "connection": {"type": "SQLite", "params": {"database": "x.db", "read_only": True}},
        "files": {"guide_path": {"path": "bundle/laptop/guide.md", "text": "## 老配置里的 guide"}},
    }
    materialize_files(entry, Path(tempfile.mkdtemp()))
    assert load_guide(TrickConfig(**entry["tricks"])) == "## 老配置里的 guide"


def test_startup_probe_reports_connected():
    """启动时真连一次库，连上就报告读了几张表，/health 与 /v1/info 都能看到。"""
    directory = Path(tempfile.mkdtemp())
    config = build_service_config([{
        "name": "好库服务", "strategy": strategy_row(), "connection": connection_row(make_db()),
        "tokens": [{"name": "default", "token": TOKEN}],
    }])
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    loaded = load_service_config(str(path))
    app = create_app(loaded, True, [], str(directory))

    status = app.state.database_status[0]
    assert status["connected"] is True and status["tables"] == 1, status

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_database_report(app.state.database_status, app.state.service_entries)
    assert "[OK]" in buffer.getvalue() and "1 张表" in buffer.getvalue()

    client = TestClient(app)
    health = client.get("/health").json()
    assert health["databases"][0]["connected"] is True
    info = client.get("/v1/info", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert info["database_connected"] is True


def test_startup_probe_warns_but_still_serves():
    """数据库连不上：启动报告讲清原因和连接参数（密码打码），服务照常起来。"""
    directory = Path(tempfile.mkdtemp())
    config = build_service_config([{
        "name": "坏库服务", "strategy": strategy_row(execution_check=True),
        "connection": connection_row(str(directory / "missing.db")),
    }])
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    loaded = load_service_config(str(path))
    app = create_app(loaded, False, [], str(directory))

    status = app.state.database_status[0]
    assert status["connected"] is False and status["error"], status

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_database_report(app.state.database_status, app.state.service_entries)
    text = buffer.getvalue()
    assert "连不上数据库" in text and "missing.db" in text, text
    assert "执行校验" in text, "开了执行校验要额外点名"

    client = TestClient(app)
    assert client.get("/health").json()["databases"][0]["connected"] is False
    assert client.get("/v1/info").json()["database_connected"] is False
    # 库连不上时请求要给出明确的 503，而不是让驱动异常变成 500
    for response in (client.get("/v1/schema"),
                     client.post("/v1/predict", json={"question": "人口最多的城市"})):
        assert response.status_code == 503, response.status_code
        assert "数据库不可用" in response.json()["detail"], response.json()


def test_describe_connection_masks_password():
    text = describe_connection({"type": "MySQL", "params": {
        "host": "db.internal", "user": "root", "password": "super-secret", "mysql_db": "shop"}})
    assert "super-secret" not in text and "******" in text, text


def test_strict_mode_exits_when_database_down():
    """--strict 把「连不上数据库」从警告变成硬门槛：不起服务，返回码 1。"""
    import server.serve as serve_module

    directory = Path(tempfile.mkdtemp())
    config = build_service_config([{
        "name": "坏库服务", "strategy": strategy_row(),
        "connection": connection_row(str(directory / "missing.db")),
    }])
    path = directory / "service.yaml"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"

    captured = []
    fake_uvicorn = types.SimpleNamespace(run=lambda app, **kwargs: captured.append(app))
    real_uvicorn = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake_uvicorn
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = serve_module.main(["--config", str(path), "--strict"])
    finally:
        if real_uvicorn is not None:
            sys.modules["uvicorn"] = real_uvicorn
    assert code == 1 and captured == [], "--strict 下不该起服务"


def test_no_auth_mode():
    config = build_service_config([{
        "name": "开放服务", "strategy": strategy_row(), "connection": connection_row(make_db()),
    }])
    assert config["services"][0]["auth"]["required"] is False
    client = TestClient(create_app(config, False, [], "."))
    assert client.get("/v1/info").status_code == 200


def test_main_generates_token_when_config_has_none():
    """配置里没有 token 时，服务模式默认仍然带鉴权，并现场生成一个可用的 token。"""
    import server.serve as serve_module

    config = build_service_config([{
        "name": "开放配置", "strategy": strategy_row(), "connection": connection_row(make_db()),
    }])
    path = Path(tempfile.mkdtemp()) / "service.yaml"
    os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
    path.write_text(dump_service_config(config, "yaml"), encoding="utf-8")

    captured = []
    fake_uvicorn = types.SimpleNamespace(run=lambda app, **kwargs: captured.append(app))
    real_uvicorn = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake_uvicorn
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            serve_module.main(["--config", str(path), "--port", "8099"])
    finally:
        if real_uvicorn is not None:
            sys.modules["uvicorn"] = real_uvicorn

    assert len(captured) == 1, "main 应该把 app 交给 uvicorn"
    token = buffer.getvalue().split("已自动生成：")[1].splitlines()[0].strip()
    assert token.startswith("sk-")

    client = TestClient(captured[0])
    assert client.get("/health").status_code == 200
    assert client.get("/v1/info").status_code == 401, "没生成 token 前不该放行"
    assert client.get("/v1/info", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_generated_token_is_unique():
    first, second = generate_token(), generate_token()
    assert first.startswith("sk-") and len(first) > 20
    assert first != second


def test_service_database_is_read_only():
    """服务模式用的连接必须只读：写语句在 SQL 层被拦。"""
    from sqlstage.database import ReadOnlyViolation
    database = build_database("SQLite", {"database": make_db(), "read_only": True})
    try:
        database.fetch("DELETE FROM city")
    except ReadOnlyViolation:
        pass
    else:
        raise AssertionError("服务模式的连接没有拦住写语句")


if __name__ == "__main__":
    test_export_replaces_secrets()
    test_multiple_services_in_one_config()
    test_guide_is_exported_as_text_only()
    test_guide_text_wins_over_path()
    test_fewshot_file_is_inlined()
    test_missing_external_file_raises()
    test_sqlite_path_is_made_portable()
    test_deploy_block_sits_on_top()
    test_deploy_overrides_win_on_load()
    test_deploy_token_without_sha256()
    test_dump_and_load_roundtrip()
    test_v1_config_still_loads()
    test_missing_env_raises()
    test_token_placeholder_needs_no_env()
    test_single_service_endpoints()
    test_multi_service_routing()
    test_materialize_and_relative_path()
    test_legacy_config_with_guide_path_still_works()
    test_startup_probe_reports_connected()
    test_startup_probe_warns_but_still_serves()
    test_describe_connection_masks_password()
    test_strict_mode_exits_when_database_down()
    test_no_auth_mode()
    test_main_generates_token_when_config_has_none()
    test_generated_token_is_unique()
    test_service_database_is_read_only()
    print("test_serve_mode: OK")
