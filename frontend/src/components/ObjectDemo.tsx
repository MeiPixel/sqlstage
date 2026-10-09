import { useEffect, useState } from 'react'
import { Alert, App, Button, Card, Input, Select, Space, Table, Tag, Typography } from 'antd'
import { generate, makeObjectTemplate, operateObject } from '../api'
import { useStudio } from '../StudioContext'
import type { GenerateResult } from '../types'

interface Template { name: string; parameter: string; plan: Record<string, unknown>; original: unknown }
interface ObjectTurn { label: string; result: GenerateResult }
const outputTokens = (result: GenerateResult) =>
  (result.trace.token_usage as { completion_tokens?: number } | null)?.completion_tokens

export default function ObjectDemo() {
  const { current, strategy } = useStudio()
  const { message } = App.useApp()
  const [turns, setTurns] = useState<ObjectTurn[]>([])
  const [question, setQuestion] = useState('价格低于5000元的笔记本，返回品牌、型号、价格，按价格降序')
  const [busy, setBusy] = useState(false)
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateIndex, setTemplateIndex] = useState<number>()
  const [filterIndex, setFilterIndex] = useState(0)
  const [parameter, setParameter] = useState('budget')
  const [value, setValue] = useState('7000')
  const [comparison, setComparison] = useState<GenerateResult>()
  const latest = turns.at(-1)?.result
  const plan = latest?.orm_source_plan
  const filters = (plan?.filters ?? []) as { field: string; op: string; value: unknown }[]
  const key = `sqlstage.orm.templates.${current?.id}`

  useEffect(() => {
    setTurns([]); setComparison(undefined)
    setTemplates(JSON.parse(localStorage.getItem(key) ?? '[]')); setTemplateIndex(undefined)
  }, [key])

  const run = async (mode: 'ask' | 'bind' | 'compare') => {
    if (!current) return message.warning('请先选择数据库连接')
    setBusy(true)
    try {
      if (mode === 'compare') {
        const result = await generate([{ role: 'user', content: turns.map((turn) => turn.label).join('；') }], current,
          { ...strategy, orm: { ...strategy.orm, enabled: false } })
        setComparison(result)
      } else if (mode === 'bind') {
        const template = templates[templateIndex!]
        const parameterValue = typeof template.original === 'number' ? Number(value) : value
        if (typeof parameterValue === 'number' && !Number.isFinite(parameterValue)) throw new Error('请输入有效数字参数')
        const result = await operateObject(current, strategy, { plan: template.plan, parameters: { [template.parameter]: parameterValue } })
        setTurns((rows) => [...rows, { label: `把条件参数 ${template.parameter} 的值改为 ${value}（替换原值）`, result }])
      } else {
        const result = await operateObject(current, strategy, { question, ...(plan ? { plan } : {}) })
        setTurns((rows) => [...rows, { label: question, result }]); setQuestion('继续限制品牌为联想')
      }
    } catch (error) { message.error(String(error)) }
    finally { setBusy(false) }
  }

  const saveTemplate = async () => {
    if (!plan) return
    try {
      const saved = await makeObjectTemplate(plan, filterIndex, parameter)
      const next = [...templates, { name: `${filters[filterIndex].field} 参数查询`, parameter, plan: saved.plan, original: filters[filterIndex].value }]
      setTemplates(next); setTemplateIndex(next.length - 1); localStorage.setItem(key, JSON.stringify(next))
      message.success('已保存模板；换参数不再请求模型')
    } catch (error) { message.error(String(error)) }
  }

  return <Card title="ORM 查询对象复用演示" extra={<Button onClick={() => { setTurns([]); setComparison(undefined) }}>新建对象</Button>}>
    <Typography.Paragraph type="secondary">
      第一次创建查询对象；后续追问由模型输出 query = query.filter(...) 等短操作，保留已有过滤与 JOIN。
      增量操作仍消耗真实模型 Token；只有已保存模板换参数是零模型调用。模板保存在当前浏览器，按连接隔离。
    </Typography.Paragraph>
    <Space.Compact style={{ width: '100%' }}>
      <Input value={question} onChange={(event) => setQuestion(event.target.value)} />
      <Button type="primary" loading={busy} disabled={!question.trim()} onClick={() => run('ask')}>{plan ? '追加操作' : '创建对象'}</Button>
    </Space.Compact>
    {turns.map((turn, index) => <Card key={index} size="small" title={`${index + 1}. ${turn.label}`} style={{ marginTop: 12 }}>
      <pre className="code">{turn.result.orm_code}</pre>
      <pre className="code">{turn.result.sql}</pre>
      <Space wrap><Tag>本次输出Token：{outputTokens(turn.result) ?? '接口未提供'}</Tag>
        <Tag>调用次数：{String(turn.result.trace.model_calls ?? '-')}</Tag>
        <Tag>预处理：{String(turn.result.trace.before_model_s ?? '-')}s</Tag>
        <Tag>模型：{String(turn.result.trace.model_s ?? '-')}s</Tag>
        <Tag>总耗时：{turn.result.latency_s}s</Tag></Space>
      <div style={{ marginTop: 8 }}>涉及表：{turn.result.tables.join('、')}；字段：{Object.entries(turn.result.columns).map(([table, fields]) => `${table}: ${fields.join('、')}`).join('；')}</div>
      {(turn.result.trace.value_corrections as { field: string; before: string; after: string }[] | undefined)?.map((fix) => <Tag key={fix.field}>纠错 {fix.field}：{fix.before} → {fix.after}</Tag>)}
    </Card>)}
    {filters.length > 0 && <Space wrap style={{ marginTop: 16 }}>
      <Select value={filterIndex} onChange={setFilterIndex} style={{ width: 280 }} options={filters.map((filter, index) => ({ value: index, label: `${filter.field} ${filter.op} ${String(filter.value)}` }))} />
      <Input addonBefore="参数名" value={parameter} onChange={(event) => setParameter(event.target.value)} style={{ width: 240 }} />
      <Button onClick={saveTemplate}>保存为参数模板</Button>
    </Space>}
    {templates.length > 0 && <Space wrap style={{ marginTop: 12 }}>
      <Select value={templateIndex} onChange={setTemplateIndex} style={{ width: 260 }} options={templates.map((template, index) => ({ value: index, label: template.name }))} />
      <Input addonBefore="新参数值" value={value} onChange={(event) => setValue(event.target.value)} style={{ width: 220 }} />
      <Button loading={busy} disabled={templateIndex == null} onClick={() => run('bind')}>本地编译 SQL（零模型调用）</Button>
    </Space>}
    {latest && <div style={{ marginTop: 16 }}>
      <Alert type="info" showIcon title="对照会额外请求一次模型，不会自动执行。输入与输出 Token 来自 API usage，不按字符串长度估算。" />
      <Button style={{ margin: '12px 0' }} loading={busy} onClick={() => run('compare')}>对照：同需求直接生成完整 SQL</Button>
      {comparison && <><pre className="code">{comparison.sql}</pre><Table size="small" pagination={false} rowKey="method"
        dataSource={[{ method: '当前对象操作', token: outputTokens(latest) ?? '未提供', time: latest.latency_s },
          { method: '直接生成完整 SQL', token: outputTokens(comparison) ?? '未提供', time: comparison.latency_s }]}
        columns={[{ title: '方式', dataIndex: 'method' }, { title: '本次输出Token', dataIndex: 'token' }, { title: '耗时(s)', dataIndex: 'time' }]} /></>}
    </div>}
  </Card>
}
