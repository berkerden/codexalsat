import { expect, test } from '@playwright/test'

const status = {
  version: '0.1.0', venue: null, mode: 'suggestions', live_enabled: false,
  blockers: ['Canlı emir adaptörü etkin değil.'], stream_status: 'not_connected', worker_error: null,
  runtime: { browser_independent: true, real_order_transport: false },
}

test.beforeEach(async ({ page }) => {
  let testnetWatcher = { run_id: null as string | null, cycle_id: null as string | null, symbol: null as 'BTCUSDT' | null, allow_mutations: false, phase: 'idle', health: 'idle', manager_running: false, coverage_observation_fresh: false, snapshot: null }
  let paperState = { session_id: '11111111-1111-4111-8111-111111111111', started_at: null as number | null, entries_enabled: false, config: null as Record<string, unknown> | null, cash: '0', equity: '0', position: null, orders: [] as unknown[], fills: [] as unknown[], reason: 'Yapılandırılmadı.' }
  const archivedItems: Array<Record<string, unknown>> = []
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
    if (requestUrl.includes('/observation')) return route.fulfill({ json: {
      schema_version: 1, session_id: paperState.session_id, archived: false,
      as_of: Date.now(), simulation: true, live_eligible: false,
      evidence_status: 'no_observations', started_at: paperState.started_at,
      calendar_elapsed_ms: 0, observation_span_ms: 0, observation: null,
      results: { scope: 'entire_paper_session', cash: '0', equity: '0', realized_pnl: '0', fill_count: 0, position_open: false },
      limitations: ['Sentetik tarayıcı testi; gerçek gözlem değildir.'],
    } })
    if (requestUrl.includes('/api/paper/archives?')) return route.fulfill({ json: { items: archivedItems, total: archivedItems.length } })
    if (requestUrl.includes('/api/paper/archive')) {
      const previous = paperState
      archivedItems.unshift({ session_id: previous.session_id, started_at: previous.started_at, archived_at: Date.now(), config: previous.config, cash: previous.cash, equity: previous.equity, realized_pnl: '0', order_count: previous.orders.length, fill_count: previous.fills.length })
      paperState = { session_id: '22222222-2222-4222-8222-222222222222', started_at: null, entries_enabled: false, config: null, cash: '0', equity: '0', position: null, orders: [], fills: [], reason: 'Yapılandırılmadı.' }
      return route.fulfill({ json: paperState })
    }
    if (requestUrl.endsWith('/api/paper')) return route.fulfill({ json: paperState })
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

test('mobil gözlem raporu kapsamları ayırır ve aynı raporu JSON indirir', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const evidence = {
    schema_version: 1, session_id: '11111111-1111-4111-8111-111111111111', archived: false,
    as_of: 1_800_000_000_000, simulation: true, live_eligible: false,
    evidence_status: 'collecting', started_at: 1_799_827_200_000,
    calendar_elapsed_ms: 172_800_000, observation_span_ms: 86_400_000,
    observation: {
      schema_version: 1, config_sha256: 'fixture', first_observed_at: 1_799_913_600_000,
      last_observed_at: 1_800_000_000_000, first_code_identity: 'fixture', last_code_identity: 'fixture',
      mixed_code_identity: false, process_count: 1, source_session_id: null,
      counts: { attempts: 10, healthy_entry: 8, healthy_management: 2, unhealthy: 0 },
      coverage_ms: { entry: 60_000, management: 10_000 }, gap_count: 1, max_gap_ms: 86_300_000,
      interruptions: { restart: 0, stop: 0, restore: 0, code_change: 0, clock_regression: 0 },
      last_status: 'healthy', last_scope: 'management', last_reason: 'ok',
    },
    results: { scope: 'entire_paper_session', cash: '1000', equity: '1000', realized_pnl: '0', fill_count: 0, position_open: false },
    limitations: ['Sentetik tarayıcı testi; gerçek gözlem değildir.'],
  }
  await page.route('**/api/paper/observation?*', (route) => route.fulfill({ json: evidence }))
  await page.goto('/')
  await page.getByRole('button', { name: 'Paper', exact: true }).first().click()
  const panel = page.getByRole('region', { name: 'Güncel ileri gözlem raporu' })
  await expect(panel.getByText('2 gün 0 sa', { exact: true })).toBeVisible()
  await expect(panel.getByText('1 dk', { exact: true })).toBeVisible()
  await expect(panel.getByText('10 sn', { exact: true })).toBeVisible()
  await expect(panel.getByText('Canlı işleme uygun değil')).toBeVisible()
  const downloadEvent = page.waitForEvent('download')
  await panel.getByRole('button', { name: 'JSON raporunu indir' }).click()
  const download = await downloadEvent
  expect(download.suggestedFilename()).toBe(`paper-gozlem-${evidence.session_id}.json`)
  const stream = await download.createReadStream()
  stream!.setEncoding('utf8')
  let contents = ''
  for await (const chunk of stream!) contents += chunk
  expect(JSON.parse(contents)).toEqual(evidence)
  await panel.getByRole('button', { name: 'Raporu yenile' }).click()
  await expect(panel.getByText('Kayıt mevcut', { exact: true })).toBeVisible()
  const box = await panel.boundingBox()
  expect(box!.x + box!.width).toBeLessThanOrEqual(390)
  await panel.screenshot({ path: '/tmp/spotlab-forward-mobile.png' })
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

test('390px mobilde PAPER arşivi erişilebilir ve yeni boş forma döner', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: 'Paper', exact: true }).first().click()

  const currentState = {
    session_id: '33333333-3333-4333-8333-333333333333', started_at: Date.now(), entries_enabled: false,
    config: { venue: 'global', symbol: 'BTCUSDT', interval: '5m', strategy: 'pullback', capital: '1000' },
    cash: '1000', equity: '1000', position: null, orders: [], fills: [], reason: 'Girişler durduruldu.',
  }
  await page.route('**/api/paper', (route) => route.fulfill({ json: currentState }), { times: 1 })
  await page.reload()
  await page.getByRole('button', { name: 'Paper', exact: true }).first().click()
  await page.getByRole('checkbox', { name: /arşivlemek istediğimi onaylıyorum/ }).check()
  await page.getByRole('button', { name: 'PAPER oturumunu arşivle' }).click()
  await expect(page.getByRole('button', { name: 'PAPER oturumunu başlat' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Deney arşivi' })).toBeVisible()
  const archiveBox = await page.locator('.archive-panel').boundingBox()
  expect(archiveBox).not.toBeNull()
  expect((archiveBox?.x ?? 0) + (archiveBox?.width ?? 0)).toBeLessThanOrEqual(390)
})
