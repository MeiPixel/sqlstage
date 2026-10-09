import { useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Col, Descriptions, Input, InputNumber, Popconfirm, Row, Select, Space,
  Table, Typography, Upload,
} from 'antd'
import {
  deleteDataset, evaluate, generateItems, getRun, importDataset, judgeItems, listDatasetItems,
  listDatasets, listJudges, listRuns, saveDataset,
} from '../api'
import { useStudio } from '../StudioContext'
import { withRowKeys } from '../table'
import type { DatasetItem, DatasetRecord, RunItem, RunRecord } from '../types'

const DIFFICULTY = [
  { value: 'easy', label: 'easy' },
  { value: 'medium', label: 'medium' },
  { value: 'hard', label: 'hard' },
]

export default function DatasetPage() {
  const { message } = App.useApp()
  const { current, strategy } = useStudio()
  const [datasets, setDatasets] = useState<DatasetRecord[]>([])
  const [datasetId, setDatasetId] = useState<number | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [items, setItems] = useState<DatasetItem[]>([])
  const [judges, setJudges] = useState<{ value: string; label: string }[]>([])
  const [judgeName, setJudgeName] = useState('sql_strict')
  const [instruction, setInstruction] = useState('')
  const [count, setCount] = useState(10)
  const [easy, setEasy] = useState(1)
  const [medium, setMedium] = useState(1)
  const [hard, setHard] = useState(1)
  const [workers, setWorkers] = useState(5)
  const [evalItems, setEvalItems] = useState<RunItem[]>([])
  const [evalSummary, setEvalSummary] = useState('')
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [runId, setRunId] = useState<number | null>(null)
  const [runItems, setRunItems] = useState<RunItem[]>([])

  const refreshDatasets = async () => setDatasets(await listDatasets())
  const refreshRuns = async () => setRuns(await listRuns())
  useEffect(() => {
    void refreshDatasets()
    void refreshRuns()
    void listJudges().then(setJudges)
  }, [])

  const requireConnection = () => {
    if (!current) {
      message.warning('请先到「配置 → 数据库连接」选择当前连接')
      return false
    }
    return true
  }

  const updateItem = (index: number, patch: Partial<DatasetItem>) =>
    setItems(items.map((item, position) => (position === index ? { ...item, ...patch } : item)))

  const generate = async () => {
    if (!requireConnection()) return
    message.loading({ content: '正在出题…', key: 'generate' })
    try {
      const { items: rows } = await generateItems(current!, strategy,
        { instruction, count, easy, medium, hard })
      setItems(rows.map((row) => ({ ...row, candidate: row.sql })))
      message.success({ content: `生成 ${rows.length} 题`, key: 'generate' })
    } catch (error) {
      message.error({ content: String(error), key: 'generate' })
    }
  }

  const judge = async () => {
    setItems((await judgeItems(items, judgeName, strategy)).items)
    message.success('判分完成')
  }

  const save = async () => {
    if (!name.trim()) {
      message.warning('请填写数据集名称')
      return
    }
    const { id } = await saveDataset(name.trim(), description, items)
    setDatasetId(id)
    message.success(`已保存数据集「${name}」`)
    await refreshDatasets()
  }

  const load = async () => {
    if (datasetId === null) {
      message.warning('请先选择数据集')
      return
    }
    const record = datasets.find((row) => row.id === datasetId)!
    setName(record.name)
    setDescription(record.description)
    setItems(await listDatasetItems(datasetId))
  }

  const remove = async () => {
    if (datasetId === null) return
    await deleteDataset(datasetId)
    setDatasetId(null)
    await refreshDatasets()
    message.success('已删除')
  }

  /** 纯前端导出：与 /api/datasets/import 的读取格式保持一致，可来回导入。 */
  const exportJsonl = () => {
    if (!items.length) {
      message.warning('当前表格没有题目，请先加载数据集或生成候选题')
      return
    }
    const content = items.map((item) => JSON.stringify({
      difficulty: item.difficulty || 'medium', query: item.query, sql: item.sql,
    })).join('\n')
    const url = URL.createObjectURL(new Blob([content], { type: 'application/jsonl;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${(name || 'dataset').trim()}.jsonl`
    link.click()
    URL.revokeObjectURL(url)
    message.success(`已导出 ${items.length} 题`)
  }

  const runEvaluate = async () => {
    if (!requireConnection()) return
    if (datasetId === null) {
      message.warning('请先选择并保存数据集')
      return
    }
    message.loading({ content: '评测中…', key: 'evaluate' })
    try {
      const result = await evaluate(datasetId, judgeName, workers, current!, strategy)
      setEvalItems(result.rows)
      setEvalSummary(`评测 #${result.run_id}：严格正确 ${result.summary.accurate}/${result.summary.count}，`
        + `平均分 ${result.summary.mean_score.toFixed(3)}`)
      await refreshRuns()
      message.success({ content: '评测完成', key: 'evaluate' })
    } catch (error) {
      message.error({ content: String(error), key: 'evaluate' })
    }
  }

  const viewRun = async () => {
    if (runId === null) return
    const record = await getRun(runId)
    setRunItems(record.items)
    setEvalSummary(`运行 #${record.run.id}（${record.run.kind}）状态 ${record.run.status}：${record.run.summary}`)
  }

  const resultColumns = [
    { title: '问题', dataIndex: 'query' },
    { title: '标准SQL', dataIndex: 'gold_sql' },
    { title: '预测SQL', dataIndex: 'pred_sql' },
    { title: '分数', dataIndex: 'score', width: 70 },
    { title: '理由', dataIndex: 'reason' },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <div className="page-head">
        <Typography.Title level={4} className="page-title">测评数据集制作</Typography.Title>
        <Typography.Paragraph className="page-desc">
          生成或导入题目 → 编辑 → 判分 → 保存为数据集；再用当前策略对数据集逐题生成并打分。
        </Typography.Paragraph>
      </div>
      {!current && <Alert type="warning" showIcon title="还没有选择连接，生成题目与评测都需要连接。" />}

      <Card title="① 数据集">
        <Space wrap align="end">
          <Select style={{ width: 220 }} placeholder="已保存数据集" value={datasetId}
            onChange={setDatasetId}
            options={datasets.map((row) => ({ value: row.id, label: `${row.name}（${row.item_count} 题）` }))} />
          <Input style={{ width: 180 }} placeholder="数据集名称" value={name}
            onChange={(event) => setName(event.target.value)} />
          <Input style={{ width: 220 }} placeholder="说明" value={description}
            onChange={(event) => setDescription(event.target.value)} />
          <Button type="primary" onClick={save}>保存当前表格为数据集</Button>
          <Button onClick={load}>加载选中数据集</Button>
          <Popconfirm title="确认删除？" onConfirm={remove}>
            <Button danger disabled={datasetId === null}>删除选中数据集</Button>
          </Popconfirm>
          <Upload accept=".jsonl" showUploadList={false} beforeUpload={async (file) => {
            const parsed = await importDataset(name || '导入数据集', await file.text())
            setItems(parsed.items.map((row) => ({ ...row, candidate: row.sql })))
            message.success(`导入 ${parsed.items.length} 题`)
            return false
          }}>
            <Button>导入 JSONL</Button>
          </Upload>
          <Button onClick={exportJsonl}>导出 JSONL</Button>
        </Space>
      </Card>

      <Card title="② 生成候选题">
        <Row gutter={16}>
          <Col span={12}>
            <Input.TextArea rows={3} placeholder="出题要求，例如：围绕笔记本品牌的价位与配置，覆盖聚合与多表筛选"
              value={instruction} onChange={(event) => setInstruction(event.target.value)} />
          </Col>
          <Col span={12}>
            <Space wrap>
              <span>题数 <InputNumber value={count} min={1} max={50}
                onChange={(value) => setCount(Number(value))} /></span>
              <span>易 <InputNumber value={easy} onChange={(value) => setEasy(Number(value))} /></span>
              <span>中 <InputNumber value={medium} onChange={(value) => setMedium(Number(value))} /></span>
              <span>难 <InputNumber value={hard} onChange={(value) => setHard(Number(value))} /></span>
            </Space>
            <div style={{ marginTop: 12 }}>
              <Space>
                <Button type="primary" disabled={!current} onClick={generate}>生成候选题</Button>
                <Button onClick={judge}>按参考SQL判分</Button>
                <Select style={{ width: 160 }} value={judgeName} onChange={setJudgeName} options={judges} />
              </Space>
            </div>
          </Col>
        </Row>
      </Card>

      <Card title={`③ 数据集内容（${items.length} 题，可直接编辑「问题」与「当前SQL」）`}>
        <Table rowKey="__rk" dataSource={withRowKeys(items)} pagination={{ pageSize: 10 }}
          size="small"
          columns={[
            {
              title: '难度', width: 110,
              render: (_, __, index) => <Select size="small" style={{ width: 90 }}
                value={items[index].difficulty} options={DIFFICULTY}
                onChange={(value) => updateItem(index, { difficulty: value })} />,
            },
            {
              title: '问题',
              render: (_, __, index) => <Input value={items[index].query}
                onChange={(event) => updateItem(index, { query: event.target.value })} />,
            },
            {
              title: '参考SQL',
              render: (_, __, index) => <Input value={items[index].sql}
                onChange={(event) => updateItem(index, { sql: event.target.value })} />,
            },
            {
              title: '当前SQL',
              render: (_, __, index) => <Input value={items[index].candidate ?? ''}
                onChange={(event) => updateItem(index, { candidate: event.target.value })} />,
            },
            { title: '分数', width: 70, render: (_, __, index) => items[index].score ?? '' },
            { title: '理由', width: 160, render: (_, __, index) => items[index].judge_reason ?? '' },
          ]} />
      </Card>

      <Card title="④ 评测任务">
        <Space wrap align="end">
          <span>数据集：{datasets.find((row) => row.id === datasetId)?.name ?? '未选择'}</span>
          <Select style={{ width: 160 }} value={judgeName} onChange={setJudgeName} options={judges} />
          <span>并发 <InputNumber value={workers} min={1} max={10}
            onChange={(value) => setWorkers(Number(value))} /></span>
          <Button type="primary" disabled={!current || datasetId === null} onClick={runEvaluate}>开始评测</Button>
        </Space>
        {evalSummary && <Descriptions size="small" style={{ marginTop: 12 }}>
          <Descriptions.Item>{evalSummary}</Descriptions.Item>
        </Descriptions>}
        <Table rowKey="__rk" dataSource={withRowKeys(evalItems)} size="small"
          pagination={{ pageSize: 10 }} columns={resultColumns} style={{ marginTop: 12 }} />
      </Card>

      <Card title="⑤ 运行历史">
        <Space wrap align="end">
          <Select style={{ width: 320 }} placeholder="运行记录" value={runId} onChange={setRunId}
            options={runs.map((row) => ({ value: row.id, label: `#${row.id} ${row.kind} ${row.created_at}` }))} />
          <Button onClick={viewRun}>查看选中运行</Button>
        </Space>
        <Table rowKey="__rk" dataSource={withRowKeys(runItems)} size="small"
          pagination={{ pageSize: 10 }} columns={resultColumns} style={{ marginTop: 12 }} />
      </Card>
    </Space>
  )
}
