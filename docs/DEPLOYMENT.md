# 部署

## 从工作台到线上服务

```
工作台（开发模式 :8020）          导出配置            部署机（服务模式 :8021）
调试策略、验证效果         sqlstage-services.yaml      一条命令起进程
     │                                                      │
     └──────────── 只暴露 /v1/predict ────────────────────┘
```

## 一、导出配置

在「服务模式」页选好要导出的服务，点导出。得到 `sqlstage-services.yaml`（或 JSON）。

配置分两段，这个设计是为了让运维改部署参数时不必回到工作台：

```yaml
version: 2

deploy:                    # ← 常改项都在这里，改完直接重启服务即可
  default:
    model:
      model: deepseek-v4-flash
      provider: deepseek
      base_url: https://api.deepseek.com
      api_key: ${ENV:MODEL_API_KEY}
    database:
      type: PostgreSQL
      params:
        host: db.internal
        port: 5432
        user: analyst
        password: ${ENV:SQLSTAGE_DB_PASSWORD}
        mysql_db: business
        read_only: true
    tokens: [${ENV:SERVICE_TOKEN}]

services:                  # ← 完整策略，调试时调这里
  default:
    model: {...}
    database: {...}
    tricks: {...}
```

`deploy` 段的同名值在加载时覆盖 `services` 段，所以生产环境只改 `deploy` 即可，
实验用的 Trick 组合原封不动保留在文件里，随时能比对。

多服务时按服务名并列：

```yaml
deploy:
  sales:   { model: {...}, database: {...}, tokens: [...] }
  support: { model: {...}, database: {...}, tokens: [...] }
```

请求路由到 `/v1/sales/predict` 与 `/v1/support/predict`。只有一个服务时 `/v1/predict` 同样可用。

## 二、密钥处理

密码与 API Key 写成 `${ENV:变量名}`，加载时展开。**变量缺失会直接报错**，不会静默当成空值
继续跑——宁可起不来，也不要连上空密码的库。

```bash
export MODEL_API_KEY=sk-...
export SQLSTAGE_DB_PASSWORD=...
export SERVICE_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

少数情况想省事，可以显式传令牌而不设环境变量：

```bash
python -m server.serve --config sqlstage-services.yaml --token sk-xxxx
```

注意配置文件里的 `auth` 段不做占位符展开，正是为了这个场景。

## 三、启动

```bash
python -m server.serve --config sqlstage-services.yaml --token sk-xxxx --host 0.0.0.0 --port 8021
```

| 参数 | 作用 |
|---|---|
| `--token` | 指定访问令牌；不传则用配置文件里的，或现场生成并打印 |
| `--no-auth` | 关闭鉴权（会打警告，仅建议内网调试用） |
| `--host` | 监听地址，默认 `127.0.0.1`；对外服务设 `0.0.0.0` |
| `--port` | 监听端口，默认 `8021` |
| `--strict` | 启动时若有数据库连不上，直接以退出码 1 结束，不起端口 |
| `--max-concurrency` | 单服务并发上限，默认 `16`；满了返回 429 |

## 四、并发与容量

一次 `/predict` 会连打多次模型（改写、召回、生成、修复）与若干次数据库查询，
默认配置下单个请求的上游耗时在数秒级。所以**并发上限是必需的容量参数**，不是可选调优：

```bash
python -m server.serve --config sqlstage-services.yaml --max-concurrency 32
```

超出上限直接返回 **429**，而不是排队等到客户端超时——让调用方自己退避重试，
队列把压力原样堆在服务端只会让整体崩得更慢。当前占用可以从 `/health` 的 `load` 字段读：

```json
{"load": {"default": {"active": 3, "limit": 16}}}
```

**调大之前先看两件事**：

- **数据库连接数**。连接池限的是**空闲**连接数，在途请求各持有一条，所以总连接数
  ≈ 池容量（连接表单里的「连接池容量」）+ 同时在途的请求数。MySQL 与 PostgreSQL 的
  最大连接数是硬约束，两者之和不要超过它，否则会先从数据库侧拿到 `too many connections`。
- **上游模型的限流**。并发高过模型侧的 RPM/TPM 配额会拿到 429，
  `model.py` 会按退避重试两次，但持续超限会持续失败。

**容量不足时的表现与对策**：

| 现象 | 原因 | 对策 |
|---|---|---|
| `/health` 的 `load.active` 长期贴近 `limit` | 并发上限偏小 | 调大 `--max-concurrency`，先确认数据库连接数够 |
| 日志频繁出现 `over capacity` | 到达闸门 | 同上；或在调用方加退避重试 |
| 日志出现 `model retry` 且最终 5xx | 模型侧限流或故障 | 降低并发，或换更大配额的模型服务 |
| 数据库侧报连接数超限 | 并发 + 池容量 > 数据库最大连接数 | 先降 `--max-concurrency`，或调小连接池容量 |

## 五、启动即探活

服务启动时会挨个服务真连一次数据库，把结果打在日志里。

连不上时**只警告并继续**，进程照常起来：

```
[warn] 服务 default：数据库连接失败（Access denied for user 'analyst'）
       /health 与 /v1/info 的 database_connected 将为 false，请求返回 503
```

这样设计的理由：数据库临时不可用时，进程不该跟着反复重启——`/health` 返回 false 让上游
负载均衡先摘流量，数据库恢复了服务自己就回来了。确认部署环境必须一次成功，加 `--strict`。

## 六、验证

```bash
# 健康检查
curl http://127.0.0.1:8021/health

# 服务信息（不回显任何密钥）
curl -H "Authorization: Bearer $SERVICE_TOKEN" http://127.0.0.1:8021/v1/info

# 表结构
curl -H "Authorization: Bearer $SERVICE_TOKEN" http://127.0.0.1:8021/v1/schema

# 提问
curl -X POST http://127.0.0.1:8021/v1/predict \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question": "最近30天销售额前10的商品"}'
```

请求体只有 `question` 与可选的 `history`（多轮上下文，`[{"role": "user", "content": "..."}]`）。
是否执行生成的 SQL 由配置里的 `tricks.execution_check` 决定——关掉时只返回 SQL 与 Trace，
不执行结果查询，但仍需连接数据库读取 Schema。返回体是引擎的 `generate()` 结果，含 `sql`、`rows` 与 `trace`。

未带令牌或令牌错误分别返回 401 / 403。

## 七、放进现有体系

服务模式只做预测，适合作为网关、报表系统或 IM 机器人的后端：

```
业务系统 / BI / IM 机器人
        │
        ▼
   反向代理（TLS、限流、访问日志）
        │
        ▼
   SqlStage 服务模式 :8021     ← 只挂 /v1/predict
        │
        ▼
   业务数据库（只读账号）
```

代理层负责 TLS 终止与限流，SqlStage 只管预测。

## 八、离线部署

配置文件里的 SQLite 路径会在导出时转成相对路径（按配置文件所在目录解析，正斜杠形式），
所以整个目录打包带走即可，不依赖开发机的绝对路径。打包时记得带上数据库文件本身。

项目外路径会给出 warning，提示你检查是否漏拷。

## 安全清单

- [ ] 数据库账号只有 `SELECT` 权限
- [ ] `--host 0.0.0.0` 时前面有反向代理，且代理做 TLS 与限流
- [ ] 令牌用环境变量注入，不写进配置文件
- [ ] 导出的 yaml 不提交进公开仓库（里面有主机名与库名）
- [ ] 保持 `read_only: true`
- [ ] 部署机上 `pip install -r requirements.txt` 后确认版本与线上一致

**工作台模式另有一条**：它能连任意数据库、改配置、跑寻优，默认不鉴权是为了本机开发方便。
一旦要对外监听，必须设 `SQLSTAGE_WORKBENCH_TOKEN`，否则任何能打到该端口的人都能借它连你的库：

```bash
export SQLSTAGE_WORKBENCH_TOKEN="$(python -c 'import secrets;print("sk-"+secrets.token_urlsafe(24))')"
python -m uvicorn server.main:app --host 0.0.0.0 --port 8020
```

设了之后管理接口需要 `Authorization: Bearer <token>`；`/api/health` 保持可探活，并返回
`auth_required: true`。更稳妥的做法是工作台只在本机跑，对外一律走服务模式（8021），
服务模式只挂预测接口，不挂改配置、读文件、跑寻优这些管理端点。

**执行护栏也要看一眼**：`max_rows` 与 `statement_timeout_ms` 用默认值的配置在
`deploy` 段的 `tricks` 里，是可以覆盖的。线上如果放宽了 `max_rows`，等于放开了全表扫描，
注意配合数据库侧的超时与连接数上限一起定。

