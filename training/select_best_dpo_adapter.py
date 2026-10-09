"""根据各 checkpoint 的 eval_loss 选择 DPO LoRA；完整训练时也比较最终 adapter。"""
import argparse
import json
from pathlib import Path


def checkpoint_eval_losses(run_dir):
    results = []
    for state_path in sorted((Path(run_dir) / "checkpoints").glob("checkpoint-*/trainer_state.json")):
        state = json.loads(state_path.read_text(encoding="utf-8"))
        step = int(state_path.parent.name.rsplit("-", 1)[1])
        losses = [row["eval_loss"] for row in state["log_history"]
                  if row.get("step") == step and "eval_loss" in row]
        if losses:
            results.append((losses[-1], step, state_path.parent))
    assert results, "没有带 eval_loss 的 DPO checkpoint"
    return sorted(results)


def adapter_directory(checkpoint):
    adapter = Path(checkpoint) / "policy"
    assert (adapter / "adapter_config.json").exists(), f"缺少 policy 适配器：{checkpoint}"
    assert (adapter / "adapter_model.safetensors").exists()
    return adapter


def select_best(run_dir):
    loss, step, checkpoint = checkpoint_eval_losses(run_dir)[0]
    return {"eval_loss": loss, "step": step, "checkpoint": str(checkpoint),
            "adapter": str(adapter_directory(checkpoint))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = select_best(args.run)
    target = Path(args.output)
    assert not target.exists(), "输出已存在，拒绝覆盖"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
