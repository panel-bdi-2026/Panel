import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ReferenceLine,
  CartesianGrid,
} from 'recharts'
import { fetchRoiHistory } from '../../api/funds'

// Nomenclatura de rangos calcada del IBKR GlobalTrader.
type Range = '1S' | 'MTD' | '1M' | '3M' | 'YTD' | '1A' | 'Todo'

const RANGES: Range[] = ['1S', 'MTD', '1M', '3M', 'YTD', '1A', 'Todo']

function fmt(v: number) {
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`
}

interface TooltipProps {
  active?: boolean
  payload?: { name: string; value: number; color: string }[]
  label?: string
}

function CustomTooltip({ active, payload, label }: TooltipProps) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl max-w-[200px]">
      <p className="text-gray-500 mb-1">{label}</p>
      {payload.map((p) => (
        <p key={p.name} style={{ color: p.color }}>
          {p.name}: <span className="font-semibold">{fmt(p.value)}</span>
        </p>
      ))}
    </div>
  )
}

function RangeSelector({ value, onChange }: { value: Range; onChange: (r: Range) => void }) {
  return (
    <div className="flex gap-0.5 bg-surface-2 rounded-md p-0.5 overflow-x-auto scrollbar-none">
      {RANGES.map((label) => (
        <button
          key={label}
          onClick={() => onChange(label)}
          className={`px-2 py-0.5 rounded text-xs font-medium transition-colors shrink-0 ${
            value === label
              ? 'bg-surface-3 text-gray-100'
              : 'text-gray-500 hover:text-gray-300'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

type ChartPoint = Record<string, string | number>

// A diferencia de las demás (basadas en "hace N días"), MTD/YTD son de
// calendario: desde el 1° del mes / del año actual.
function rangeCutoff(range: Range): string | null {
  const now = new Date()
  switch (range) {
    case '1S': { const d = new Date(now); d.setDate(d.getDate() - 7);  return d.toISOString().slice(0, 10) }
    case '1M': { const d = new Date(now); d.setDate(d.getDate() - 30); return d.toISOString().slice(0, 10) }
    case '3M': { const d = new Date(now); d.setDate(d.getDate() - 90); return d.toISOString().slice(0, 10) }
    case '1A': { const d = new Date(now); d.setDate(d.getDate() - 365); return d.toISOString().slice(0, 10) }
    case 'MTD': return new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10)
    case 'YTD': return new Date(now.getFullYear(), 0, 1).toISOString().slice(0, 10)
    case 'Todo': return null
  }
}

function filterByRange(points: ChartPoint[], range: Range): ChartPoint[] {
  const cutoff = rangeCutoff(range)
  if (!cutoff || points.length === 0) return points
  return points.filter((p) => (p['date'] as string) >= cutoff)
}

// Re-bases all numeric series so the first visible point = 0%
function rebase(points: ChartPoint[], keys: string[]): ChartPoint[] {
  if (points.length === 0) return points
  const base: Record<string, number> = {}
  for (const k of keys) {
    const first = points.find((p) => p[k] !== undefined && p[k] !== null)
    base[k] = first ? (first[k] as number) : 0
  }
  return points.map((p) => {
    const out: ChartPoint = { ...p }
    for (const k of keys) {
      if (out[k] !== undefined && out[k] !== null) {
        out[k] = (out[k] as number) - base[k]
      }
    }
    return out
  })
}

// Exportada para que otros gráficos de fondos (ej. el donut de asignación)
// usen la misma paleta y el color de un fondo sea consistente entre vistas.
export const FUND_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4']
const GRADIENT_ID = 'equityGradient'

interface Props {
  fundId?: string
}

export function EquityChart({ fundId }: Props) {
  const [range, setRange] = useState<Range>('Todo')

  const { data, isLoading, error } = useQuery({
    queryKey: ['roi-history'],
    queryFn: fetchRoiHistory,
    refetchInterval: 5 * 60 * 1000,
    staleTime: 4 * 60 * 1000,
  })

  // Per-fund view
  const fundPoints = useMemo(() => {
    if (!data || !fundId) return null
    const fund = data.per_fund?.find((f) => f.id === fundId)
    if (!fund?.dates.length) return null
    const pts = fund.dates.map((d, i) => ({
      date: d,
      [fund.name]: fund.cumulative_return_pct[i],
      SPY: fund.benchmark_cumulative_return_pct[i],
    }))
    const filtered = filterByRange(pts, range)
    return { fund, points: rebase(filtered, [fund.name, 'SPY']) }
  }, [data, fundId, range])

  // Portfolio view
  const portfolioPoints = useMemo(() => {
    if (!data || fundId) return null
    const pts = data.dates.map((d, i) => {
      const row: Record<string, string | number> = {
        date: d,
        Portfolio: data.fund_cumulative_return_pct[i],
        SPY: data.benchmark_cumulative_return_pct[i],
      }
      data.per_fund?.forEach((f) => {
        const idx = f.dates.indexOf(d)
        if (idx !== -1) row[f.name] = f.cumulative_return_pct[idx]
      })
      return row
    })
    const seriesKeys = ['Portfolio', 'SPY', ...(data.per_fund?.map((f) => f.name) ?? [])]
    const filtered = filterByRange(pts, range)
    return rebase(filtered, seriesKeys)
  }, [data, fundId, range])

  if (isLoading) {
    return (
      <div className="h-64 flex items-center justify-center text-gray-500 text-sm">
        Calculando curva de equity…
      </div>
    )
  }

  if (error || !data?.dates?.length) {
    return (
      <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
        <span className="text-2xl">📈</span>
        <span>Sin historial de trades aún</span>
      </div>
    )
  }

  // Gradiente compartido — verde si termina en positivo, rojo si negativo
  const GradientDefs = ({ positive }: { positive: boolean }) => (
    <defs>
      <linearGradient id={GRADIENT_ID} x1="0" y1="0" x2="0" y2="1">
        <stop offset="5%" stopColor={positive ? '#22c55e' : '#ef4444'} stopOpacity={0.35} />
        <stop offset="95%" stopColor={positive ? '#22c55e' : '#ef4444'} stopOpacity={0} />
      </linearGradient>
    </defs>
  )

  // --- Per-fund chart ---
  if (fundId) {
    if (!fundPoints) {
      return (
        <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
          <span className="text-2xl">📈</span>
          <span>Sin trades para este fondo aún</span>
        </div>
      )
    }
    const { fund, points } = fundPoints
    const lastVal = (points.at(-1)?.[fund.name] ?? 0) as number
    return (
      <div className="space-y-2">
        <div className="flex justify-end">
          <RangeSelector value={range} onChange={setRange} />
        </div>
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <GradientDefs positive={lastVal >= 0} />
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} interval="preserveStartEnd" />
              <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} tickFormatter={fmt} width={52} />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine y={0} stroke="#374151" />
              <Area type="monotone" dataKey={fund.name} stroke="none" fill={`url(#${GRADIENT_ID})`} isAnimationActive={false} />
              <Line type="monotone" dataKey={fund.name} stroke={lastVal >= 0 ? '#22c55e' : '#ef4444'} dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="SPY" stroke="#6b7280" dot={false} strokeWidth={1} strokeDasharray="4 2" />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>
    )
  }

  // --- Portfolio chart ---
  const points = portfolioPoints ?? []
  const lastPoint = points.at(-1) ?? {}
  const lastPortfolio = points.length ? ((lastPoint['Portfolio'] ?? 0) as number) : 0
  const lastBench = points.length ? ((lastPoint['SPY'] ?? 0) as number) : 0
  const alpha = lastPortfolio - lastBench

  const rangeLabel = range === 'Todo' ? 'total' : `en ${range}`

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-6 text-sm">
          <div>
            <span className="text-gray-500 text-xs">Portfolio</span>
            <span className={`ml-2 font-semibold nums ${lastPortfolio >= 0 ? 'text-profit' : 'text-loss'}`}>
              {fmt(lastPortfolio)}
            </span>
          </div>
          <div>
            <span className="text-gray-500 text-xs">SPY</span>
            <span className="ml-2 text-gray-400 font-medium nums">{fmt(lastBench)}</span>
          </div>
          <div>
            <span className="text-gray-500 text-xs">Alpha {rangeLabel}</span>
            <span className={`ml-2 font-semibold nums ${alpha >= 0 ? 'text-blue-400' : 'text-loss'}`}>
              {fmt(alpha)}
            </span>
          </div>
        </div>
        <RangeSelector value={range} onChange={setRange} />
      </div>

      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <GradientDefs positive={lastPortfolio >= 0} />
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} tickFormatter={fmt} width={52} />
            <Tooltip content={<CustomTooltip />} />
            <Legend wrapperStyle={{ fontSize: '11px', color: '#9ca3af' }} />
            <ReferenceLine y={0} stroke="#374151" />
            <Area type="monotone" dataKey="Portfolio" stroke="none" fill={`url(#${GRADIENT_ID})`} isAnimationActive={false} legendType="none" />
            <Line type="monotone" dataKey="Portfolio" stroke={lastPortfolio >= 0 ? '#22c55e' : '#ef4444'} dot={false} strokeWidth={2.5} />
            <Line type="monotone" dataKey="SPY" stroke="#6b7280" dot={false} strokeWidth={1.5} strokeDasharray="4 2" />
            {data.per_fund?.map((f, i) => (
              <Line
                key={f.id}
                type="monotone"
                dataKey={f.name}
                stroke={FUND_COLORS[i % FUND_COLORS.length]}
                dot={false}
                strokeWidth={1}
                strokeOpacity={0.6}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
