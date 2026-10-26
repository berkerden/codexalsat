export type Venue = 'global' | 'tr'
export type AppMode = 'suggestions' | 'paper' | 'live'
export type Tab = 'overview' | 'research' | 'paper' | 'operations'

export interface StatusResponse {
  version: string
  venue: string | null
  mode: string
  live_enabled: boolean
  blockers: string[]
  runtime: Record<string, unknown>
  data_healthy?: boolean
  stream_status?: string
  worker_error?: string | null
  commit?: string
}

export interface Candle {
  open_time: number
  close_time: number
  open: string
  high: string
  low: string
  close: string
  volume: string
}

export interface Quote {
  bid: string
  ask: string
  spread_bps: string
  observed_at: number
  bid_quantity?: string
  ask_quantity?: string
}

export interface MarketQuality {
  healthy: boolean
  issues: Array<string | { code?: string; message?: string; [key: string]: unknown }>
}

export interface Signal {
  action: string
  reason: string
  strategy?: string
  observed_at?: number
  confidence?: string
  entry?: string
  target?: string
  stop?: string
  costs?: Record<string, unknown>
  [key: string]: unknown
}

export interface MarketResponse {
  symbol: string
  interval: string
  source: string
  candles: Candle[]
  quote: Quote | null
  quality: MarketQuality
  signal: Signal | null
}

export interface ResearchCosts {
  entry_fee: string
  exit_fee: string
  spread_bps: string
  slippage_bps: string
}

export interface ResearchRequest {
  venue: Venue
  symbol: string
  interval: string
  days: number
  hold_minutes: number
  costs: ResearchCosts
}

export type ResearchReport = Record<string, unknown>

export interface PaperPosition {
  symbol?: string
  quantity?: string
  average_price?: string
  unrealized_pnl?: string | null
  [key: string]: unknown
}

export interface PaperState {
  active?: boolean
  config?: Record<string, unknown> | null
  cash?: string
  equity?: string
  position?: PaperPosition | null
  orders?: unknown[]
  fills?: unknown[]
  entries_enabled?: boolean
  reason?: string | null
  [key: string]: unknown
}

export interface ApiErrorBody {
  detail?: string | { message?: string; blockers?: string[] } | unknown[]
  message?: string
  blockers?: string[]
}

export type TestnetSymbol = 'BTCUSDT' | 'SOLUSDT'
export type TestnetWatcherPhase = 'idle' | 'starting' | 'running' | 'stopping' | 'stopped' | 'attention' | 'closed'
export type TestnetWatcherHealth = 'idle' | 'starting' | 'healthy' | 'retrying' | 'stale' | 'stopping' | 'stopped' | 'attention' | 'closed'

export interface TestnetCycleSummary {
  cycle_id: string
  symbol: TestnetSymbol
  state: string
  stop_entry_requested: boolean
  updated_at_ms: number
}

export interface WatchSnapshot {
  cycle_id?: string
  mode?: string
  production_enabled?: boolean
  state?: string
  reason?: string | null
  stop_entry_requested?: boolean | null
  entry_state?: string | null
  protection_state?: string | null
  owned_quantity?: string | null
  dust_quantity?: string | null
  unprotected_quantity?: string | null
  checked_at_ms?: number | null
  last_success_at_ms?: number | null
  observed_at_ms?: number | null
  coverage_observation_fresh?: boolean
  consecutive_failures?: number
  retry_in_seconds?: number
  resume_required?: boolean
  [key: string]: unknown
}

export interface TestnetStatusResponse {
  mode: 'testnet'
  production_enabled: false
  server_instance_id: string
  observed_at_ms: number
  ledger_status: 'ready' | 'missing' | 'unavailable'
  cycles: TestnetCycleSummary[]
  watcher: {
    run_id: string | null
    cycle_id: string | null
    symbol: TestnetSymbol | null
    allow_mutations: boolean
    phase: TestnetWatcherPhase
    health: TestnetWatcherHealth
    manager_running: boolean
    coverage_observation_fresh: boolean
    snapshot: WatchSnapshot | null
  }
}

export interface TestnetSessionResponse {
  server_instance_id: string
  control_token: string
}
