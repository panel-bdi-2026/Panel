import { useMemo } from 'react'
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  Cell,
} from 'recharts'
import type { FundTrade } from '../../api/types'
import { fmtUsd } from '../../lib/format'

interface Props {
  trades: FundTrade[]
}

interface TooltipProps {
  active?: boolean
  payload?: { value: number }[]
  label?: string
}

function CustomTooltip({ active, payload, label }: TooltipProps) {
  if (!active || !payload?.length) return null
  const val = payload[0].value
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-300 font-semibold mb-0.5">{label}</p>
      <p className={`font-semibold nums ${val >= 0 ? 'text-green-400' : 'text-red-400'}`}>
        {val >= 0 ? '+' : ''}{fmtUsd(val)}
      </p>
    </div>
  )
}

export function PnlBySymbolChart({ trades }: Props) {
  const data = useMemo(() => {
    const map: Record<string, number> = {}
    for (const t of trades) {
      if (t.side === 'SELL' && t.realized_pnl != null) {
        map[t.symbol] = (map[t.symbol] ?? 0) + t.realized_pnl
      }
    }
    return Object.entries(map)
      .map(([symbol, pnl]) => ({ symbol, pnl }))
      .sort((a, b) => b.pnl - a.pnl)
  }, [trades])

  if (data.length === 0) {
    return (
      <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
        <span>Sin trades cerrados aún</span>
      </div>
    )
  }

  const yMax = Math.max(...data.map((d) => Math.abs(d.pnl)))
  const domain: [number, number] = [-yMax * 1.1, yMax * 1.1]

  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 28, left: 0 }}>
          <XAxis
            dataKey="symbol"
            tick={{ fill: '#6b7280', fontSize: 9 }}
            tickLine={false}
            interval={0}
            angle={-35}
            textAnchor="end"
          />
          <YAxis
            domain={domain}
            tick={{ fill: '#6b7280', fontSize: 10 }}
            tickLine={false}
            tickFormatter={(v: number) =>
              `${v >= 0 ? '' : '-'}$${Math.abs(v) >= 1000 ? `${(Math.abs(v) / 1000).toFixed(1)}k` : Math.abs(v).toFixed(0)}`
            }
            width={44}
          />
          <Tooltip content={<CustomTooltip />} />
          <ReferenceLine y={0} stroke="#374151" strokeWidth={1} />
          <Bar dataKey="pnl" isAnimationActive={false} radius={[2, 2, 0, 0]}>
            {data.map((entry) => (
              <Cell
                key={entry.symbol}
                fill={entry.pnl >= 0 ? '#22c55e' : '#ef4444'}
                fillOpacity={0.85}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}
