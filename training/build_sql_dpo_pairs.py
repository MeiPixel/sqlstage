"""将模型真实错题评测结果转换为 SQL DPO 偏好对。"""
import argparse
import json
from pathlib import Path


def record_key(record):
    return json.dumps(record["messages"], ensure_ascii=False, sort_keys=True)


def build_pairs(source_records, prediction_records, excluded_records=()):
    predictions = {row["index"]: row for row in prediction_records}
    assert len(predictions) == len(prediction_records), "预测 index 重复"
    assert set(predictions) == set(range(len(source_records))), "预测与源数据 index 不完整对应"
    excluded = {record_key(row) for row in excluded_records}
    pairs = []
    for index, source in enumerate(source_records):
        if record_key(source) in excluded:
            continue
        assert source["messages"][-1]["role"] == "assistant"
        prompt_messages = source["messages"][:-1]
        chosen = source["messages"][-1]["content"].strip()
        prediction = predictions[index]
        assert prediction["gold_sql"].strip() == chosen, f"第{index}条标准答案不一致"
        if prediction["exact_ast_match"]:
            continue
        rejected = (prediction.get("raw_sql") or "").strip()
        if not rejected:
            continue
        assert rejected != chosen, f"第{index}条标记错误但文本与答案相同"
        pairs.append({"messages": prompt_messages, "chosen": chosen, "rejected": rejected,
                      "source_index": index, "rejected_syntax_valid": prediction["syntax_valid"]})
    assert pairs, "没有可训练的错误偏好对"
    return pairs


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exclude-source", help="从训练偏好对排除的保留集源数据")
    args = parser.parse_args()
    output = Path(args.output)
    assert not output.exists(), "输出已存在，拒绝覆盖"
    excluded = load_jsonl(args.exclude_source) if args.exclude_source else []
    predictions = load_jsonl(args.predictions)
    pairs = build_pairs(load_jsonl(args.source), predictions, excluded)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pairs), encoding="utf-8")
    print(json.dumps({"source": args.source, "excluded_source": args.exclude_source, "count": len(pairs),
                      "empty_rejected_skipped": sum(not (x.get("raw_sql") or "").strip()
                                                    and not x["exact_ast_match"] for x in predictions),
                      "syntax_valid_rejected": sum(x["rejected_syntax_valid"] for x in pairs)},
                     ensure_ascii=False))
