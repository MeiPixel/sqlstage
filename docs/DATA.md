# 自带数据接入

本版本不附带任何比赛或第三方数据库、题库、Guide 和训练数据。测试夹具由代码临时创建，不能用来衡量实际准确率。

## 数据库

在配置页添加 SQLite、MySQL 或 PostgreSQL 连接。SQLite 使用后端机器上的文件路径，不是前端浏览器的本机路径；远程库填写主机、端口、用户名、密码和库名。推荐只读账号及只读连接。

Schema 从所选数据库读取。检索只能利用实际提供的字段名、说明、样例值和外键；不存在的外键不会凭空推断成可靠 JOIN 规则。字段说明和表说明可在策略中配置。保存的连接仅位于本地 Store，不能公开。

## 导入测试集

数据集页接受 JSON 数组或 JSONL。最小记录结构：

```json
{"query": "Find products cheaper than 50", "sql": "SELECT name FROM products WHERE price < 50"}
```

这是格式说明，不是随仓库提供的题库。标准 SQL 的表、字段和方言应与选定连接完全一致。自动寻优选择 `saved:<id>` 自建集时必须明确选择对应数据库；未选连接会报错，不会切换到其他库。

## Few-shot 与 Guide

Few-shot 使用 `tricks.fewshot_path` 指向自己的 JSONL，每条包含 `query` 和 `sql`。设置数量、检索方法、同表限制和同题排除规则。搜索自动把本轮抽中的题从示例池排除；训练、验证和独立评测仍需自行隔离。

Guide 可填写 `tricks.guide_text` 或指向自己的 Markdown 文件 `tricks.guide_path`。推荐说明业务概念、字段语义、单位与 JOIN 关系，不把评测题答案编进 Guide。开启 Guide 却没有提供有效文本，不代表获得了知识增强，应查看 Trace 与请求上下文。

## 可选资源与业务预设

仓库不附带示例数据库或业务预设，也不创建空的资源占位目录。优先通过工作台添加自己的连接、数据集和 ORM 规则，并将规则保存为策略。

已有资源可自行放入兼容目录 `examples/`：`manifest.json` 的 `databases` 数组包含 `name`、`title`、`file`（项目根的相对路径）；数据库位于 `databases/<name>.sqlite`，题库位于 `datasets/<name>.json`，Guide 位于 `guides/<table>/guide.md`，字段知识位于 `knowledge/<table>/knowledge.jsonl`。文件式 ORM 预设可放入 `orm_presets/*.json`，结构参见 `server/presets.py` 与 `sqlstage/config.py` 的 `OrmConfig`。不安装这些资源时，相应 API 返回空列表，不影响自建连接和策略。

自己安装的数据库、题库、知识文件和 ORM 规则会被 `.gitignore` 排除。发布时还需检查 Git 跟踪列表，不能只相信忽略规则；已被跟踪的文件不受 `.gitignore` 保护。

## 授权与泄漏检查

- 确认数据库、问题、标准答案、合成数据和模型各自的使用与再分发权。
- 不把验证题、同题改写或其答案放入 Few-shot 和训练集后再称作独立评测。
- 不把完整业务数据或明文凭据放入示例、截图、Issue、报告与服务配置。
- 项目的 Apache 许可仅适用于项目代码，不自动覆盖用户导入的资源。
