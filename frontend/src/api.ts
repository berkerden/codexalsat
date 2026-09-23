import type {
  ApiErrorBody,
  MarketResponse,
  PaperState,
  ResearchReport,
  ResearchRequest,
  StatusResponse,
  Venue,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody | null

  constructor(message: string, status: number, body: ApiErrorBody | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let body: ApiErrorBody | null = null
    try {
      body = (await response.json()) as ApiErrorBody
    } catch {
      // An empty or non-JSON error still carries a useful HTTP status.
    }
    const detail = typeof body?.detail === 'string'
      ? body.detail
      : body?.detail && !Array.isArray(body.detail) && typeof body.detail === 'object' && typeof body.detail.message === 'string'
        ? body.detail.message
        : body?.message
    throw new ApiError(detail ?? `İstek başarısız (${response.status})`, response.status, body)
  }
  return (await response.json()) as T
}

export const api = {
  status: (signal?: AbortSignal) => request<StatusResponse>('/api/status', { signal }),
  async symbols(venue: Venue, signal?: AbortSignal): Promise<string[]> {
    const data = await request<unknown>(`/api/symbols?venue=${encodeURIComponent(venue)}`, { signal })
    if (Array.isArray(data)) {
      return data.flatMap((item) =>
        typeof item === 'string'
          ? [item]
          : item && typeof item === 'object' && 'symbol' in item && typeof item.symbol === 'string'
            ? [item.symbol]
            : [],
      )
    }
    if (data && typeof data === 'object' && 'symbols' in data && Array.isArray(data.symbols)) {
      return data.symbols.flatMap((item) =>
        typeof item === 'string'
          ? [item]
          : item && typeof item === 'object' && 'symbol' in item && typeof item.symbol === 'string'
            ? [item.symbol]
            : [],
      )
    }
    return []
  },
  market: (venue: Venue, symbol: string, interval: string, signal?: AbortSignal) =>
    request<MarketResponse>(
      `/api/market?venue=${encodeURIComponent(venue)}&symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`,
      { signal },
    ),
  research: (payload: ResearchRequest) =>
    request<ResearchReport>('/api/research', { method: 'POST', body: JSON.stringify(payload) }),
  paper: (signal?: AbortSignal) => request<PaperState>('/api/paper', { signal }),
  paperStart: (config: Record<string, unknown>) =>
    request<PaperState>('/api/paper/start', { method: 'POST', body: JSON.stringify(config) }),
  paperStop: () => request<PaperState>('/api/paper/stop', { method: 'POST', body: '{}' }),
  paperClose: () => request<PaperState>('/api/paper/close', { method: 'POST', body: '{}' }),
}

export function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === 'AbortError') return ''
  if (error instanceof Error) return error.message
  return 'Beklenmeyen bir hata oluştu.'
}
