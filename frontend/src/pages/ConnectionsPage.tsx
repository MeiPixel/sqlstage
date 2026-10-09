import { useState } from 'react'
import { Alert, App, Button, Card, Form, Input, InputNumber, Popconfirm, Select, Space, Switch, Table, Tag, Typography } from 'antd'
import { deleteConnection, saveConnection } from '../api'
import { useStudio } from '../StudioContext'
import type { ConnectionParams, ConnectionRecord } from '../types'

const EMPTY: ConnectionParams = {
  database: '', host: '127.0.0.1', port: 3306,
  user: 'root', password: '', mysql_db: '', read_only: true, pool_size: 5,
}

function parseParams(record: ConnectionRecord): ConnectionParams {
  const params = JSON.parse(record.params) as Partial<ConnectionParams>
  return { ...EMPTY, ...params, read_only: params.read_only !== false }
}

function summarize(record: ConnectionRecord) {
  const params = parseParams(record)
  const target = record.type === 'SQLite' ? params.database : `${params.host}:${params.port}/${params.mysql_db}`
  const mode = params.read_only ? '只读' : '可写'
  const pool = record.type === 'SQLite' ? '' : ` · 池 ${params.pool_size}`
  return `${target}（${mode}${pool}）`
}

export default function ConnectionsPage() {
  const { message } = App.useApp()
  const { connections, currentId, setCurrentId, refreshConnections } = useStudio()
  const [name, setName] = useState('')
  const [type, setType] = useState('SQLite')
  const [params, setParams] = useState<ConnectionParams>(EMPTY)

  const save = async () => {
    if (!name.trim()) {
      message.warning('请填写连接名称')
      return
    }
    const { id } = await saveConnection({ name: name.trim(), type, params })
    message.success(`已保存连接「${name}」并设为当前`)
    setCurrentId(id)
    await refreshConnections()
  }

  const load = (record: ConnectionRecord) => {
    setName(record.name)
    setType(record.type)
    setParams(parseParams(record))
  }

  const remove = async (id: number) => {
    await deleteConnection(id)
    message.success('已删除')
    await refreshConnections()
  }

  return (
    <Space orientation="vertical" size="large" style={{ width: '100%' }}>
      <Card title="连接参数">
        <Form layout="vertical">
          <Space wrap align="end">
            <Form.Item label="连接名称" style={{ marginBottom: 0 }}>
              <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 本地 MySQL" />
            </Form.Item>
            <Form.Item label="连接方式" style={{ marginBottom: 0 }}>
              <Select value={type} style={{ width: 150 }} onChange={setType}
                options={['SQLite', 'MySQL', 'PostgreSQL'].map((value) => ({ value, label: value }))} />
            </Form.Item>
          </Space>
          {type === 'SQLite' ? (
            <Form.Item label="SQLite 路径" style={{ marginTop: 16, marginBottom: 0 }}>
              <Input value={params.database} placeholder="例如 test.db"
                onChange={(event) => setParams({ ...params, database: event.target.value })} />
            </Form.Item>
          ) : (
            <Space wrap align="end" style={{ marginTop: 16 }}>
              <Form.Item label="Host" style={{ marginBottom: 0 }}>
                <Input value={params.host} onChange={(event) => setParams({ ...params, host: event.target.value })} />
              </Form.Item>
              <Form.Item label="端口" style={{ marginBottom: 0 }}>
                <InputNumber value={params.port}
                  onChange={(value) => setParams({ ...params, port: Number(value) })} />
              </Form.Item>
              <Form.Item label="用户" style={{ marginBottom: 0 }}>
                <Input value={params.user} onChange={(event) => setParams({ ...params, user: event.target.value })} />
              </Form.Item>
              <Form.Item label="密码" style={{ marginBottom: 0 }}>
                <Input.Password value={params.password}
                  onChange={(event) => setParams({ ...params, password: event.target.value })} />
              </Form.Item>
              <Form.Item label="数据库名" style={{ marginBottom: 0 }}>
                <Input value={params.mysql_db}
                  onChange={(event) => setParams({ ...params, mysql_db: event.target.value })} />
              </Form.Item>
              <Form.Item label="连接池容量" style={{ marginBottom: 0 }}>
                <InputNumber value={params.pool_size} min={0} max={64}
                  onChange={(value) => setParams({ ...params, pool_size: Number(value) })} />
              </Form.Item>
            </Space>
          )}
          <Form.Item label="只读连接" style={{ marginTop: 16, marginBottom: 0 }}>
            <Space>
              <Switch checked={params.read_only}
                onChange={(checked) => setParams({ ...params, read_only: checked })} />
              <Typography.Text type="secondary">
                {params.read_only
                  ? '只允许 SELECT：写语句在连接层与 SQL 层双重拦截'
                  : '允许写语句：模型生成的 INSERT / UPDATE / DELETE 会真实执行'}
              </Typography.Text>
            </Space>
          </Form.Item>
          {!params.read_only && (
            <Alert style={{ marginTop: 12 }} type="warning" showIcon
              title="已关闭只读保护。建议仅在离线临时库上使用，生产库请改用数据库侧只读账号。" />
          )}
          <div style={{ marginTop: 16 }}>
            <Button type="primary" onClick={save}>保存连接并设为当前</Button>
          </div>
        </Form>
      </Card>

      <Card title="已保存连接">
        <Table rowKey="id" dataSource={connections} pagination={false}
          columns={[
            { title: '名称', dataIndex: 'name' },
            { title: '方式', dataIndex: 'type', render: (value: string) => <Tag color="blue">{value}</Tag> },
            { title: '参数', render: (_, record) => summarize(record) },
            { title: '状态', width: 90, render: (_, record) => record.id === currentId ? <Tag color="green">当前</Tag> : null },
            {
              title: '操作', width: 230,
              render: (_, record) => (
                <Space>
                  <Button size="small" onClick={() => setCurrentId(record.id)}>设为当前</Button>
                  <Button size="small" onClick={() => load(record)}>编辑</Button>
                  <Popconfirm title="确认删除？" onConfirm={() => remove(record.id)}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>
    </Space>
  )
}
