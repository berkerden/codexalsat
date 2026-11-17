// Real browser requests through Compose's Vite proxy; no API route mocks.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const { chromium, expect } = require('@playwright/test')
const base = new URL(process.env.SPOTLAB_COMPOSE_WEB_URL ?? '')
assert.equal(base.protocol, 'http:')
assert.equal(base.hostname, '127.0.0.1')
assert.equal(base.pathname, '/')
assert.equal(base.username + base.password + base.search + base.hash, '')
const sessionId = process.env.SPOTLAB_COMPOSE_EXPECTED_SESSION_ID
const commit = process.env.SPOTLAB_COMPOSE_EXPECTED_COMMIT
assert.match(sessionId ?? '', /^[a-f0-9-]{36}$/)
assert.match(commit ?? '', /^[a-f0-9]{40}$/)

const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
  page.setDefaultTimeout(20_000)
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(base.href, { waitUntil: 'domcontentloaded' })
  const read = path => page.evaluate(async pathname => {
    const response = await fetch(pathname)
    if (!response.ok) throw new Error(`API status ${response.status}`)
    return response.json()
  }, path)
  const status = await read('/api/status')
  assert.equal(status.commit, commit)
  assert.equal(status.live_enabled, false)
  const paper = await read('/api/paper')
  assert.equal(paper.session_id, sessionId)
  assert.equal(paper.entries_enabled, false)
  assert.equal(paper.position, null)

  const nav = page.getByRole('navigation', { name: 'Ana bölümler' })
  await nav.getByRole('button', { name: /Paper/ }).click()
  await expect(page.getByRole('region', { name: 'Güncel ileri gözlem raporu' })).toBeVisible()
  const archives = await read('/api/paper/archives')
  assert.equal(archives.total, 1)
  assert.ok(archives.items[0].fill_count > 0)
  const archive = page.getByRole('region', { name: 'Deney arşivi' })
  await expect(archive.getByLabel('1 arşivlenmiş oturum')).toBeVisible()
  await archive.locator('.archive-list button').first().click()
  await expect(archive.getByText(archives.items[0].session_id, { exact: true })).toBeVisible()

  await nav.getByRole('button', { name: /Operasyon|Durum/ }).click()
  await expect(page.getByRole('button', { name: 'Canlı modu etkinleştir' })).toBeDisabled()
  const testnetPanel = page.getByRole('region', { name: 'Sanal fon izleyicisi' })
  await expect(testnetPanel).toBeVisible()
  await expect(testnetPanel.getByText('TESTNET · ÜRETİM KAPALI')).toBeVisible()
  const testnet = await read('/api/testnet/status')
  assert.equal(testnet.ledger_status, 'ready')
  assert.equal(testnet.production_enabled, false)
  assert.equal(testnet.watcher.phase, 'idle')
  assert.equal(testnet.watcher.manager_running, false)
  assert.equal(testnet.watcher.allow_mutations, false)
  await expect(testnetPanel.getByRole('alert')).toHaveCount(0)
  assert.deepEqual(errors, [])
  console.log('Compose browser: real proxy, paper/archive UI and idle Testnet verified.')
} finally {
  await browser.close()
}
