from pathlib import Path


def load_guide(config):
    if config.guide_text:
        return config.guide_text
    return Path(config.guide_path).read_text(encoding="utf-8") if config.guide_path else ""


def render_guide_context(guide, max_chars):
    return "" if not guide else "\n\n## Database Guide\n" + guide[:max_chars]


def generate_database_guide(schema_text, model):
    prompt = (
        "根据数据库结构生成可执行的Database Guide，必须包含六节："
        "数据库用途、核心表、字段语义、JOIN路径、常用过滤、易错点。"
        "所有表和字段必须来自Schema。\n\n" + schema_text
    )
    return model.complete([{"role": "user", "content": prompt}])
