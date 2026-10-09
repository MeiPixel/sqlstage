"""完整保留策略必需内容，按完整行裁剪可选样例并明确报告。"""
def pack_prompt_context(core, full_schema, guide, fewshot, max_chars):
    assert full_schema.startswith(core), "渲染结果的核心结构不匹配"
    required = len(core) + len(guide) + len(fewshot)
    if required > max_chars:
        raise ValueError(f"完整Schema、Guide和Few-shot需要{required}字符，超过提示预算{max_chars}；请减少召回范围或附加上下文")
    remaining = max_chars - required
    extra = full_schema[len(core):]
    selected = []
    for line in extra.splitlines(keepends=True):
        if len(line) <= remaining:
            selected.append(line)
            remaining -= len(line)
    kept = "".join(selected)
    return core + kept, guide, fewshot, {"core_chars": len(core), "required_chars": required,
                                       "optional_chars_kept": len(kept), "optional_chars_dropped": len(extra) - len(kept)}

