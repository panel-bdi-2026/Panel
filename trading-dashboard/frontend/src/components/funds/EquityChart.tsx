import { useQuery } from '@tanstack/react-query'
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  ReferenceLine,
  CartesianGrid,
} from 'recharts'
import { fetchRoiHistory } from '../../api/funds'

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
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-500 mb-1">{label}</p>
      {payload.map((p) => (
        <p key={p.name} style={{ color: p.color }}>
          {p.name}: <span className="font-semibold">{fmt(p.value)}</span>
        </p>
      ))}
    </div>
  )
}

const FUND_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4']

interface Props {
  fundId?: string
}

export function EquityChart({ fundId }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['roi-history'],
    queryFn: fetchRoiHistory,
    refetchInterval: 5 * 60 * 1000,
    staleTime: 4 * 60 * 1000,
  })

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

  if (fundId) {
    const fund = data.per_fund?.find((f) => f.id === fundId)
    if (!fund?.dates.length) {
      return (
        <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
          <span className="text-2xl">📈</span>
          <span>Sin trades para este fondo aún</span>
        </div>
      )
    }
    const points = fund.dates.map((d, i) => ({
      date: d,
      [fund.name]: fund.cumulative_return_pct[i],
      'SPY': fund.benchmark_cumulative_return_pct[i],
    }))
    return (
      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} tickFormatter={fmt} width={52} />
            <Tooltip content={<CustomTooltip />} />
            <ReferenceLine y={0} stroke="#374151" />
            <Line type="monotone" dataKey={fund.name} stroke="#3b82f6" dot={false} strokeWidth={2} />
            <Line type="monotone" dataKey="SPY" stroke="#6b7280" dot={false} strokeWidth={1} strokeDasharray="4 2" />
          </LineChart>
        </ResponsiveContainer>
      </div>
    )
  }

  const points = data.dates.map((d, i) => {
    const row: Record<string, string | number> = {
      date: d,
      'Portfolio': data.fund_cumulative_return_pct[i],
      'SPY': data.benchmark_cumulative_return_pct[i],
    }
    data.per_fund?.forEach((f) => {
      const idx = f.dates.indexOf(d)
      if (idx !== -1) row[f.name] = f.cumulative_return_pct[idx]
    })
    return row
  })

  const lastPortfolio = data.fund_cumulative_return_pct.at(-1) ?? 0
  const lastBench = data.benchmark_cumulative_return_pct.at(-1) ?? 0
  const alpha = lastPortfolio - lastBench

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-6 text-sm">
        <div>
          <span className="text-gray-500 text-xs">Portfolio</span>
          <span className={`ml-2 font-semibold ${lastPortfolio >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {fmt(lastPortfolio)}
          </span>
        </div>
        <div>
          <span className="text-gray-500 text-xs">SPY</span>
          <span className="ml-2 text-gray-400 font-medium">{fmt(lastBench)}</span>
        </div>
        <div>
          <span className="text-gray-500 text-xs">Alpha</span>
          <span className={`ml-2 font-semibold ${alpha >= 0 ? 'text-blue-400' : 'text-red-400'}`}>
            {fmt(alpha)}
          </span>
        </div>
      </div>

      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
            <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} tickFormatter={fmt} width={52} />
            <Tooltip content={<CustomTooltip />} />
            <Legend wrapperStyle={{ fontSize: '11px', color: '#9ca3af' }} />
            <ReferenceLine y={0} stroke="#374151" />
            <Line type="monotone" dataKey="Portfolio" stroke="#10b981" dot={false} strokeWidth={2.5} />
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
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
