import { useEffect, useRef, useState } from 'react'
import {
  Alert, App, Button, Card, Collapse, Empty, Input, Modal, Select, Space, Table, Tag, Typography,
} from 'antd'
import {
  appendDatasetItems, generate, listDatasets, listGenerations, listOrmPresets, listSamples,
  loadSample, saveDataset,
} from '../api'
import { presetToOrm } from '../orm'
import { useStudio } from '../StudioContext'
import { withRowKeys } from '../table'
import type {
  ChatMessage, DatasetItem, DatasetRecord, GenerateResult, GenerationRecord, SampleDatabase,
  SampleQuestion,
} from '../types'
import ObjectDemo from '../components/ObjectDemo'

interface Turn {
  key: string
  role: 'user' | 'assistant'
  content: string
  question?: string
  result?: GenerateResult
}

function ResultTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (!rows.length) return <Empty description="无数据" />
  const columns = Object.keys(rows[0]).map((key) => ({
    title: key, dataIndex: key, key,
    render: (value: unknown) => String(value ?? ''),
  }))
  return <Table rowKey="__rk" dataSource={withRowKeys(rows)} columns={columns}
    pagination={{ pageSize: 10 }} size="small" />
}

function AddToDatasetButton({ item }: { item: DatasetItem }) {
  const { message } = App.useApp()
  const [open, setOpen] = useState(false)
  const [datasets, setDatasets] = useState<DatasetRecord[]>([])
  const [target, setTarget] = useState<number | null>(null)
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)

  const openModal = async () => {
    setOpen(true)
    setSaving(false)
    setName('')
    try {
      const rows = await listDatasets()
      setDatasets(rows)
      setTarget(rows[0]?.id ?? null)
    } catch (error) {
      message.error(String(error))
    }
  }

  const submit = async () => {
    const title = name.trim()
    if (!title && target === null) {
      message.warning('请选择已有数据集，或填写名称新建一个')
      return
    }
    setSaving(true)
    try {
      if (title) {
        const { id } = await saveDataset(title, '从生成页沉淀', [item])
        message.success(`已新建数据集「${title}」并加入 1 题（#${id}）`)
      } else {
        const result = await appendDatasetItems(target!, [item])
        message.success(`已加入数据集，当前共 ${result.total} 题`)
      }
      setOpen(false)
    } catch (error) {
      message.error(String(error))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Button size="small" onClick={openModal}>加入数据集</Button>
      <Modal title="把这道题加入数据集" open={open} onCancel={() => setOpen(false)}
        onOk={submit} okText="加入" confirmLoading={saving} destroyOnHidden>
        <Space orientation="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <div style={{ marginBottom: 4 }}>问题</div>
            <Typography.Text type="secondary">{item.query}</Typography.Text>
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>SQL</div>
            <pre className="code" style={{ margin: 0 }}>{item.sql || '(空)'}</pre>
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>追加到已有数据集</div>
            <Select style={{ width: '100%' }} value={target} onChange={setTarget}
              placeholder={datasets.length ? '选择数据集' : '还没有已保存的数据集'}
              options={datasets.map((row) => ({ value: row.id, label: `${row.name}（${row.item_count} 题）` }))} />
          </div>
          <div>
            <div style={{ marginBottom: 4 }}>或新建一个（填写后优先）</div>
            <Input placeholder="新数据集名称" value={name}
              onChange={(event) => setName(event.target.value)} />
          </div>
        </Space>
      </Modal>
    </>
  )
}

function AssistantTurn({ turn }: { turn: Turn }) {
  const result = turn.result
  return (
    <>
      <pre className="code">{turn.content || '(空)'}</pre>
      {result && (
        <Typography.Text type="secondary" style={{ display: 'block', margin: '8px 0' }}>
          召回表：{result.tables.join('、') || '-'} · 执行行数：{result.rows.length} · {result.latency_s}s
        </Typography.Text>
      )}
      {result && <Space wrap style={{ margin: '8px 0' }}>
        <Tag>请求到首次模型调用：{String(result.trace.before_model_s ?? '-')}s</Tag>
        <Tag>模型调用累计：{String(result.trace.model_s ?? '-')}s</Tag>
        <Tag>模型调用次数：{String(result.trace.model_calls ?? '-')}</Tag>
        <Tag>输出 Token：{String((result.trace.token_usage as { completion_tokens?: number } | null)?.completion_tokens ?? '接口未提供')}</Tag>
      </Space>}
      {result?.orm_code && <pre className="code">{result.orm_code}</pre>}
      {Number((result?.trace.context_budget as { optional_chars_dropped?: number } | undefined)?.optional_chars_dropped ?? 0) > 0 &&
        <Alert type="info" title="提示预算已完整保留表结构、Guide 和 Few-shot，部分样例/字段说明按完整行省略；详见 Trace。" />}
      {result && result.rows.length > 0 && <ResultTable rows={result.rows} />}
      <div style={{ marginTop: 8 }}>
        <AddToDatasetButton item={{ difficulty: 'medium', query: turn.question ?? '', sql: turn.content }} />
      </div>
      {result && (
        <Collapse ghost size="small" style={{ marginTop: 8 }} items={[
          { key: 'columns', label: '召回字段',
            children: <Table size="small" pagination={false} rowKey="table"
              dataSource={Object.entries(result.columns).map(([table, fields]) => ({ table, fields: fields.join('、') }))}
              columns={[{ title: '表', dataIndex: 'table' }, { title: '字段', dataIndex: 'fields' }]} /> },
          { key: 'trace', label: 'Trace',
            children: <pre className="code">{JSON.stringify(result.trace, null, 2)}</pre> },
          ...(result.raw ? [{ key: 'raw', label: '模型原始输出',
            children: <pre className="code">{result.raw}</pre> }] : []),
        ]} />
      )}
    </>
  )
}

export default function GeneratePage() {
  const { message } = App.useApp()
  const { current, strategy, updateOrm, refreshConnections, setCurrentId } = useStudio()
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [history, setHistory] = useState<GenerationRecord[]>([])
  const [samples, setSamples] = useState<SampleDatabase[]>([])
  const [sampleName, setSampleName] = useState<string>()
  const [questions, setQuestions] = useState<SampleQuestion[]>([])
  const [sampleInfo, setSampleInfo] = useState<SampleDatabase | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  useEffect(() => {
    void listSamples().then(({ databases }) => {
      setSamples(databases)
      setSampleName(databases[0]?.name)
    }).catch(() => setSamples([]))
  }, [])

  const refreshHistory = async () => setHistory(await listGenerations())

  const useSample = async () => {
    if (!sampleName) return
    try {
      const loaded = await loadSample(sampleName)
      await refreshConnections()
      setCurrentId(loaded.connection.id)
      setQuestions(loaded.questions)
      setSampleInfo(loaded.database)
      if (loaded.orm_preset) {
        const presets = await listOrmPresets()
        const preset = presets.find((row) => row.name === loaded.orm_preset)
        if (preset) {
          updateOrm({ ...presetToOrm(preset), enabled: true })
          message.success(`已加载「${loaded.database.title}」，并开启 ORM 预置「${preset.name}」`)
        }
      } else {
        message.success(`已加载「${loaded.database.title}」，共 ${loaded.questions.length} 条示例问题`)
      }
    } catch (error) {
      message.error(String(error))
    }
  }

  const send = async (question?: string) => {
    const text = (question ?? input).trim()
    if (!current) {
      message.warning('请先在「配置 → 数据库连接」里选择当前连接，或用上方示例库一键加载')
      return
    }
    if (!text) {
      message.warning('请先输入问题')
      return
    }
    const messages: ChatMessage[] = [
      ...turns.map((turn) => ({ role: turn.role, content: turn.content })).slice(-12),
      { role: 'user', content: text },
    ]
    setTurns((previous) => [...previous, { key: `u${Date.now()}`, role: 'user', content: text }])
    setInput('')
    setLoading(true)
    try {
      const result = await generate(messages, current, strategy)
      setTurns((previous) => [...previous,
        { key: `a${Date.now()}`, role: 'assistant', content: result.sql, question: text, result }])
      await refreshHistory()
    } catch (error) {
      message.error(String(error))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <div className="page-head">
        <Typography.Title level={4} className="page-title">Text2SQL 使用</Typography.Title>
        <Typography.Paragraph className="page-desc">
          用当前连接与当前策略方案多轮对话：上一轮的问题与 SQL 会作为上下文，可以直接追问「那最便宜的呢」。
        </Typography.Paragraph>
      </div>

      <Card title="内置示例库" size="small"
        extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>
          随仓库分发，加载后即为当前连接（只读）
        </Typography.Text>}>
        <Space wrap align="center">
          <Select style={{ width: 340 }} value={sampleName} onChange={setSampleName}
            placeholder={samples.length ? '选择示例库' : '暂无内置示例库'}
            options={samples.map((row) => ({
              value: row.name,
              label: `${row.title}（${row.tables} 表 / ${row.rows} 行 / ${row.questions} 例）`,
              disabled: !row.available,
            }))} />
          <Button type="primary" onClick={useSample} disabled={!sampleName}>加载为当前连接</Button>
        </Space>
        {sampleInfo && (
          <Alert style={{ marginTop: 12 }} type="success" showIcon
            message={`已加载 ${sampleInfo.title} · ${sampleInfo.tables} 张表 · ${sampleInfo.rows} 行`}
            description={sampleInfo.description} />
        )}
        {questions.length > 0 && (
          <Table style={{ marginTop: 12 }} rowKey="question" size="small"
            dataSource={questions} pagination={{ pageSize: 5 }}
            columns={[
              { title: '示例问题', dataIndex: 'question', ellipsis: true },
              {
                title: '标准 SQL', dataIndex: 'sql', ellipsis: true,
                render: (value: string) => <code>{value}</code>,
              },
              {
                title: '外部知识', dataIndex: 'evidence', width: 180, ellipsis: true,
                render: (value: string) => value || <Typography.Text type="secondary">-</Typography.Text>,
              },
              {
                title: '', width: 150,
                render: (_: unknown, row: SampleQuestion) => (
                  <Space size={4}>
                    <Button size="small" onClick={() => setInput(row.question)}>填入</Button>
                    <Button size="small" type="primary" onClick={() => send(row.question)}>直接提问</Button>
                  </Space>
                ),
              },
            ]} />
        )}
      </Card>

      {!current && <Alert type="warning" showIcon
        title="还没有选择连接，请先到「配置 → 数据库连接」新建或选择，或用上方示例库一键加载。" />}

      <Card title="对话" extra={
        <Space>
          <Tag>{turns.length ? `${turns.length} 条消息` : '新会话'}</Tag>
          <Button size="small" disabled={!turns.length} onClick={() => setTurns([])}>清空对话</Button>
        </Space>
      }>
        <div className="chat">
          {turns.length === 0 && <Empty description="开始提问吧，例如：价格最高的商品叫什么" />}
          {turns.map((turn) => (
            <div key={turn.key} className={`chat-row ${turn.role}`}>
              <div className={`chat-bubble ${turn.role}`}>
                {turn.role === 'user' ? turn.content : <AssistantTurn turn={turn} />}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
        <Input.TextArea rows={2} style={{ marginTop: 12 }} value={input}
          onChange={(event) => setInput(event.target.value)}
          onPressEnter={(event) => {
            if (!event.shiftKey) {
              event.preventDefault()
              void send()
            }
          }}
          placeholder="输入问题，回车发送，Shift+回车换行" />
        <div style={{ marginTop: 12 }}>
          <Button type="primary" loading={loading} disabled={!current} onClick={() => send()}>发送</Button>
        </div>
      </Card>

      <Card title="生成历史" extra={<Button size="small" onClick={refreshHistory}>刷新</Button>}>
        <Table rowKey="id" dataSource={history} pagination={{ pageSize: 5 }} size="small"
          columns={[
            { title: '时间', dataIndex: 'created_at', width: 180 },
            { title: '问题', dataIndex: 'question' },
            { title: 'SQL', dataIndex: 'sql', ellipsis: true },
            { title: '时延(s)', dataIndex: 'latency_s', width: 90 },
            {
              title: '', width: 110,
              render: (_: unknown, row: GenerationRecord) => (
                <AddToDatasetButton
                  item={{ difficulty: 'medium', query: row.question, sql: row.sql }} />
              ),
            },
          ]} />
      </Card>
      <ObjectDemo />
    </Space>
  )
}
