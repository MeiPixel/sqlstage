# 贡献指南

先阅读 [架构](docs/ARCHITECTURE.md)。修改功能前检索已有模块，优先复用；尽量保留已验证的 Prompt，不将 UI 逻辑塞进引擎。

## 开发与测试

使用 Python 虚拟环境安装 `requirements.txt`，在 `frontend/` 执行 `npm ci`。后端启动、前端启动与检查命令见根目录 README。

新增核心 Python 函数先用独立脚本验证，包含函数、原生 `assert` 样例和主入口；通过后才集成，永久回归用例放入 `tests/`。不引入测试框架，不通过无意义兜底隐藏错误。

```bash
python tools/run_tests.py --verbose
python tools/run_tests.py retrieval
```

默认入口把各脚本的 Store 隔离到临时目录，使用模型替身，不产生 API 费用。不直接运行依赖工作台 Store 的测试脚本；通过上述入口执行。

MySQL/PG 测试是可选的外部集成测试：

```bash
python tools/run_tests.py adapters --integration
```

该测试会重建 `sqlstage_test`（MySQL）和 `sqlstage_test_pg`（PostgreSQL）。先阅读 `tests/test_adapters.py` 并只连接专用测试实例；**不得使用业务服务或生产账号**。默认套件不运行它。

前端修改后执行 `npm run build`。依赖更新后检查 `npm audit` 与 `python -m pip check`，同时提交 `package-lock.json`。CI 覆盖 Windows/Linux 后端、前端构建和 Python 包构建。

## 提交边界

- 不提交真实数据库、题库、Few-shot、Guide、标准答案、模型、报告或导出的服务配置。
- 不提交 `.env`、Token、API Key、数据库密码、本机路径、SSH 账号或原始 Git 历史归档。
- 测试夹具只使用自己编造的小样本，在临时目录生成；不复制比赛样本。
- 保留问题的完整报错，不把模型失败计成正确或把未执行的测试标成通过。
- PR 中说明变动动机、兼容性影响及实际执行的测试；内部工作日志、整理记录和历史归档不进入源码仓库。

## 准确率实验

固定数据库版本、抽样种子、标准答案和评测口径。报告应注明模型与思考设置、表/字段召回、Few-shot 排除规则、候选数、Token 与耗时。离线模型替身测试不代表模型准确率。不要使用测试集搜到的最佳分数替代独立保留集结果。
