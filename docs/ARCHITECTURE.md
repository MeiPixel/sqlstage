# 架构

## 两种运行形态

同一套引擎，两种进程形态。区别只在挂了哪些 HTTP 端点。

```
开发模式（工作台）                          服务模式（部署）
server/main.py :8020                        server/serve.py :8021
├── 连接/策略/数据/搜索管理路由             ├── /health
├── React 前端 :5173                        ├── /v1/info
└── 读写本机 store/ 与 runs/                 ├── /v1/schema
                                            ├── /v1/predict
                                            └── 只读挂载配置里的数据库
```

引擎在 `sqlstage/`，不依赖 UI 框架与 Web 框架，两种形态共用。

## 生成链路

一次 `engine.generate(question)` 的完整路径：

```
问题
 │
 ├─ 问题改写（可选，多轮上下文）
 ├─ 难度分级（可选，影响 Top-K 与召回方式）
 │
 ├─ 表召回 ── all_schema / keyword / bm25 / bm25_field / embedding / llm / hybrid
 │            沿外键做 JOIN 扩展
 ├─ 字段召回 ── Top-K 截断 / BM25 / 大模型选字段 / 大表按问题裁剪
 │
 ├─ 提示构建
 │   ├─ Schema 文本（表、字段、类型、主外键）
 │   ├─ 字段说明与样例值
 │   ├─ Database Guide（guide_text 或 guide_path）
 │   ├─ Few-shot 检索（BM25 / 向量 / 混合，可限定同表）
 │   └─ ORM 规则（可选，见下）
 │
 ├─ 候选生成 ── Direct SQL 或 ORM 计划
 ├─ 候选择优 ── 规范化投票 / 大模型裁判
 │
 ├─ 执行反馈自愈（可选）── 报错回喂重试 / 空结果重生成
 │
 ├─ 只读校验（两道，见下）
 └─ SQL + Trace
```

`result["trace"]["executed_tricks"]` 记录**实际走到过**的组件，`result["trace"]["timing"]`
给出分段耗时。配置里声明了但没触发的开关不会出现在这里——这是判断「该不该开」的基础。

## ORM 约束层

与「让模型写对 SQL」不同，ORM 走的是「让模型描述查询意图，规则负责编译成 SQL」。

```
模型输出结构化计划 → 确定性规则编译 → SQL
                      ├─ field_match    精确 / 包含 / 前缀匹配
                      ├─ value_maps     用户说法 → 库内真实取值
                      ├─ unit_rules     单位换算
                      ├─ numeric_cast   数字文本字段自动 CAST
                      ├─ field_alias    业务别名 → 真实字段
                      └─ named          派生字段（表达式内联）
```

关键点：规则在编译期烘焙进 SQL，不依赖模型自觉。同一指标在任何提问方式下算法一致，
这解决了企业落地最常见的「口径不对」问题，而不只是「生成不出」。

## 只读保护与执行护栏

三道只读拦截 + 两道执行护栏。任一命中即拒绝执行。

**一、连接层**——会话级只读，不依赖 SQL 写法：

| 数据库 | 手段 |
|---|---|
| SQLite | `file:...?mode=ro` URI |
| MySQL | `SET SESSION TRANSACTION READ ONLY` |
| PostgreSQL | `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` |

**二、SQL 层**——执行前用 sqlglot 解析：根节点必须是 `SELECT` / `UNION` / `INTERSECT` / `EXCEPT`，
`exp.With` 会继续下钻校验 CTE，多语句逐条检查。命中写操作抛 `ReadOnlyViolation`
（继承 `DatabaseError`，因此会被 `try_execute` 与引擎修复循环自然接住）。

**三、危险函数层**——只读不等于安全。以下函数以 `SELECT` 形式出现，实际读写服务器本地文件、
跨库执行或加载扩展，连接层的只读事务拦不住它们（PG 在只读事务里 `pg_write_file` 照样成功），
因此在 AST 层按函数名拒绝：

| 类别 | 函数 |
|---|---|
| 读写本地文件 | `pg_read_file` `pg_read_binary_file` `pg_write_file` `pg_ls_dir` `lo_import` `lo_export` `readfile` `load_file` `dumpfile` |
| 跨库执行 | `dblink` `dblink_exec` `dblink_open` |
| 加载扩展（任意代码执行） | `load_extension` |
| 外部请求（SSRF） | `sys_exec` `sys_eval` |

行锁同样拒绝：`SELECT ... FOR UPDATE` / `FOR SHARE` / `LOCK IN SHARE MODE` 属于写语义。

**执行护栏**——模型生成的 SQL 不受控，执行侧有硬边界，参数在 `TrickConfig` 里，
由 `Text2SQLEngine.__init__` 同步到连接对象，因此四个入口自动都带上：

| 参数 | 默认 | 机制 |
|---|---|---|
| `max_rows` | 5000 | 超行数抛 `RowLimitExceeded`，**不截断**——半截结果会被下游当成完整答案 |
| `statement_timeout_ms` | 30000 | SQLite `progress_handler` / MySQL `max_execution_time` / PG `statement_timeout` |

## 连接池

`database.py` 的 `ConnectionPool` 用一个 `LifoQueue` 持有空闲连接，`pool_size` 来自连接参数。
SQLite 的 `pool_size` 固定为 0：连本地文件建连接几乎零成本，池化没有收益。

三个适配器共用同一套取还流程，各自定义两件事：

| 方法 | MySQL | PostgreSQL | SQLite |
|---|---|---|---|
| `open()` 新建连接 | `pymysql.connect` | `pg.connect` | `sqlite3.connect`（只读 URI） |
| `ping()` 借出探活 | `connection.ping()` | `SELECT 1`（pg8000 无 ping） | 不池化，不调用 |

借出前探活而不是事后靠报错判断：服务端的 `wait_timeout` 可能远短于默认的 8 小时，
被掐掉的连接如果流到用户手里，表现是「同样的请求时好时坏」——比明确报错更难排查。
归还时统一 `rollback`：语句执行失败会让连接停在失败事务里，PG 尤其明显，
下一个使用者直接吃到 `current transaction is aborted`。

`pool_size` 限的是空闲连接数，不是在途总数。总连接数 ≈ 池容量 + 同时在途的请求数，
所以上限由服务模式的并发闸门（`--max-concurrency`）与池容量共同决定。

## 配置模型

```
StrategyConfig
├── model    ModelConfig    模型服务、Base URL、温度、思考模式
├── tricks   TrickConfig    48 个组件开关与参数
├── orm      OrmConfig      字段级业务规则
└── name
```

`apply_strategy_overrides(strategy, {"tricks.fewshot_top_k": 3})` 按点分路径覆盖，
自动寻优就是在这上面做网格搜索。

## 目录职责

```
sqlstage/          引擎，无 UI 依赖
├── engine.py      生成主循环、Trace 记录
├── config.py      三个配置数据类
├── database.py    三种适配器，统一 DatabaseError，只读在此层与 engine 双重实施
├── schema.py      SQLite schema 读取与渲染
├── retrieval.py   表召回、字段召回、BM25
├── pipeline.py    难度分级、提示裁剪、确定性修复
├── fewshot.py     示例加载与检索
├── guide.py       Database Guide 加载与渲染
├── orm.py         ORM 规则应用与编译
├── candidates.py  多候选生成、投票、裁判
├── evaluation.py  严格 AST 与 EX 口径
├── benchmark.py   批量评测
├── optimization.py 搜索空间展开、逐组评估、排名
├── search_runner.py 统一搜索编排、预算、缓存与排行榜
├── search_demo.py 可选资源路径约定与演示兼容入口
├── store.py       本机 SQLite 存储
├── dataset.py     数据集出题与解析
├── model.py       OpenAI 兼容客户端与分段计时
└── tricks.py      Trick 说明文档

server/            FastAPI 后端（开发模式挂全部管理端点，服务模式只挂预测端点）
frontend/          React + TypeScript
training/          LoRA SFT / DPO
```
