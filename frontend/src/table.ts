export function withRowKeys<T extends object>(rows: T[]) {
  return rows.map((row, index) => ({ ...row, __rk: String(index) }))
}
