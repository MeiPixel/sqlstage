export interface ConnectionRecord {
  id: number
  name: string
  type: string
  params: string
  created_at: string
}

export interface ConnectionParams {
  database: string
  host: string
  port: number
  user: string
  password: string
  mysql_db: string
  read_only: boolean
  pool_size: number
}

export interface ModelConfig {
  provider: string
  base_url: string
  api_key: string
  model: string
  temperature: number
  max_tokens: number
  thinking: string
  timeout_seconds: number
  embedding_model: string
  extra_body: Record<string, unknown>
}

export interface TrickConfig {
  table_recall: string
  table_top_k: number
  join_expansion: boolean
  difficulty_routing: boolean
  camel_case_split: boolean
  llm_validate_names: boolean
  llm_lexical_fallback: boolean
  table_descriptions: Record<string, string>
  column_recall: string
  column_top_k: number
  column_description_recall: boolean
  column_description_threshold: number
  sample_rows: number
  field_description_count: number
  keep_primary_keys: boolean
  keep_foreign_keys: boolean
  entity_sniffing: boolean
  distinct_probe: boolean
  fewshot_enabled: boolean
  fewshot_method: string
  fewshot_top_k: number
  fewshot_path: string
  fewshot_same_table: boolean
  fewshot_exclude_self: boolean
  fewshot_difficulty: boolean
  fewshot_dedup: boolean
  rag_enabled: boolean
  guide_enabled: boolean
  guide_path: string
  guide_text: string
  guide_max_chars: number
  candidate_count: number
  candidate_variants: string[]
  candidate_temperatures: number[]
  candidate_vote: boolean
  candidate_llm_selector: boolean
  candidate_execution_check: boolean
  execution_feedback: boolean
  repair_rounds: number
  empty_result_refine: boolean
  semantic_refine: boolean
  question_rewrite: boolean
  prompt_max_chars: number
  dialect_conversion: boolean
  deterministic_repair: boolean
  execution_check: boolean
}

export interface RuleRow {
  when_field: string
  when_op: string
  when_value: string
  then_field: string
  then_op: string
  then_value: string
}

export interface MappingRow {
  field: string
  source: string
  target: string
}

export interface UnitRow {
  field: string
  unit: string
  factor: string
}

export interface DependencyRow {
  field: string
  required: string
}

export interface NamedRow {
  name: string
  expression: string
}

export interface JoinRow {
  trigger_field: string
  table: string
  on: string
}

export interface MutexRow {
  a: string
  b: string
  c: string
}

export interface FieldMatchRow {
  field: string
  mode: string
}

export interface CastRow {
  field: string
}

export interface AliasRow {
  field: string
  alias: string
}

export interface OrmConfig {
  enabled: boolean
  link_rules: RuleRow[]
  value_maps: MappingRow[]
  unit_rules: UnitRow[]
  required_fields: DependencyRow[]
  mutex_fields: MutexRow[]
  derived_fields: NamedRow[]
  metrics: NamedRow[]
  join_rules: JoinRow[]
  table_prefix: string
  quote_order_by: boolean
  field_match: FieldMatchRow[]
  numeric_cast: CastRow[]
  field_alias: AliasRow[]
  vector_fields: { field: string; distance: string }[]
}

export interface OrmPreset {
  name: string
  description: string
  field_match: Record<string, string>
  value_maps: Record<string, Record<string, string>>
  numeric_cast: string[]
  field_alias: Record<string, string[]>
}

export interface PublishRecord {
  id: number
  name: string
  strategy_id: number
  connection_id: number
  created_at: string
}

/** 服务模式导出的配置文件内容，由前端落成文件。一份配置可以含多个服务。 */
export interface ServiceExport {
  filename: string
  format: string
  content: string
  services: string[]
  warnings: string[]
}

export interface ServiceToken {
  token: string
  sha256: string
}

export interface StrategyConfig {
  name: string
  model: ModelConfig
  tricks: TrickConfig
  orm: OrmConfig
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface GenerateResult {
  sql: string
  rows: Record<string, unknown>[]
  tables: string[]
  columns: Record<string, string[]>
  orm_plan: unknown
  orm_source_plan?: Record<string, unknown>
  orm_code?: string
  raw: string
  latency_s: number
  trace: Record<string, unknown>
}

export interface StrategyRecord {
  id: number
  name: string
  description: string
  config: string
  created_at: string
  updated_at: string
}

export interface DatasetRecord {
  id: number
  name: string
  description: string
  created_at: string
  item_count: number
}

export interface DatasetItem {
  difficulty: string
  query: string
  sql: string
  candidate?: string
  score?: number | null
  judge_reason?: string
}

export interface RunRecord {
  id: number
  kind: string
  dataset_id: number | null
  status: string
  summary: string
  created_at: string
  finished_at: string | null
}

export interface RunItem {
  id: number
  query: string
  gold_sql: string
  pred_sql: string
  score: number
  reason: string
  latency_s: number
}

export interface GenerationRecord {
  id: number
  question: string
  sql: string
  latency_s: number
  created_at: string
}

/** 内置示例库，由 examples/manifest.json 提供。 */
export interface SampleDatabase {
  name: string
  title: string
  description: string
  dialect: string
  file: string
  tables: number
  rows: number
  language: string
  orm_preset?: string
  tags: string[]
  questions: number
  available: boolean
  path: string
}

export interface SampleQuestion {
  question: string
  sql: string
  evidence: string
}
