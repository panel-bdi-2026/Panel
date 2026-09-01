export function fmtUsd(n: number | null | undefined, decimals = 2): string {
  if (n == null || isNaN(n)) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n)
}

export function fmtPct(n: number | null | undefined, decimals = 2): string {
  if (n == null || isNaN(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(decimals)}%`
}

export function fmtAge(isoOrTs: string | number | null | undefined): string {
  if (isoOrTs == null) return '—'
  const ms = typeof isoOrTs === 'number' ? isoOrTs * 1000 : Date.parse(isoOrTs)
  const diff = Math.floor((Date.now() - ms) / 1000)
  if (diff < 60) return `${diff}s`
  if (diff < 3600) return `${Math.floor(diff / 60)}m`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`
  return `${Math.floor(diff / 86400)}d`
}

export function fmtNumber(n: number | null | undefined, decimals = 0): string {
  if (n == null || isNaN(n)) return '—'
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(n)
}
