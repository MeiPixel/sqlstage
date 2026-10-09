"""Qwen3 单轮 SQL SFT：只训练回答，保存最佳 LoRA 与可直接部署的合并模型。"""
import argparse
import json
from functools import partial
from pathlib import Path

import torch
import torch.nn.functional as F


def encode_example(record, tokenizer, max_length):
    messages = record["messages"]
    assert messages[-1]["role"] == "assistant", "最后一条必须是 assistant"
    assert all(m["role"] != "assistant" for m in messages[:-1]), "当前数据要求单轮监督"
    prompt = tokenizer.apply_chat_template(messages[:-1], tokenize=True,
        add_generation_prompt=True, enable_thinking=False)
    complete = tokenizer.apply_chat_template(messages, tokenize=True,
        add_generation_prompt=False, enable_thinking=False)
    assert complete[:len(prompt)] == prompt, "训练与推理前缀 token 不一致"
    assert len(prompt) < len(complete), "没有可监督的回答 token"
    assert len(complete) <= max_length, f"样本长度 {len(complete)} 超过 {max_length}；禁止截断"
    return {"input_ids": complete,
            "labels": [-100] * len(prompt) + complete[len(prompt):]}


def collate_examples(examples, pad_token_id):
    width = max(len(x["input_ids"]) for x in examples)
    ids, labels, masks = [], [], []
    for example in examples:
        size = len(example["input_ids"])
        ids.append(example["input_ids"] + [pad_token_id] * (width - size))
        labels.append(example["labels"] + [-100] * (width - size))
        masks.append([1] * size + [0] * (width - size))
    return {"input_ids": torch.tensor(ids), "labels": torch.tensor(labels),
            "attention_mask": torch.tensor(masks)}


def assistant_loss(model, inputs):
    labels = inputs["labels"]
    positions = torch.nonzero((labels[:, 1:] != -100).any(dim=0), as_tuple=True)[0]
    assert positions.numel() > 0, "批次没有监督 token"
    outputs = model(input_ids=inputs["input_ids"], attention_mask=inputs["attention_mask"],
                    logits_to_keep=positions, use_cache=False)
    targets = labels[:, 1:][:, positions]
    loss = F.cross_entropy(outputs.logits.float().reshape(-1, outputs.logits.shape[-1]),
                           targets.reshape(-1), ignore_index=-100)
    return loss, outputs


def checkpoint_schedule(checkpoint_steps):
    assert checkpoint_steps > 0
    return {"eval_strategy": "steps", "save_strategy": "steps",
            "eval_steps": checkpoint_steps, "save_steps": checkpoint_steps}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--data", required=True, help="包含 train.jsonl 与 validation.jsonl 的目录")
    parser.add_argument("--output", required=True, help="必须为新目录；保留全部 checkpoint")
    parser.add_argument("--epochs", type=float, default=2)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--max-length", type=int, default=65536)
    parser.add_argument("--rope-factor", type=float, default=2.0)
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--checkpoint-steps", type=int, default=20)
    parser.add_argument("--skip-merged", action="store_true", help="仅导出LoRA adapter，节省部署不使用merged模型时的磁盘")
    parser.add_argument("--init-adapter", default=None, help="从已有LoRA权重热启动继续训练")
    parser.add_argument("--resume", default=None, help="从指定checkpoint恢复训练（含优化器/调度器状态）")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attn-implementation", choices=["sdpa", "flash_attention_2"], default="sdpa")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments, set_seed
    from peft import LoraConfig, PeftModel, get_peft_model

    output = Path(args.output)
    if args.resume:
        assert Path(args.resume).exists(), f"resume路径不存在：{args.resume}"
        output.mkdir(parents=True, exist_ok=True)
    else:
        assert not output.exists(), f"输出目录已存在，拒绝覆盖：{output}"
    assert torch.cuda.is_available(), "训练要求 CUDA GPU"
    set_seed(args.seed)
    torch.set_num_threads(8)
    if not args.resume:
        output.mkdir(parents=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    tokenizer.padding_side = "right"
    assert tokenizer.pad_token_id is not None
    datasets, statistics = {}, {}
    for split in ("train", "validation"):
        with (Path(args.data) / f"{split}.jsonl").open(encoding="utf-8") as stream:
            datasets[split] = [encode_example(json.loads(line), tokenizer, args.max_length) for line in stream]
        lengths = sorted(len(x["input_ids"]) for x in datasets[split])
        assert lengths, f"{split} 为空"
        statistics[split] = {"count": len(lengths), "min": lengths[0], "max": lengths[-1],
            "mean": sum(lengths) / len(lengths), "p50": lengths[len(lengths) // 2],
            "p95": lengths[int((len(lengths) - 1) * .95)],
            "supervised_tokens": sum(sum(t != -100 for t in x["labels"]) for x in datasets[split])}
        print(json.dumps({"token_lengths": {split: statistics[split]}}, ensure_ascii=False), flush=True)
    (output / "training_config.json").write_text(json.dumps({"args": vars(args), "tokens": statistics},
        ensure_ascii=False, indent=2), encoding="utf-8")

    config = AutoConfig.from_pretrained(args.model, local_files_only=True)
    if args.rope_factor > 1:
        config.rope_scaling = {"rope_type": "yarn", "factor": args.rope_factor,
                               "original_max_position_embeddings": 32768}
        config.max_position_embeddings = int(32768 * args.rope_factor)
    config.use_cache = False
    model = AutoModelForCausalLM.from_pretrained(args.model, config=config, local_files_only=True,
        torch_dtype=torch.bfloat16, attn_implementation=args.attn_implementation)
    if args.init_adapter:
        model = PeftModel.from_pretrained(model, args.init_adapter, is_trainable=True)
    else:
        model = get_peft_model(model, LoraConfig(task_type="CAUSAL_LM", r=args.lora_rank,
            lora_alpha=2 * args.lora_rank, lora_dropout=.05, bias="none",
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    class AssistantTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            loss, result = assistant_loss(model, inputs)
            return (loss, result) if return_outputs else loss

    training_args = TrainingArguments(output_dir=str(output / "checkpoints"),
        num_train_epochs=args.epochs, max_steps=args.max_steps,
        per_device_train_batch_size=args.batch_size, per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation, learning_rate=args.learning_rate,
        warmup_ratio=.05, lr_scheduler_type="cosine", weight_decay=.01,
        bf16=True, tf32=True, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False}, optim="adamw_torch",
        load_best_model_at_end=True, **checkpoint_schedule(args.checkpoint_steps),
        metric_for_best_model="eval_loss", greater_is_better=False,
        logging_steps=1, prediction_loss_only=True, report_to="none", seed=args.seed,
        remove_unused_columns=False, dataloader_num_workers=args.workers, group_by_length=True,
        dataloader_pin_memory=True, label_names=["labels"])
    trainer = AssistantTrainer(model=model, args=training_args, train_dataset=datasets["train"],
        eval_dataset=datasets["validation"], data_collator=partial(collate_examples,
        pad_token_id=tokenizer.pad_token_id), processing_class=tokenizer)
    # 损失已按当前微批次监督 token 求均值，由 Trainer 做梯度累积缩放。
    trainer.model_accepts_loss_kwargs = False
    baseline = trainer.evaluate(metric_key_prefix="before")
    print(json.dumps({"before_training": baseline}), flush=True)
    result = trainer.train(resume_from_checkpoint=args.resume)
    metrics = {"before": baseline, "train": result.metrics, "after": trainer.evaluate(),
               "best_checkpoint": trainer.state.best_model_checkpoint,
               "peak_cuda_allocated_gib": torch.cuda.max_memory_allocated() / 1024 ** 3}
    trainer.model.save_pretrained(output / "adapter", safe_serialization=True)
    tokenizer.save_pretrained(output / "adapter")
    if not args.skip_merged:
        merged = trainer.model.merge_and_unload()
        merged.config.use_cache = True
        merged.save_pretrained(output / "merged", safe_serialization=True)
        tokenizer.save_pretrained(output / "merged")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"complete": str(output), "metrics": metrics}, ensure_ascii=False), flush=True)
