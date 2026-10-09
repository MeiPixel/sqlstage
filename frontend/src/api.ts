import { toBackendStrategy } from './orm'
import type {
  ChatMessage, ConnectionParams, ConnectionRecord, DatasetItem, DatasetRecord, GenerateResult,
  GenerationRecord, OrmPreset, PublishRecord, RunItem, RunRecord, SampleDatabase, SampleQuestion,
  ServiceExport, ServiceToken, StrategyConfig, StrategyRecord,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API_BASE + url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    throw new Error(await response.text())
  }
  return (await response.json()) as T
}

const post = <T,>(url: string, body: unknown) =>
  request<T>(url, { method: 'POST', body: JSON.stringify(body) })

export const listConnections = () => request<ConnectionRecord[]>('/api/connections')

export const saveConnection = (payload: { name: string; type: string; params: ConnectionParams }) =>
  post<{ id: number }>('/api/connections', payload)

export const deleteConnection = (id: number) =>
  request<{ deleted: number }>(`/api/connections/${id}`, { method: 'DELETE' })

export const listStrategies = () => request<StrategyRecord[]>('/api/strategies')

export const saveStrategy = (name: string, description: string, config: StrategyConfig) =>
  post<{ id: number }>('/api/strategies', { name, description, config: toBackendStrategy(config) })

export const deleteStrategy = (id: number) =>
  request<{ deleted: number }>(`/api/strategies/${id}`, { method: 'DELETE' })

export const generate = (messages: ChatMessage[], connection: ConnectionRecord, strategy: StrategyConfig) =>
  post<GenerateResult>('/api/generate', {
    messages,
    connection_type: connection.type,
    connection: JSON.parse(connection.params) as ConnectionParams,
    strategy: toBackendStrategy(strategy),
  })

export const listGenerations = () => request<GenerationRecord[]>('/api/generations')

export const guideTables = () => request<string[]>('/api/guide/tables')

export const viewTableGuide = (name: string) =>
  request<{ name: string; text: string }>(`/api/guide/table/${encodeURIComponent(name)}`)

export const generateGuide = (connection: ConnectionRecord, strategy: StrategyConfig) =>
  post<{ text: string }>('/api/guide/generate', {
    connection_type: connection.type,
    connection: JSON.parse(connection.params) as ConnectionParams,
    strategy: toBackendStrategy(strategy),
  })

export const listJudges = () => request<{ value: string; label: string }[]>('/api/judges')

export const listDatasets = () => request<DatasetRecord[]>('/api/datasets')

export const listDatasetItems = (id: number) => request<DatasetItem[]>(`/api/datasets/${id}/items`)

export const saveDataset = (name: string, description: string, items: DatasetItem[]) =>
  post<{ id: number }>('/api/datasets', { name, description, items })

export const deleteDataset = (id: number) =>
  request<{ deleted: number }>(`/api/datasets/${id}`, { method: 'DELETE' })

export const generateItems = (connection: ConnectionRecord, strategy: StrategyConfig, payload: {
  instruction: string; count: number; easy: number; medium: number; hard: number
}) => post<{ items: DatasetItem[] }>('/api/datasets/generate', {
  connection_type: connection.type,
  connection: JSON.parse(connection.params) as ConnectionParams,
  strategy: toBackendStrategy(strategy),
  ...payload,
})

export const judgeItems = (items: DatasetItem[], judgeName: string, strategy: StrategyConfig) =>
  post<{ items: DatasetItem[] }>('/api/datasets/judge', {
    items, judge_name: judgeName, strategy: toBackendStrategy(strategy),
  })

export const importDataset = (name: string, content: string) =>
  post<{ name: string; items: DatasetItem[] }>('/api/datasets/import', { name, content })

export const evaluate = (datasetId: number, judgeName: string, workers: number,
  connection: ConnectionRecord, strategy: StrategyConfig) =>
  post<{ run_id: number; rows: RunItem[]; summary: Record<string, number> }>('/api/evaluate', {
    dataset_id: datasetId, judge_name: judgeName, workers,
    connection_type: connection.type,
    connection: JSON.parse(connection.params) as ConnectionParams,
    strategy: toBackendStrategy(strategy),
  })

export const listRuns = () => request<RunRecord[]>('/api/runs')

export const getRun = (id: number) => request<{ run: RunRecord; items: RunItem[] }>(`/api/runs/${id}`)

export const searchDatasets = () =>
  request<{ datasets: string[]; headers: string[];
    bindings: Record<string, { database: string; kind: string; sample_name?: string; title?: string }> }>('/api/search/datasets')

interface SearchPayload {
  dataset: string
  seed: number
  dimensions: Record<string, unknown[]>
  strategy?: StrategyConfig
  sample_count?: number
  max_predictions?: number
  use_cache?: boolean
  connection_type?: string
  connection?: ConnectionParams
}

export const searchPreview = (payload: SearchPayload) => post<{
  records: { id: number; query: string; sql: string; evidence?: string }[]; budget: string;
  database: { dialect: string; tables: string[] }; combinations: number; predictions: number; within_budget: boolean
}>('/api/search/preview', { ...payload,
  strategy: payload.strategy ? toBackendStrategy(payload.strategy) : undefined,
})

export const searchStart = (payload: SearchPayload & {
  workers: number; strategy: StrategyConfig
}) => post<{ task_id: string }>('/api/search/start', {
  ...payload, strategy: toBackendStrategy(payload.strategy),
})

export interface SearchCombination {
  index: number
  count: number
  accuracy: number
  latency_s: number
  errors: number
  cached: boolean
  execution_accuracy?: number
  actual_model_requests?: number
  strategy: Record<string, unknown>
  details: { query: string; gold_sql: string; pred_sql: string; correct: boolean; execution_match?: boolean; execution_note?: string; latency_s: number; error?: string; trace: Record<string, unknown> }[]
}

export const searchResults = (taskId: string) => request<SearchCombination[]>(`/api/search/results/${taskId}`)

export const searchStatus = (taskId: string) =>
  request<{ status: string; message: string; table: unknown[][]; csv_path: string | null;
    error: string | null }>(`/api/search/status/${taskId}`)

export const searchCancel = (taskId: string) =>
  post<{ task_id: string; status: string }>(`/api/search/cancel/${taskId}`, {})

export const searchRun = (payload: {
  dataset: string; seed: number; dimensions: Record<string, unknown[]>;
  workers: number; strategy: StrategyConfig
}) => post<{ status: string; table: unknown[][]; csv_path: string }>('/api/search/run', {
  ...payload, strategy: toBackendStrategy(payload.strategy),
})

export const appendDatasetItems = (datasetId: number, items: DatasetItem[]) =>
  post<{ dataset_id: number; added: number; total: number }>(`/api/datasets/${datasetId}/items`, { items })

export const listSamples = () => request<{ databases: SampleDatabase[] }>('/api/samples')

export const sampleQuestions = (name: string) =>
  request<{ questions: SampleQuestion[] }>(`/api/samples/${encodeURIComponent(name)}/questions`)

export const loadSample = (name: string) =>
  post<{ connection: ConnectionRecord; database: SampleDatabase; questions: SampleQuestion[];
    orm_preset: string | null }>(`/api/samples/${encodeURIComponent(name)}/load`, {})

export const configDefaults = () => request<Record<string, unknown>>('/api/config/defaults')

/** 产物下载走带白名单的后端端点，前端读不到服务端磁盘。 */
export const downloadUrl = (path: string) =>
  `${API_BASE}/api/files/download?path=${encodeURIComponent(path)}`

export const tricksMarkdown = () => request<{ markdown: string }>('/api/tricks')

export const listOrmPresets = () => request<OrmPreset[]>('/api/orm/presets')

export const operateObject = (connection: ConnectionRecord, strategy: StrategyConfig, payload: {
  question?: string; plan?: Record<string, unknown>; parameters?: Record<string, unknown>
}) => post<GenerateResult>('/api/orm/object', {
  connection_type: connection.type, connection: JSON.parse(connection.params),
  strategy: toBackendStrategy(strategy), ...payload,
})

export const makeObjectTemplate = (plan: Record<string, unknown>, filter_index: number, parameter: string) =>
  post<{ plan: Record<string, unknown> }>('/api/orm/template', { plan, filter_index, parameter })

export const listPublishes = () => request<PublishRecord[]>('/api/publish')

export const savePublish = (payload: { name: string; strategy_id: number; connection_id: number }) =>
  post<{ id: number }>('/api/publish', payload)

export const deletePublish = (name: string) =>
  request<{ deleted: string }>(`/api/publish/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const newServiceToken = () => post<ServiceToken>('/api/publish/token', {})

/** names 留空表示导出全部已发布的服务，导出的配置会同时容纳多个服务。 */
export const exportServiceConfig = (payload: {
  names: string[]; format: string; inline_secrets: boolean
  tokens: { name: string; token: string }[]
}) => post<ServiceExport>('/api/publish/export', payload)
