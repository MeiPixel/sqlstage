"""服务模式配置：开发模式导出成 yaml/json，服务模式加载同一份文件启动。

一份配置可以容纳**多个服务**（每个服务自带策略、连接、token），v1 的单服务配置会自动升级。
密钥不落明文：导出时把 api_key / 数据库密码换成 ``${ENV:变量名}`` 占位符，
加载时从环境变量展开。要内联明文得显式传 ``inline_secrets=True``。
guide 只导出**正文**：策略里 guide_path 指向的文件在导出时被读出来写进 guide_text，
配置里不再保留路径（引擎本来就是「有正文用正文」，留个开发机路径只会误导）。
few-shot 是结构化列表，内容内联进 ``files`` 段，启动时物化到临时目录，避免路径失效。

配置最上面是 ``deploy`` 段——**部署时最常改的东西**（数据库账号密码、模型 Key、访问 token）。
它和 ``services`` 段是同一批字段的两份副本：改 ``deploy`` 就够了，加载时覆盖到 ``services`` 上；
``services`` 保持完整导出，保证「拿到的文件里所有字段都在」。
"""
import json
import os
from copy import deepcopy
from pathlib import Path

import yaml

SERVICE_CONFIG_VERSION = 2
SUPPORTED_VERSIONS = {1, 2}
API_KEY_PLACEHOLDER = "${ENV:DEEPSEEK_API_KEY}"
PASSWORD_PLACEHOLDER = "${ENV:SQLSTAGE_DB_PASSWORD}"
TOKEN_PLACEHOLDER = "${ENV:SQLSTAGE_SERVICE_TOKEN}"
ENV_PREFIX = "${ENV:"
ENV_SUFFIX = "}"
# tricks 里指向外部文件、需要把正文内联进 files 段的字段。
# guide 不在这里：它只导出正文，见 inline_guide。
INLINE_FILE_FIELDS = ("fewshot_path",)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def generate_token():
    import secrets
    return "sk-" + secrets.token_urlsafe(24)


def token_sha256(token):
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def relative_to_project(path):
    """能写成相对项目根的路径就写成相对的，方便整个目录搬到服务器。

    一律用正斜杠：Windows 导出的反斜杠路径拿到 Linux 服务器上是打不开的。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix(), True
    try:
        return candidate.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix(), True
    except ValueError:
        return str(candidate), False


def read_external_file(raw, owner):
    """把配置里引用的外部文件读出来，相对路径按项目根解析。读不到直接报错。"""
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"{owner} 引用的外部文件 {raw} 导出时读不到（{path}）")
    return path.read_text(encoding="utf-8")


def inline_guide(tricks, name, warnings=None):
    """guide 只留正文：guide_path 指向的文件读出来写进 guide_text，路径本身不导出。

    引擎 ``load_guide`` 是「有 guide_text 就用它」，所以两个都填时以正文为准，
    此时路径文件不导出，并给一条提示——否则服务器上会看到一条指向开发机文件的死路径。
    """
    raw = tricks["guide_path"]
    tricks.pop("guide_path")
    if not raw:
        return
    if tricks["guide_text"]:
        if warnings is not None:
            warnings.append(f"服务 {name} 的 guide 同时填了正文与路径（{raw}），"
                            "按引擎取值规则以正文为准，路径文件未导出")
        return
    tricks["guide_text"] = read_external_file(raw, f"服务 {name} 的 guide")


def collect_inline_files(tricks, name):
    """把 fewshot 指向的文件内容读进来，配置只带路径是没法搬家的。"""
    files = {}
    for field in INLINE_FILE_FIELDS:
        raw = tricks[field]
        if not raw:
            continue
        files[field] = {"path": str(raw), "text": read_external_file(raw, f"服务 {name} 的 {field}")}
    return files


def build_service_entry(name, strategy_row, connection_row, tokens=(),
                        inline_secrets=False, warnings=None):
    strategy = json.loads(strategy_row["config"])
    params = dict(json.loads(connection_row["params"]))
    model = dict(strategy["model"])
    tricks = strategy["tricks"]

    if not inline_secrets:
        if model.get("api_key"):
            model["api_key"] = API_KEY_PLACEHOLDER
        if params.get("password"):
            params["password"] = PASSWORD_PLACEHOLDER

    if connection_row["type"] == "SQLite" and params.get("database"):
        params["database"], portable = relative_to_project(params["database"])
        if not portable and warnings is not None:
            warnings.append(
                f"服务 {name} 的 SQLite 库在 {params['database']}，是开发机绝对路径，"
                "部署时要一起拷贝或改成相对路径")

    auth_tokens = []
    for item in tokens:
        entry = {"name": item["name"]}
        entry["token"] = item["token"] if inline_secrets else TOKEN_PLACEHOLDER
        entry["sha256"] = token_sha256(item["token"])
        auth_tokens.append(entry)

    inline_guide(tricks, name, warnings)

    return {
        "name": name,
        "source": {"strategy": strategy_row["name"], "connection": connection_row["name"]},
        "model": model,
        "tricks": tricks,
        "orm": strategy["orm"],
        "connection": {"type": connection_row["type"], "params": params},
        "auth": {"required": bool(auth_tokens), "tokens": auth_tokens},
        "files": collect_inline_files(tricks, name),
    }


def build_deploy_block(entries):
    """把每个服务最常改的三组参数抄一份到配置最上面，部署时改这里就行。

    字段与 services 段完全一致（不是只挑几个），所以加载时直接整体覆盖即可；
    services 段仍保留完整导出，服务器上的配置永远是自包含的。
    """
    return {
        entry["name"]: {
            "model": dict(entry["model"]),
            "database": dict(entry["connection"]["params"]),
            "tokens": [dict(item) for item in entry["auth"]["tokens"]],
        }
        for entry in entries
    }


def build_service_config(services, inline_secrets=False):
    """services: [{"name", "strategy", "connection", "tokens"}]，一份配置可以有多个服务。"""
    warnings = []
    entries = [build_service_entry(item["name"], item["strategy"], item["connection"],
                                   item.get("tokens", ()), inline_secrets, warnings)
               for item in services]
    return {"version": SERVICE_CONFIG_VERSION, "deploy": build_deploy_block(entries),
            "services": entries, "warnings": warnings}


YAML_HEADER = """# SqlStage 服务配置（开发模式导出，服务模式加载）
#
# 部署时一般只改最上面的 deploy 段：数据库账号密码、模型 Key、访问 token。
#   改完 deploy 就生效，它会覆盖下面 services 段里同名的字段。
#   密钥默认写成 ${ENV:变量名}，启动前设置对应环境变量，也可以直接写明文。
#   换 token 就写明文或 ${ENV:变量}，并把该条目的 sha256 那行删掉（有 sha256 就以 sha256 为准）。
# services 段是开发模式的完整导出（含全部字段），通常不用动。
"""


def dump_service_config(config, fmt):
    if fmt == "yaml":
        return YAML_HEADER + "\n" + yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    if fmt == "json":
        return json.dumps(config, ensure_ascii=False, indent=2)
    raise ValueError(f"导出格式只支持 yaml / json：{fmt}")


def expand_env_value(value):
    if not value.startswith(ENV_PREFIX) or not value.endswith(ENV_SUFFIX):
        return value
    name = value[len(ENV_PREFIX):-len(ENV_SUFFIX)]
    if name not in os.environ:
        raise KeyError(f"配置引用了环境变量 {name}，但当前环境没有设置它")
    return os.environ[name]


def expand_config(node):
    if isinstance(node, dict):
        return {key: expand_config(value) for key, value in node.items()}
    if isinstance(node, list):
        return [expand_config(item) for item in node]
    if isinstance(node, str):
        return expand_env_value(node)
    return node


def upgrade_v1(raw):
    """v1 是单服务平铺格式，包成 services 列表继续使用。"""
    known = {"version", "name", "source", "model", "tricks", "orm", "connection", "auth"}
    entry = {key: raw[key] for key in known if key in raw}
    return {"version": SERVICE_CONFIG_VERSION, "services": [entry], "warnings": []}


def apply_deploy_overrides(services, deploy):
    """用 deploy 段覆盖 services 段里同名的字段，deploy 里没写的照旧。"""
    for entry in services:
        override = deploy.get(entry["name"])
        if override is None:
            continue
        if "model" in override:
            entry["model"].update(override["model"])
        if "database" in override:
            entry["connection"]["params"].update(override["database"])
        if override.get("tokens"):
            entry["auth"]["tokens"] = [dict(item) for item in override["tokens"]]
            entry["auth"]["required"] = True
    return services


def load_service_config(path):
    text = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(text)
    elif suffix == ".json":
        raw = json.loads(text)
    else:
        raise ValueError(f"服务配置文件必须是 .yaml / .yml / .json：{path}")

    version = raw["version"]
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"服务配置版本不支持：{version}（当前支持 {sorted(SUPPORTED_VERSIONS)}）")
    if version == 1:
        raw = upgrade_v1(raw)

    loaded = {"version": SERVICE_CONFIG_VERSION, "warnings": raw.get("warnings", [])}
    services = apply_deploy_overrides([deepcopy(entry) for entry in raw["services"]],
                                      raw.get("deploy", {}))
    expanded = []
    for entry in services:
        # auth 与 files 不展开：token 明文可能还是占位符，外部文件内容里出现 ${ENV:} 也不该被替换
        keep = {key: entry[key] for key in ("auth", "files") if key in entry}
        body = expand_config({key: value for key, value in entry.items()
                              if key not in ("auth", "files")})
        body.update(keep)
        expanded.append(body)
    loaded["services"] = expanded
    return loaded
