import { decimal, isDecimal, labelize, time } from '../format'
import type { MarketResponse } from '../types'

export function SignalCard({ market, now }: { market: MarketResponse; now: number }) {
  const signal = market.signal
  const trade = signal?.action && !signal.action.toLocaleUpperCase('tr-TR').includes('YAPMA')
  const costs = signal?.costs && typeof signal.costs === 'object' ? signal.costs : null
  const margin = signal?.margin && typeof signal.margin === 'object' && !Array.isArray(signal.margin)
    ? signal.margin as Record<string, unknown>
    : null
  const marginLabels: Record<string, string> = {
    net_target_profit: 'Hedefte net kâr',
    total_entry_cash: 'Toplam giriş nakdi',
    net_target_return: 'Hedefte net getiri',
    entry_fee: 'Giriş komisyonu',
    target_exit_fee: 'Hedef çıkış komisyonu',
    break_even_price: 'Başa baş fiyatı',
    modeled_stop_loss: 'Modellenen stop zararı',
    net_reward_risk: 'Net ödül / risk',
  }
  const quoteExpired = !market.quote?.observed_at || now - market.quote.observed_at > 5_000
  const signalExpired = typeof signal?.valid_until === 'number' && now > signal.valid_until
  const currentQuality = market.quality.healthy && !quoteExpired && !signalExpired

  return (
    <section className={`panel signal-panel ${trade ? 'signal-watch' : 'signal-no-trade'}`} aria-labelledby="signal-title">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">Net karar</span>
          <h2 id="signal-title">{signal?.action || 'İŞLEM YAPMA'}</h2>
        </div>
        <span className={`quality-dot ${currentQuality ? 'healthy' : 'unhealthy'}`}>
          {currentQuality ? 'Veri sağlıklı' : 'Veri engelli'}
        </span>
      </div>
      <p className="signal-reason">{signal?.reason || 'Doğrulanmış bir işlem gerekçesi bulunmuyor.'}</p>
      {market.quality.issues.length > 0 && (
        <ul className="issue-list" aria-label="Veri kalite sorunları">
          {market.quality.issues.map((issue, index) => (
            <li key={`${typeof issue === 'string' ? issue : issue.code ?? issue.message}-${index}`}>
              {typeof issue === 'string' ? issue : issue.message ?? JSON.stringify(issue)}
            </li>
          ))}
        </ul>
      )}
      {(quoteExpired || signalExpired) && (
        <ul className="issue-list freshness-issues" aria-label="Güncellik sorunları">
          {quoteExpired && <li>Kotasyon güncelliğini yitirdi; yeni işlem açılamaz.</li>}
          {signalExpired && <li>Sinyalin geçerlilik süresi doldu.</li>}
        </ul>
      )}
      <dl className="metric-grid compact">
        <div><dt>Strateji</dt><dd>{signal?.strategy || '—'}</dd></div>
        <div><dt>Veri zamanı</dt><dd>{time(typeof signal?.data_time === 'number' ? signal.data_time : signal?.observed_at)}</dd></div>
        <div><dt>Alış / Satış</dt><dd>{decimal(market.quote?.bid, 8)} / {decimal(market.quote?.ask, 8)}</dd></div>
        <div><dt>Spread</dt><dd>{decimal(market.quote?.spread_bps, 3)} bp</dd></div>
      </dl>
      {costs && (
        <div className="cost-box">
          <strong>Maliyet varsayımları</strong>
          <dl>
            {Object.entries(costs).map(([key, value]) => (
              <div key={key}><dt>{labelize(key)}</dt><dd>{String(value)}</dd></div>
            ))}
          </dl>
        </div>
      )}
      {margin && (
        <div className="scenario-box">
          <div className="scenario-title">
            <span>Varsayımsal hedef senaryosu</span>
            {signal?.scenario_only === true && <i>Senaryo</i>}
          </div>
          <p>{typeof signal?.quantity_basis === 'string' ? signal.quantity_basis : 'Miktar temeli backend tarafından belirtilmedi; pozisyon önerisi değildir.'}</p>
          <dl>
            {Object.entries(marginLabels).map(([key, label]) => {
              if (margin[key] === null || margin[key] === undefined) return null
              const isReturn = key === 'net_target_return'
              const raw = Number(margin[key])
              const rendered = isReturn && Number.isFinite(raw)
                ? `${decimal(raw * 100, 4)}%`
                : decimal(String(margin[key]), key.includes('price') || key.includes('cash') || key.includes('profit') || key.includes('loss') || key.includes('fee') ? 8 : 6)
              return <div key={key}><dt>{label}</dt><dd>{rendered}</dd></div>
            })}
          </dl>
          <div className="scenario-meta">
            <span>Elde tutma <b>{typeof signal?.holding_minutes === 'number' ? `${signal.holding_minutes} dk` : '—'}</b></span>
            <span>Geçerlilik <b>{time(typeof signal?.valid_until === 'number' ? signal.valid_until : undefined)}</b></span>
          </div>
          <p className="fine-print">Hedefte net kâr bir senaryo sonucudur; beklenen net sonuç değildir.</p>
        </div>
      )}
      {signal?.expected_net_result !== null && signal?.expected_net_result !== undefined && (
        <p className="expected-result">Beklenen net sonuç <b>{decimal(String(signal.expected_net_result), 8)}</b></p>
      )}
      {typeof signal?.evidence === 'string' && <p className="fine-print">Kanıt notu: {signal.evidence}</p>}
      <p className="fine-print">Sinyal öneridir; ekonomik avantaj onayı veya emir değildir.</p>
      {(isDecimal(signal?.entry) || isDecimal(signal?.target) || isDecimal(signal?.stop)) && (
        <div className="levels" aria-label="Hesaplanmış fiyat seviyeleri">
          {isDecimal(signal?.entry) && <span>Giriş <b>{decimal(signal.entry, 8)}</b></span>}
          {isDecimal(signal?.target) && <span>Hedef <b>{decimal(signal.target, 8)}</b></span>}
          {isDecimal(signal?.stop) && <span>Stop <b>{decimal(signal.stop, 8)}</b></span>}
        </div>
      )}
    </section>
  )
}
