import { FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './api'
import { CandleChart } from './components/CandleChart'
import { JsonDetails } from './components/JsonDetails'
import { SignalCard } from './components/SignalCard'
import { TestnetPanel } from './components/TestnetPanel'
import { decimal, money, time } from './format'
import type {
  MarketResponse,
  PaperArchiveDetail,
  PaperArchiveSummary,
  PaperState,
  ResearchReport,
  ResearchRequest,
  StatusResponse,
  Tab,
  Venue,
} from './types'

const intervals = ['1m', '3m', '5m', '15m', '1h']
const navItems: { id: Tab; label: string; short: string }[] = [
  { id: 'overview', label: 'Genel bakış', short: 'Bakış' },
  { id: 'research', label: 'Araştırma', short: 'Araştır' },
  { id: 'paper', label: 'Paper işlem', short: 'Paper' },
  { id: 'operations', label: 'Operasyon', short: 'Durum' },
]

function modeLabel(mode: string | null | undefined): string {
  const labels: Record<string, string> = { suggestions: 'Öneriler', paper: 'Paper', live: 'Canlı' }
  return mode ? labels[mode] ?? mode : 'Öneriler'
}

function strategyLabel(strategy: unknown): string {
  if (typeof strategy !== 'string') return '—'
  const labels: Record<string, string> = {
    pullback: 'Trend geri çekilmesi',
    breakout: 'Hacimli kırılım',
    mean_reversion: 'Ortalamaya dönüş',
  }
  return labels[strategy] ?? strategy
}

const emptyPaperForm = {
  strategy: 'pullback', capital: '1000', risk_per_trade: '0.005', daily_loss_limit: '0.02',
  max_drawdown: '0.08', max_open_risk: '0.01', target_pct: '0.015', stop_pct: '0.0075', fee_rate: '0.001',
  slippage_bps: '2', max_spread_bps: '10', hold_minutes: '60', max_trades_per_day: '20',
  max_consecutive_losses: '3', authorization_minutes: '60',
}

type PaperForm = typeof emptyPaperForm
type PaperCommand = 'start' | 'stop' | 'close' | 'archive'

function ModeRail({ status, paper, onNavigate }: {
  status: StatusResponse | null
  paper: PaperState | null
  onNavigate: (tab: Tab) => void
}) {
  const paperOn = paper?.entries_enabled === true
  return (
    <div className="mode-rail" aria-label="Çalışma kipleri">
      <button className="mode-item active" onClick={() => onNavigate('overview')}>
        <span className="mode-icon">01</span><span><b><span className="mode-full">Öneriler</span><span className="mode-short">Öneri</span></b><small>Salt okunur sinyal</small></span><i>Etkin</i>
      </button>
      <button className={`mode-item ${paperOn ? 'paper-on' : ''}`} onClick={() => onNavigate('paper')}>
        <span className="mode-icon">02</span><span><b><span className="mode-full">Paper</span><span className="mode-short">Paper</span></b><small>Sanal bakiye</small></span><i>{paperOn ? 'Açık' : 'Kapalı'}</i>
      </button>
      <button className="mode-item disabled" onClick={() => onNavigate('operations')} aria-disabled="true">
        <span className="mode-icon">03</span><span><b><span className="mode-full">Canlı</span><span className="mode-short">Canlı</span></b><small>Gerçek emir</small></span><i>{status?.live_enabled ? 'Kontrol et' : 'Kilitli'}</i>
      </button>
    </div>
  )
}

function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [venue, setVenue] = useState<Venue | ''>('global')
  const [symbol, setSymbol] = useState('')
  const [interval, setInterval] = useState('5m')
  const [status, setStatus] = useState<StatusResponse | null>(null)
  const [paper, setPaper] = useState<PaperState | null>(null)
  const [symbols, setSymbols] = useState<string[]>([])
  const [market, setMarket] = useState<MarketResponse | null>(null)
  const [statusError, setStatusError] = useState('')
  const [marketError, setMarketError] = useState('')
  const [marketLoading, setMarketLoading] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [now, setNow] = useState(Date.now)
  const [controlError, setControlError] = useState('')
  const [paperBusy, setPaperBusy] = useState(false)
  const paperRef = useRef<PaperState | null>(null)
  const paperSequenceRef = useRef(0)
  const paperCommandPendingRef = useRef(false)
  const retiredPaperSessionsRef = useRef(new Set<string>())

  const acceptPaper = useCallback((sequence: number, next: PaperState) => {
    if (sequence !== paperSequenceRef.current || retiredPaperSessionsRef.current.has(next.session_id)) return
    paperRef.current = next
    setPaper(next)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let pending = false
    const poll = () => {
      if (pending) return
      pending = true
      const paperSequence = paperCommandPendingRef.current ? null : ++paperSequenceRef.current
      const paperRequest = paperSequence === null ? Promise.resolve(null) : api.paper(controller.signal)
      Promise.allSettled([api.status(controller.signal), paperRequest]).then(([statusResult, paperResult]) => {
        if (controller.signal.aborted) return
        if (statusResult.status === 'fulfilled') { setStatus(statusResult.value); setStatusError('') }
        else setStatusError(errorMessage(statusResult.reason))
        if (paperSequence !== null && paperResult.status === 'fulfilled' && paperResult.value) acceptPaper(paperSequence, paperResult.value)
      }).finally(() => { pending = false })
    }
    poll()
    const pollId = window.setInterval(poll, 3_000)
    const clockId = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => { controller.abort(); window.clearInterval(pollId); window.clearInterval(clockId) }
  }, [acceptPaper])

  useEffect(() => {
    if (!venue) return
    const controller = new AbortController()
    api.symbols(venue, controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return
        setMarketError('')
        setSymbols(items)
        const next = items.includes('BTCUSDT') ? 'BTCUSDT' : ''
        setMarket(null)
        setMarketLoading(Boolean(next))
        setSymbol(next)
      })
      .catch((error: unknown) => setMarketError(errorMessage(error)))
    return () => controller.abort()
  }, [venue])

  useEffect(() => {
    if (!venue || !symbol) return
    const controller = new AbortController()
    let pending = false
    const poll = () => {
      if (pending) return
      pending = true
      api.market(venue, symbol, interval, controller.signal)
        .then((result) => {
          if (controller.signal.aborted) return
          setMarketError('')
          setMarket(result)
        })
        .catch((error: unknown) => { if (!controller.signal.aborted) setMarketError(errorMessage(error)) })
        .finally(() => {
          pending = false
          if (!controller.signal.aborted) setMarketLoading(false)
        })
    }
    poll()
    const pollId = window.setInterval(poll, 3_000)
    return () => { controller.abort(); window.clearInterval(pollId) }
  }, [venue, symbol, interval, refreshKey])

  const selectedLabel = venue ? (venue === 'global' ? 'Binance Global' : 'Binance TR') : 'Borsa seçilmedi'
  const changeVenue = (next: Venue | '') => {
    setVenue(next)
    setSymbols([])
    setSymbol('')
    setMarket(null)
  }
  const changeSymbol = (next: string) => { setSymbol(next); setMarket(null); setMarketLoading(Boolean(next)) }
  const changeInterval = (next: string) => { setInterval(next); setMarket(null); setMarketLoading(Boolean(symbol)) }
  const paperControl = async (command: PaperCommand, config?: Record<string, unknown>): Promise<PaperState> => {
    const current = paperRef.current
    if (!current?.session_id) throw new Error('Paper oturum kimliği alınamadı; durum yenilenene kadar bekleyin.')
    if (paperCommandPendingRef.current) throw new Error('Başka bir paper kontrolü tamamlanıyor.')
    const expectedSessionId = current.session_id
    const sequence = ++paperSequenceRef.current
    paperCommandPendingRef.current = true
    setPaperBusy(true)
    setControlError('')
    try {
      let next: PaperState
      if (command === 'start') next = await api.paperStart(config ?? {}, expectedSessionId)
      else if (command === 'stop') next = await api.paperStop(expectedSessionId)
      else if (command === 'close') next = await api.paperClose(expectedSessionId)
      else next = await api.paperArchiveCurrent(expectedSessionId)
      if (command === 'archive') retiredPaperSessionsRef.current.add(expectedSessionId)
      acceptPaper(sequence, next)
      return next
    } catch (cause) {
      if (sequence === paperSequenceRef.current) setControlError(errorMessage(cause))
      throw cause
    } finally {
      if (sequence === paperSequenceRef.current) {
        paperCommandPendingRef.current = false
        setPaperBusy(false)
      }
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>
          <span><b>SpotLab</b><small>Araştırma konsolu</small></span>
        </div>
        <ModeRail status={status} paper={paper} onNavigate={setTab} />
        <div className="side-note">
          <span className="status-light" />
          <div><b>Yerel motor</b><small>{status ? `v${status.version}` : 'Bağlantı kontrol ediliyor'}</small></div>
        </div>
        <p className="side-disclaimer">Bu araç emir tavsiyesi vermez. Canlı işlemler kapalıdır.</p>
      </aside>

      <main>
        <header className="topbar">
          <nav aria-label="Ana bölümler">
            {navItems.map((item) => (
              <button key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>
                <span className="full-label">{item.label}</span><span className="short-label">{item.short}</span>
              </button>
            ))}
          </nav>
          <div className="top-actions">
            <div className="paper-kill-switch" aria-label="Paper acil kontrolleri">
              <button disabled={paperBusy || !paper?.session_id || !paper.entries_enabled} onClick={() => void paperControl('stop').catch(() => undefined)}>Girişleri durdur</button>
              <button disabled={paperBusy || !paper?.session_id || !paper.position} onClick={() => void paperControl('close').catch(() => undefined)}>Pozisyonu kapat</button>
            </div>
            <div className="connection-pill"><span className={statusError ? 'bad' : ''} />{statusError ? 'API erişilemiyor' : 'Yerel bağlantı'}</div>
          </div>
        </header>

        <div className="workspace">
          <section className="page-intro">
            <div>
              <span className="kicker">{selectedLabel} · {modeLabel(status?.mode)}</span>
              <h1>{tab === 'overview' ? 'Piyasa çalışma alanı' : navItems.find((item) => item.id === tab)?.label}</h1>
              <p>{introFor(tab)}</p>
            </div>
            <div className="utc-clock"><span>UTC zaman</span><b>{time(now, true)}</b></div>
          </section>

          <section className="market-controls panel" aria-label="Piyasa seçimi">
            <label>
              <span>Borsa <em>Zorunlu</em></span>
              <select value={venue} onChange={(event) => changeVenue(event.target.value as Venue | '')}>
                <option value="">Borsa seçin…</option>
                <option value="global">Binance Global</option>
                <option value="tr" disabled>Binance TR · adaptör doğrulanmadı</option>
              </select>
            </label>
            <label>
              <span>Spot parite</span>
              <select value={symbol} onChange={(event) => changeSymbol(event.target.value)} disabled={!venue || symbols.length === 0}>
                <option value="">{venue ? 'Parite seçin…' : 'Önce borsa seçin'}</option>
                {symbols.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label>
              <span>Mum aralığı</span>
              <select value={interval} onChange={(event) => changeInterval(event.target.value)} disabled={!venue}>
                {intervals.map((item) => <option key={item}>{item}</option>)}
              </select>
            </label>
            <button className="icon-button" onClick={() => { setMarketLoading(true); setRefreshKey((key) => key + 1) }} disabled={!symbol || marketLoading} aria-label="Piyasa verisini yenile">
              <span aria-hidden="true">↻</span>{marketLoading ? 'Alınıyor' : 'Yenile'}
            </button>
          </section>

          {marketError && <div className="alert error" role="alert"><b>Veri alınamadı</b><span>{marketError}</span></div>}
          {controlError && <div className="alert error" role="alert"><b>Paper kontrolü başarısız</b><span>{controlError}</span></div>}
          {tab === 'overview' && <Overview venue={venue} market={market} loading={marketLoading} status={status} now={now} />}
          {tab === 'research' && <Research venue={venue} symbol={symbol} interval={interval} />}
          {tab === 'paper' && <Paper key={paper?.session_id ?? 'paper-loading'} venue={venue} symbol={symbol} interval={interval} paper={paper} busy={paperBusy} onCommand={paperControl} />}
          {tab === 'operations' && <Operations status={status} paper={paper} error={statusError} />}
        </div>
      </main>
    </div>
  )
}

function introFor(tab: Tab) {
  if (tab === 'research') return 'Kapanmış mumlar üzerinde tekrar üretilebilir bir araştırma çalıştırın.'
  if (tab === 'paper') return 'Gerçek piyasa verisini, yalnızca sanal bakiye ile izleyin.'
  if (tab === 'operations') return 'Motor durumunu ve canlı kullanım engellerini denetleyin.'
  return 'Gerçek spot verisini, maliyetleri ve işlem yapmama gerekçelerini birlikte görün.'
}

function Overview({ venue, market, loading, status, now }: {
  venue: Venue | ''
  market: MarketResponse | null
  loading: boolean
  status: StatusResponse | null
  now: number
}) {
  if (!venue) {
    return <EmptyState title="Piyasa bağlantısı siz seçince başlar" text="Hangi hizmetin kullanılacağı varsayılmaz. Yukarıdan Binance Global veya Binance TR seçin." />
  }
  if (loading && !market) return <div className="panel skeleton" aria-label="Piyasa verisi yükleniyor"><i /><i /><i /></div>
  if (!market) return <EmptyState title="Bir spot parite seçin" text="Liste yalnızca seçtiğiniz borsanın doğrulanmış sembollerinden gelir." />
  const last = market.candles.at(-1)
  return (
    <>
      <section className="market-strip">
        <div><span>Son fiyat</span><b>{decimal(last?.close, 8)}</b><small>{market.symbol}</small></div>
        <div><span>Alış</span><b>{decimal(market.quote?.bid, 8)}</b><small>Gerçek kotasyon</small></div>
        <div><span>Satış</span><b>{decimal(market.quote?.ask, 8)}</b><small>{time(market.quote?.observed_at)}</small></div>
        <div><span>Kaynak</span><b className="source-name">{market.source}</b><small>{market.interval} kapanmış mum</small></div>
      </section>
      <div className="overview-grid">
        <section className="panel chart-panel">
          <div className="panel-heading"><div><span className="eyebrow">Fiyat hareketi</span><h2>{market.symbol} / {market.interval}</h2></div><span className="live-tag">GERÇEK VERİ</span></div>
          <CandleChart candles={market.candles} signal={market.signal} symbol={market.symbol} />
        </section>
        <SignalCard market={market} now={now} />
      </div>
      {status && status.blockers.length > 0 && (
        <section className="panel safety-row">
          <span className="shield" aria-hidden="true">◇</span>
          <div><b>Canlı kullanım güvenlik kilidinde</b><p>{status.blockers.length} açık engel var. Araştırma ve paper akışları kullanılabilir.</p></div>
          <button onClick={() => document.querySelector<HTMLButtonElement>('nav button:last-child')?.click()}>Engelleri gör</button>
        </section>
      )}
    </>
  )
}

function Research({ venue, symbol, interval }: { venue: Venue | ''; symbol: string; interval: string }) {
  const [days, setDays] = useState('180')
  const [holdMinutes, setHoldMinutes] = useState('30')
  const [costs, setCosts] = useState({ entry_fee: '', exit_fee: '', spread_bps: '', slippage_bps: '' })
  const [report, setReport] = useState<ResearchReport | null>(null)
  const [sessionReports, setSessionReports] = useState<Record<string, ResearchReport>>({})
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!venue || !symbol) return setError('Önce borsa ve parite seçin.')
    setLoading(true); setError(''); setReport(null)
    const payload: ResearchRequest = {
      venue, symbol, interval, days: Number(days), hold_minutes: Number(holdMinutes), costs,
    }
    try {
      const nextReport = await api.research(payload)
      setReport(nextReport)
      setSessionReports((current) => ({ ...current, [interval]: nextReport }))
    }
    catch (cause) { setError(errorMessage(cause)) }
    finally { setLoading(false) }
  }

  return (
    <div className="two-column">
      <form className="panel form-panel" onSubmit={submit}>
        <div className="panel-heading"><div><span className="eyebrow">Yeni çalışma</span><h2>Araştırma protokolü</h2></div></div>
        <p className="form-copy">Periyot ve maliyetleri açıkça girin. Sonuç, veri ve kod kimliğiyle backend tarafından üretilir.</p>
        <div className="form-grid">
          <Field label="Geçmiş gün" value={days} onChange={setDays} type="number" min="1" required />
          <Field label="Elde tutma (dk)" value={holdMinutes} onChange={setHoldMinutes} type="number" min="1" required />
          <Field label="Giriş komisyonu" value={costs.entry_fee} onChange={(value) => setCosts({ ...costs, entry_fee: value })} placeholder="0.001" inputMode="decimal" required />
          <Field label="Çıkış komisyonu" value={costs.exit_fee} onChange={(value) => setCosts({ ...costs, exit_fee: value })} placeholder="0.001" inputMode="decimal" required />
          <Field label="Spread (bp)" value={costs.spread_bps} onChange={(value) => setCosts({ ...costs, spread_bps: value })} placeholder="1.0" inputMode="decimal" required />
          <Field label="Kayma (bp)" value={costs.slippage_bps} onChange={(value) => setCosts({ ...costs, slippage_bps: value })} placeholder="2.0" inputMode="decimal" required />
        </div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        <button className="primary-button" disabled={loading || !venue || !symbol}>{loading ? 'Araştırılıyor…' : 'Araştırmayı çalıştır'}</button>
        <p className="fine-print">180 günden kısa çalışmalar pilot sayılabilir; arayüz sonuçları AL yetkisine dönüştürmez.</p>
      </form>
      <section className="panel report-panel" aria-live="polite">
        <div className="panel-heading"><div><span className="eyebrow">Backend çıktısı</span><h2>Araştırma raporu</h2></div></div>
        {!report ? <EmptyInner title="Henüz rapor yok" text="Çalıştırılan raporun ölçümleri ve yeniden üretim ayrıntıları burada görünür." /> : <ResearchResult report={report} sessionReports={sessionReports} />}
      </section>
    </div>
  )
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function ResearchResult({ report, sessionReports }: { report: ResearchReport; sessionReports: Record<string, ResearchReport> }) {
  const decision = record(report.decision)
  const data = record(report.data)
  const selected = record(report.selected_candidate)
  const sealed = record(report.sealed_test)
  const normal = record(sealed.normal_costs)
  const stress = record(sealed.stress_costs)
  const action = typeof decision.action === 'string' ? decision.action : 'Sonuç yok'
  const blockers = Array.isArray(decision.blockers) ? decision.blockers : []
  const blockerLabels: Record<string, string> = {
    less_than_180_days: 'Veri süresi 180 günden kısa.',
    fewer_than_100_effective_oos_trades: 'Etkin örneklem dışı işlem sayısı 100’den az.',
    multiple_comparison_adjusted_ci_not_positive: 'Çoklu karşılaştırmaya göre düzeltilmiş güven aralığı pozitif değil.',
    stress_cost_expectancy_not_positive: 'Stres maliyetleri altında net beklenti pozitif değil.',
  }
  const noteLabels: Record<string, string> = {
    'Operational evidence thresholds are minimums, not a guarantee of advantage.': 'Operasyonel kanıt eşikleri asgari koşullardır; avantaj garantisi değildir.',
    'Small-input result is a pilot and cannot authorize a period or live trade.': 'Kısa veri sonucu pilottur; periyot veya canlı işlem yetkisi vermez.',
  }
  const decisionNote = typeof decision.note === 'string'
    ? noteLabels[decision.note] ?? decision.note
    : 'Backend karar notu bulunmuyor.'
  const percentage = (value: unknown): string => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—'
    return `${decimal(Number(value) * 100, 4)}%`
  }
  const comparisonRows = Object.entries(sessionReports).map(([key, value]) => {
    const itemDecision = record(value.decision)
    const itemData = record(value.data)
    const itemNormal = record(record(value.sealed_test).normal_costs)
    return { interval: key, action: itemDecision.action, pilot: itemData.pilot, trades: itemNormal.trade_count, expectancy: itemNormal.net_expectancy }
  })
  return (
    <div className="research-result">
      <div className={`decision-banner ${action === 'AL' ? 'decision-positive' : ''}`}>
        <span>Kanıt kararı</span><b>{action}</b><p>{decisionNote}</p>
      </div>
      <dl className="report-values">
        <div><dt>Veri süresi</dt><dd>{decimal(typeof data.duration_days === 'string' ? data.duration_days : undefined, 1)} gün</dd></div>
        <div><dt>Rapor sınıfı</dt><dd>{data.pilot === true ? 'Pilot · yetki vermez' : data.pilot === false ? 'Eşik kapsamı' : '—'}</dd></div>
        <div><dt>Mühürlü test işlemi</dt><dd>{normal.trade_count === undefined ? '—' : String(normal.trade_count)}</dd></div>
        <div><dt>Net beklenti</dt><dd>{percentage(normal.net_expectancy)}</dd></div>
        <div><dt>Stres beklentisi</dt><dd>{percentage(stress.net_expectancy)}</dd></div>
        <div><dt>Seçilen aile</dt><dd>{strategyLabel(selected.strategy)}</dd></div>
      </dl>
      {blockers.length > 0 && <div className="research-blockers"><b>Karar engelleri</b><ul>{blockers.map((item, index) => <li key={`${String(item)}-${index}`}>{blockerLabels[String(item)] ?? String(item)}</li>)}</ul></div>}
      {comparisonRows.length > 1 && (
        <div className="interval-comparison">
          <b>Bu oturumdaki periyotlar</b>
          <p>Aynı sembol ve tarayıcı oturumunda ayrı ayrı çalıştırılan raporlar; birleşik kanıt değildir.</p>
          <div className="comparison-table" role="table" aria-label="Araştırma periyot karşılaştırması">
            {comparisonRows.map((row) => <div role="row" key={row.interval}><b role="cell">{row.interval}</b><span role="cell">{String(row.action ?? '—')}</span><span role="cell">{row.pilot === true ? 'Pilot' : 'Eşik'}</span><span role="cell">{String(row.trades ?? 0)} işlem</span><span role="cell">{percentage(row.expectancy)}</span></div>)}
          </div>
        </div>
      )}
      <JsonDetails value={report} />
      <p className="fine-print">Rapor olduğu gibi gösterilir; eksik ölçüm veya avantaj arayüz tarafından tamamlanmaz.</p>
    </div>
  )
}

function Paper({ venue, symbol, interval, paper, busy, onCommand }: {
  venue: Venue | ''
  symbol: string
  interval: string
  paper: PaperState | null
  busy: boolean
  onCommand: (command: PaperCommand, config?: Record<string, unknown>) => Promise<PaperState>
}) {
  const [form, setForm] = useState<PaperForm>(emptyPaperForm)
  const [error, setError] = useState('')
  const [archiveConfirmed, setArchiveConfirmed] = useState(false)
  const [archiveRefresh, setArchiveRefresh] = useState(0)
  const [archiveOffset, setArchiveOffset] = useState(0)
  const [archives, setArchives] = useState<PaperArchiveSummary[]>([])
  const [archiveTotal, setArchiveTotal] = useState(0)
  const [archivesLoading, setArchivesLoading] = useState(true)
  const [archivesError, setArchivesError] = useState('')
  const [selectedArchiveId, setSelectedArchiveId] = useState<string | null>(null)
  const [archiveDetail, setArchiveDetail] = useState<PaperArchiveDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailRequestRevision, setDetailRequestRevision] = useState(0)
  const archiveLimit = 20
  const locked = Boolean(paper?.config)
  const sessionSymbol = typeof paper?.config?.symbol === 'string' ? paper.config.symbol : ''
  const sessionInterval = typeof paper?.config?.interval === 'string' ? paper.config.interval : ''
  const contextMismatch = locked && (symbol !== sessionSymbol || interval !== sessionInterval)
  const displayedForm = paper?.config
    ? Object.fromEntries(
        Object.keys(form).map((key) => [
          key,
          String(paper.config?.[key] ?? form[key as keyof PaperForm]),
        ]),
      ) as PaperForm
    : form

  const canArchive = Boolean(paper?.session_id && paper.config && !paper.entries_enabled && !paper.position)

  useEffect(() => {
    const controller = new AbortController()
    api.paperArchives(archiveLimit, archiveOffset, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        setArchives(result.items)
        setArchiveTotal(result.total)
      })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setArchivesError(errorMessage(cause)) })
      .finally(() => { if (!controller.signal.aborted) setArchivesLoading(false) })
    return () => controller.abort()
  }, [archiveOffset, archiveRefresh])

  useEffect(() => {
    if (!selectedArchiveId) return
    const controller = new AbortController()
    api.paperArchiveDetail(selectedArchiveId, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setArchiveDetail(result) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setArchivesError(errorMessage(cause)) })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false) })
    return () => controller.abort()
  }, [selectedArchiveId, detailRequestRevision])

  const update = (key: keyof PaperForm, value: string) => setForm((current) => ({ ...current, [key]: value }))
  const selectArchive = (sessionId: string | null) => {
    setArchiveDetail(null)
    setDetailLoading(Boolean(sessionId))
    setArchivesError('')
    setSelectedArchiveId(sessionId)
    setDetailRequestRevision((value) => value + 1)
  }
  const changeArchiveOffset = (nextOffset: number) => {
    setArchivesLoading(true)
    setArchivesError('')
    setSelectedArchiveId(null)
    setArchiveDetail(null)
    setArchiveOffset(nextOffset)
  }
  const mutate = async (command: PaperCommand, config?: Record<string, unknown>) => {
    setError('')
    try { return await onCommand(command, config) } catch (cause) { setError(errorMessage(cause)); return null }
  }
  const start = (event: FormEvent) => {
    event.preventDefault()
    if (!venue || !symbol) return setError('Önce borsa ve parite seçin.')
    const integerKeys = new Set(['hold_minutes', 'max_trades_per_day', 'max_consecutive_losses', 'authorization_minutes'])
    const config = Object.fromEntries(Object.entries(displayedForm).map(([key, value]) => [key, integerKeys.has(key) ? Number(value) : value]))
    void mutate('start', { venue, symbol, interval, ...config })
  }
  const archiveCurrent = async () => {
    if (!archiveConfirmed || !canArchive) return
    const result = await mutate('archive')
    if (!result) return
    setArchiveOffset(0)
    setSelectedArchiveId(null)
    setArchiveRefresh((value) => value + 1)
  }
  const position = paper?.position

  return (
    <>
      <div className="paper-banner"><span>PAPER</span><b>Sanal bakiye · gerçek piyasa verisi</b><p>Borsaya emir gönderilmez. Sonuçlar gerçek kazanç değildir.</p></div>
      <section className="market-strip paper-metrics">
        <div><span>Sanal nakit</span><b>{money(paper?.cash)}</b><small>Paper kayıt defteri</small></div>
        <div><span>Sanal özsermaye</span><b>{money(paper?.equity)}</b><small>Güncel işaretleme</small></div>
        <div><span>Girişler</span><b>{paper?.entries_enabled ? 'Açık' : 'Kapalı'}</b><small>{paper?.reason || 'Henüz yapılandırılmadı'}</small></div>
        <div><span>Açık pozisyon</span><b>{position ? String(position.symbol || symbol) : 'Yok'}</b><small>{position?.quantity ? `${position.quantity} adet` : '—'}</small></div>
      </section>
      <div className="two-column paper-layout">
        <form className="panel form-panel" onSubmit={start}>
          <div className="panel-heading"><div><span className="eyebrow">Yalnız paper</span><h2>Sanal deney ayarları</h2>{locked && <p className="session-scope">Oturum: <b>{sessionSymbol}</b> · <b>{sessionInterval}</b></p>}</div>{locked && <span className="lock-tag">Sabit kayıt</span>}</div>
          <p className="form-copy">{locked ? 'Bu oturumun ayarları değiştirilemez. Başlat düğmesi aynı ayarlarla süreli giriş yetkisini yeniler.' : 'Alanlarda yalnız paper için düzenlenebilir bir 1.000 USDT deney örneği var. Bunlar gerçek sermaye veya canlı risk yetkisi değildir.'}</p>
          {contextMismatch && <div className="inline-error scope-warning" role="status">Yetkiyi yenilemek için üstte <b>{sessionSymbol}</b> ve <b>{sessionInterval}</b> seçin. Mevcut paper oturumunun kapsamı değiştirilemez.</div>}
          <fieldset disabled={locked || busy}>
            <div className="form-grid">
              <label className="field"><span>Strateji</span><select value={displayedForm.strategy} onChange={(e) => update('strategy', e.target.value)}><option value="pullback">Trend geri çekilmesi</option><option value="breakout">Hacimli kırılım</option><option value="mean_reversion">Ortalamaya dönüş</option></select></label>
              <Field label="Sanal sermaye (USDT)" value={displayedForm.capital} onChange={(v) => update('capital', v)} placeholder="Siz girin" required inputMode="decimal" />
              <Field label="İşlem riski (oran)" value={displayedForm.risk_per_trade} onChange={(v) => update('risk_per_trade', v)} placeholder="örn. 0.01" required inputMode="decimal" />
              <Field label="Günlük zarar sınırı" value={displayedForm.daily_loss_limit} onChange={(v) => update('daily_loss_limit', v)} placeholder="örn. 0.03" required inputMode="decimal" />
              <Field label="Azami düşüş" value={displayedForm.max_drawdown} onChange={(v) => update('max_drawdown', v)} placeholder="örn. 0.10" required inputMode="decimal" />
              <Field label="Azami açık risk" value={displayedForm.max_open_risk} onChange={(v) => update('max_open_risk', v)} placeholder="örn. 0.02" required inputMode="decimal" />
              <Field label="Hedef hareketi" value={displayedForm.target_pct} onChange={(v) => update('target_pct', v)} placeholder="oran" required inputMode="decimal" />
              <Field label="Stop hareketi" value={displayedForm.stop_pct} onChange={(v) => update('stop_pct', v)} placeholder="oran" required inputMode="decimal" />
              <Field label="Komisyon oranı" value={displayedForm.fee_rate} onChange={(v) => update('fee_rate', v)} placeholder="oran" required inputMode="decimal" />
              <Field label="Kayma (bp)" value={displayedForm.slippage_bps} onChange={(v) => update('slippage_bps', v)} placeholder="bp" required inputMode="decimal" />
              <Field label="Azami spread (bp)" value={displayedForm.max_spread_bps} onChange={(v) => update('max_spread_bps', v)} placeholder="bp" required inputMode="decimal" />
              <Field label="Elde tutma (dk)" value={displayedForm.hold_minutes} onChange={(v) => update('hold_minutes', v)} type="number" min="1" required />
              <Field label="Günlük azami işlem" value={displayedForm.max_trades_per_day} onChange={(v) => update('max_trades_per_day', v)} type="number" min="1" required />
              <Field label="Art arda kayıp sınırı" value={displayedForm.max_consecutive_losses} onChange={(v) => update('max_consecutive_losses', v)} type="number" min="1" required />
              <Field label="Yetki süresi (dk)" value={displayedForm.authorization_minutes} onChange={(v) => update('authorization_minutes', v)} type="number" min="1" required />
            </div>
          </fieldset>
          {error && <div className="inline-error" role="alert">{error}</div>}
          <div className="button-row">
            <button className="primary-button" disabled={busy || !paper?.session_id || !venue || !symbol || contextMismatch}>{locked ? 'Aynı ayarlarla yetkilendir' : 'PAPER oturumunu başlat'}</button>
            <button type="button" className="secondary-button" disabled={busy || !paper?.session_id || !paper.entries_enabled} onClick={() => void mutate('stop')}>Girişleri durdur</button>
          </div>
        </form>
        <section className="panel paper-state">
          <div className="panel-heading"><div><span className="eyebrow">Sanal kayıt defteri</span><h2>Oturum durumu</h2></div></div>
          {position ? <JsonDetails value={position} title="Açık paper pozisyonu" /> : <EmptyInner title="Açık pozisyon yok" text="Motor yalnız doğrulanmış bir sinyal ve tüm risk kontrolleri geçtiğinde sanal pozisyon açar." />}
          <div className="ledger-counts"><span><b>{paper?.orders?.length ?? 0}</b> emir kaydı</span><span><b>{paper?.fills?.length ?? 0}</b> sanal dolum</span></div>
          {paper && <JsonDetails value={paper} title="Tüm paper durumu" />}
          <button className="danger-button" disabled={busy || !paper?.session_id || !position} onClick={() => void mutate('close')}>PAPER pozisyonunu kapat</button>
          <p className="fine-print">Durdurmak yalnız yeni girişleri kapatır. Açık sanal pozisyon yönetimi devam eder.</p>
          <div className="archive-current">
            <div><span className="eyebrow">Oturumu kapat</span><h3>PAPER deneyini arşivle</h3></div>
            <p>Bu işlem geçmişi silmez. Durdurulmuş ve açık pozisyonu olmayan oturumu salt okunur arşive taşır; ardından yeni, boş bir PAPER formu açılır.</p>
            <label className="archive-confirm">
              <input type="checkbox" checked={archiveConfirmed} onChange={(event) => setArchiveConfirmed(event.target.checked)} disabled={busy || !canArchive} />
              <span>Bu PAPER oturumunu arşivlemek istediğimi onaylıyorum.</span>
            </label>
            <button type="button" className="archive-button" disabled={busy || !canArchive || !archiveConfirmed} onClick={() => void archiveCurrent()}>PAPER oturumunu arşivle</button>
            {!canArchive && <small className="archive-hint">Arşiv için oturum yapılandırılmış, girişler durdurulmuş ve pozisyon kapatılmış olmalıdır.</small>}
          </div>
        </section>
      </div>
      <PaperArchives
        items={archives}
        total={archiveTotal}
        offset={archiveOffset}
        limit={archiveLimit}
        loading={archivesLoading}
        error={archivesError}
        selectedId={selectedArchiveId}
        detail={archiveDetail}
        detailLoading={detailLoading}
        onSelect={selectArchive}
        onOffset={changeArchiveOffset}
      />
    </>
  )
}

function PaperArchives({ items, total, offset, limit, loading, error, selectedId, detail, detailLoading, onSelect, onOffset }: {
  items: PaperArchiveSummary[]
  total: number
  offset: number
  limit: number
  loading: boolean
  error: string
  selectedId: string | null
  detail: PaperArchiveDetail | null
  detailLoading: boolean
  onSelect: (sessionId: string | null) => void
  onOffset: (offset: number) => void
}) {
  const showingFrom = total === 0 ? 0 : offset + 1
  const showingTo = Math.min(offset + items.length, total)
  return (
    <section className="panel archive-panel" aria-labelledby="paper-archives-title">
      <div className="panel-heading">
        <div><span className="eyebrow">Salt okunur PAPER kayıtları</span><h2 id="paper-archives-title">Deney arşivi</h2></div>
        <span className="count-badge" aria-label={`${total} arşivlenmiş oturum`}>{total}</span>
      </div>
      <p className="form-copy">Arşiv kayıtları yalnız geçmiş sanal deneyleri gösterir; gerçek kazanç veya gelecekteki sonuçlara ilişkin kanıt değildir.</p>
      {error && <div className="inline-error" role="alert">{error}</div>}
      {loading ? <div className="archive-loading" aria-live="polite">PAPER arşivi alınıyor…</div> : items.length === 0 ? (
        <EmptyInner title="Arşivlenmiş PAPER oturumu yok" text="Uygun bir deney arşivlendiğinde geçmişi burada salt okunur olarak kalır." />
      ) : (
        <div className="archive-browser">
          <div className="archive-list" aria-label="Arşivlenmiş PAPER oturumları">
            {items.map((item) => {
              const symbol = typeof item.config?.symbol === 'string' ? item.config.symbol : 'PAPER'
              const interval = typeof item.config?.interval === 'string' ? item.config.interval : '—'
              return (
                <button key={item.session_id} className={selectedId === item.session_id ? 'selected' : ''} aria-pressed={selectedId === item.session_id} onClick={() => onSelect(item.session_id)}>
                  <span><b>PAPER</b>{symbol} · {interval}</span>
                  <small>{time(item.archived_at, true)} · {item.order_count} emir · {item.fill_count} dolum</small>
                  <i>{money(item.realized_pnl)}</i>
                </button>
              )
            })}
            <div className="archive-pagination" aria-label="PAPER arşivi sayfalama">
              <button disabled={offset === 0 || loading} onClick={() => onOffset(Math.max(0, offset - limit))}>Önceki</button>
              <span>{showingFrom}–{showingTo} / {total}</span>
              <button disabled={offset + items.length >= total || loading} onClick={() => onOffset(offset + limit)}>Sonraki</button>
            </div>
          </div>
          <div className="archive-detail" aria-live="polite">
            {detailLoading ? <div className="archive-loading">PAPER kaydı açılıyor…</div> : detail && detail.session_id === selectedId ? (
              <>
                <div className="archive-detail-heading"><span className="paper-readonly-tag">PAPER · SALT OKUNUR</span><b>{detail.session_id}</b><small>{time(detail.archived_at, true)}</small></div>
                <dl className="metric-grid">
                  <div><dt>Sanal nakit</dt><dd>{money(detail.state.cash)}</dd></div>
                  <div><dt>Sanal özsermaye</dt><dd>{money(detail.state.equity)}</dd></div>
                  <div><dt>Emir kaydı</dt><dd>{detail.state.orders?.length ?? 0}</dd></div>
                  <div><dt>Sanal dolum</dt><dd>{detail.state.fills?.length ?? 0}</dd></div>
                </dl>
                <JsonDetails value={detail.state} title="Salt okunur PAPER oturumu" />
              </>
            ) : <EmptyInner title="Bir PAPER kaydı seçin" text="Arşivdeki oturumun değiştirilemeyen ayrıntıları burada gösterilir." />}
          </div>
        </div>
      )}
    </section>
  )
}

function Operations({ status, paper, error }: { status: StatusResponse | null; paper: PaperState | null; error: string }) {
  const blockers = status?.blockers ?? []
  return (
    <div className="operations-grid">
      <TestnetPanel />
      <section className="panel live-lock">
        <span className="lock-illustration" aria-hidden="true">⌾</span>
        <span className="eyebrow">Gerçek emir yetkisi</span>
        <h2>Canlı mod kilitli</h2>
        <p>Canlı etkinleştirme bu panelden yapılamaz. Backend güvenlik kontrolleri tamamlanana kadar reddeder.</p>
        <button disabled>Canlı modu etkinleştir</button>
      </section>
      <section className="panel blockers-panel">
        <div className="panel-heading"><div><span className="eyebrow">Zorunlu kontroller</span><h2>Açık engeller</h2></div><span className="count-badge">{blockers.length}</span></div>
        {error ? <div className="inline-error">{error}</div> : blockers.length ? <ol className="blocker-list">{blockers.map((blocker, index) => <li key={`${blocker}-${index}`}><span>{String(index + 1).padStart(2, '0')}</span><p>{blocker}</p></li>)}</ol> : <EmptyInner title="Engel listesi alınamadı" text="Canlı yetki yine de kapalıdır; durum yanıtı doğrulanmadan kullanılamaz." />}
      </section>
      <section className="panel runtime-panel">
        <div className="panel-heading"><div><span className="eyebrow">Süreç bilgisi</span><h2>Yerel motor</h2></div></div>
        <dl className="metric-grid">
          <div><dt>Sürüm</dt><dd>{status?.version || '—'}</dd></div>
          <div><dt>Borsa</dt><dd>{status?.venue || 'Seçilmedi'}</dd></div>
          <div><dt>Mod</dt><dd>{modeLabel(status?.mode)}</dd></div>
          <div><dt>Paper giriş</dt><dd>{paper?.entries_enabled ? 'Açık' : 'Kapalı'}</dd></div>
        </dl>
        <dl className="metric-grid operations-extra">
          <div><dt>Piyasa akışı</dt><dd>{status?.stream_status || '—'}</dd></div>
          <div><dt>İşçi hatası</dt><dd>{status?.worker_error || 'Yok'}</dd></div>
        </dl>
        {status?.runtime && <JsonDetails value={status.runtime} title="Çalışma zamanı ayrıntıları" />}
      </section>
    </div>
  )
}

function Field({ label, value, onChange, ...props }: {
  label: string; value: string; onChange: (value: string) => void
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'>) {
  return <label className="field"><span>{label}</span><input value={value} onChange={(event) => onChange(event.target.value)} {...props} /></label>
}

function EmptyState({ title, text }: { title: string; text: string }) {
  return <section className="panel empty-state"><span className="empty-orbit" aria-hidden="true"><i /><i /></span><h2>{title}</h2><p>{text}</p></section>
}

function EmptyInner({ title, text }: { title: string; text: string }) {
  return <div className="empty-inner"><span aria-hidden="true">···</span><b>{title}</b><p>{text}</p></div>
}

export default App
