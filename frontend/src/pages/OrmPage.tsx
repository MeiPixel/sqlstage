import { useEffect, useState } from 'react'
import { App, Button, Card, Checkbox, Input, Select, Space, Table, Typography } from 'antd'
import { listOrmPresets } from '../api'
import { presetToOrm } from '../orm'
import { useStudio } from '../StudioContext'
import type { OrmPreset, StrategyConfig } from '../types'

type Rows = Record<string, string>

interface ColumnSpec {
  key: string
  label: string
  width?: number
}

function EditableTable<T extends Rows>({ title, hint, rows, columns, blank, onChange }: {
  title: string
  hint: string
  rows: T[]
  columns: ColumnSpec[]
  blank: T
  onChange: (rows: T[]) => void
}) {
  const update = (index: number, key: string, value: string) =>
    onChange(rows.map((row, position) => (position === index ? { ...row, [key]: value } : row)))
  const data = rows.map((row, index) => ({ ...row, __rk: row.__rk || `r${index}` }))

  return (
    <Card title={title} size="small">
      <p style={{ color: '#888', marginTop: 0 }}>{hint}</p>
      <Table rowKey="__rk" dataSource={data} pagination={false} size="small"
        columns={[
          ...columns.map((column) => ({
            title: column.label, key: column.key, width: column.width,
            render: (_: unknown, __: unknown, index: number) => (
              <Input value={rows[index][column.key]} placeholder=""
                onChange={(event) => update(index, column.key, event.target.value)} />
            ),
          })),
          {
            title: '', width: 70,
            render: (_: unknown, __: unknown, index: number) => (
              <Button size="small" danger
                onClick={() => onChange(rows.filter((_, position) => position !== index))}>删除</Button>
            ),
          },
        ]} />
      <Button style={{ marginTop: 8 }}
        onClick={() => onChange([...rows, { ...blank, __rk: `r${rows.length}-${Date.now()}` }])}>
        添加一行
      </Button>
    </Card>
  )
}

export default function OrmPage() {
  const { message } = App.useApp()
  const { strategy, updateOrm } = useStudio()
  const orm: StrategyConfig['orm'] = strategy.orm
  const [presets, setPresets] = useState<OrmPreset[]>([])
  const [presetName, setPresetName] = useState<string>()

  const applyPreset = (name: string) => {
    const preset = presets.find((row) => row.name === name)
    if (!preset) return
    setPresetName(name)
    updateOrm({ ...presetToOrm(preset), enabled: true })
    message.success(`已载入「${preset.description || preset.name}」并开启 ORM`)
  }

  useEffect(() => {
    void listOrmPresets().then((rows) => {
      setPresets(rows)
      // 只预填规则、不改 enabled：默认走直出 SQL，用户点到 ORM 方案时才开启
      if (rows.length && strategy.orm.field_match.length === 0 && strategy.orm.value_maps.length === 0) {
        setPresetName(rows[0].name)
        updateOrm(presetToOrm(rows[0]))
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <div className="page-head">
        <Typography.Title level={4} className="page-title">ORM 配置</Typography.Title>
        <Typography.Paragraph className="page-desc">
          开启后模型先生成结构化查询计划，再由下面的业务规则确定性编译成 SQL。每行一条规则，空行不生效。
        </Typography.Paragraph>
      </div>
      <Card title="ORM 模式">
        <Typography.Paragraph type="secondary">
          查询对象保留已有过滤、排序和 JOIN。自然语言追问会真实调用模型生成短操作（Token 不会记成零）；保存成参数模板后，换参数只需本地编译。
          字段联动、依赖、值映射与型号纠错在编译时执行，最终仍展示可检查的 SQL。
        </Typography.Paragraph>
        <Space wrap size="large">
          <Checkbox checked={orm.enabled} onChange={(event) => updateOrm({ enabled: event.target.checked })}>
            启用 ORM 查询计划
          </Checkbox>
          <Input style={{ width: 300 }} prefix="表名全限定前缀" placeholder="如 shop."
            value={orm.table_prefix} onChange={(event) => updateOrm({ table_prefix: event.target.value })} />
          <Checkbox checked={orm.quote_order_by}
            onChange={(event) => updateOrm({ quote_order_by: event.target.checked })}>
            ORDER BY 列加反引号（MySQL 聚合别名场景）
          </Checkbox>
        </Space>
        <Space wrap align="center" style={{ marginTop: 16 }}>
          <span style={{ color: '#888' }}>ORM 方案</span>
          <Select style={{ width: 360 }} value={presetName} placeholder="载入现成配置"
            onChange={applyPreset}
            options={presets.map((row) => ({ value: row.name, label: `${row.name} - ${row.description}` }))} />
        </Space>
      </Card>
      <EditableTable title="型号近似匹配" hint="填写 字段 或 表.字段。先将该字段前1000个去重值转换为字符 n-gram 向量候选，再用编辑距离限制自动替换；距离2表示最多两次增删改。只处理等值条件，不改 LIKE。未找到近似项时保留原值。"
        rows={orm.vector_fields} columns={[{ key: 'field', label: '字段（如 laptop.型号）' }, { key: 'distance', label: '最大编辑距离' }]}
        blank={{ field: '', distance: '2' }} onChange={(vector_fields) => updateOrm({ vector_fields })} />

      <EditableTable title="1. 字段联动" hint="命中前三列的筛选条件时，自动追加后三列条件。"
        rows={orm.link_rules} blank={{ when_field: '', when_op: '=', when_value: '', then_field: '', then_op: '=', then_value: '' }}
        columns={[
          { key: 'when_field', label: '触发字段' }, { key: 'when_op', label: '运算符', width: 90 },
          { key: 'when_value', label: '触发值' }, { key: 'then_field', label: '追加字段' },
          { key: 'then_op', label: '运算符', width: 90 }, { key: 'then_value', label: '追加值' },
        ]}
        onChange={(rows) => updateOrm({ link_rules: rows })} />

      <EditableTable title="2. 值映射" hint="把用户习惯说法规范成数据库真实存储值，例如 苹果 → Apple。字段可写 表名.字段名。"
        rows={orm.value_maps} blank={{ field: '', source: '', target: '' }}
        columns={[{ key: 'field', label: '字段' }, { key: 'source', label: '用户表达' }, { key: 'target', label: '数据库值' }]}
        onChange={(rows) => updateOrm({ value_maps: rows })} />

      <EditableTable title="3. 单位换算" hint="筛选值是“数字+单位”时乘以换算倍率，例如 重量_g | kg | 1000。"
        rows={orm.unit_rules} blank={{ field: '', unit: '', factor: '' }}
        columns={[{ key: 'field', label: '字段' }, { key: 'unit', label: '输入单位' }, { key: 'factor', label: '换算倍率' }]}
        onChange={(rows) => updateOrm({ unit_rules: rows })} />

      <EditableTable title="4. 字段依赖" hint="使用左侧字段时自动把右侧字段加入 SELECT。"
        rows={orm.required_fields} blank={{ field: '', required: '' }}
        columns={[{ key: 'field', label: '使用字段' }, { key: 'required', label: '必须同时返回字段' }]}
        onChange={(rows) => updateOrm({ required_fields: rows })} />

      <EditableTable title="5. 互斥字段" hint="同一行内的字段不能同时出现在筛选条件中，违反直接报错。每行至少两个字段。"
        rows={orm.mutex_fields} blank={{ a: '', b: '', c: '' }}
        columns={[{ key: 'a', label: '互斥字段 A' }, { key: 'b', label: '互斥字段 B' }, { key: 'c', label: '互斥字段 C' }]}
        onChange={(rows) => updateOrm({ mutex_fields: rows })} />

      <EditableTable title="6. 派生字段" hint="可复用计算表达式，例如 折后价 | 原价 * (1 - 折扣率)。"
        rows={orm.derived_fields} blank={{ name: '', expression: '' }}
        columns={[{ key: 'name', label: '派生字段名' }, { key: 'expression', label: 'SQL 表达式' }]}
        onChange={(rows) => updateOrm({ derived_fields: rows })} />

      <EditableTable title="7. 业务指标" hint="统一统计口径，例如 销售额 | SUM(实付金额)。"
        rows={orm.metrics} blank={{ name: '', expression: '' }}
        columns={[{ key: 'name', label: '指标名称' }, { key: 'expression', label: 'SQL 表达式' }]}
        onChange={(rows) => updateOrm({ metrics: rows })} />

      <EditableTable title="8. 自动 JOIN" hint="触发字段出现时确定性加入指定表和 ON 条件，例如 用户名称 | users | orders.user_id = users.id。"
        rows={orm.join_rules} blank={{ trigger_field: '', table: '', on: '' }}
        columns={[{ key: 'trigger_field', label: '触发字段' }, { key: 'table', label: '关联表' }, { key: 'on', label: 'ON 条件' }]}
        onChange={(rows) => updateOrm({ join_rules: rows })} />

      <EditableTable title="9. 字段匹配方式" hint="该字段筛选时强制用包含匹配（LIKE）或前缀匹配，默认等值。字段写 表名.字段名。"
        rows={orm.field_match} blank={{ field: '', mode: 'contains' }}
        columns={[{ key: 'field', label: '字段' }, { key: 'mode', label: '匹配方式', width: 160 }]}
        onChange={(rows) => updateOrm({ field_match: rows })} />

      <EditableTable title="10. 数值转换" hint="字段以文本存储数字，比较时自动 CAST 成 REAL，例如 室内机重量_kg。"
        rows={orm.numeric_cast} blank={{ field: '' }}
        columns={[{ key: 'field', label: '字段' }]}
        onChange={(rows) => updateOrm({ numeric_cast: rows })} />

      <EditableTable title="11. 字段别名" hint="用户说法落到别名时替换为真实字段，例如 headphones.耳机类型 的别名 佩戴方式，多个用逗号分隔。"
        rows={orm.field_alias} blank={{ field: '', alias: '' }}
        columns={[{ key: 'field', label: '真实字段' }, { key: 'alias', label: '别名（逗号分隔）' }]}
        onChange={(rows) => updateOrm({ field_alias: rows })} />
    </Space>
  )
}
