import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import { configDefaults, listConnections } from './api'
import { fromBackendStrategy } from './orm'
import { DEFAULT_STRATEGY } from './strategy'
import type { ConnectionRecord, ModelConfig, OrmConfig, StrategyConfig, TrickConfig } from './types'

const CURRENT_KEY = 'studio.currentConnectionId'

interface StudioContextValue {
  strategy: StrategyConfig
  setStrategy: (strategy: StrategyConfig) => void
  updateModel: (patch: Partial<ModelConfig>) => void
  updateTricks: (patch: Partial<TrickConfig>) => void
  updateOrm: (patch: Partial<OrmConfig>) => void
  connections: ConnectionRecord[]
  current: ConnectionRecord | null
  currentId: number | null
  setCurrentId: (id: number | null) => void
  refreshConnections: () => Promise<void>
}

const StudioContext = createContext<StudioContextValue | null>(null)

export function StudioProvider({ children }: { children: ReactNode }) {
  const [strategy, setStrategy] = useState<StrategyConfig>(DEFAULT_STRATEGY)
  const [connections, setConnections] = useState<ConnectionRecord[]>([])
  const [currentId, setCurrentId] = useState<number | null>(() => {
    const raw = localStorage.getItem(CURRENT_KEY)
    return raw ? Number(raw) : null
  })

  const refreshConnections = useCallback(async () => {
    const rows = await listConnections()
    setConnections(rows)
    setCurrentId((id) => (rows.some((row) => row.id === id) ? id : rows[0]?.id ?? null))
  }, [])

  useEffect(() => { void refreshConnections() }, [refreshConnections])
  useEffect(() => {
    if (currentId !== null) localStorage.setItem(CURRENT_KEY, String(currentId))
  }, [currentId])

  // 后端 dataclass 才是默认值的唯一来源，strategy.ts 那份只作首屏兜底，
  // 避免后端改了默认值而界面仍显示旧值。
  useEffect(() => {
    void configDefaults().then((raw) => setStrategy(fromBackendStrategy(raw)))
  }, [])

  const value: StudioContextValue = {
    strategy,
    setStrategy,
    updateModel: (patch) => setStrategy((previous) => ({ ...previous, model: { ...previous.model, ...patch } })),
    updateTricks: (patch) => setStrategy((previous) => ({ ...previous, tricks: { ...previous.tricks, ...patch } })),
    updateOrm: (patch) => setStrategy((previous) => ({ ...previous, orm: { ...previous.orm, ...patch } })),
    connections,
    current: connections.find((row) => row.id === currentId) ?? null,
    currentId,
    setCurrentId,
    refreshConnections,
  }

  return <StudioContext.Provider value={value}>{children}</StudioContext.Provider>
}

export function useStudio() {
  const value = useContext(StudioContext)
  if (!value) throw new Error('useStudio 必须在 StudioProvider 内使用')
  return value
}
