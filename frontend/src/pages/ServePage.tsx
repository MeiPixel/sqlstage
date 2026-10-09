import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Empty, Input, Select, Space, Switch, Table, Tag, Typography,
} from 'antd'
import {
  deletePublish, exportServiceConfig, listPublishes, listStrategies, newServiceToken, savePublish,
} from '../api'
import { useStudio } from '../StudioContext'
import type {
  ConnectionRecord, PublishRecord, ServiceExport, ServiceToken, StrategyRecord,
} from '../types'

interface TokenRow {
  name: string
  token: string
}

const ENDPOINTS = [
  { path: 'GET /health', desc: '健康检查，返回服务列表', auth: '免鉴权' },
  { path: 'GET /v1/services', desc: '列出配置里的所有服务与方言', auth: '需 token' },
  { path: 'GET /v1/{服务名}/info', desc: '服务名、方言、ORM 与关键开关（不含任何密钥）', auth: '需 token' },
  { path: 'GET /v1/{服务名}/schema', desc: '库表结构，供调用方自己拼问题', auth: '需 token' },
  { path: 'POST /v1/{服务名}/predict', desc: '传 { question, history }，返回 SQL 与结果行', auth: '需 token' },
]

function PublishForm({ onSaved }: { onSaved: () => void }) {
  const { message } = App.useApp()
  const { connections, currentId } = useStudio()
  const [strategies, setStrategies] = useState<StrategyRecord[]>([])
  const [name, setName] = useState('')
  const [strategyId, setStrategyId] = useState<number | null>(null)
  const [connectionId, setConnectionId] = useState<number | null>(currentId)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    void listStrategies().then((rows) => {
      setStrategies(rows)
      setStrategyId(rows[0]?.id ?? null)
    })
  }, [])
  useEffect(() => { setConnectionId(currentId) }, [currentId])

  const submit = async () => {
    if (!name.trim()) {
      message.warning('先给服务起个名字')
      return
    }
    if (strategyId === null || connectionId === null) {
      message.warning('需要选择策略方案和数据库连接')
      return
    }
    setSaving(true)
    try {
      await savePublish({ name: name.trim(), strategy_id: strategyId, connection_id: connectionId })
      setName('')
      onSaved()
      message.success(`已发布 ${name.trim()}`)
    } catch (error) {
      message.error(String(error))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space wrap size={12}>
      <Input placeholder="服务名，例如 城市问答" value={name} onChange={(e) => setName(e.target.value)}
        style={{ width: 200 }} />
      <Select placeholder="策略方案" value={strategyId} onChange={setStrategyId}
        style={{ width: 200 }} options={strategies.map((row) => ({ value: row.id, label: row.name }))} />
      <Select placeholder="数据库连接" value={connectionId} onChange={setConnectionId}
        style={{ width: 200 }}
        options={connections.map((row: ConnectionRecord) => ({ value: row.id, label: `${row.name} · ${row.type}` }))} />
      <Button type="primary" loading={saving} onClick={() => void submit()}>发布</Button>
    </Space>
  )
}

export default function ServePage() {
  const { message } = App.useApp()
  const [publishes, setPublishes] = useState<PublishRecord[]>([])
  const [selectedKeys, setSelectedKeys] = useState<string[]>([])
  const [tokens, setTokens] = useState<TokenRow[]>([])
  const [tokenInput, setTokenInput] = useState('')
  const [format, setFormat] = useState('yaml')
  const [inlineSecrets, setInlineSecrets] = useState(false)
  const [exported, setExported] = useState<ServiceExport | null>(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    setPublishes(await listPublishes())
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const primaryToken = tokens[0]?.token ?? ''
  const exportLabel = selectedKeys.length === 0
    ? `导出全部 ${publishes.length} 个服务`
    : `导出选中的 ${selectedKeys.length} 个服务`

  const generate = async () => {
    const created: ServiceToken = await newServiceToken()
    setTokens((rows) => [...rows, { name: `token-${rows.length + 1}`, token: created.token }])
  }

  const addTypedToken = () => {
    const value = tokenInput.trim()
    if (!value) {
      message.warning('token 不能为空')
      return
    }
    if (tokens.some((row) => row.token === value)) {
      message.warning('这个 token 已经加过了')
      return
    }
    setTokens((rows) => [...rows, { name: `token-${rows.length + 1}`, token: value }])
    setTokenInput('')
  }

  const runExport = async () => {
    setLoading(true)
    try {
      setExported(await exportServiceConfig({
        names: selectedKeys, format, inline_secrets: inlineSecrets, tokens,
      }))
    } catch (error) {
      message.error(String(error))
    } finally {
      setLoading(false)
    }
  }

  const download = () => {
    if (!exported) return
    const blob = new Blob([exported.content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = exported.filename
    link.click()
    URL.revokeObjectURL(url)
  }

  const copy = (text: string) => {
    void navigator.clipboard.writeText(text)
    message.success('已复制')
  }

  const firstService = exported?.services[0] ?? ''
  const path = exported && exported.services.length > 1
    ? `/v1/${firstService}/predict` : '/v1/predict'
  const startCommand = exported
    ? `python -m server.serve --config ${exported.filename}${primaryToken ? ` --token ${primaryToken}` : ''} --host 0.0.0.0 --port 8021`
    : 'python -m server.serve --config sqlstage-services.yaml --token <你的 token> --host 0.0.0.0 --port 8021'
  const curlCommand = `curl -X POST http://127.0.0.1:8021${path} -H "Authorization: Bearer ${primaryToken || '<你的 token>'}" -H "Content-Type: application/json" -d "{\\"question\\": \\"人口最多的城市\\"}"`

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
        开发模式负责调参和评测，调好后把「策略 + 连接 + token」导出成一份配置，
        在服务器上用服务模式起一个只做问答的最小服务：不挂工作台的管理接口，调用方拿不到你的配置。
        一份配置可以装多个服务，按名字路由。
      </Typography.Paragraph>

      <Card title="1 · 发布服务">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <PublishForm onSaved={refresh} />
          {publishes.length === 0
            ? <Empty description="还没有发布任何服务" />
            : (
              <Table rowKey="name" size="small" pagination={false} dataSource={publishes}
                rowSelection={{
                  selectedRowKeys: selectedKeys,
                  onChange: (keys) => {
                    setSelectedKeys(keys.map(String))
                    setExported(null)
                  },
                }}
                columns={[
                  { title: '服务名', dataIndex: 'name' },
                  { title: '策略', dataIndex: 'strategy_id' },
                  { title: '连接', dataIndex: 'connection_id' },
                  { title: '操作', key: 'op', render: (_value, row) => (
                    <Button danger size="small" onClick={() => {
                      void deletePublish(row.name).then(() => {
                        setSelectedKeys((keys) => keys.filter((key) => key !== row.name))
                        return refresh()
                      })
                    }}>删除</Button>
                  ) },
                ]} />
            )}
          <Typography.Text type="secondary">
            不勾选任何服务 = 导出全部；勾选后只导出选中的那几个。
          </Typography.Text>
        </Space>
      </Card>

      <Card title="2 · 配置导出与访问 token">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Space wrap size={12}>
            <Button onClick={() => { void generate() }}>生成 token</Button>
            <Input placeholder="或粘贴你自己指定的 token" value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)} style={{ width: 260 }} />
            <Button onClick={addTypedToken}>添加</Button>
          </Space>

          {tokens.length > 0 && (
            <Table rowKey="token" size="small" pagination={false} dataSource={tokens}
              columns={[
                { title: '名称', dataIndex: 'name', width: 120 },
                { title: 'token', dataIndex: 'token', render: (value: string) => (
                  <Typography.Text code copyable={{ text: value }}>{value}</Typography.Text>
                ) },
                { title: '操作', key: 'op', width: 80, render: (_value, row: TokenRow) => (
                  <Button size="small" onClick={() => setTokens((rows) =>
                    rows.filter((item) => item.token !== row.token))}>移除</Button>
                ) },
              ]} />
          )}

          <Space wrap size={16}>
            <Space size={8}>
              <Typography.Text>格式</Typography.Text>
              <Select value={format} onChange={setFormat} style={{ width: 100 }}
                options={[{ value: 'yaml', label: 'YAML' }, { value: 'json', label: 'JSON' }]} />
            </Space>
            <Space size={8}>
              <Typography.Text>内联密钥</Typography.Text>
              <Switch checked={inlineSecrets} onChange={setInlineSecrets} />
            </Space>
            <Button type="primary" loading={loading} onClick={() => void runExport()}>{exportLabel}</Button>
            {exported && <Button onClick={download}>下载文件</Button>}
          </Space>

          {inlineSecrets && (
            <Alert type="warning" showIcon message="内联密钥会把 API Key 和数据库密码明文写进配置文件，只在自己保管的机器上使用。" />
          )}
          {!inlineSecrets && (
            <Alert type="info" showIcon message="默认只写 ${ENV:...} 占位符，部署时先设置环境变量：DEEPSEEK_API_KEY、SQLSTAGE_DB_PASSWORD。Guide 导出时已展开成正文（配置里不带路径），few-shot 引用的文件会内联进配置，都不用担心路径失效。" />
          )}
          <Alert type="info" showIcon
            message="配置文件最上面的 deploy 段就是给部署用的：每个服务的数据库账号密码、模型 Key、访问 token 都在那里，改它会覆盖下面 services 段里的开发值。services 段保持完整导出，所有字段都在文件里。" />

          {exported?.warnings.map((warning) => (
            <Alert key={warning} type="warning" showIcon message={warning} />
          ))}

          {exported && (
            <div>
              <Space size={8} wrap>
                <Typography.Text strong>{exported.filename}</Typography.Text>
                {exported.services.map((name) => <Tag key={name} color="blue">{name}</Tag>)}
              </Space>
              <pre className="code" style={{ maxHeight: 320, overflow: 'auto' }}>{exported.content}</pre>
            </div>
          )}
        </Space>
      </Card>

      <Card title="3 · 服务器启动与调用">
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <div>
            <Typography.Text strong>启动命令</Typography.Text>
            <pre className="code">{startCommand}</pre>
            <Button size="small" onClick={() => copy(startCommand)}>复制</Button>
          </div>
          <div>
            <Typography.Text strong>调用示例</Typography.Text>
            <pre className="code">{curlCommand}</pre>
            <Button size="small" onClick={() => copy(curlCommand)}>复制</Button>
          </div>
          <Table rowKey="path" size="small" pagination={false} dataSource={ENDPOINTS}
            columns={[
              { title: '端点', dataIndex: 'path', render: (value: string) => <Typography.Text code>{value}</Typography.Text> },
              { title: '说明', dataIndex: 'desc' },
              { title: '鉴权', dataIndex: 'auth', width: 90,
                render: (value: string) => <Tag color={value === '免鉴权' ? 'green' : 'blue'}>{value}</Tag> },
            ]} />
          <Typography.Text type="secondary">
            配置里只有一个服务时，不带服务名的 /v1/predict 同样可用；多个服务必须带名字。
          </Typography.Text>
          <Alert type="info" showIcon
            message="启动时会对每个服务真连一次数据库：连上就打印读到几张表，连不上会打印原因和连接参数（密码打码）后照常启动，数据库修好后不用重启。想让它变成硬门槛就加 --strict，有服务连不上直接退出。" />
          <Alert type="warning" showIcon
            message="服务模式的数据库连接同样是只读的，模型幻觉出的写语句在 SQL 层就会被拦下。对外网开放时仍建议前面再套一层反向代理与限流。" />
        </Space>
      </Card>
    </Space>
  )
}
