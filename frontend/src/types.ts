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
