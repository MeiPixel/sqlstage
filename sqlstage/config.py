from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path


@dataclass
class ModelConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_KEY", ""))
    model: str = "deepseek-v4-flash"
    temperature: float = 0.0
    max_tokens: int = 2048
    thinking: str = "disabled"
    timeout_seconds: int = 120
    # 上游抖动时的重试次数（不含首次调用）。0 是不重试。
    # 只对连接失败、超时、429、5xx 生效，4xx 请求本身的问题不重试。
    max_retries: int = 2
    embedding_model: str = ""
    extra_body: dict = field(default_factory=dict)


@dataclass
class TrickConfig:
    table_recall: str = "all_schema"
    table_top_k: int = 3
    join_expansion: bool = True
    difficulty_routing: bool = False
    camel_case_split: bool = True
    llm_validate_names: bool = True
    llm_lexical_fallback: bool = False
    table_descriptions: dict = field(default_factory=dict)
    column_recall: str = "all_columns"
    column_top_k: int = 30
    column_description_recall: bool = False
    column_description_threshold: int = 30
    sample_rows: int = 3
    field_description_count: int = 3
    keep_primary_keys: bool = True
    keep_foreign_keys: bool = True
    entity_sniffing: bool = False
    distinct_probe: bool = False
    fewshot_enabled: bool = False
    fewshot_method: str = "fixed"
    fewshot_top_k: int = 3
    fewshot_path: str = ""
    fewshot_same_table: bool = True
    fewshot_exclude_self: bool = True
    fewshot_difficulty: bool = False
    fewshot_dedup: bool = False
    rag_enabled: bool = False
    guide_enabled: bool = False
    guide_path: str = ""
    guide_text: str = ""
    guide_max_chars: int = 4000
    candidate_count: int = 1
    candidate_variants: list[str] = field(default_factory=lambda: ["direct", "join_first", "minimal"])
    candidate_temperatures: list[float] = field(default_factory=lambda: [0.0, 0.4, 0.8])
    candidate_vote: bool = True
    candidate_llm_selector: bool = False
    candidate_execution_check: bool = False
    execution_feedback: bool = False
    repair_rounds: int = 0
    empty_result_refine: bool = False
    semantic_refine: bool = False
    question_rewrite: bool = False
    prompt_max_chars: int = 30000
    dialect_conversion: bool = False
    deterministic_repair: bool = False
    execution_check: bool = True
    # 执行护栏：模型生成的 SQL 不受控，全表扫描会打满内存或拖死数据库。
    # max_rows 为返回行数上限（超出即报错，不做静默截断，避免给出错误答案却不自知），
    # statement_timeout_ms 为数据库侧语句超时，三个适配器分别落到各自的机制上。
    max_rows: int = 5000
    statement_timeout_ms: int = 30000


@dataclass
class OrmConfig:
    enabled: bool = False
    link_rules: list[dict] = field(default_factory=list)
    value_maps: dict = field(default_factory=dict)
    unit_rules: dict = field(default_factory=dict)
    required_fields: dict = field(default_factory=dict)
    mutex_fields: list[list[str]] = field(default_factory=list)
    derived_fields: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    join_rules: list[dict] = field(default_factory=list)
    table_prefix: str = ""
    quote_order_by: bool = False
    field_match: dict = field(default_factory=dict)
    numeric_cast: list[str] = field(default_factory=list)
    field_alias: dict = field(default_factory=dict)
    vector_fields: dict = field(default_factory=dict)


@dataclass
class StrategyConfig:
    name: str = "default"
    model: ModelConfig = field(default_factory=ModelConfig)
    tricks: TrickConfig = field(default_factory=TrickConfig)
    orm: OrmConfig = field(default_factory=OrmConfig)

    def save(self, path):
        Path(path).write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=raw["name"],
            model=ModelConfig(**raw["model"]),
            tricks=TrickConfig(**raw["tricks"]),
            orm=OrmConfig(**raw["orm"]),
        )
