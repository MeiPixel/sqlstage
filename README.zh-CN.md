# SQLStage

**在你自己的数据库上，构建、评测和优化 Text2SQL 工作流。**

[English](README.md) · [文档](docs/ARCHITECTURE.md) · [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md)

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![License Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue)
![Status Beta](https://img.shields.io/badge/Status-Beta-orange)

SQLStage 是面向开发者的开源 **Text2SQL 工作台与 Python 库**。连接自己的数据库和大模型服务后，你可以生成 SQL、查看生成过程，并在自己的测试集上比较表召回、上下文组织、Few-shot、候选生成与业务规则的效果。

它关注的是完整的工程闭环：**生成 → 观察 → 评测 → 调优 → 部署**。首页、自动寻优和预测服务共用同一套引擎。

## 为什么选择 SQLStage？

做 Text2SQL 不只是选一个模型。你还需要决定：给模型多少表和字段、放哪些示例、数据库说明有没有帮助，以及追问时如何保留已有条件和业务口径。SQLStage 把这些决策变成可以配置、查看和对比的工作流。

- **看得见生成过程。** 查看召回的表和字段、实际执行的组件、候选 SQL、Token 消耗，以及发送模型请求前和模型调用阶段的耗时。
- **用自己的数据选择策略。** 对召回方法、Few-shot 数量、Guide、字段说明和候选数做有预算的网格搜索，查看逐题结果，再将选定配置应用到首页。
- **把查询保留为可继续操作的对象。** 支持直接生成 SQL，也支持用查询对象持续过滤、复用模板、编排 JOIN，并显式约束业务规则。
- **按需要接入。** 使用 React 工作台、直接调用 Python 引擎，或导出配置启动独立 FastAPI 预测服务。

如果你在开发可观测的 Text2SQL 原型、做策略消融，或者搭建有业务规则的数据库问答助手，SQLStage 值得尝试。它尤其适合回答：**“在我的数据库上，哪一种组合更合适？”**

## 核心能力

| 方向 | 可配置的能力 |
| --- | --- |
| 数据库与模型 | SQLite、MySQL、PostgreSQL；OpenAI 兼容对话接口；模型、URL、温度、输出上限与服务方支持的思考设置 |
| 表与字段召回 | 全 Schema、关键词、BM25、字段级 BM25、Embedding、混合检索、大模型选表；字段选择与外键 JOIN 扩展 |
| 上下文组织 | Few-shot 检索、数据库 Guide、字段说明、样例值、实体/取值探测与上下文预算 |
| SQL 生成 | 直接 SQL 或 ORM 计划；Pick-N 候选、投票、大模型裁判、执行反馈与可选修复步骤 |
| 评测与搜索 | 固定种子抽样、AST 与执行结果对比、逐题错误、SQLite 搜索缓存、组合排序和策略复用 |
| 业务规则 | 字段别名、值映射、单位、匹配方式、必返字段、条件联动、JOIN、派生字段和指标 |
| 服务与训练 | 服务配置导出、独立预测 API；可选 LoRA SFT/DPO 数据准备、训练与评测脚本 |

Pick-N 是“同一题生成 N 个 SQL 候选，再进行选择”；Guide 是你提供的数据库说明，并非项目自带的正确答案来源。

Embedding 需要能提供向量的模型接口；思考参数也需要对应服务支持。当前工作台与详细文档以中文为主，同时提供完整英文 README。

## 快速开始

**需要：** Python 3.10+、Node.js 22.12+ 或 20.19+、可访问的数据库与模型 API。使用远程模型接口时，不需要本机 GPU。

以下命令在仓库根目录执行。`requirements-lock.txt` 是 Python 3.10 离线验收使用的环境快照；`requirements.txt` 提供依赖范围。GPU 训练依赖单独安装。

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
(cd frontend && npm ci)

bash run.sh check
bash run.sh backend
```

在仓库根目录另开一个终端：

```bash
bash run.sh frontend
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
cd frontend
npm ci
cd ..

.\run.bat check
.\run.bat backend
```

另开一个终端：

```powershell
.\run.bat frontend
```

打开 **http://127.0.0.1:5173**。后端端口为 `8020`，交互式 API 文档位于 **http://127.0.0.1:8020/docs**。两个进程都在前台运行，按 Ctrl+C 停止。

### 第一次生成 SQL

1. 在配置页添加自己的数据库连接，推荐使用只读账号。SQLite 路径指向后端所在机器的文件。
2. 配置模型 URL、名称和 API Key。默认使用 `deepseek-v4-flash`，关闭思考；`ModelConfig` 可读取 `DEEPSEEK_API_KEY`。
3. 在生成页输入问题，检查 SQL、召回内容、Token 和耗时。
4. 在数据集页导入带标准 SQL 的题目；到自动寻优页选择该数据集及其对应的数据库连接。

本项目是**仅代码发布**：不包含比赛或第三方数据库、题库、答案、Guide、训练数据和模型权重。内置示例列表为空是正常状态。

`.env.example` 只是变量说明，程序**不会自动加载 `.env`**。请在终端设置环境变量，或在界面填写配置；仅设置 `OPENAI_MODEL` 不会自动切换模型。

## 作为 Python 库使用

只需要 Python 引擎时，无需 Node.js 或 FastAPI。激活虚拟环境后，从本地源码安装：

```bash
python -m pip install -e .
```

假设你的 SQLite 数据库中有 `products(name, price, ...)` 表：

```python
from sqlstage import ModelConfig, StrategyConfig, Text2SQLEngine, TrickConfig

strategy = StrategyConfig(
    model=ModelConfig(),  # 读取 DEEPSEEK_API_KEY，默认关闭思考。
    tricks=TrickConfig(execution_check=False, sample_rows=0),
)
engine = Text2SQLEngine("your.sqlite", strategy)

result = engine.generate("找出价格低于50的商品，返回名称和价格。")
print(result["sql"])
print(result["tables"], result["columns"])
print(result["trace"]["token_usage"])
print(result["trace"]["before_model_s"], result["trace"]["model_s"])
```

`execution_check=False` 表示不执行生成的结果查询，读取 Schema 仍需连接数据库。示例中的 `sample_rows=0` 表示不往提示词加入抽样数据。Token 来自服务方返回的 usage；如果没有返回，就不伪造估算值。

wheel 提供 Python 模块，不包含 React 前端或数据资源。完整工作台请使用源码目录。

## 用网格搜索选择策略

先建立一个可用基线，再搜索一个或少数几个决策：

- 全量 Schema 还是表召回；Top-K 取多少，是否扩展 JOIN。
- Few-shot 放几条，用什么检索方式，是否限定同表或去重。
- Guide 加不加，字段说明给几条，样例值放多少。
- 单次生成还是 Pick-3/Pick-5，是否投票或使用大模型裁判。
- 直接 SQL 还是 ORM 计划，是否加入执行与修正步骤。

例如，下面的搜索空间在有效策略去重前有 **12 种组合**：

```python
search_space = {
    "tricks.fewshot_top_k": [0, 3, 5],
    "tricks.guide_enabled": [False, True],
    "tricks.field_description_count": [0, 3],
}
```

如果每题只生成一个候选、每组固定抽 3 题，就是 36 次基础 SQL 生成；额外的召回、裁判或修复还需要预算。请提供自己的 Few-shot 池、Guide 和字段说明，否则这些开关并不代表有效的增强。

工作台默认 **4 组 × 固定 10 题**，每轮所有模型请求共享 **50 次硬上限**。大模型召回、Embedding、候选生成、裁判、修复和重试都计入。先预览组合与预算，超出时减少抽样题数或搜索维度。

搜索与首页使用同一个生成引擎，继承未搜索的配置；抽中的题会从 Few-shot 池排除。SQLite 在策略、数据与代码指纹一致时可复用缓存。排序优先考虑 AST 准确率，再比较错误数与耗时；报告同时保留执行结果匹配和逐题 Trace。

排行榜是验证集上的结果，不代表泛化保证。选好配置后，请用未参与搜索的保留集复核。详见[搜索说明](docs/SEARCH.md)。

## ORM：让查询成为可继续操作的对象

SQLStage 的 ORM 是**面向 SQL 生成的查询计划与对象层**，不是 Django ORM 或 SQLAlchemy 这类应用 ORM 的替代品。

它可以让追问保留之前的条件，而不是从一个全新的 SQL 字符串开始：

```python
from sqlstage.query_object import table

query = (
    table("products")
    .filter(price__lt=50)
    .select("name", "price")
    .order_by("-price")
)
original = query

query = query.filter(brand="Acme")
print(query.sql())
assert "Acme" not in original.sql()  # 原查询对象不被修改。
```

生成的 SQL：

```sql
SELECT name, price FROM products
WHERE price < 50 AND brand = 'Acme'
ORDER BY price DESC
```

同一层还支持 JOIN、分组、LIMIT、参数模板和业务规则。例如，价格的单位、必须返回的字段、允许使用的关联关系，可以写在配置里，不必每次向模型重新解释。

上面的 Python 对象操作在本地执行。**自然语言追问**仍需模型生成增量操作，仍然消耗 Token；只有绑定已有模板的参数是纯本地步骤。模型输出的操作通过 AST 白名单解释，不执行任意 `eval`/`exec`。当前拼写纠正使用字符 n-gram 与编辑距离，并非预训练语义向量。详见[ORM 说明](docs/ORM.md)。

## 从工作台到预测服务

在界面导出服务配置后，使用虚拟环境启动独立服务：

```bash
python -m server.serve --config sqlstage-services.yaml --strict --port 8021
```

只导出一个服务时，通过 `POST /v1/predict` 请求，并携带已配置的 Bearer Token：

```http
POST /v1/predict
Authorization: Bearer <your-service-token>
Content-Type: application/json

{"question": "列出销售额最高的十个商品。"}
```

服务支持配置化策略、数据库检查、鉴权和并发上限；只暴露预测相关接口，不挂载工作台的连接、数据集和搜索管理 API。模型密钥、数据库密码和访问令牌通过导出配置中的环境变量占位符设置。详见[部署说明](docs/DEPLOYMENT.md)。

## 项目状态与边界

SQLStage 当前处于 **Beta**。仅代码版本已在 Windows / Python 3.10.11 上通过 13 个离线测试脚本、前端类型与构建检查、本地 HTTP/界面检查，以及独立安装包导入验证。CI 配置覆盖 Windows/Linux 后端测试、前端构建和打包。

这些检查验证的是工程行为，**不是模型准确率**。公开仓库不宣称某个准确率或某种策略在所有数据库上最优。GPU 训练脚本为可选工具，需要独立验证训练环境与模型兼容性。

只读检查、危险函数拦截、行数限制和超时都是护栏，**不是安全沙盒**。请使用最小权限账号，敏感场景应审查生成的 SQL。Schema、示例与召回内容可能发送给模型服务；本地 `runs/studio.db` 可能保存凭据。不要公开运行目录和导出的服务配置。详见[安全说明](SECURITY.md)。

## 开发与文档

激活虚拟环境后：

```bash
python tools/run_tests.py --verbose
(cd frontend && npm run build)
python tools/check_release.py
```

默认测试使用临时 SQLite 与模型替身，不调用付费模型，不连接外部 MySQL/PostgreSQL，也不使用你的工作台 Store。外部适配器测试必须显式传入 `--integration`，会重建专用测试库，禁止在生产实例执行。

| 文档 | 内容 |
| --- | --- |
| [架构](docs/ARCHITECTURE.md) | 生成链路、配置、数据库适配器与执行护栏 |
| [自带数据接入](docs/DATA.md) | 数据库连接、标准题目、Few-shot 与 Guide |
| [自动寻优](docs/SEARCH.md) | 搜索维度、预算、缓存与评测边界 |
| [ORM](docs/ORM.md) | 查询对象、参数模板、业务规则与 Token 口径 |
| [部署](docs/DEPLOYMENT.md) | 服务导出、鉴权与运行 |
| [训练](training/README.md) | 可选 SFT/DPO 工具与训练/推理对齐 |
| [贡献指南](CONTRIBUTING.md) | 开发准备、原生测试与贡献要求 |

欢迎改进文档、召回策略、数据库支持和评测方法。提交新功能时请附带测试，使用合成夹具，不上传私有数据集。

### 目录结构

```text
sqlstage/       Python 引擎、召回、ORM、评测与搜索
server/         工作台 API 与独立预测服务
frontend/       React + TypeScript 工作台
training/       可选训练工具，不含数据或权重
tests/          离线回归脚本与合成夹具
tools/          测试与发布检查入口
docs/           架构与使用说明
```

运行报告、缓存、虚拟环境和归档材料不进入 Git 或源码发布包。

## 许可证

[Apache License 2.0](LICENSE)，另见 [NOTICE](NOTICE)。第三方依赖、模型和用户导入的数据仍遵循各自的许可与访问要求。
