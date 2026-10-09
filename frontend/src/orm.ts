import type {
  DependencyRow, JoinRow, MappingRow, MutexRow, NamedRow, OrmConfig, OrmPreset, RuleRow,
  StrategyConfig, UnitRow,
} from './types'
import { DEFAULT_STRATEGY } from './strategy'

/** 把 orm_presets/*.json 的紧凑格式摊平成界面上的行。
 *  是否启用由调用方决定：预填规则不应该顺手把 ORM 打开。 */
export function presetToOrm(preset: OrmPreset): Partial<OrmConfig> {
  return {
    field_match: Object.entries(preset.field_match).map(([field, mode]) => ({ field, mode })),
    value_maps: Object.entries(preset.value_maps).flatMap(([field, mapping]) =>
      Object.entries(mapping).map(([source, target]) => ({ field, source, target }))),
    numeric_cast: preset.numeric_cast.map((field) => ({ field })),
    field_alias: Object.entries(preset.field_alias).map(([field, alias]) =>
      ({ field, alias: alias.join(', ') })),
  }
}

export function fromBackendStrategy(raw: any): StrategyConfig {
  const orm: any = raw?.orm ?? {}
  return {
    name: raw?.name ?? 'default',
    model: { ...DEFAULT_STRATEGY.model, ...(raw?.model ?? {}) },
    tricks: { ...DEFAULT_STRATEGY.tricks, ...(raw?.tricks ?? {}) },
    orm: {
      ...DEFAULT_STRATEGY.orm,
      enabled: Boolean(orm.enabled),
      table_prefix: orm.table_prefix ?? '',
      quote_order_by: Boolean(orm.quote_order_by),
      link_rules: (orm.link_rules ?? []).map((rule: any) => ({
        when_field: rule.when?.field ?? '', when_op: rule.when?.op ?? '=',
        when_value: rule.when?.value ?? '', then_field: rule.then?.[0]?.field ?? '',
        then_op: rule.then?.[0]?.op ?? '=', then_value: rule.then?.[0]?.value ?? '',
      })),
      value_maps: Object.entries(orm.value_maps ?? {}).flatMap(([field, mapping]) =>
        Object.entries(mapping as Record<string, unknown>)
          .map(([source, target]) => ({ field, source, target: String(target) }))),
      unit_rules: Object.entries(orm.unit_rules ?? {}).flatMap(([field, mapping]) =>
        Object.entries(mapping as Record<string, unknown>)
          .map(([unit, factor]) => ({ field, unit, factor: String(factor) }))),
      required_fields: Object.entries(orm.required_fields ?? {}).flatMap(([field, required]) =>
        (required as string[]).map((item) => ({ field, required: item }))),
      mutex_fields: (orm.mutex_fields ?? []).map((fields: string[]) =>
        ({ a: fields[0] ?? '', b: fields[1] ?? '', c: fields[2] ?? '' })),
      derived_fields: Object.entries(orm.derived_fields ?? {})
        .map(([name, expression]) => ({ name, expression: String(expression) })),
      metrics: Object.entries(orm.metrics ?? {})
        .map(([name, expression]) => ({ name, expression: String(expression) })),
      join_rules: (orm.join_rules ?? []).map((rule: any) => ({
        trigger_field: rule.trigger_field ?? '', table: rule.join?.table ?? '', on: rule.join?.on ?? '',
      })),
      field_match: Object.entries(orm.field_match ?? {}).map(([field, mode]) => ({ field, mode: String(mode) })),
      numeric_cast: (orm.numeric_cast ?? []).map((field: string) => ({ field })),
      field_alias: Object.entries(orm.field_alias ?? {}).map(([field, alias]) =>
        ({ field, alias: (alias as string[]).join(', ') })),
      vector_fields: Object.entries(orm.vector_fields ?? {}).map(([field, distance]) => ({ field, distance: String(distance) })),
    },
  }
}

function groupValues(rows: MappingRow[]) {
  const result: Record<string, Record<string, string>> = {}
  for (const row of rows) {
    if (!row.field || !row.source) continue
    result[row.field] = { ...(result[row.field] ?? {}), [row.source]: row.target }
  }
  return result
}

function groupUnits(rows: UnitRow[]) {
  const result: Record<string, Record<string, number>> = {}
  for (const row of rows) {
    if (!row.field || !row.unit || !row.factor) continue
    result[row.field] = { ...(result[row.field] ?? {}), [row.unit]: Number(row.factor) }
  }
  return result
}

function groupLists(rows: DependencyRow[]) {
  const result: Record<string, string[]> = {}
  for (const row of rows) {
    if (!row.field || !row.required) continue
    result[row.field] = [...(result[row.field] ?? []), row.required]
  }
  return result
}

function groupNamed(rows: NamedRow[]) {
  return Object.fromEntries(rows.filter((row) => row.name && row.expression)
    .map((row) => [row.name, row.expression]))
}

function toRules(rows: RuleRow[]) {
  return rows.filter((row) => row.when_field && row.then_field).map((row) => ({
    when: { field: row.when_field, op: row.when_op, value: row.when_value },
    then: [{ field: row.then_field, op: row.then_op, value: row.then_value }],
  }))
}

function toMutex(rows: MutexRow[]) {
  return rows.map((row) => [row.a, row.b, row.c].filter(Boolean))
    .filter((fields) => fields.length > 1)
}

function toJoins(rows: JoinRow[]) {
  return rows.filter((row) => row.trigger_field && row.table && row.on)
    .map((row) => ({ trigger_field: row.trigger_field, join: { table: row.table, on: row.on } }))
}

export function toBackendStrategy(strategy: StrategyConfig) {
  const orm: OrmConfig = strategy.orm
  return {
    name: strategy.name,
    model: strategy.model,
    tricks: strategy.tricks,
    orm: {
      enabled: orm.enabled,
      link_rules: toRules(orm.link_rules),
      value_maps: groupValues(orm.value_maps),
      unit_rules: groupUnits(orm.unit_rules),
      required_fields: groupLists(orm.required_fields),
      mutex_fields: toMutex(orm.mutex_fields),
      derived_fields: groupNamed(orm.derived_fields),
      metrics: groupNamed(orm.metrics),
      join_rules: toJoins(orm.join_rules),
      table_prefix: orm.table_prefix,
      quote_order_by: orm.quote_order_by,
      field_match: Object.fromEntries(orm.field_match
        .filter((row) => row.field && row.mode).map((row) => [row.field, row.mode])),
      numeric_cast: orm.numeric_cast.filter((row) => row.field).map((row) => row.field),
      field_alias: Object.fromEntries(orm.field_alias
        .filter((row) => row.field && row.alias)
        .map((row) => [row.field, row.alias.split(',').map((item) => item.trim()).filter(Boolean)])),
      vector_fields: Object.fromEntries(orm.vector_fields.filter((row) => row.field)
        .map((row) => [row.field, Number(row.distance)])),
    },
  }
}
