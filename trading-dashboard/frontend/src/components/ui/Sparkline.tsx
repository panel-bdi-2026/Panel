interface Props {
  data: number[]
  width?: number
  height?: number
  color?: string
  fill?: boolean
  className?: string
}

/**
 * Sparkline SVG puro (sin dependencias) — línea con relleno opcional.
 * Ideal para micro-tendencias en cards/filas.
 */
export function Sparkline({ data, width = 72, height = 24, color, fill = true, className = '' }: Props) {
  if (!data || data.length < 2) {
    return <svg width={width} height={height} className={className} />
  }
  const min = Math.min(...data)
  const max = Math.max(...data)
  const range = max - min || 1
  const stepX = width / (data.length - 1)
  const pts = data.map((v, i) => {
    const x = i * stepX
    const y = height - ((v - min) / range) * (height - 2) - 1
    return [x, y] as const
  })
  const line = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`
  // Color por tendencia si no se especifica
  const trendColor = color ?? (data[data.length - 1] >= data[0] ? '#22c55e' : '#ef4444')
  const gid = `spark-${Math.random().toString(36).slice(2, 8)}`

  return (
    <svg width={width} height={height} className={className} preserveAspectRatio="none">
      {fill && (
        <>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={trendColor} stopOpacity="0.25" />
              <stop offset="100%" stopColor={trendColor} stopOpacity="0" />
            </linearGradient>
          </defs>
          <path d={area} fill={`url(#${gid})`} />
        </>
      )}
      <path d={line} fill="none" stroke={trendColor} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}
