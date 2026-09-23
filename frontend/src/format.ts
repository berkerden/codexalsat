export function decimal(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return String(value)
  return new Intl.NumberFormat('tr-TR', {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  }).format(parsed)
}

export function money(value: string | number | null | undefined, currency = 'USDT'): string {
  const rendered = decimal(value, 2)
  return rendered === '—' ? rendered : `${rendered} ${currency}`
}

export function time(value: number | null | undefined, withDate = false): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('tr-TR', {
    ...(withDate ? { day: '2-digit', month: 'short' } : {}),
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(new Date(value))
}

export function isDecimal(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))
}

export function labelize(key: string): string {
  const labels: Record<string, string> = {
    entry_fee: 'Giriş komisyonu',
    exit_fee: 'Çıkış komisyonu',
    spread_bps: 'Spread',
    slippage_bps: 'Kayma',
  }
  return labels[key] ?? key.replaceAll('_', ' ')
}
