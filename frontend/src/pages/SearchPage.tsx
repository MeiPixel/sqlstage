import { useEffect, useRef, useState } from 'react'
import {
  Alert, App, Button, Card, Checkbox, Col, InputNumber, Row, Select, Space, Table, Tag,
  Typography,
} from 'antd'
import { downloadUrl, loadSample, searchCancel, searchDatasets, searchPreview, searchResults, searchStart, searchStatus, type SearchCombination } from '../api'
import { useStudio } from '../StudioContext'
import { fromBackendStrategy } from '../orm'
import type { ConnectionParams, ConnectionRecord } from '../types'

type Dimensions = Record<string, string[]>

interface Field {
  key: string
  label: string
  values: string[]
  desc: string
  expand?: Record<string, string>
}

const DEFAULT_DIMENSIONS: Dimensions = {
  fewshots: ['0', '3'],
  candidates: ['1'],
  guides: ['关闭', '开启'],
  descriptions: ['继承'],
  samples: ['继承'],
  table_ks: ['继承'],
  table_methods: ['继承'],
  joins: ['继承'],
  column_methods: ['继承'],
  column_ks: ['继承'],
  same_tables: ['继承'],
  votes: ['继承'],
  selectors: ['继承'],
  generation_modes: ['直出SQL'],
  camel_splits: ['继承'],
  validates: ['继承'],
  fallbacks: ['继承'],
  desc_recalls: ['继承'],
  dedups: ['继承'],
}

const GROUPS: { title: string; fields: Field[] }[] = [
  {
    title: 'Schema 与召回',
    fields: [
      {
        key: 'table_methods', label: '表召回方式',
        values: ['BM25', 'BM25字段级', '全部表', '大模型选表', '关键词', 'Embedding', 'Hybrid'], desc: '',
        expand: {
          'BM25': '表名与字段名一起做词面打分，不调用模型、最快，表名或字段名含关键词时够用',
          'BM25字段级': '字段级打分并加入低基数枚举取值（如品牌、类型），问题里出现具体值也能命中',
          '全部表': '所有表都交给模型，小库最稳，大库会占满 Prompt 反而降准',
          '大模型选表': '让模型读问题挑表，理解力最强，但每题多一次调用与耗时',
          '关键词': '按名称关键词匹配，不增加模型调用',
          'Embedding': '通过向量服务理解同义词，增加向量请求费用',
          'Hybrid': '融合词面与向量排名，避免只保留词面结果',
        },
      },
      {
        key: 'table_ks', label: '候选表 Top-K', values: ['1', '3', '5', '13'],
        desc: '最多保留几张候选表，数值小速度快，但可能漏掉需要的表',
      },
      {
        key: 'joins', label: '外键 JOIN 扩展', values: ['关闭', '开启'],
        desc: '沿外键自动补上关联表，多表查询建议开启，单表库可关闭',
      },
      {
        key: 'column_methods', label: '字段召回方式',
        values: ['全部字段', 'BM25', '大模型选字段', '关键词', 'Embedding', 'Hybrid'], desc: '',
        expand: {
          '全部字段': '每张候选表保留全部列，字段少时最稳，宽表会很长',
          'BM25': '按问题给字段打分取 Top-K，宽表压缩效果明显',
          '大模型选字段': '让模型挑相关字段，理解语义最好，但每题多一次调用',
          '关键词': '按问题关键词挑选字段，不增加模型调用',
          'Embedding': '语义挑选字段，每张表增加向量请求',
          'Hybrid': '融合词面与语义字段候选',
        },
      },
      {
        key: 'column_ks', label: '字段召回 Top-K', values: ['10', '30', '70'],
        desc: '每表最多保留的字段数，过小会漏掉过滤或排序所需字段',
      },
      {
        key: 'desc_recalls', label: '大表字段压缩', values: ['关闭', '开启'],
        desc: '字段过多时按问题裁剪，减少无关字段对模型的干扰',
      },
    ],
  },
  {
    title: 'Few-shot',
    fields: [
      {
        key: 'fewshots', label: 'Few-shot 数量', values: ['0', '3', '5', '7', '9'],
        desc: '给模型看几道相似示例，数量多更稳但会拉长 Prompt，0 即关闭',
      },
      {
        key: 'same_tables', label: '限定同表', values: ['关闭', '开启'],
        desc: '只取使用相同表的示例，结构稳定但可能错过跨表解法',
      },
      {
        key: 'dedups', label: 'Few-shot 池去重', values: ['关闭', '开启'],
        desc: '去掉池里重复的题目，避免多个示例其实是同一道题',
      },
    ],
  },
  {
    title: '候选与生成',
    fields: [
      {
        key: 'candidates', label: 'SQL 候选数', values: ['1', '3', '5'],
        desc: '每题生成几条候选 SQL，越多越稳，模型调用量成倍增加',
      },
      {
        key: 'guides', label: '数据库 Guide', values: ['关闭', '开启'],
        desc: '是否注入数据库说明书，表名字段名不直观时很有用',
      },
      {
        key: 'generation_modes', label: '生成路径', values: ['直出SQL', 'ORM'], desc: '',
        expand: {
          '直出SQL': '模型直接写 SQL，适合窗口函数、子查询等复杂语法',
          'ORM': '模型出结构化查询计划再编译，可审计、适合规则明确的库',
        },
      },
      {
        key: 'descriptions', label: '每字段说明条数', values: ['0', '1', '3', '5'],
        desc: '给字段附上几条语义说明，帮助模型理解字段含义',
      },
      {
        key: 'samples', label: '每表样例行数', values: ['0', '1', '3'],
        desc: '从当前数据库读取该表的真实样例行，不是每列独立采样；帮助理解日期、单位与枚举写法',
      },
      {
        key: 'votes', label: '规范化投票', values: ['关闭', '开启'],
        desc: '多个候选写出相同 SQL 时优先选它，几乎零额外成本',
      },
      {
        key: 'selectors', label: 'LLM 裁判选候选', values: ['关闭', '开启'],
        desc: '再调一次模型比较候选语义，更准但增加调用与耗时',
      },
    ],
  },
  {
    title: '名称处理',
    fields: [
      {
        key: 'camel_splits', label: 'camelCase 切分', values: ['关闭', '开启'],
        desc: '把驼峰字段名拆成单词，提升中文问题的字段匹配率',
      },
      {
        key: 'validates', label: '召回白名单校验', values: ['关闭', '开启'],
        desc: '过滤模型编造的不存在表名与字段名，防止生成失败',
      },
      {
        key: 'fallbacks', label: '召回词法兜底', values: ['关闭', '开启'],
        desc: '模型召回为空时改用词面匹配兜底，避免零召回',
      },
    ],
  },
]

GROUPS.push({ title: '其他 Trick（默认继承）', fields: [
  ...[
    ['difficulty_routing', '难度路由', '按问题难度调整候选表范围，可能改变召回。'],
    ['keep_primary_keys', '保留主键', '字段裁剪后仍保留主键。'],
    ['keep_foreign_keys', '保留外键', '字段裁剪后保留关联键，防止 JOIN 断链。'],
    ['fewshot_difficulty', '示例难度匹配', '只用相近难度的示例，示例池不足时会减少数量。'],
    ['rag_enabled', 'Guide 片段召回', '从说明书按问题提取相关片段。'],
    ['entity_sniffing', '实体值定位', '查询真实取值，定位问题里的品牌或型号所在字段。'],
    ['distinct_probe', '常见取值探测', '读取字段高频值，有数据库查询开销。'],
    ['question_rewrite', '问题改写', '先改写明确条件，额外一次模型请求。'],
    ['semantic_refine', 'SQL 语义复核', '检查 SQL 是否保留全部条件，额外一次模型请求。'],
    ['execution_feedback', '执行错误修复', 'SQL 报错后调用模型修正，由修复轮数控制。'],
    ['empty_result_refine', '空结果复核', '空结果时再检查值，不允许删除问题条件。'],
    ['candidate_execution_check', '候选执行检查', '用数据库验证候选可执行性。'],
    ['execution_check', '最终 SQL 执行', '在真实绑定库执行最终查询。'],
    ['dialect_conversion', '方言转换', '把模型输出转换到绑定数据库方言。'],
    ['deterministic_repair', '确定性修复', '用已知规则清理格式，不额外调用模型。'],
  ].map(([key, label, desc]) => ({ key: `tricks.${key}`, label, desc, values: ['关闭', '开启'] })),
  { key: 'tricks.fewshot_method', label: '示例选择方法', values: ['固定', '关键词', 'BM25', 'Embedding', 'Hybrid'], desc: '改变示例召回方式；向量方法需要配置 Embedding 服务。' },
  { key: 'tricks.repair_rounds', label: '修复轮数', values: ['0', '1', '2'], desc: '开启执行反馈时最多修复几轮，增加调用预算。' },
  { key: 'tricks.prompt_max_chars', label: '提示字符预算', values: ['15000', '30000', '60000'], desc: '限制上下文长度，不是模型输出 Token 上限。' },
  { key: 'tricks.guide_max_chars', label: 'Guide 字符预算', values: ['1000', '4000', '8000'], desc: '控制说明书占用的上下文。' },
  { key: 'tricks.column_description_threshold', label: '字段压缩阈值', values: ['20', '30', '70'], desc: '超过多少个字段才启动自动裁剪。' },
  { key: 'tricks.candidate_variants', label: '候选提示变体', values: ['direct', 'join_first', 'minimal', 'direct+join_first+minimal'], desc: '加号表示同一组内轮换的提示模板。' },
  { key: 'tricks.candidate_temperatures', label: '候选温度组', values: ['0', '0+0.4+0.8'], desc: '加号表示多个候选轮换的采样温度。' },
] })

for (const group of GROUPS) for (const field of group.fields) {
  field.values = ['继承', ...field.values]
  if (field.expand) field.expand = { '继承': '完整继承首页当前策略，不额外覆盖此参数。', ...field.expand }
}

const defaultDimensions = () => Object.fromEntries(GROUPS.flatMap((group) => group.fields.map((field) =>
  [field.key, DEFAULT_DIMENSIONS[field.key] ?? ['继承']])) ) as Dimensions

interface ValueRow {
  key: string
  field: string
  category: string
  label: string
  categorySpan: number
  labelSpan: number
  value?: string
  values?: string[]
  desc: string
}

const ROWS: ValueRow[] = (() => {
  const rows: ValueRow[] = []
  for (const group of GROUPS) {
    const groupRows: ValueRow[] = []
    for (const field of group.fields) {
      if (field.expand) {
        const entries = Object.entries(field.expand)
        entries.forEach(([value, desc], index) => {
          groupRows.push({
            key: `${field.key}:${value}`, field: field.key, category: group.title,
            label: field.label, categorySpan: 0,
            labelSpan: index === 0 ? entries.length : 0, value, desc,
          })
        })
      } else {
        groupRows.push({
          key: field.key, field: field.key, category: group.title, label: field.label,
          categorySpan: 0, labelSpan: 1, values: field.values, desc: field.desc,
        })
      }
    }
    groupRows[0].categorySpan = groupRows.length
    rows.push(...groupRows)
  }
  return rows
})()

export default function SearchPage() {
  const { message } = App.useApp()
  const { strategy, current, setStrategy, setCurrentId, refreshConnections } = useStudio()
  const [datasets, setDatasets] = useState<string[]>([])
  const [headers, setHeaders] = useState<string[]>([])
  const [dataset, setDataset] = useState('')
  const [seed, setSeed] = useState(42)
  const [dimensions, setDimensions] = useState<Dimensions>(defaultDimensions)
  const [bindings, setBindings] = useState<Record<string, { database: string; kind: string; sample_name?: string; title?: string }>>({})
  const [sampleCount, setSampleCount] = useState(10)
  const [useCache, setUseCache] = useState(true)
  const [boundDatabase, setBoundDatabase] = useState('')
  const [combinations, setCombinations] = useState<SearchCombination[]>([])
  const [runDataset, setRunDataset] = useState('')
  const [runConnection, setRunConnection] = useState<ConnectionRecord | null>(null)
  const [workers, setWorkers] = useState(5)
  const [records, setRecords] = useState<{ id: number; query: string; sql: string }[]>([])
  const [budget, setBudget] = useState('')
  const [ranking, setRanking] = useState<unknown[][]>([])
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [csvPath, setCsvPath] = useState<string | null>(null)
  const [taskId, setTaskId] = useState<string | null>(null)
  const timerRef = useRef<number | null>(null)

  const stopPolling = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  useEffect(() => () => stopPolling(), [])

  useEffect(() => {
    void searchDatasets().then((meta) => {
      setDatasets(meta.datasets)
      setHeaders(meta.headers)
      setBindings(meta.bindings)
      setDataset(meta.datasets[0] ?? '')
    })
  }, [])

  const updateValues = (key: string, values: string[]) => {
    if (values.length === 0) {
      message.warning('每个维度至少要保留一个取值')
      return
    }
    setDimensions({ ...dimensions, [key]: values })
  }

  const toggleValue = (key: string, value: string, checked: boolean) =>
    updateValues(key, checked
      ? [...dimensions[key], value]
      : dimensions[key].filter((item) => item !== value))

  const preview = async () => {
    if (!dataset) return
    const result = await searchPreview({ dataset, seed, dimensions, strategy, sample_count: sampleCount,
      max_predictions: 50, use_cache: useCache, connection_type: current?.type,
      connection: current ? JSON.parse(current.params) as ConnectionParams : undefined })
    setRecords(result.records)
    setBudget(result.budget.replace(/\*\*/g, ''))
    setBoundDatabase(`${result.database.dialect}：${result.database.tables.join('、')}`)
    return result
  }

  const poll = (taskId: string) => {
    stopPolling()
    timerRef.current = window.setInterval(async () => {
      try {
        const snapshot = await searchStatus(taskId)
        if (snapshot.message) setStatus(snapshot.message.replace(/\*\*/g, ''))
        if (snapshot.table?.length) setRanking(snapshot.table)
        if (snapshot.csv_path) setCsvPath(snapshot.csv_path)
        if (snapshot.status === 'done' || snapshot.status === 'stopped') {
          stopPolling()
          setLoading(false)
          setStopping(false)
          if (snapshot.csv_path) setCombinations(await searchResults(taskId))
          message.success(snapshot.status === 'stopped' ? '已停止，保留已完成部分' : '搜索完成')
        } else if (snapshot.status === 'error') {
          stopPolling()
          setLoading(false)
          setStopping(false)
          message.error(snapshot.error ?? '搜索失败')
        }
      } catch (error) {
        stopPolling()
        setLoading(false)
        setStopping(false)
        message.error(String(error))
      }
    }, 1500)
  }

  const run = async () => {
    if (!dataset) {
      message.warning('请先选择测试集')
      return
    }
    setLoading(true)
    setStatus('正在启动…')
    try {
      const checked = await preview()
      if (!checked?.within_budget) {
        message.warning('当前组合超过 50 条 SQL 预算，请减少候选数、搜索取值或每组题数。')
        setLoading(false)
        return
      }
      const { task_id: started } = await searchStart({ dataset, seed, dimensions, workers, strategy,
        sample_count: sampleCount, max_predictions: 50, use_cache: useCache, connection_type: current?.type,
        connection: current ? JSON.parse(current.params) as ConnectionParams : undefined })
      setRunDataset(dataset)
      setRunConnection(current)
      setRanking([])
      setCombinations([])
      setTaskId(started)
      setCsvPath(null)
      poll(started)
    } catch (error) {
      setLoading(false)
      message.error(String(error))
    }
  }

  const cancel = async () => {
    if (!taskId) return
    setStopping(true)
    try {
      await searchCancel(taskId)
      message.info('已请求停止，当前这组跑完即结束')
    } catch (error) {
      setStopping(false)
      message.error(String(error))
    }
  }

  const best = ranking.length ? String(ranking[0][2]) : ''
  const bestLatency = ranking.length ? String(ranking[0][4]) : ''

  const applyBest = async () => {
    const best = combinations[0]
    if (!best || best.errors === best.count) return
    const restored = fromBackendStrategy(best.strategy)
    restored.model.api_key = strategy.model.api_key
    const binding = bindings[runDataset]
    if (binding?.sample_name) {
      const loaded = await loadSample(binding.sample_name)
      await refreshConnections()
      setCurrentId(loaded.connection.id)
    } else if (runConnection) setCurrentId(runConnection.id)
    setStrategy(restored)
    message.success('已把最佳完整策略及对应数据库应用到首页；API Key 沿用当前配置。')
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <div className="page-head">
        <Typography.Title level={4} className="page-title">自动寻优</Typography.Title>
        <Typography.Paragraph className="page-desc">
          测试集绑定对应数据库 → 固定抽样 → 搜索勾选参数，其余完整继承首页策略。默认 4 组，每轮最多 50 条候选 SQL，重复配置优先复用缓存。
        </Typography.Paragraph>
      </div>

      <Card title="① 数据与抽样">
        {datasets.length === 0 && <Alert style={{ marginBottom: 16 }} type="info" showIcon
          title="此版本仅包含代码，不附带数据库和题库。请先在配置页添加数据库连接，再到数据集页导入含标准 SQL 的测试题。" />}
        <Row gutter={[16, 16]} align="bottom">
          <Col xs={24} md={10}>
            <div className="dimension-label">测试集（需带标准 SQL）</div>
            <Select style={{ width: '100%' }} value={dataset} onChange={setDataset} placeholder="先导入自己的测试集"
              options={datasets.map((name) => ({ value: name, label: bindings[name]?.title ?? name }))} />
          </Col>
          <Col xs={12} md={4}>
            <div className="dimension-label">抽样种子</div>
            <InputNumber style={{ width: '100%' }} value={seed}
              onChange={(value) => setSeed(Number(value))} />
          </Col>
          <Col xs={12} md={4}>
            <div className="dimension-label">每组并发题数</div>
            <InputNumber style={{ width: '100%' }} min={1} max={10} value={workers}
              onChange={(value) => setWorkers(Number(value))} />
          </Col>
          <Col xs={24} md={6}>
            <Button block onClick={preview}>抽取 {sampleCount} 题并预览</Button>
          </Col>
        </Row>
        <Space style={{ marginTop: 12 }} wrap>
          <span>每组固定题数</span><InputNumber min={1} max={50} value={sampleCount} onChange={(value) => setSampleCount(Number(value))} />
          <Checkbox checked={useCache} onChange={(event) => setUseCache(event.target.checked)}>复用相同策略缓存（SQLite 数据变更会失效）</Checkbox>
          <Tag>候选 SQL 总预算：50</Tag>
          <Typography.Text type="secondary">绑定：{bindings[dataset]?.database ?? '尚未选择'}{bindings[dataset]?.kind === 'saved' ? ` / ${current?.name ?? '请先选连接'}` : ''}</Typography.Text>
        </Space>
        {boundDatabase && <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>{boundDatabase}</Typography.Paragraph>}
        {budget && <Alert style={{ marginTop: 16 }} type="info" showIcon title={budget} />}
      </Card>

      <Card title="② 搜索维度" extra={
        <Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            勾选的取值会进入笛卡尔积组合
          </Typography.Text>
          <Button type="link" onClick={() => setDimensions(defaultDimensions())}>恢复低成本默认</Button>
        </Space>
      }>
        <Table rowKey="key" dataSource={ROWS} pagination={false} size="small"
          columns={[
            {
              title: '类别', dataIndex: 'category', width: 120, align: 'center',
              onCell: (row: ValueRow) => ({ rowSpan: row.categorySpan }),
              render: (value: string) => <Typography.Text strong>{value}</Typography.Text>,
            },
            {
              title: '维度', dataIndex: 'label', width: 150,
              onCell: (row: ValueRow) => ({ rowSpan: row.labelSpan }),
            },
            {
              title: '取值', width: 260,
              render: (_: unknown, row: ValueRow) => row.value !== undefined
                ? (
                  <Checkbox checked={dimensions[row.field].includes(row.value)}
                    onChange={(event) => toggleValue(row.field, row.value!, event.target.checked)}>
                    <span className="mono-strong">{row.value}</span>
                  </Checkbox>
                )
                : (
                  <Checkbox.Group value={dimensions[row.field]}
                    onChange={(values) => updateValues(row.field, values as string[])}
                    options={(row.values ?? []).map((value) => ({ value, label: value }))} />
                ),
            },
            {
              title: '说明', dataIndex: 'desc',
              render: (value: string) => (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>{value}</Typography.Text>
              ),
            },
          ]} />
      </Card>

      <Card title="③ 执行与结果" extra={
        ranking.length > 0 && (
          <Space size={28}>
            <div className="stat">
              <span className="stat-value">{best}</span>
              <span className="stat-label">最佳组合正确数</span>
            </div>
            <div className="stat">
              <span className="stat-value">{bestLatency}s</span>
              <span className="stat-label">平均时延</span>
            </div>
            <div className="stat">
              <span className="stat-value">{ranking.length}</span>
              <span className="stat-label">已完成组合</span>
            </div>
          </Space>
        )
      }>
        <Space align="center" wrap>
          <Button type="primary" size="large" loading={loading && !stopping} onClick={run}>开始自动搜索</Button>
          <Button size="large" danger disabled={!loading} loading={stopping} onClick={cancel}>
            {stopping ? '停止中…' : '停止搜索'}
          </Button>
          {csvPath && (
            <Button size="large" onClick={() => window.open(downloadUrl(csvPath), '_blank')}>
              下载排行榜 CSV
            </Button>
          )}
          {combinations.length > 0 && <Button onClick={applyBest} disabled={combinations[0].errors === combinations[0].count}>应用最佳方案到首页</Button>}
          <Typography.Text type="secondary">会真实调用模型，请先确认配置页的模型与 API Key。</Typography.Text>
        </Space>
        {loading && (
          <Alert style={{ marginTop: 16 }} type="info" showIcon
            message="搜索进行中，随时可以停止——已完成的组合会保留并写入排行榜。" />
        )}
        {status && <pre className="code" style={{ marginTop: 16 }}>{status}</pre>}
        {ranking.length > 0 && (
          <Table style={{ marginTop: 16 }} rowKey="__rk"
            dataSource={ranking.map((row, index) => ({ __rk: String(index), row }))}
            size="middle" scroll={{ x: 'max-content' }}
            columns={headers.map((title, column) => ({
              title, key: String(column), width: column === 0 ? 80 : undefined,
              ellipsis: column === headers.length - 1,
              render: (record: { row: unknown[] }) => column === 0
                ? <Tag color={String(record.row[column]) === '1' ? 'gold' : 'default'}>{String(record.row[column])}</Tag>
                : String(record.row[column] ?? ''),
            }))} />
        )}
      </Card>

      {combinations.length > 0 && <Card title="方案审计（完整策略已保存）">
        <Table rowKey="index" dataSource={combinations} size="small" pagination={false}
          columns={[
            { title: '组合', dataIndex: 'index' },
            { title: 'AST准确率', render: (_: unknown, row: SearchCombination) => `${(row.accuracy * 100).toFixed(1)}%` },
            { title: '当前数据EX', render: (_: unknown, row: SearchCombination) => row.execution_accuracy == null ? '未评测' : `${(row.execution_accuracy * 100).toFixed(1)}%` },
            { title: '本轮累计模型请求', dataIndex: 'actual_model_requests' },
            { title: '题数', dataIndex: 'count' },
            { title: '错误数', dataIndex: 'errors' },
            { title: '来源', render: (_: unknown, row: SearchCombination) => row.cached ? '缓存：零新增模型调用' : '本轮真实推理' },
          ]}
          expandable={{ expandedRowRender: (row) => <Table rowKey="query" dataSource={row.details} pagination={false} size="small"
            columns={[{ title: '问题', dataIndex: 'query' }, { title: '预测SQL', dataIndex: 'pred_sql' },
              { title: '标准SQL', dataIndex: 'gold_sql' }, { title: 'AST', render: (_: unknown, item: SearchCombination['details'][number]) => item.correct ? '正确' : '不同' },
              { title: 'EX / 说明', render: (_: unknown, item: SearchCombination['details'][number]) => `${item.execution_match == null ? '未评测' : item.execution_match ? '一致' : '不同'} ${item.execution_note ?? ''}` },
              { title: '输出Token', render: (_: unknown, item: SearchCombination['details'][number]) => String((item.trace?.token_usage as { completion_tokens?: number } | null)?.completion_tokens ?? '未提供') },
              { title: '耗时(s)', dataIndex: 'latency_s' }, { title: '错误', dataIndex: 'error' }]} /> }} />
      </Card>}

      {records.length > 0 && (
        <Card title={`固定抽样题目（${records.length} 题）`}>
          <Table rowKey="id" dataSource={records} size="small" pagination={false}
            columns={[
              { title: '题号', dataIndex: 'id', width: 80 },
              { title: '问题', dataIndex: 'query' },
              { title: '标准SQL', dataIndex: 'sql', ellipsis: true },
            ]} />
        </Card>
      )}
    </Space>
  )
}
