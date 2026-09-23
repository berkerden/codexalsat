import { useId } from 'react'
import { decimal, isDecimal, time } from '../format'
import type { Candle, Signal } from '../types'

interface Props {
  candles: Candle[]
  signal: Signal | null
  symbol: string
}

export function CandleChart({ candles, signal, symbol }: Props) {
  const gradientId = useId().replaceAll(':', '')
  const visible = candles.slice(-64).filter((candle) =>
    [candle.open, candle.high, candle.low, candle.close].every((value) => Number.isFinite(Number(value))),
  )

  if (!visible.length) {
    return (
      <div className="chart-empty" role="status">
        <span className="empty-mark" aria-hidden="true">⌁</span>
        <strong>Grafik için kapanmış mum bekleniyor</strong>
        <span>Seçili piyasadan veri geldiğinde burada gösterilir.</span>
      </div>
    )
  }

  const overlays = [
    isDecimal(signal?.target) ? { value: Number(signal.target), label: 'Hedef', className: 'target' } : null,
    isDecimal(signal?.stop) ? { value: Number(signal.stop), label: 'Stop', className: 'stop' } : null,
  ].filter((item): item is { value: number; label: string; className: string } => item !== null)
  const rawValues = visible.flatMap((candle) => [Number(candle.low), Number(candle.high)])
  rawValues.push(...overlays.map((item) => item.value))
  const low = Math.min(...rawValues)
  const high = Math.max(...rawValues)
  const padding = Math.max((high - low) * 0.08, high * 0.0005)
  const min = low - padding
  const max = high + padding
  const width = 900
  const height = 330
  const plot = { left: 18, right: 78, top: 18, bottom: 34 }
  const innerWidth = width - plot.left - plot.right
  const innerHeight = height - plot.top - plot.bottom
  const x = (index: number) => plot.left + ((index + 0.5) / visible.length) * innerWidth
  const y = (value: number) => plot.top + ((max - value) / (max - min || 1)) * innerHeight
  const candleWidth = Math.max(2, Math.min(9, (innerWidth / visible.length) * 0.58))
  const last = visible.at(-1)!
  const change = ((Number(last.close) - Number(visible[0].open)) / Number(visible[0].open)) * 100

  return (
    <figure className="chart" aria-label={`${symbol} mum grafiği`}>
      <div className="chart-meta">
        <div>
          <span className="eyebrow">Son kapanış</span>
          <strong>{decimal(last.close, 8)}</strong>
        </div>
        <span className={`change ${change >= 0 ? 'up' : 'down'}`}>{change >= 0 ? '+' : ''}{decimal(change, 2)}%</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img">
        <title>{`${symbol}; ${visible.length} gerçek kapanmış mum. Son kapanış ${last.close}.`}</title>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#dbece7" stopOpacity=".48" />
            <stop offset="1" stopColor="#dbece7" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 0.25, 0.5, 0.75, 1].map((part) => {
          const value = max - part * (max - min)
          const pos = plot.top + part * innerHeight
          return (
            <g key={part}>
              <line className="grid-line" x1={plot.left} x2={width - plot.right} y1={pos} y2={pos} />
              <text className="axis-label" x={width - plot.right + 8} y={pos + 4}>{decimal(value, 4)}</text>
            </g>
          )
        })}
        <path
          className="close-area"
          fill={`url(#${gradientId})`}
          d={`${visible.map((candle, index) => `${index ? 'L' : 'M'}${x(index)},${y(Number(candle.close))}`).join(' ')} L${x(visible.length - 1)},${plot.top + innerHeight} L${x(0)},${plot.top + innerHeight} Z`}
        />
        {visible.map((candle, index) => {
          const open = Number(candle.open)
          const close = Number(candle.close)
          const rising = close >= open
          const bodyTop = y(Math.max(open, close))
          const bodyHeight = Math.max(1.5, Math.abs(y(open) - y(close)))
          return (
            <g key={candle.open_time} className={rising ? 'candle-up' : 'candle-down'}>
              <line x1={x(index)} x2={x(index)} y1={y(Number(candle.high))} y2={y(Number(candle.low))} />
              <rect x={x(index) - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} rx="1" />
            </g>
          )
        })}
        {overlays.map((item) => (
          <g key={item.label} className={`price-overlay ${item.className}`}>
            <line x1={plot.left} x2={width - plot.right} y1={y(item.value)} y2={y(item.value)} />
            <text x={plot.left + 7} y={y(item.value) - 5}>{item.label} · {decimal(item.value, 5)}</text>
          </g>
        ))}
        <text className="axis-label" x={plot.left} y={height - 8}>{time(visible[0].open_time)}</text>
        <text className="axis-label" textAnchor="end" x={width - plot.right} y={height - 8}>{time(last.close_time)}</text>
      </svg>
    </figure>
  )
}
