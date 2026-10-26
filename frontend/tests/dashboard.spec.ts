import { expect, test } from '@playwright/test'

const status = {
  version: '0.1.0', venue: null, mode: 'suggestions', live_enabled: false,
  blockers: ['Canlı emir adaptörü etkin değil.'], stream_status: 'not_connected', worker_error: null,
  runtime: { browser_independent: true, real_order_transport: false },
}

test.beforeEach(async ({ page }) => {
  let testnetWatcher = { run_id: null as string | null, cycle_id: null as string | null, symbol: null as 'BTCUSDT' | null, allow_mutations: false, phase: 'idle', health: 'idle', manager_running: false, coverage_observation_fresh: false, snapshot: null }
  await page.route('**/api/**', async (route) => {
    const requestUrl = route.request().url()
    if (requestUrl.includes('/api/testnet/session')) return route.fulfill({ json: { server_instance_id: 'e2e-server', control_token: 'e2e-token' } })
    if (requestUrl.includes('/api/testnet/watch/start')) {
      const payload = route.request().postDataJSON() as { cycle_id: string; confirm_testnet: boolean }
      testnetWatcher = { run_id: 'e2e-run', cycle_id: payload.cycle_id, symbol: 'BTCUSDT', allow_mutations: payload.confirm_testnet, phase: 'running', health: 'healthy', manager_running: true, coverage_observation_fresh: false, snapshot: null }
      return route.fulfill({ status: 202, json: { mode: 'testnet', production_enabled: false, server_instance_id: 'e2e-server', observed_at_ms: Date.now(), ledger_status: 'ready', cycles: [{ cycle_id: 'e2e-cycle', symbol: 'BTCUSDT', state: 'ENTRY_SUBMITTED', stop_entry_requested: false, updated_at_ms: Date.now() }], watcher: testnetWatcher } })
    }
    if (requestUrl.includes('/api/testnet/watch/e2e-run/stop')) {
      testnetWatcher = { ...testnetWatcher, phase: 'stopped', health: 'stopped', manager_running: false }
      return route.fulfill({ json: { mode: 'testnet', production_enabled: false, server_instance_id: 'e2e-server', observed_at_ms: Date.now(), ledger_status: 'ready', cycles: [{ cycle_id: 'e2e-cycle', symbol: 'BTCUSDT', state: 'ENTRY_SUBMITTED', stop_entry_requested: false, updated_at_ms: Date.now() }], watcher: testnetWatcher } })
    }
    if (requestUrl.includes('/api/testnet/status')) return route.fulfill({ json: { mode: 'testnet', production_enabled: false, server_instance_id: 'e2e-server', observed_at_ms: Date.now(), ledger_status: 'ready', cycles: [{ cycle_id: 'e2e-cycle', symbol: 'BTCUSDT', state: 'ENTRY_SUBMITTED', stop_entry_requested: false, updated_at_ms: Date.now() }], watcher: testnetWatcher } })
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

test('390px mobilde navigasyon açık ve paper kontrolleri viewport altında', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  const modeLabels = page.locator('.mode-short')
  await expect(modeLabels.nth(0)).toHaveText('Öneri')
  await expect(modeLabels.nth(0)).toBeVisible()
  await expect(modeLabels.nth(1)).toHaveText('Paper')
  await expect(modeLabels.nth(1)).toBeVisible()
  await expect(modeLabels.nth(2)).toHaveText('Canlı')
  await expect(modeLabels.nth(2)).toBeVisible()

  const research = page.getByRole('button', { name: 'Araştır', exact: true })
  await expect(research).toBeVisible()
  await research.click()
  await expect(page.getByRole('heading', { name: 'Araştırma', exact: true })).toBeVisible()

  const controls = page.getByLabel('Paper acil kontrolleri')
  const box = await controls.boundingBox()
  expect(box).not.toBeNull()
  expect(Math.round((box?.y ?? 0) + (box?.height ?? 0))).toBe(844)
  expect(box?.y ?? 0).toBeGreaterThan(780)
})

test('Testnet koruma onayıyla izleyiciyi başlatır ve aynı çalışmayı durdurur', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: 'Durum', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Sanal fon izleyicisi' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Yalnız izle' })).toBeVisible()
  await page.getByRole('checkbox', { name: /Korumayı yönet/ }).check()

  const startRequest = page.waitForRequest((request) => request.url().endsWith('/api/testnet/watch/start'))
  await page.getByRole('button', { name: 'Korumayı yönet' }).click()
  expect((await startRequest).postDataJSON()).toEqual({ cycle_id: 'e2e-cycle', confirm_testnet: true })
  await expect(page.getByRole('definition').filter({ hasText: 'Korumayı yönet' })).toBeVisible()
  const panelBox = await page.locator('.testnet-panel').boundingBox()
  expect(panelBox).not.toBeNull()
  expect((panelBox?.x ?? 0) + (panelBox?.width ?? 0)).toBeLessThanOrEqual(390)

  const stopRequest = page.waitForRequest((request) => request.url().endsWith('/api/testnet/watch/e2e-run/stop'))
  await page.getByRole('button', { name: 'İzleyiciyi durdur' }).click()
  await stopRequest
  await expect(page.getByText('Durduruldu', { exact: true })).toBeVisible()
})
