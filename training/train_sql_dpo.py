"""从既有 SQL LoRA 初始化，使用真实错误偏好对继续做 DPO。"""
import argparse
import json
from pathlib import Path

import torch


def preference_objective(loss_type, rpo_alpha):
    assert loss_type in {"sigmoid", "robust", "ipo"}
    assert rpo_alpha >= 0
    return {
        "loss_type": loss_type,
        "rpo_alpha": rpo_alpha if rpo_alpha > 0 else None,
    }


def render_pair(record, tokenizer, max_length):
    assert record["messages"] and record["messages"][-1]["role"] == "user"
    prompt = tokenizer.apply_chat_template(record["messages"], tokenize=False,
        add_generation_prompt=True, enable_thinking=False)
    chosen, rejected = record["chosen"].strip(), record["rejected"].strip()
    assert chosen and rejected and chosen != rejected
    prompt_tokens = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    chosen_tokens = tokenizer(chosen, add_special_tokens=False)["input_ids"]
    rejected_tokens = tokenizer(rejected, add_special_tokens=False)["input_ids"]
    assert max(len(chosen_tokens), len(rejected_tokens)) <= 512, "SQL回答超过512 token"
    return {"prompt": prompt, "chosen": chosen, "rejected": rejected}


def load_pairs(path, tokenizer, max_length):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    assert rows, f"偏好集为空：{path}"
    return [render_pair(row, tokenizer, max_length) for row in rows]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--init-adapter", required=True)
    parser.add_argument("--data", required=True, help="含 train.jsonl 和 validation.jsonl")
    parser.add_argument("--output", required=True)
    parser.add_argument("--learning-rate", type=float, default=1e-6)
    parser.add_argument("--beta", type=float, default=.05)
    parser.add_argument("--loss-type", choices=("sigmoid", "robust", "ipo"), default="sigmoid")
    parser.add_argument("--rpo-alpha", type=float, default=0.0)
    parser.add_argument("--epochs", type=float, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=16384)
    parser.add_argument("--checkpoint-steps", type=int, default=25)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    objective = preference_objective(args.loss_type, args.rpo_alpha)

    from datasets import Dataset
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
    from trl import DPOConfig, DPOTrainer

    output = Path(args.output)
    assert not output.exists(), f"输出目录已存在，拒绝覆盖：{output}"
    assert torch.cuda.is_available(), "DPO训练要求CUDA"
    set_seed(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    tokenizer.padding_side = "left"
    splits = {name: load_pairs(Path(args.data) / f"{name}.jsonl", tokenizer, args.max_length)
              for name in ("train", "validation")}
    output.mkdir(parents=True)
    (output / "training_config.json").write_text(json.dumps({"args": vars(args),
        "counts": {key: len(value) for key, value in splits.items()}}, ensure_ascii=False, indent=2), encoding="utf-8")

    base = AutoModelForCausalLM.from_pretrained(args.model, local_files_only=True,
        torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    model = PeftModel.from_pretrained(base, args.init_adapter,
        adapter_name="reference", is_trainable=False)
    model.load_adapter(args.init_adapter, adapter_name="policy", is_trainable=True)
    model.set_adapter("policy")
    model.print_trainable_parameters()
    config = DPOConfig(output_dir=str(output / "checkpoints"), beta=args.beta,
        loss_type=objective["loss_type"], rpo_alpha=objective["rpo_alpha"],
        model_adapter_name="policy", ref_adapter_name="reference",
        use_logits_to_keep=True,
        max_length=args.max_length, max_prompt_length=args.max_length - 512,
        truncation_mode="keep_end", num_train_epochs=args.epochs,
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation,
        gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        learning_rate=args.learning_rate, warmup_ratio=.05, lr_scheduler_type="cosine",
        bf16=True, tf32=True, optim="adamw_torch", eval_strategy="steps",
        save_strategy="steps", eval_steps=args.checkpoint_steps, save_steps=args.checkpoint_steps,
        logging_steps=1, report_to="none", seed=args.seed, dataloader_num_workers=args.workers,
        remove_unused_columns=False, load_best_model_at_end=True,
        metric_for_best_model="eval_loss", greater_is_better=False)
    trainer = DPOTrainer(model=model, ref_model=None, args=config,
        train_dataset=Dataset.from_list(splits["train"]),
        eval_dataset=Dataset.from_list(splits["validation"]), processing_class=tokenizer)
    before = trainer.evaluate(metric_key_prefix="before")
    result = trainer.train()
    metrics = {"before": before, "train": result.metrics, "after": trainer.evaluate(),
               "peak_cuda_allocated_gib": torch.cuda.max_memory_allocated() / 1024 ** 3}
    model.set_adapter("policy")
    model.save_pretrained(output / "adapter", selected_adapters=["policy"], safe_serialization=True)
    tokenizer.save_pretrained(output / "adapter")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"complete": str(output), "metrics": metrics}, ensure_ascii=False), flush=True)
