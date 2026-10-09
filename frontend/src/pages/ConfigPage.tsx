import { useEffect, useState, type ReactNode } from 'react'
import {
  App, Button, Card, Checkbox, Col, Divider, Form, Input, InputNumber, Popconfirm, Radio, Row,
  Select, Space, Table, Tabs, Typography,
} from 'antd'
import {
  deletePublish, deleteStrategy, generateGuide, guideTables, listPublishes, listStrategies,
  savePublish, saveStrategy, viewTableGuide,
} from '../api'
import { fromBackendStrategy } from '../orm'
import { useStudio } from '../StudioContext'
import type { PublishRecord, StrategyRecord } from '../types'
import ConnectionsPage from './ConnectionsPage'

function JsonField({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const [text, setText] = useState(JSON.stringify(value))
  return (
    <Input.TextArea rows={3} value={text} onChange={(event) => setText(event.target.value)}
      onBlur={() => onChange(JSON.parse(text))} />
  )
}

const options = (values: [string, string][]) =>
  values.map(([value, label]) => ({ value, label }))

function item(label: string, node: ReactNode, span = 8) {
  return <Col span={span} key={label}><Form.Item label={label}>{node}</Form.Item></Col>
}

export default function ConfigPage() {
  const { message } = App.useApp()
  const { strategy, setStrategy, updateModel, updateTricks, current } = useStudio()
  const [strategies, setStrategies] = useState<StrategyRecord[]>([])
  const [planName, setPlanName] = useState('')
  const [planDesc, setPlanDesc] = useState('')
  const [selectedPlan, setSelectedPlan] = useState<number | null>(null)
  const [tableNames, setTableNames] = useState<string[]>([])
  const [guideTable, setGuideTable] = useState<string | null>(null)
  const [viewedGuide, setViewedGuide] = useState('')
  const [guideLoading, setGuideLoading] = useState(false)
  const [publishes, setPublishes] = useState<PublishRecord[]>([])

  const refreshPlans = async () => setStrategies(await listStrategies())
  useEffect(() => {
    void refreshPlans()
    void guideTables().then(setTableNames)
    void listPublishes().then(setPublishes)
  }, [])

  const savePlan = async () => {
    if (!planName.trim()) {
      message.warning('请填写方案名称')
      return
    }
    await saveStrategy(planName.trim(), planDesc, strategy)
    message.success(`已保存方案「${planName}」`)
    await refreshPlans()
  }

  const loadPlan = () => {
    const record = strategies.find((row) => row.id === selectedPlan)
    if (!record) {
      message.warning('请先选择方案')
      return
    }
    setStrategy(fromBackendStrategy(JSON.parse(record.config)))
    message.success(`已加载方案「${record.name}」`)
  }

  const removePlan = async () => {
    if (selectedPlan === null) return
    await deleteStrategy(selectedPlan)
    setSelectedPlan(null)
    await refreshPlans()
    message.success('已删除')
  }

  const publish = async () => {
    const record = strategies.find((row) => row.id === selectedPlan)
    if (!record || !current) {
      message.warning('请先选择策略方案与当前连接')
      return
    }
    await savePublish({ name: record.name, strategy_id: record.id, connection_id: current.id })
    setPublishes(await listPublishes())
    message.success(`已发布「${record.name}」`)
  }

  const removePublish = async (name: string) => {
    await deletePublish(name)
    setPublishes(await listPublishes())
    message.success('已取消发布')
  }

  const makeGuide = async () => {
    if (!current) {
      message.warning('请先选择当前连接')
      return
    }
    setGuideLoading(true)
    try {
      const { text } = await generateGuide(current, strategy)
      updateTricks({ guide_text: text })
      message.success(`已生成 ${text.length} 字符并写入 Guide 正文`)
    } catch (error) {
      message.error(String(error))
    } finally {
      setGuideLoading(false)
    }
  }

  const showGuide = async () => {
    if (!guideTable) {
      message.warning('请先选择表')
      return
    }
    setViewedGuide((await viewTableGuide(guideTable)).text)
  }

  const checkbox = (label: string, key: keyof typeof strategy.tricks, span = 6) =>
    item(label, <Checkbox checked={Boolean(strategy.tricks[key])}
      onChange={(event) => updateTricks({ [key]: event.target.checked })} />, span)

  const modelForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {item('模型族', <Select style={{ width: '100%' }} value={strategy.model.provider}
          onChange={(value) => updateModel({ provider: value })}
          options={['deepseek', 'qwen', 'gemma', 'vllm', 'sglang', 'openai']
            .map((value) => ({ value, label: value }))} />)}
        {item('思考模式', <Radio.Group value={strategy.model.thinking}
          onChange={(event) => updateModel({ thinking: event.target.value })}
          options={options([['default', '默认'], ['disabled', '关闭'], ['enabled', '开启']])} />)}
      </Row>
      <Row gutter={16}>
        {item('Base URL', <Input value={strategy.model.base_url}
          onChange={(event) => updateModel({ base_url: event.target.value })} />, 12)}
        {item('模型', <Input value={strategy.model.model}
          onChange={(event) => updateModel({ model: event.target.value })} />, 6)}
        {item('Embedding 模型', <Input value={strategy.model.embedding_model}
          onChange={(event) => updateModel({ embedding_model: event.target.value })} />, 6)}
      </Row>
      <Row gutter={16}>
        {item('API Key（留空读取 DEEPSEEK_API_KEY）', <Input.Password value={strategy.model.api_key}
          onChange={(event) => updateModel({ api_key: event.target.value })} />, 12)}
        {item('温度', <InputNumber value={strategy.model.temperature} style={{ width: '100%' }}
          onChange={(value) => updateModel({ temperature: Number(value) })} />, 4)}
        {item('最大 Token', <InputNumber value={strategy.model.max_tokens} style={{ width: '100%' }}
          onChange={(value) => updateModel({ max_tokens: Number(value) })} />, 4)}
        {item('超时秒', <InputNumber value={strategy.model.timeout_seconds} style={{ width: '100%' }}
          onChange={(value) => updateModel({ timeout_seconds: Number(value) })} />, 4)}
      </Row>
      <Row gutter={16}>
        {item('额外请求体（JSON）', <JsonField key={JSON.stringify(strategy.model.extra_body)}
          value={strategy.model.extra_body}
          onChange={(value) => updateModel({ extra_body: value as Record<string, unknown> })} />, 24)}
      </Row>
    </Form>
  )

  const tableForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {item('选表策略', <Select style={{ width: '100%' }} value={strategy.tricks.table_recall}
          onChange={(value) => updateTricks({ table_recall: value })}
          options={options([
            ['all_schema', '全部表（适合小数据库）'], ['keyword', '关键词匹配（最快）'],
            ['bm25', 'BM25 表名+字段名（推荐起点）'], ['bm25_field', 'BM25 字段级+枚举取值（值命中）'],
            ['embedding', 'Embedding 语义召回'], ['hybrid', '词面+语义混合召回'],
            ['llm', '让大模型选择表']])} />, 12)}
        {item('最多保留候选表数', <InputNumber value={strategy.tricks.table_top_k} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ table_top_k: Number(value) })} />, 6)}
      </Row>
      <Row gutter={16}>
        {checkbox('自动补充可能需要 JOIN 的关联表', 'join_expansion')}
        {checkbox('按题目难度动态调整召回范围', 'difficulty_routing')}
        {checkbox('切分 camelCase 字段名', 'camel_case_split')}
        {checkbox('校验大模型召回的表/字段', 'llm_validate_names')}
        {checkbox('大模型召回为空时词面兜底', 'llm_lexical_fallback')}
      </Row>
      <Row gutter={16}>
        {item('表说明与离线关键词（JSON：表名 → 关键词）',
          <JsonField key={JSON.stringify(strategy.tricks.table_descriptions)}
            value={strategy.tricks.table_descriptions}
            onChange={(value) => updateTricks({ table_descriptions: value as Record<string, string> })} />, 24)}
      </Row>
    </Form>
  )

  const columnForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {item('选字段策略', <Select style={{ width: '100%' }} value={strategy.tricks.column_recall}
          onChange={(value) => updateTricks({ column_recall: value })}
          options={options([
            ['all_columns', '保留全部字段'], ['keyword', '关键词匹配'], ['bm25', 'BM25 词面召回'],
            ['embedding', 'Embedding 语义召回'], ['hybrid', '词面+语义混合召回'],
            ['llm', '让大模型选择字段']])} />, 12)}
        {item('每表最多保留字段数', <InputNumber value={strategy.tricks.column_top_k} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ column_top_k: Number(value) })} />, 6)}
        {item('给模型查看的样例行数', <InputNumber value={strategy.tricks.sample_rows} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ sample_rows: Number(value) })} />, 6)}
      </Row>
      <Row gutter={16}>
        {checkbox('大表字段过多时按问题自动压缩', 'column_description_recall')}
        {item('字段数超过该值才压缩', <InputNumber value={strategy.tricks.column_description_threshold}
          style={{ width: '100%' }}
          onChange={(value) => updateTricks({ column_description_threshold: Number(value) })} />, 6)}
      </Row>
      <Row gutter={16}>
        {checkbox('始终保留主键', 'keep_primary_keys')}
        {checkbox('始终保留外键', 'keep_foreign_keys')}
        {checkbox('根据问题中的名称定位字段（实体嗅探）', 'entity_sniffing', 8)}
        {checkbox('展示枚举字段常见取值（高频值探测）', 'distinct_probe', 8)}
      </Row>
    </Form>
  )

  const guideForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {checkbox('启用 Database Guide', 'guide_enabled', 6)}
        {item('Guide 字符预算', <InputNumber value={strategy.tricks.guide_max_chars} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ guide_max_chars: Number(value) })} />, 6)}
        {item('Guide 路径', <Input value={strategy.tricks.guide_path}
          onChange={(event) => updateTricks({ guide_path: event.target.value })} />, 12)}
      </Row>
      <Row gutter={16}>
        {item('Guide 正文', <Input.TextArea rows={8} value={strategy.tricks.guide_text}
          onChange={(event) => updateTricks({ guide_text: event.target.value })} />, 24)}
      </Row>
      <Space style={{ marginBottom: 16 }}>
        <Button loading={guideLoading} disabled={!current} onClick={makeGuide}>根据当前数据库生成六段式 Guide</Button>
        <Select style={{ width: 220 }} placeholder="选择表查看 Guide" value={guideTable}
          onChange={setGuideTable} options={tableNames.map((name) => ({ value: name, label: name }))} />
        <Button onClick={showGuide}>查看表格 Guide</Button>
      </Space>
      {viewedGuide && <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>{viewedGuide}</pre>}
    </Form>
  )

  const fewshotForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {checkbox('提供相似问题和正确 SQL 示例', 'fewshot_enabled', 6)}
        {item('示例选择方法', <Select style={{ width: '100%' }} value={strategy.tricks.fewshot_method}
          onChange={(value) => updateTricks({ fewshot_method: value })}
          options={options([
            ['fixed', '按文件顺序固定选择'], ['keyword', '关键词匹配'], ['bm25', 'BM25 词面相似度'],
            ['embedding', 'Embedding 语义相似度'], ['hybrid', '词面+语义混合']])} />, 8)}
        {item('最多提供几个示例', <InputNumber value={strategy.tricks.fewshot_top_k} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ fewshot_top_k: Number(value) })} />, 5)}
        {item('Few-shot JSONL', <Input value={strategy.tricks.fewshot_path}
          onChange={(event) => updateTricks({ fewshot_path: event.target.value })} />, 5)}
      </Row>
      <Row gutter={16}>
        {checkbox('限定同表', 'fewshot_same_table')}
        {checkbox('排除同题', 'fewshot_exclude_self')}
        {checkbox('难度匹配', 'fewshot_difficulty')}
        {checkbox('Few-shot 池去重', 'fewshot_dedup')}
        {checkbox('安全 RAG', 'rag_enabled')}
      </Row>
    </Form>
  )

  const pickForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {item('候选数', <Radio.Group value={strategy.tricks.candidate_count}
          onChange={(event) => updateTricks({ candidate_count: Number(event.target.value) })}
          options={options([['1', '1'], ['3', '3'], ['5', '5']])} />, 4)}
        {item('Prompt 变体', <Checkbox.Group value={strategy.tricks.candidate_variants}
          onChange={(values) => updateTricks({ candidate_variants: values as string[] })}
          options={options([['direct', 'direct'], ['join_first', 'join_first'], ['minimal', 'minimal']])} />, 12)}
        {item('候选温度（JSON 数组）', <JsonField key={JSON.stringify(strategy.tricks.candidate_temperatures)}
          value={strategy.tricks.candidate_temperatures}
          onChange={(value) => updateTricks({ candidate_temperatures: value as number[] })} />, 8)}
      </Row>
      <Row gutter={16}>
        {checkbox('规范化投票', 'candidate_vote')}
        {checkbox('LLM 裁判', 'candidate_llm_selector')}
        {checkbox('逐候选执行验证', 'candidate_execution_check')}
      </Row>
    </Form>
  )

  const repairForm = (
    <Form layout="vertical">
      <Row gutter={16}>
        {checkbox('执行最终 SQL', 'execution_check')}
        {checkbox('错误反馈', 'execution_feedback')}
        {checkbox('空结果反思', 'empty_result_refine')}
        {checkbox('语义复核', 'semantic_refine')}
      </Row>
      <Row gutter={16}>
        {checkbox('问题改写', 'question_rewrite')}
        {checkbox('方言转换', 'dialect_conversion')}
        {checkbox('确定性修复', 'deterministic_repair')}
        {item('修复轮数', <InputNumber value={strategy.tricks.repair_rounds} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ repair_rounds: Number(value) })} />, 4)}
        {item('Prompt 字符预算', <InputNumber value={strategy.tricks.prompt_max_chars} style={{ width: '100%' }}
          onChange={(value) => updateTricks({ prompt_max_chars: Number(value) })} />, 6)}
      </Row>
    </Form>
  )

  const items = [
    { key: 'database', label: '数据库连接', children: <ConnectionsPage /> },
    { key: 'model', label: '大模型', children: modelForm },
    { key: 'table', label: '表召回', children: tableForm },
    { key: 'column', label: '字段召回', children: columnForm },
    { key: 'guide', label: 'Guide', children: guideForm },
    { key: 'fewshot', label: 'Few-shot', children: fewshotForm },
    { key: 'pick', label: 'Pick3 与多候选', children: pickForm },
    { key: 'repair', label: '修复与运行', children: repairForm },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <div className="page-head">
        <Typography.Title level={4} className="page-title">配置</Typography.Title>
        <Typography.Paragraph className="page-desc">
          先选好数据库连接，再按 Text2SQL 的 Prompt 自上而下配置；整套配置可保存为命名方案随时复用。
        </Typography.Paragraph>
      </div>
      <Card title="策略方案（保存 / 加载整套配置）">
        <Space wrap align="end">
          <Select style={{ width: 220 }} placeholder="已保存方案" value={selectedPlan}
            onChange={setSelectedPlan}
            options={strategies.map((row) => ({ value: row.id, label: row.name }))} />
          <Input style={{ width: 200 }} placeholder="方案名称" value={planName}
            onChange={(event) => setPlanName(event.target.value)} />
          <Input style={{ width: 240 }} placeholder="说明" value={planDesc}
            onChange={(event) => setPlanDesc(event.target.value)} />
          <Button type="primary" onClick={savePlan}>保存当前配置为方案</Button>
          <Button onClick={loadPlan}>加载选中方案</Button>
          <Popconfirm title="确认删除？" onConfirm={removePlan}>
            <Button danger disabled={selectedPlan === null}>删除选中方案</Button>
          </Popconfirm>
        </Space>
        <Divider style={{ margin: '12px 0 0' }} />
        <Tabs items={items} />
        <Divider>发布为接口</Divider>
        <Space wrap align="end" style={{ marginBottom: 12 }}>
          <Button type="primary" onClick={publish}>发布选中方案（用当前连接）</Button>
          <span style={{ color: '#888' }}>
            调用：POST /api/publish/&lt;名称&gt;/predict，请求体 {'{"question":"..."}'}
          </span>
        </Space>
        <Table rowKey="id" dataSource={publishes} pagination={false} size="small"
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: '策略方案', dataIndex: 'strategy_id', width: 100 },
            { title: '连接', dataIndex: 'connection_id', width: 80 },
            {
              title: '调用地址',
              render: (_: unknown, row: PublishRecord) => (
                <Typography.Text code copyable>{`/api/publish/${row.name}/predict`}</Typography.Text>
              ),
            },
            {
              title: '', width: 100,
              render: (_: unknown, row: PublishRecord) => (
                <Button size="small" danger onClick={() => removePublish(row.name)}>取消发布</Button>
              ),
            },
          ]} />
      </Card>
    </Space>
  )
}
