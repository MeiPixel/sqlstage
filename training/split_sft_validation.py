"""从SFT训练集固定切出内部验证集；外部评测集保持完全隔离。"""
import argparse
import hashlib
import json
from pathlib import Path


def split_records(records, validation_count, seed):
    assert 0 < validation_count < len(records)
    ranked = sorted(range(len(records)), key=lambda index: hashlib.sha256(
        f"{seed}:{index}".encode()).digest())
    validation_indexes = set(ranked[:validation_count])
    train = [row for index, row in enumerate(records) if index not in validation_indexes]
    validation = [row for index, row in enumerate(records) if index in validation_indexes]
    return train, validation


def write_jsonl(path, records):
    with path.open("x", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--validation-count", type=int, default=75)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    records = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines()]
    train, validation = split_records(records, args.validation_count, args.seed)
    output = Path(args.output)
    assert not output.exists(), f"输出目录已存在：{output}"
    output.mkdir(parents=True)
    write_jsonl(output / "train.jsonl", train)
    write_jsonl(output / "validation.jsonl", validation)
    print(json.dumps({"train": len(train), "validation": len(validation), "seed": args.seed}))
