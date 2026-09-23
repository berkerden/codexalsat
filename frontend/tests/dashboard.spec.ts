import { expect, test } from '@playwright/test'

const status = {
  version: '0.1.0', venue: null, mode: 'suggestions', live_enabled: false,
  blockers: ['Canlı emir adaptörü etkin değil.'], stream_status: 'not_connected', worker_error: null,
  runtime: { browser_independent: true, real_order_transport: false },
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const requestUrl = route.request().url()
    if (requestUrl.includes('/api/status')) return route.fulfill({ json: status })
    if (requestUrl.includes('/api/paper')) return route.fulfill({ json: { entries_enabled: false, config: null, cash: '0', equity: '0', position: null, orders: [], fills: [], reason: 'Yapılandırılmadı.' } })
    if (requestUrl.includes('/api/symbols')) return route.fulfill({ json: { symbols: ['BTCUSDT', 'SOLUSDT'] } })
    if (requestUrl.includes('/api/market')) return route.fulfill({ json: {
      symbol: requestUrl.includes('SOLUSDT') ? 'SOLUSDT' : 'BTCUSDT', interval: '5m', source: 'Binance Global REST/WS',
      candles: [{ open_time: 1_700_000_000_000, close_time: 1_700_000_299_999, open: '65000', high: '65100', low: '64900', close: '65050', volume: '12.5' }],
      quote: { bid: '65049.9', ask: '65050.1', spread_bps: '0.03', observed_at: 1_700_000_300_000 },
      quality: { healthy: true, issues: [] },
      signal: { action: 'İŞLEM YAPMA', reason: 'Ekonomik avantaj doğrulanmadı.', strategy: 'pullback', entry: '65050.1' },
    } })
    return route.fulfill({ status: 404, json: { detail: 'Mock rota bulunamadı' } })
  })
})

test('Global BTC görünümü ve kilitli canlı operasyonu', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByLabel('Spot parite')).toHaveValue('BTCUSDT')
  await expect(page.getByRole('heading', { name: 'İŞLEM YAPMA' })).toBeVisible()
  await page.getByRole('button', { name: /Operasyon/ }).click()
  await expect(page.getByRole('button', { name: 'Canlı modu etkinleştir' })).toBeDisabled()
  await expect(page.getByText('Canlı emir adaptörü etkin değil.')).toBeVisible()
})
