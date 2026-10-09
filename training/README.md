# 训练链路

SqlStage 的推理侧不训练模型，但它的**组件化架构本身就是数据合成器**：每个 Trick 都能单独
开关。复用同一条提示构建链路可减少差异，但本目录不会自动保证训练与推理一致；
还要对齐模型聊天模板、Schema、召回、Few-shot 排除、业务规则和上下文截断策略。

本目录提供 LoRA SFT 与 DPO 训练脚本，以及配套的数据切分、偏好对构建与保留集评测工具。
训练入口主要面向 Qwen SQL LoRA；更换模型须验证模板、目标模块、位置编码和训练器兼容性。
此公开包不含训练数据、权重或 adapter，也没有在本次整理中重新运行 GPU 训练。

## 安装

```bash
python -m pip install -e ".[train]"
```

需要一块显存足够的 GPU。显存取决于序列长度、精度、优化器和注意力实现，不能仅凭 4B
参数量承诺 24GB 足够；默认最长序列较大，应先按自己的硬件缩短并验证。训练扩展依赖
给出了版本下限，不是所有最新版组合的兼容保证。只做工作台推理不需安装这一组依赖。

## 数据格式

**SFT**：JSONL，每行一个 `messages` 数组，最后一条必须是 `assistant`，且只允许单轮监督
（前面的消息里不能出现 `assistant`）。

```json
{"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "问题与 Schema"}, {"role": "assistant", "content": "SELECT ..."}]}
```

**DPO**：JSONL，每行 `messages`（不含回答）+ `chosen` / `rejected` 两个 SQL。

```json
{"messages": [{"role": "user", "content": "..."}], "chosen": "SELECT ...", "rejected": "SELEC ..."}
```

训练脚本会断言「训练与推理前缀 token 一致」且回答非空，并对超过 `--max-length` 的样本
直接报错而不是静默截断——静默截断会让模型学到半截句子。

## 流程

### 一、切分验证集

外部评测集必须与训练集**完全隔离**，内部验证集从训练集里固定切出（默认按题目哈希切，
同一 seed 结果可复现）：

```bash
python training/split_sft_validation.py --input data/all.jsonl --output data/split --validation-count 75
```

产出 `data/split/train.jsonl` 与 `data/split/validation.jsonl`。

### 二、LoRA SFT

```bash
python training/train_qwen3_sft.py \
  --model Qwen/Qwen3-4B \
  --data data/split \
  --output runs/sft_v1 \
  --epochs 2 --learning-rate 1e-4 --lora-rank 16
```

按验证集 loss 保存最优 checkpoint 到 `output/best`，同时导出可直接部署的合并模型。
加 `--skip-merged` 只导出 adapter，能省下几份权重占用的磁盘。
`--init-adapter` 可以从已有 LoRA 热启动，`--resume` 从 checkpoint 恢复（含优化器状态）。

### 三、构建 DPO 偏好对

把模型在保留集上的**真实错题**转成偏好对——`chosen` 是标准 SQL，`rejected` 是模型错答：

```bash
python training/build_sql_dpo_pairs.py \
  --source data/holdout.jsonl \
  --predictions runs/sft_v1/predictions.jsonl \
  --output data/dpo.jsonl
```

`--exclude-source` 用来把某些源数据从偏好对里剔除（比如已确认标准答案本身有问题的题目）。

### 四、DPO 继续训练

```bash
python training/train_sql_dpo.py \
  --model Qwen/Qwen3-4B \
  --init-adapter runs/sft_v1/best \
  --data data/dpo_split \
  --output runs/dpo_v1 \
  --learning-rate 1e-6 --beta 0.05
```

`--rpo-alpha` 可启用 RPO（额外惩罚被拒绝回答的概率），`--loss-type` 支持 `sigmoid` /
`robust` / `ipo`。多个 checkpoint 里挑最优用 `select_best_dpo_adapter.py`。

### 五、保留集评测

先起一个 vLLM 服务，再用脚本打保留集：

```bash
vllm serve <合并模型路径> --port 6006
python training/eval_sft_holdout.py --input data/holdout.jsonl --output runs/holdout.jsonl --model <模型名>
```

逐题结果写 JSONL，旁边自动生成 `.summary.json`。注意这里的口径是**严格 AST 规范序列化匹配**，
不等同于官方 Exact Match——大小写、引号风格不同都会判错，评测时务必看清口径再下结论。

## 数据质量关

合成数据容易把噪声一起训进去。本链路在三个位置设卡，建议自建数据时同样照做：

1. **SQL 解析校验**：每条标准 SQL 必须能被 sqlglot 解析为单一查询语句；
2. **空库执行校验**：在空 schema 上 `EXPLAIN` 通过，确认引用的表与列真实存在；
3. **模板指纹隔离**：按 SQL 模板指纹切分训练与保留集，防止同一模板的题同时出现在两侧。

## 脚本一览

| 脚本 | 作用 |
|---|---|
| `split_sft_validation.py` | 从训练集固定切出内部验证集 |
| `train_qwen3_sft.py` | LoRA SFT，导出最优 adapter 与合并模型 |
| `build_sql_dpo_pairs.py` | 真实错题转 DPO 偏好对 |
| `train_sql_dpo.py` | DPO / RPO 继续训练 |
| `select_best_dpo_adapter.py` | 按验证集 loss 选最优 DPO checkpoint |
| `eval_sft_holdout.py` | 保留集评测（严格 AST 口径） |
