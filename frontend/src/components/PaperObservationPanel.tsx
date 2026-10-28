import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../api'
import { money, time } from '../format'
import type { PaperObservationReport } from '../types'

function duration(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 60_000) return `${Math.floor(value / 1_000)} sn`
  const minutes = Math.floor(value / 60_000)
  const days = Math.floor(minutes / 1_440)
  const hours = Math.floor((minutes % 1_440) / 60)
  const remainder = minutes % 60
  if (days > 0) return `${days} gün ${hours} sa`
  if (hours > 0) return `${hours} sa ${remainder} dk`
  return `${minutes} dk`
}

function download(report: PaperObservationReport) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `paper-gozlem-${report.session_id}.json`
  link.click()
  URL.revokeObjectURL(url)
}

export function PaperObservationPanel({ sessionId, archived = false, compact = false }: {
  sessionId: string
  archived?: boolean
  compact?: boolean
}) {
  const [response, setResponse] = useState<{ key: string; report: PaperObservationReport | null; error: string } | null>(null)
  const [revision, setRevision] = useState(0)
  const requestRef = useRef(0)
  const responseKey = `${archived ? 'archive' : 'current'}:${sessionId}:${revision}`

  useEffect(() => {
    const controller = new AbortController()
    const request = ++requestRef.current
    const read = archived
      ? api.paperArchiveObservation(sessionId, controller.signal)
      : api.paperObservation(sessionId, controller.signal)
    read.then((result) => {
      if (controller.signal.aborted || request !== requestRef.current) return
      if (result.session_id !== sessionId) {
        setResponse({ key: responseKey, report: null, error: 'Rapor farklı bir PAPER oturumuna ait; yeniden deneyin.' })
        return
      }
      setResponse({ key: responseKey, report: result, error: '' })
    }).catch((cause: unknown) => {
      if (controller.signal.aborted || request !== requestRef.current) return
      setResponse({ key: responseKey, report: null, error: errorMessage(cause) })
    })
    return () => controller.abort()
  }, [archived, responseKey, sessionId])

  const loading = response?.key !== responseKey
  const report = loading ? null : response.report
  const error = loading ? '' : response.error
  const observation = report?.observation
  const status = report?.evidence_status === 'archived' ? 'Arşivlendi' : report?.evidence_status === 'collecting' ? 'Kayıt mevcut' : 'Kayıt yok'
  const unobservedSpan = Math.max(0, (report?.observation_span_ms ?? 0) - (observation?.coverage_ms.entry ?? 0) - (observation?.coverage_ms.management ?? 0))
  return (
    <section className={`paper-observation ${compact ? 'compact' : 'panel'}`} aria-label={`${archived ? 'Arşiv' : 'Güncel'} ileri gözlem raporu`}>
      <div className="panel-heading observation-heading">
        <div><span className="eyebrow">Salt okunur · ileri gözlem</span><h2>Gözlem kanıtı</h2></div>
        {report && <span className="observation-status">{status}</span>}
      </div>
      <p className="observation-intro">Geçen takvim süresi ile gerçekten örneklenen sağlıklı süre aynı değildir. Sayılar bağımsız örnek değil, gözlem kayıtlarıdır.</p>
      {loading ? <div className="observation-loading" aria-live="polite">Gözlem raporu alınıyor…</div> : error ? (
        <div className="observation-error" role="alert"><b>Rapor alınamadı</b><span>{error}</span><button type="button" onClick={() => setRevision((value) => value + 1)}>Tekrar dene</button></div>
      ) : report ? (
        <>
          <div className="observation-safety" role="status"><b>Canlı işleme uygun değil</b><span>Bu rapor 30 günlük koşulu, ekonomik avantajı veya canlı işlem onayını doğrulamaz.</span></div>
          <dl className="observation-times">
            <div><dt>Geçen takvim süresi</dt><dd>{duration(report.calendar_elapsed_ms)}</dd><small>Kapsama eşit değildir</small></div>
            <div><dt>Gözlem kayıt aralığı</dt><dd>{duration(report.observation_span_ms)}</dd><small>İlk kayıttan rapor anına</small></div>
            <div className="entry"><dt>Giriş değerlendirmesi</dt><dd>{duration(observation?.coverage_ms.entry ?? 0)}</dd><small>{observation?.counts.healthy_entry ?? 0} sağlıklı gözlem</small></div>
            <div className="management"><dt>Pozisyon yönetimi</dt><dd>{duration(observation?.coverage_ms.management ?? 0)}</dd><small>{observation?.counts.healthy_management ?? 0} kotasyon odaklı gözlem</small></div>
            <div className="unknown"><dt>Sağlıklı gözlemle kapsanmayan süre</dt><dd>{duration(unobservedSpan)}</dd><small>Takvim süresinden değil, gözlem aralığından türetilir</small></div>
          </dl>
          {!observation ? <div className="observation-empty"><b>Henüz ileri gözlem kaydı yok</b><span>Takvim süresi ilerlese bile örneklenmeyen aralıklar kanıt sayılmaz.</span></div> : (
            <div className="observation-detail-grid">
              <dl className="metric-grid">
                <div><dt>Toplam gözlem</dt><dd>{observation.counts.attempts}</dd></div>
                <div><dt>Sağlıksız gözlem</dt><dd>{observation.counts.unhealthy}</dd></div>
                <div><dt>10 saniyeyi aşan aralık</dt><dd>{observation.gap_count}</dd></div>
                <div><dt>En uzun boşluk</dt><dd>{duration(observation.max_gap_ms)}</dd></div>
                <div><dt>Süreç dönemleri</dt><dd>{observation.process_count}</dd></div>
                <div><dt>Son gözlem</dt><dd>{time(observation.last_observed_at, true)}</dd></div>
              </dl>
              {observation.source_session_id && <p className="inherited-evidence">Bu rapor, geri yüklenen <b>{observation.source_session_id}</b> oturumundan devralınmış geçmiş gözlem içerir; tamamı bu süreçte üretilmemiştir.</p>}
              {observation.mixed_code_identity && <p className="mixed-code">Gözlem aralığında kod kimliği değişti. Sonuçlar tek bir yazılım sürümüne ait değildir.</p>}
            </div>
          )}
          <div className="observation-results">
            <div><b>Tüm PAPER oturumu sonucu</b><span>Bu sonuçlar ileri gözlem başlamadan önceki hareketleri de içerebilir.</span></div>
            <dl><div><dt>Nakit</dt><dd>{money(report.results.cash)}</dd></div><div><dt>Özsermaye</dt><dd>{money(report.results.equity)}</dd></div><div><dt>Gerçekleşen K/Z</dt><dd>{money(report.results.realized_pnl)}</dd></div><div><dt>Dolum</dt><dd>{report.results.fill_count}</dd></div></dl>
          </div>
          {report.limitations.length > 0 && <details className="observation-limitations"><summary>Sınırlamalar ({report.limitations.length})</summary><ul>{report.limitations.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></details>}
          <p className="coverage-method">Örneklenen kapsam, aralarında en fazla 10 saniye bulunan sağlıklı ardışık kayıtlarla hesaplanır; kesintisiz izleme garantisi değildir.</p>
          <div className="observation-footer"><small>Son rapor: {time(report.as_of, true)} · simülasyon</small><div><button type="button" onClick={() => setRevision((value) => value + 1)}>Raporu yenile</button><button type="button" onClick={() => download(report)}>JSON raporunu indir</button></div></div>
        </>
      ) : null}
    </section>
  )
}
