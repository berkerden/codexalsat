import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'

const market = {
  symbol: 'BTCUSDT', interval: '5m', source: 'Binance Global REST/WS',
  candles: [
    { open_time: 1_700_000_000_000, close_time: 1_700_000_299_999, open: '65000', high: '65100', low: '64900', close: '65050', volume: '12.5' },
    { open_time: 1_700_000_300_000, close_time: 1_700_000_599_999, open: '65050', high: '65200', low: '65000', close: '65180', volume: '10.2' },
  ],
  quote: { bid: '65179.9', ask: '65180.1', spread_bps: '0.03068', observed_at: 1_700_000_600_000 },
  quality: { healthy: true, issues: [] },
  signal: {
    action: 'İŞLEM YAPMA', reason: 'Örneklem dışı avantaj doğrulanmadı.', strategy: 'pullback', entry: '65180.1',
    evidence: 'Araştırma kanıtı değildir.', data_time: 1_700_000_600_000, scenario_only: true,
    quantity_basis: 'Paper yapılandırılmadığı için 1 birimlik gösterim.', holding_minutes: 60, valid_until: 1_700_000_605_000,
    expected_net_result: null,
    costs: {
      entry_fee: '0.001', exit_fee: '0.001', spread_bps: '0.030681312312', slippage_bps: '2',
      assumptions: ['spread is split equally across entry and exit'],
    },
    margin: { net_target_profit: '812.50', total_entry_cash: '65180.1', net_target_return: '0.0124', entry_fee: '65.18', target_exit_fee: '66.16', break_even_price: '65310.2', modeled_stop_loss: '620.4', net_reward_risk: '1.31' },
  },
}

const status = {
  version: '0.1.0', venue: null, mode: 'suggestions', live_enabled: false,
  blockers: ['Testnet yaşam döngüsü doğrulanmadı.'], stream_status: 'not_connected', worker_error: null,
  runtime: { browser_independent: true, real_order_transport: false },
}

const paper = {
  mode: 'suggestions', entries_enabled: false, config: null, cash: '0', equity: '0',
  position: null, orders: [], fills: [], reason: 'Paper trading henüz yapılandırılmadı.',
}

const researchReport = {
  symbol: 'BTCUSDT', interval: '5m',
  data: { duration_days: '182.5', pilot: false },
  selected_candidate: { strategy: 'pullback', target_pct: '0.007', stop_pct: '0.004' },
  sealed_test: {
    normal_costs: { trade_count: 104, net_expectancy: '-0.003063' },
    stress_costs: { net_expectancy: '-0.0042' },
  },
  decision: {
    action: 'İŞLEM YAPMA',
    note: 'Operational evidence thresholds are minimums, not a guarantee of advantage.',
    blockers: ['multiple_comparison_adjusted_ci_not_positive', 'stress_cost_expectancy_not_positive'],
  },
}

function json(value: unknown, statusCode = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status: statusCode, headers: { 'Content-Type': 'application/json' } }))
}

describe('SpotLab dashboard', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/status') return json(status)
      if (path === '/api/paper') return json(paper)
      if (path.startsWith('/api/symbols')) return json({ symbols: ['BTCUSDT', 'SOLUSDT'] })
      if (path.startsWith('/api/market')) {
        const requestedSymbol = path.includes('symbol=SOLUSDT') ? 'SOLUSDT' : 'BTCUSDT'
        return json({ ...market, symbol: requestedSymbol })
      }
      if (path === '/api/research') return json(researchReport)
      return json({ detail: 'Testte tanımsız istek' }, 404)
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('gerçek API yanıtından BTC görünümünü ve işlem yapmama nedenini sunar', async () => {
    render(<App />)
    expect(screen.getByRole('combobox', { name: /Borsa/ })).toHaveValue('global')
    await waitFor(() => expect(screen.getByRole('combobox', { name: /Spot parite/ })).toHaveValue('BTCUSDT'))
    expect(await screen.findByRole('heading', { name: 'İŞLEM YAPMA' })).toBeInTheDocument()
    expect(screen.getByText('Örneklem dışı avantaj doğrulanmadı.')).toBeInTheDocument()
    expect(screen.getByText('Binance Global REST/WS')).toBeInTheDocument()
    expect(screen.getByText('Trend geri çekilmesi')).toBeInTheDocument()
    expect(screen.getAllByText('0,1%')).toHaveLength(2)
    expect(screen.getAllByText('0,031 bp')).toHaveLength(2)
    expect(screen.getByText('Spread giriş ve çıkış arasında eşit bölündü.')).toBeInTheDocument()
    expect(screen.queryByText('suggestions')).not.toBeInTheDocument()
    expect(screen.getByText('Varsayımsal hedef senaryosu')).toBeInTheDocument()
    expect(screen.getByText('Paper yapılandırılmadığı için 1 birimlik gösterim.')).toBeInTheDocument()
    expect(screen.getByText(/beklenen net sonuç değildir/i)).toBeInTheDocument()
    expect(screen.getByText(/Kotasyon güncelliğini yitirdi/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/market?venue=global&symbol=BTCUSDT&interval=5m'), expect.anything())
  })

  it('parite değişiminde eski bağlamı temizler ve yeni piyasayı getirir', async () => {
    render(<App />)
    const symbolSelect = await screen.findByRole('combobox', { name: /Spot parite/ })
    await waitFor(() => expect(symbolSelect).toHaveValue('BTCUSDT'))
    await screen.findByText('BTCUSDT / 5m')
    await userEvent.selectOptions(symbolSelect, 'SOLUSDT')
    expect(screen.queryByText('BTCUSDT / 5m')).not.toBeInTheDocument()
    expect(await screen.findByText('SOLUSDT / 5m')).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('symbol=SOLUSDT'), expect.anything())
  })

  it('canlı düğmesini kapalı tutar ve backend engelini gösterir', async () => {
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: /Operasyon/ }))
    expect(screen.getByRole('button', { name: 'Canlı modu etkinleştir' })).toBeDisabled()
    expect(await screen.findByText('Testnet yaşam döngüsü doğrulanmadı.')).toBeInTheDocument()
  })

  it('araştırma finansal oranlarını ve karar gerekçelerini Türkçe sunar', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByRole('combobox', { name: /Spot parite/ })).toHaveValue('BTCUSDT'))
    await userEvent.click(screen.getByRole('button', { name: /Araştırma/ }))
    await userEvent.type(screen.getByLabelText('Giriş komisyonu'), '0.001')
    await userEvent.type(screen.getByLabelText('Çıkış komisyonu'), '0.001')
    await userEvent.type(screen.getByLabelText('Spread (bp)'), '1')
    await userEvent.type(screen.getByLabelText('Kayma (bp)'), '2')
    await userEvent.click(screen.getByRole('button', { name: 'Araştırmayı çalıştır' }))
    expect(await screen.findByText('-0,3063%')).toBeInTheDocument()
    expect(screen.getByText('-0,42%')).toBeInTheDocument()
    expect(screen.getByText('Trend geri çekilmesi')).toBeInTheDocument()
    expect(screen.getByText('Operasyonel kanıt eşikleri asgari koşullardır; avantaj garantisi değildir.')).toBeInTheDocument()
    expect(screen.getByText('Çoklu karşılaştırmaya göre düzeltilmiş güven aralığı pozitif değil.')).toBeInTheDocument()
  })

  it('kilitli paper oturumunun kayıtlı kapsamını gösterir ve farklı periyotta yetkiyi kapatır', async () => {
    vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/status') return json(status)
      if (path === '/api/paper') return json({ ...paper, config: { symbol: 'BTCUSDT', interval: '15m', strategy: 'pullback' } })
      if (path.startsWith('/api/symbols')) return json({ symbols: ['BTCUSDT', 'SOLUSDT'] })
      if (path.startsWith('/api/market')) return json(market)
      return json({ detail: 'Testte tanımsız istek' }, 404)
    })
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: /Paper işlem/ }))
    expect(await screen.findByText(/Oturum:/)).toHaveTextContent('Oturum: BTCUSDT · 15m')
    expect(screen.getByText(/Yetkiyi yenilemek için/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Aynı ayarlarla yetkilendir' })).toBeDisabled()
  })
})
