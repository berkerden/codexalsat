import { useEffect, useRef, useState } from 'react'
import { ApiError, api, errorMessage } from '../api'
import type { TestnetStatusResponse } from '../types'

const CLOSED_CYCLES = new Set(['EXITED', 'NO_FILL', 'ABORTED'])
const busyPhases = new Set(['starting', 'running', 'stopping'])

const phaseLabels: Record<string, string> = {
  idle: 'Boşta', starting: 'Başlatılıyor', running: 'Çalışıyor', stopping: 'Durduruluyor',
  stopped: 'Durduruldu', attention: 'Dikkat gerekiyor', closed: 'Kapandı',
}
const stateLabels: Record<string, string> = {
  PROTECTED: 'Korumalı', PROTECTION_UNVERIFIED: 'Koruma doğrulanmadı', ENTRY_SUBMITTED: 'Giriş gönderildi',
  ENTRY_OPEN: 'Alış açık', PLANNED: 'Planlandı', ENTRY_UNKNOWN: 'Alış doğrulanamadı',
  BLOCKED: 'Engellendi', UNPROTECTED: 'Korumasız', EXITED: 'Çıkış tamamlandı', NO_FILL: 'Dolum yok', ABORTED: 'İptal edildi',
}

const reasonLabels: Record<string, string> = {
  account_identity_unavailable: 'Testnet hesap kimliği doğrulanamadı.',
  local_ledger_changed: 'Yerel kayıt defteri değişti; yeniden kontrol gerekiyor.',
  ManagerLockError: 'Başka bir yönetici bu döngüyü yönetiyor.',
  RetryableTransportError: 'Testnet bağlantısı geçici olarak yanıt vermiyor.',
  TimeoutError: 'Testnet durum kontrolü zaman aşımına uğradı.',
  entry_stopped: 'Yeni giriş kalıcı olarak durduruldu.',
}

function safeReason(reason: unknown): string {
  if (typeof reason !== 'string' || !reason) return 'Ayrıntı bekleniyor.'
  return reasonLabels[reason] ?? 'Testnet izleyicisi dikkat gerektiren bir durum bildirdi.'
}

function positiveQuantity(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0
}

function timestamp(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Bilinmiyor'
  return new Intl.DateTimeFormat('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(value)
}

function quantity(value: unknown): string {
  return typeof value === 'string' && value ? value : 'Bilinmiyor'
}

export function TestnetPanel() {
  const [data, setData] = useState<TestnetStatusResponse | null>(null)
  const [selectedCycle, setSelectedCycle] = useState('')
  const [manageProtection, setManageProtection] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [controlError, setControlError] = useState('')
  const [pending, setPending] = useState(false)
  const [now, setNow] = useState(Date.now)
  const tokenRef = useRef<string | null>(null)
  const instanceRef = useRef<string | null>(null)
  const selectedCycleRef = useRef('')
  const contextRef = useRef(0)
  const responseEpochRef = useRef(0)
  const controlAbortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let requestPending = false
    const poll = async () => {
      if (requestPending || Boolean(controlAbortRef.current)) return
      requestPending = true
      const responseEpoch = responseEpochRef.current
      try {
        const next = await api.testnetStatus(controller.signal)
        if (controller.signal.aborted || responseEpoch !== responseEpochRef.current) return
        if (instanceRef.current && instanceRef.current !== next.server_instance_id) {
          controlAbortRef.current?.abort()
          tokenRef.current = null
          setManageProtection(false)
          setPending(false)
          contextRef.current += 1
        }
        instanceRef.current = next.server_instance_id
        setData(next)
        setNow(Date.now)
        setLoadError('')
        const available = next.cycles.filter((cycle) => !CLOSED_CYCLES.has(cycle.state))
        const nextSelection = available.some((cycle) => cycle.cycle_id === selectedCycleRef.current) ? selectedCycleRef.current : (available[0]?.cycle_id ?? '')
        if (selectedCycleRef.current && selectedCycleRef.current !== nextSelection) {
          contextRef.current += 1
          controlAbortRef.current?.abort()
          controlAbortRef.current = null
          setPending(false)
          setManageProtection(false)
          setControlError('')
        }
        selectedCycleRef.current = nextSelection
        setSelectedCycle(nextSelection)
      } catch (cause) {
        if (!controller.signal.aborted && responseEpoch === responseEpochRef.current) setLoadError(errorMessage(cause))
      } finally {
        requestPending = false
      }
    }
    void poll()
    const pollId = window.setInterval(() => void poll(), 2_000)
    const clockId = window.setInterval(() => setNow(Date.now), 1_000)
    return () => {
      contextRef.current += 1
      controlAbortRef.current?.abort()
      controller.abort()
      window.clearInterval(pollId)
      window.clearInterval(clockId)
    }
  }, [])

  const activeCycles = data?.cycles.filter((cycle) => !CLOSED_CYCLES.has(cycle.state)) ?? []
  const watcher = data?.watcher
  const snapshot = watcher?.snapshot
  const responseAge = data ? now - data.observed_at_ms : Number.POSITIVE_INFINITY
  const snapshotAge = typeof snapshot?.observed_at_ms === 'number' ? now - snapshot.observed_at_ms : Number.POSITIVE_INFINITY
  const stale = !data || responseAge < 0 || responseAge > 5_000 || Boolean(loadError) || watcher?.health === 'stale'
  const snapshotStale = snapshot !== null && (snapshotAge < 0 || snapshotAge > 5_000)
  const protectedNow = !stale && !snapshotStale && watcher?.health === 'healthy' && watcher.coverage_observation_fresh && snapshot?.coverage_observation_fresh === true && snapshot.state === 'PROTECTED'
  const displayedState = snapshot?.state === 'PROTECTED' && !protectedNow ? 'PROTECTION_UNVERIFIED' : snapshot?.state
  const displayedUnprotected = protectedNow ? snapshot?.unprotected_quantity : snapshot?.owned_quantity
  const exposure = positiveQuantity(snapshot?.owned_quantity) || positiveQuantity(snapshot?.unprotected_quantity)
  const controlsBusy = pending || Boolean(watcher?.manager_running) || (watcher ? busyPhases.has(watcher.phase) : false)
  const canStop = Boolean(watcher?.run_id) && watcher?.manager_running === true && watcher.phase !== 'stopping'

  const selectCycle = (cycleId: string) => {
    contextRef.current += 1
    controlAbortRef.current?.abort()
    controlAbortRef.current = null
    setPending(false)
    selectedCycleRef.current = cycleId
    setSelectedCycle(cycleId)
    setManageProtection(false)
    setControlError('')
  }

  const obtainToken = async (signal: AbortSignal, expectedContext: number) => {
    if (tokenRef.current) return tokenRef.current
    const session = await api.testnetSession(signal)
    if (contextRef.current !== expectedContext || signal.aborted) return null
    if (!instanceRef.current || session.server_instance_id !== instanceRef.current) {
      tokenRef.current = null
      setManageProtection(false)
      setControlError('Sunucu yenilendi. Durumu yenileyip tekrar deneyin.')
      return null
    }
    tokenRef.current = session.control_token
    return session.control_token
  }

  const control = async (action: (token: string, signal: AbortSignal) => Promise<TestnetStatusResponse>) => {
    const controller = new AbortController()
    controlAbortRef.current = controller
    responseEpochRef.current += 1
    const expectedContext = contextRef.current
    setPending(true)
    setControlError('')
    try {
      const token = await obtainToken(controller.signal, expectedContext)
      if (!token || contextRef.current !== expectedContext) return
      const next = await action(token, controller.signal)
      if (contextRef.current !== expectedContext || controller.signal.aborted) return
      if (next.server_instance_id !== instanceRef.current) {
        tokenRef.current = null
        setManageProtection(false)
        setControlError('Sunucu yenilendi. Durumu yenileyip tekrar deneyin.')
        return
      }
      setData(next)
      setNow(Date.now)
    } catch (cause) {
      if (contextRef.current !== expectedContext) return
      if (cause instanceof ApiError && cause.status === 403) {
        tokenRef.current = null
        setManageProtection(false)
        setControlError('Kontrol oturumu geçersiz. Durumu yenileyip yeniden onaylayın.')
      } else setControlError(errorMessage(cause))
    } finally {
      if (contextRef.current === expectedContext) {
        controlAbortRef.current = null
        setPending(false)
      }
    }
  }

  const start = () => {
    const cycleId = selectedCycle
    if (!cycleId || !activeCycles.some((cycle) => cycle.cycle_id === cycleId)) return
    void control((token, signal) => api.testnetWatchStart(cycleId, manageProtection, token, signal))
  }

  const stop = () => {
    const runId = watcher?.run_id
    if (!runId) return
    void control((token, signal) => api.testnetWatchStop(runId, token, signal))
  }

  return (
    <section className="panel testnet-panel" aria-labelledby="testnet-title">
      <div className="testnet-identity">
        <div><span className="eyebrow">Ayrı Testnet alanı</span><h2 id="testnet-title">Sanal fon izleyicisi</h2></div>
        <span className="testnet-badge">TESTNET · ÜRETİM KAPALI</span>
      </div>
      <p className="form-copy">Bu alan yalnız Binance Spot Testnet sanal fonlarını izler. Tarayıcı kapansa da backend çalıştığı sürece izleyici devam eder; backend durur veya uyursa kesilir ve kendiliğinden başlamaz.</p>

      {loadError && <div className="inline-error" role="alert">Durum yenilenemedi: {loadError}. Son alınan görünüm korunuyor.</div>}
      {data?.ledger_status === 'missing' && <div className="testnet-empty" role="status"><b>Testnet kayıt defteri bulunamadı</b><span>Önce backend üzerinden bir Testnet döngüsü oluşturulmalı.</span></div>}
      {data?.ledger_status === 'unavailable' && <div className="inline-error" role="alert">Testnet kayıt defterine şu anda erişilemiyor.</div>}
      {data?.ledger_status === 'ready' && activeCycles.length === 0 && <div className="testnet-empty" role="status"><b>İzlenebilir açık döngü yok</b><span>Kapanmış döngüler bu kontrolde gösterilmez.</span></div>}
      {!data && !loadError && <div className="testnet-empty" role="status"><b>Testnet durumu alınıyor</b><span>Bu kontrol hiçbir izleyiciyi otomatik başlatmaz.</span></div>}

      {data?.ledger_status === 'ready' && activeCycles.length > 0 && (
        <div className="testnet-controls">
          <label className="field"><span>Mevcut açık döngü</span><select aria-label="Testnet döngüsü" value={selectedCycle} onChange={(event) => selectCycle(event.target.value)} disabled={controlsBusy}>{activeCycles.map((cycle) => <option key={cycle.cycle_id} value={cycle.cycle_id}>{cycle.symbol} · {cycle.cycle_id} · {stateLabels[cycle.state] ?? 'Bilinmiyor'}</option>)}</select></label>
          <div className="watch-mode"><b>Yalnız izle</b><span>Varsayılan kip; emir veya koruma değişikliği yapmaz.</span></div>
          <label className="consent-check"><input type="checkbox" checked={manageProtection} onChange={(event) => setManageProtection(event.target.checked)} disabled={controlsBusy} /><span><b>Korumayı yönet</b> — Kalan alış emrinin iptal edilmesine ve doğrulanmış mevcut miktar için koruyucu emir kurulmasına izin veriyorum; yeni alış açılmaz. Bu seçim kalıcı giriş durdurma ister ve seçim veya sunucu değişince silinir.</span></label>
          <div className="testnet-buttons">
            <button className="primary-button" type="button" onClick={start} disabled={!selectedCycle || controlsBusy || stale}>{pending ? 'İstek gönderiliyor' : manageProtection ? 'Korumayı yönet' : 'Yalnız izle'}</button>
          </div>
          <p className="fine-print">İzleyiciyi durdurmak açık emirleri iptal etmez ve pozisyonu kapatmaz. Backend yeniden başladığında izleyici otomatik devam etmez.</p>
        </div>
      )}

      <div className="testnet-stop-control">
        <button className="secondary-button" type="button" onClick={stop} disabled={pending || !canStop}>İzleyiciyi durdur</button>
        <span>Yalnız bu uygulamanın eşleşen izleyici görevini durdurur; emirleri iptal etmez veya pozisyonu kapatmaz.</span>
      </div>

      {controlError && <div className="inline-error" role="alert">{controlError}</div>}
      {watcher && (
        <div className="watch-status">
          <div className={`watch-health ${protectedNow ? 'protected' : stale || snapshotStale ? 'stale' : ''}`}><span /> <b>{protectedNow ? 'Koruma güncel' : stale || snapshotStale ? 'Durum güncel değil' : watcher.health === 'retrying' ? 'Yeniden deneniyor' : watcher.phase === 'idle' ? 'Bu uygulamanın izleyicisi boşta' : 'İzleyici durumu'}</b></div>
          <dl className="metric-grid">
            <div><dt>Aşama</dt><dd>{phaseLabels[watcher.phase] ?? 'Bilinmiyor'}</dd></div>
            <div><dt>Döngü durumu</dt><dd>{displayedState ? stateLabels[displayedState] ?? 'Bilinmiyor' : 'Bilinmiyor'}</dd></div>
            <div><dt>Döngü</dt><dd>{watcher.cycle_id ?? '—'}</dd></div>
            <div><dt>Kip</dt><dd>{watcher.allow_mutations ? 'Korumayı yönet' : 'Yalnız izle'}</dd></div>
            <div><dt>Son kontrol</dt><dd>{timestamp(snapshot?.checked_at_ms)}</dd></div>
            <div><dt>Mevcut miktar</dt><dd>{quantity(snapshot?.owned_quantity)}</dd></div>
            <div><dt>Korumasız miktar</dt><dd>{quantity(displayedUnprotected)}</dd></div>
            <div><dt>Sonuç</dt><dd>{safeReason(snapshot?.reason)}</dd></div>
          </dl>
        </div>
      )}
      {exposure && <div className="exposure-warning" role="alert"><b>Açık Testnet maruziyeti var</b><span>İzleyiciyi durdurmak bu pozisyonu kapatmaz. Koruma durumu güncel görünmüyorsa backend yönetimini yeniden başlatın.</span></div>}
    </section>
  )
}
