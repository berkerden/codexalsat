import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TestnetPanel } from '../components/TestnetPanel'
import type { TestnetStatusResponse } from '../types'

const status = (overrides: Partial<TestnetStatusResponse> = {}): TestnetStatusResponse => ({
  mode: 'testnet',
  production_enabled: false,
  server_instance_id: 'server-a',
  observed_at_ms: Date.now(),
  ledger_status: 'ready',
  cycles: [
    { cycle_id: 'cycle-btc', symbol: 'BTCUSDT', state: 'PROTECTED', stop_entry_requested: false, updated_at_ms: Date.now() },
    { cycle_id: 'cycle-sol', symbol: 'SOLUSDT', state: 'ENTRY_SUBMITTED', stop_entry_requested: false, updated_at_ms: Date.now() },
    { cycle_id: 'cycle-closed', symbol: 'BTCUSDT', state: 'EXITED', stop_entry_requested: true, updated_at_ms: Date.now() },
  ],
  watcher: {
    run_id: null,
    cycle_id: null,
    symbol: null,
    allow_mutations: false,
    phase: 'idle',
    health: 'idle',
    manager_running: false,
    coverage_observation_fresh: false,
    snapshot: null,
  },
  ...overrides,
})

function json(value: unknown, statusCode = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status: statusCode, headers: { 'Content-Type': 'application/json' } }))
}

describe('TestnetPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/testnet/status') return json(status())
      if (path === '/api/testnet/session') return json({ server_instance_id: 'server-a', control_token: 'secret-token' })
      if (path === '/api/testnet/watch/start') return json(status({ watcher: { ...status().watcher, run_id: 'run-1', cycle_id: 'cycle-btc', symbol: 'BTCUSDT', phase: 'running', health: 'healthy', manager_running: true } }))
      return json({ detail: 'Tanımsız test isteği' }, 404)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it.each([
    ['missing', 'Testnet kayıt defteri bulunamadı'],
    ['unavailable', 'Testnet kayıt defterine şu anda erişilemiyor.'],
  ] as const)('%s kayıt defteri durumunu açıkça gösterir', async (ledgerStatus, message) => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => String(input) === '/api/testnet/status' ? json(status({ ledger_status: ledgerStatus, cycles: [] })) : json({}, 404))
    render(<TestnetPanel />)
    expect(await screen.findByText(message)).toBeInTheDocument()
  })

  it('boş ve hata durumlarını, otomatik kontrol isteği göndermeden gösterir', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => String(input) === '/api/testnet/status' ? json(status({ cycles: [] })) : json({}, 404))
    render(<TestnetPanel />)
    expect(await screen.findByText('İzlenebilir açık döngü yok')).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/watch/'), expect.anything())
    cleanup()
    vi.mocked(fetch).mockRejectedValue(new Error('bağlantı yok'))
    render(<TestnetPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('bağlantı yok')
  })

  it('varsayılan salt okunur başlatmayı false onay ve bellek içi oturum başlığıyla gönderir', async () => {
    render(<TestnetPanel />)
    await screen.findByRole('combobox', { name: 'Testnet döngüsü' })
    expect(screen.getByRole('checkbox', { name: /Korumayı yönet/ })).not.toBeChecked()
    const readOnlyStart = screen.getByRole('button', { name: 'Yalnız izle' })
    await waitFor(() => expect(readOnlyStart).toBeEnabled())
    await userEvent.click(readOnlyStart)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/testnet/watch/start', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-Spotlab-Control-Token': 'secret-token' }),
      body: JSON.stringify({ cycle_id: 'cycle-btc', confirm_testnet: false }),
    })))
    expect(localStorage.length).toBe(0)
  })

  it('koruma yönetimini ancak ayrı açık onayla true gönderir', async () => {
    render(<TestnetPanel />)
    const consent = await screen.findByRole('checkbox', { name: /Korumayı yönet/ })
    await userEvent.click(consent)
    const manageStart = screen.getByRole('button', { name: 'Korumayı yönet' })
    await waitFor(() => expect(manageStart).toBeEnabled())
    await userEvent.click(manageStart)
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/testnet/watch/start', expect.objectContaining({ body: JSON.stringify({ cycle_id: 'cycle-btc', confirm_testnet: true }) })))
  })

  it('döngü seçimi ve sunucu örneği değiştiğinde açık onayı temizler', async () => {
    let current = status()
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => String(input) === '/api/testnet/status' ? json(current) : json({}, 404))
    render(<TestnetPanel />)
    const consent = await screen.findByRole('checkbox', { name: /Korumayı yönet/ })
    await userEvent.click(consent)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Testnet döngüsü' }), 'cycle-sol')
    expect(consent).not.toBeChecked()
    await userEvent.click(consent)
    current = status({ server_instance_id: 'server-b' })
    await waitFor(() => expect(consent).not.toBeChecked(), { timeout: 3_000 })
  })

  it('seçili döngü sunucudan kaybolunca yeni seçime geçer ve açık onayı temizler', async () => {
    let current = status()
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => String(input) === '/api/testnet/status' ? json(current) : json({}, 404))
    render(<TestnetPanel />)
    const select = await screen.findByRole('combobox', { name: 'Testnet döngüsü' })
    await userEvent.selectOptions(select, 'cycle-sol')
    const consent = screen.getByRole('checkbox', { name: /Korumayı yönet/ })
    await userEvent.click(consent)
    current = status({ cycles: [status().cycles[0]] })
    await waitFor(() => expect(select).toHaveValue('cycle-btc'), { timeout: 3_000 })
    expect(consent).not.toBeChecked()
  })

  it('yalnız yanıttaki etkin run id hedefini durdurur', async () => {
    const running = status({ ledger_status: 'unavailable', cycles: [], watcher: { ...status().watcher, run_id: 'run/exact', cycle_id: 'cycle-btc', symbol: 'BTCUSDT', phase: 'running', health: 'healthy', manager_running: true } })
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/testnet/status') return json(running)
      if (path === '/api/testnet/session') return json({ server_instance_id: 'server-a', control_token: 'secret-token' })
      if (path === '/api/testnet/watch/run%2Fexact/stop') return json(status())
      return json({}, 404)
    })
    render(<TestnetPanel />)
    await userEvent.click(await screen.findByRole('button', { name: 'İzleyiciyi durdur' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/testnet/watch/run%2Fexact/stop', expect.objectContaining({ body: '{}' })))
  })

  it('beş saniyeyi aşan veya hata alan görünümü korumalı diye sunmaz ve maruziyet uyarısını korur', async () => {
    const staleProtected = status({
      observed_at_ms: Date.now(),
      watcher: {
        ...status().watcher,
        run_id: 'run-1', cycle_id: 'cycle-btc', symbol: 'BTCUSDT', phase: 'running', health: 'healthy', manager_running: true,
        coverage_observation_fresh: true,
        snapshot: { state: 'PROTECTED', reason: 'unknown_internal_reason', owned_quantity: '0.01', unprotected_quantity: '0', coverage_observation_fresh: true, observed_at_ms: Date.now() - 6_000 },
      },
    })
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => String(input) === '/api/testnet/status' ? json(staleProtected) : json({}, 404))
    render(<TestnetPanel />)
    expect(await screen.findByText('Durum güncel değil')).toBeInTheDocument()
    expect(screen.queryByText('Koruma güncel')).not.toBeInTheDocument()
    expect(screen.getByText('Koruma doğrulanmadı')).toBeInTheDocument()
    expect(screen.getByText('Korumasız miktar').parentElement).toHaveTextContent('0.01')
    expect(screen.getByText('Açık Testnet maruziyeti var')).toBeInTheDocument()
    expect(screen.getByText('Testnet izleyicisi dikkat gerektiren bir durum bildirdi.')).toBeInTheDocument()
  })


  it.each(['start', 'stop'] as const)('gecikmiş eski sorgu %s sonucunu geri alamaz', async (action) => {
    const running = status({ watcher: { ...status().watcher, run_id: 'run-1', cycle_id: 'cycle-btc', symbol: 'BTCUSDT', phase: 'running', health: 'healthy', manager_running: true, coverage_observation_fresh: true,
      snapshot: { state: 'PROTECTED', reason: null, owned_quantity: '0.01', unprotected_quantity: '0', coverage_observation_fresh: true, observed_at_ms: Date.now() },
    } })
    const initial = action === 'stop' ? running : status()
    const completed = action === 'stop' ? status({ watcher: { ...running.watcher, phase: 'stopped', health: 'stopped', manager_running: false, coverage_observation_fresh: false } }) : running
    let polls = 0
    let release!: (response: Response) => void
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/testnet/status') {
        polls += 1
        if (polls === 2) return new Promise<Response>((resolve) => { release = resolve })
        return json(polls > 2 ? completed : initial)
      }
      if (path === '/api/testnet/session') return json({ server_instance_id: 'server-a', control_token: 'token' })
      if (path.includes('/watch/')) return json(completed)
      return json({}, 404)
    })
    render(<TestnetPanel />)
    await waitFor(() => expect(polls).toBe(2), { timeout: 3_000 })
    await userEvent.click(screen.getByRole('button', { name: action === 'stop' ? 'İzleyiciyi durdur' : 'Yalnız izle' }))
    const expected = action === 'stop' ? 'Durduruldu' : 'Çalışıyor'
    await screen.findByText(expected)
    await act(async () => { release(new Response(JSON.stringify(initial), { status: 200 })) })
    expect(screen.getByText(expected)).toBeInTheDocument()
    if (action === 'stop') {
      expect(screen.queryByText('Koruma güncel')).not.toBeInTheDocument()
      expect(screen.getByText('Koruma doğrulanmadı')).toBeInTheDocument()
    }
  })

  it('403 kontrol yanıtını kendiliğinden yinelemez ve yeni onay ister', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/testnet/status') return json(status())
      if (path === '/api/testnet/session') return json({ server_instance_id: 'server-a', control_token: 'expired' })
      if (path === '/api/testnet/watch/start') return json({ detail: 'forbidden' }, 403)
      return json({}, 404)
    })
    render(<TestnetPanel />)
    const startButton = await screen.findByRole('button', { name: 'Yalnız izle' })
    await waitFor(() => expect(startButton).toBeEnabled())
    await userEvent.click(startButton)
    expect(await screen.findByText(/Kontrol oturumu geçersiz/)).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === '/api/testnet/watch/start')).toHaveLength(1)
  })
})
