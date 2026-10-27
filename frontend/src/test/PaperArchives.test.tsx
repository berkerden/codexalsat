import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from '../App'

const id = '11111111-1111-4111-8111-111111111111'
const archiveId = '33333333-3333-4333-8333-333333333333'
const current = { session_id: id, started_at: 1700000000000, config: { symbol: 'BTCUSDT', interval: '5m', strategy: 'pullback', capital: '1000' }, entries_enabled: false, position: null, cash: '1000', equity: '1000', orders: [], fills: [] }
const archived = { session_id: archiveId, started_at: 1700000000000, archived_at: 1700001000000, config: { symbol: 'SOLUSDT', interval: '15m' }, cash: '90', equity: '90', realized_pnl: '-10', order_count: 2, fill_count: 2 }
const detail = { session_id: archiveId, archived_at: archived.archived_at, state: { ...current, ...archived, orders: [{}, {}], fills: [{}, {}] } }
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
let paperRead: () => Promise<Response>
let archiveRead: (path: string) => Promise<Response>
let historyRead: () => Promise<Response>
let paperArchive: () => Promise<Response>
let pollers: Array<() => void>

beforeEach(() => {
  paperRead = () => json(current)
  archiveRead = () => json({ items: [archived], total: 1 })
  historyRead = () => json(detail)
  paperArchive = () => json({ ...current, session_id: '22222222-2222-4222-8222-222222222222', config: null, started_at: null, cash: '0', equity: '0' })
  pollers = []
  vi.spyOn(window, 'setInterval').mockImplementation((handler, delay) => {
    if (delay === 3000 && typeof handler === 'function') pollers.push(handler as () => void)
    return 1
  })
  vi.spyOn(window, 'clearInterval').mockImplementation(() => {})
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/paper') return paperRead()
    if (path === '/api/paper/archive') return paperArchive()
    if (path.startsWith('/api/paper/archives?')) return archiveRead(path)
    if (path.startsWith('/api/paper/archives/')) return historyRead()
    if (path === '/api/status') return json({ version: 'test', mode: 'paper', live_enabled: false, blockers: [], runtime: {} })
    if (path.startsWith('/api/symbols')) return json({ symbols: ['BTCUSDT', 'SOLUSDT'] })
    if (path.startsWith('/api/market')) return json({ symbol: 'BTCUSDT', interval: '5m', source: 'fixture', candles: [], quote: null, quality: { healthy: false, issues: [] }, signal: null })
    return json({ detail: 'Unexpected fixture route' }, 404)
  }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function openPaper() {
  render(<App />)
  await waitFor(() => expect(screen.getByRole('combobox', { name: /Spot parite/ })).toHaveValue('BTCUSDT'))
  await userEvent.click(screen.getByRole('button', { name: /Paper işlem/ }))
  await screen.findByText('Sabit kayıt')
  return screen.getByRole('region', { name: 'Deney arşivi' })
}

it('aynı arşivi yeniden açar ve hata sonrası aynı kaydı tekrar okuyabilir', async () => {
  let reads = 0
  historyRead = () => ++reads === 3 ? json({ detail: 'Geçici okuma hatası' }, 503) : json(detail)
  const region = await openPaper()
  const row = await within(region).findByRole('button', { name: /SOLUSDT/ })
  for (let index = 0; index < 2; index++) {
    await userEvent.click(row)
    expect(await within(region).findByText('PAPER · SALT OKUNUR')).toBeInTheDocument()
    expect(within(region).queryByText('PAPER kaydı açılıyor…')).not.toBeInTheDocument()
  }
  await userEvent.click(row)
  expect(await within(region).findByRole('alert')).toHaveTextContent('Geçici okuma hatası')
  await userEvent.click(row)
  expect(await within(region).findByText('PAPER · SALT OKUNUR')).toBeInTheDocument()
  expect(within(region).queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Sanal sermaye (USDT)')).toHaveValue('1000')
  expect(screen.getByText(/Oturum:/)).toHaveTextContent('BTCUSDT · 5m')
  expect(reads).toBe(4)
})

it('arşiv sayfalamasında gerçek toplamı ve istenen kaydı kullanır', async () => {
  archiveRead = (path) => json({ items: path.includes('offset=20') ? [{ ...archived, session_id: 'last', config: { symbol: 'BTCUSDT', interval: '1h' } }] : Array.from({ length: 20 }, (_, index) => ({ ...archived, session_id: `archive-${index}` })), total: 21 })
  const region = await openPaper()
  expect(await within(region).findByText('1–20 / 21')).toBeInTheDocument()
  await userEvent.click(within(region).getByRole('button', { name: 'Sonraki' }))
  expect(await within(region).findByText('21–21 / 21')).toBeInTheDocument()
  expect(within(region).getByRole('button', { name: 'Sonraki' })).toBeDisabled()
  expect(within(region).getByRole('button', { name: /BTCUSDT/ })).toBeInTheDocument()
  await userEvent.click(within(region).getByRole('button', { name: 'Önceki' }))
  expect(await within(region).findByText('1–20 / 21')).toBeInTheDocument()
})

it('arşivlemeden önce başlamış geç durum yanıtı yeni boş formu geri alamaz', async () => {
  await openPaper()
  let resolveOld!: (value: Response) => void
  paperRead = () => new Promise<Response>((resolve) => { resolveOld = resolve })
  await act(async () => { pollers.forEach((poll) => poll()) })
  await userEvent.click(screen.getByRole('checkbox', { name: /arşivlemek istediğimi onaylıyorum/ }))
  await userEvent.click(screen.getByRole('button', { name: 'PAPER oturumunu arşivle' }))
  expect(await screen.findByRole('button', { name: 'PAPER oturumunu başlat' })).toBeEnabled()
  await act(async () => { resolveOld(await json(current)) })
  expect(screen.getByRole('button', { name: 'PAPER oturumunu başlat' })).toBeEnabled()
  expect(screen.getByLabelText('Sanal sermaye (USDT)')).toBeEnabled()
  expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path) === '/api/paper/start')).toBe(false)
})
