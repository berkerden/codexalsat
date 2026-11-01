import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PaperObservationPanel } from '../components/PaperObservationPanel'

const sessionId = '11111111-1111-4111-8111-111111111111'
const report = {
  schema_version: 1 as const,
  session_id: sessionId,
  archived: false,
  as_of: 1_800_000_000_000,
  simulation: true as const,
  live_eligible: false as const,
  evidence_status: 'collecting' as const,
  started_at: 1_799_827_200_000,
  calendar_elapsed_ms: 172_800_000,
  observation_span_ms: 86_400_000,
  observation: {
    schema_version: 1,
    config_sha256: 'config',
    first_observed_at: 1_799_913_600_000,
    last_observed_at: 1_800_000_000_000,
    first_code_identity: 'old',
    last_code_identity: 'new',
    mixed_code_identity: true,
    process_count: 2,
    source_session_id: '00000000-0000-4000-8000-000000000000',
    counts: { attempts: 14, healthy_entry: 8, healthy_management: 3, unhealthy: 3 },
    coverage_ms: { entry: 28_800_000, management: 7_200_000 },
    gap_count: 4,
    max_gap_ms: 3_600_000,
    interruptions: { restart: 1, stop: 0, restore: 1, code_change: 1, clock_regression: 0 },
    last_status: 'healthy',
    last_scope: 'entry',
    last_reason: null,
  },
  results: { scope: 'entire_paper_session' as const, cash: '1010', equity: '1015', realized_pnl: '10', fill_count: 2, position_open: true },
  limitations: ['Gözlem ekonomik avantajı doğrulamaz.'],
}

const drawdown = (overrides: Record<string, unknown> = {}) => ({
  schema_version: 1,
  history_complete: true,
  maximum_drawdown: '0.025',
  first_marked_at: 1_799_999_000_000,
  last_marked_at: 1_800_000_000_000,
  maximum_drawdown_at: 1_799_999_500_000,
  last_quote_at: 1_799_999_935_000,
  available: true,
  current_drawdown: '0.01',
  quote_age_ms: 65_000,
  freshness: 'fresh',
  ...overrides,
})

const json = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }))

beforeEach(() => vi.stubGlobal('fetch', vi.fn(() => json(report))))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('takvim süresini iki örnekleme kapsamından ayırır ve canlı onayı vermez', async () => {
  render(<PaperObservationPanel sessionId={sessionId} />)
  expect(await screen.findByText('2 gün 0 sa')).toBeInTheDocument()
  expect(screen.getByText('8 sa 0 dk')).toBeInTheDocument()
  expect(screen.getByText('2 sa 0 dk')).toBeInTheDocument()
  expect(screen.getByText('14 sa 0 dk')).toBeInTheDocument()
  expect(screen.getByText('8 sağlıklı gözlem')).toBeInTheDocument()
  expect(screen.getByText('3 kotasyon odaklı gözlem')).toBeInTheDocument()
  expect(screen.getByText('Canlı işleme uygun değil')).toBeInTheDocument()
  expect(screen.getByText(/30 günlük koşulu, ekonomik avantajı veya canlı işlem onayını doğrulamaz/)).toBeInTheDocument()
  expect(screen.getByText(/devralınmış geçmiş gözlem içerir/)).toBeInTheDocument()
  expect(screen.getByText(/ileri gözlem başlamadan önceki hareketleri/)).toBeInTheDocument()
  expect(screen.getByText(/en fazla 10 saniye bulunan sağlıklı ardışık kayıtlarla/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Raporu yenile' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
})

it('boş raporu açıkça gösterir ve yalnız başarılı raporda indirme sunar', async () => {
  vi.mocked(fetch).mockImplementation(() => json({ ...report, evidence_status: 'no_observations', observation: null, observation_span_ms: 0 }))
  render(<PaperObservationPanel sessionId={sessionId} />)
  expect(await screen.findByText('Henüz ileri gözlem kaydı yok')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'JSON raporunu indir' })).toBeInTheDocument()
})

it('hata sonrası aynı raporu tekrar okuyabilir', async () => {
  let reads = 0
  vi.mocked(fetch).mockImplementation(() => ++reads === 1 ? json({ detail: 'Geçici rapor hatası' }, 503) : json(report))
  render(<PaperObservationPanel sessionId={sessionId} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Geçici rapor hatası')
  expect(screen.queryByRole('button', { name: 'JSON raporunu indir' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Tekrar dene' }))
  expect(await screen.findByText('14')).toBeInTheDocument()
  expect(reads).toBe(2)
})

it('oturum değişince geç kalan eski yanıtı göstermez', async () => {
  let resolveOld!: (value: Response) => void
  vi.mocked(fetch).mockImplementation((input) => String(input).includes(sessionId)
    ? new Promise<Response>((resolve) => { resolveOld = resolve })
    : json({ ...report, session_id: '22222222-2222-4222-8222-222222222222', observation: null }))
  const view = render(<PaperObservationPanel sessionId={sessionId} />)
  view.rerender(<PaperObservationPanel sessionId="22222222-2222-4222-8222-222222222222" />)
  expect(await screen.findByText('Henüz ileri gözlem kaydı yok')).toBeInTheDocument()
  resolveOld(await json(report))
  await waitFor(() => expect(screen.queryByText('14')).not.toBeInTheDocument())
})

it('yanlış oturum kimlikli yanıtı rapor olarak göstermez', async () => {
  vi.mocked(fetch).mockImplementation(() => json({ ...report, session_id: 'başka-oturum' }))
  render(<PaperObservationPanel sessionId={sessionId} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Rapor farklı bir PAPER oturumuna ait')
  expect(screen.queryByRole('button', { name: 'JSON raporunu indir' })).not.toBeInTheDocument()
})

it('geçerli işaret yoksa düşüşü sıfır yerine bilinmiyor gösterir', async () => {
  vi.mocked(fetch).mockImplementation(() => json({
    ...report,
    results: { ...report.results, observed_drawdown: drawdown({
      history_complete: false,
      maximum_drawdown: null,
      first_marked_at: null,
      last_marked_at: null,
      maximum_drawdown_at: null,
      last_quote_at: null,
      available: false,
      current_drawdown: null,
      quote_age_ms: null,
      freshness: 'unavailable',
    }) },
  }))
  render(<PaperObservationPanel sessionId={sessionId} />)
  const card = await screen.findByRole('region', { name: 'Örneklenen gözlenen maksimum düşüş' })
  expect(within(card).getByText('Kotasyon yok')).toBeInTheDocument()
  expect(within(card).getAllByText('Bilinmiyor').length).toBeGreaterThan(0)
  expect(within(card).getByText(/bilinmeyen değer sıfır olarak gösterilmez/)).toBeInTheDocument()
  expect(within(card).queryByText('%0', { exact: true })).not.toBeInTheDocument()
})

it('bayat kotasyonda oranı yüzdeye çevirir ve tazeliğin rapor anına ait olduğunu açıklar', async () => {
  vi.mocked(fetch).mockImplementation(() => json({ ...report, results: { ...report.results, observed_drawdown: drawdown({ freshness: 'stale' }) } }))
  render(<PaperObservationPanel sessionId={sessionId} />)
  const card = await screen.findByRole('region', { name: 'Örneklenen gözlenen maksimum düşüş' })
  expect(within(card).getByText('%2,5')).toBeInTheDocument()
  expect(within(card).getByText('%1')).toBeInTheDocument()
  expect(within(card).getByText('Eski kotasyon')).toBeInTheDocument()
  expect(within(card).getByText('Rapor anındaki kotasyon yaşı')).toBeInTheDocument()
  expect(within(card).getByText(/tazelik durumu raporun .* anındaki sabit görünümüdür/)).toBeInTheDocument()
  expect(within(card).getByText(/özsermaye = nakit \+ miktar × bid × \(1 − ücret\)/)).toBeInTheDocument()
  expect(within(card).getByText(/Varsayımsal kayma, gerçek bir simüle dolum gerçekleşene kadar düşülmez/)).toBeInTheDocument()
  expect(within(card).getByText(/gün içi veya işaretler arası gerçek maksimum olduğu anlamına gelmez/)).toBeInTheDocument()
})

it('kısmi geçmişte maksimumu gösterirken önceki tarihin bilinmediğini işaretler', async () => {
  vi.mocked(fetch).mockImplementation(() => json({ ...report, results: { ...report.results, observed_drawdown: drawdown({ history_complete: false }) } }))
  render(<PaperObservationPanel sessionId={sessionId} />)
  const card = await screen.findByRole('region', { name: 'Örneklenen gözlenen maksimum düşüş' })
  expect(within(card).getByText('%2,5')).toBeInTheDocument()
  expect(within(card).getByText('Önceki geçmiş bilinmiyor.')).toBeInTheDocument()
  expect(within(card).getByText(/yalnız mevcut işaretleme geçmişini kapsar/)).toBeInTheDocument()
})
