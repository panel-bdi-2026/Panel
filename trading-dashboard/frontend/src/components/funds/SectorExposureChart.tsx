import { useQuery } from '@tanstack/react-query'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts'
import type { Fund } from '../../api/types'
import { fetchSectors } from '../../api/signals'
import { fmtUsd } from '../../lib/format'
import { FUND_COLORS } from './EquityChart'

interface Props {
  fund: Fund
  priceBySymbol: Record<string, number | null>
}

interface TooltipProps {
  active?: boolean
  payload?: { name: string; value: number }[]
}

function DonutTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null
  const p = payload[0]
  return (
    <div className="bg-gray-900 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-200 font-medium">{p.name}</p>
      <p className="text-gray-400 nums">{fmtUsd(p.value)}</p>
    </div>
  )
}

export function SectorExposureChart({ fund, priceBySymbol }: Props) {
  const openPositions = Object.entries(fund.positions).filter(([, pos]) => pos.quantity !== 0)
  const symbols = openPositions.map(([sym]) => sym).sort()

  const { data: sectorBySymbol, isLoading } = useQuery({
    queryKey: ['sectors', symbols.join(',')],
    queryFn: () => fetchSectors(symbols),
    enabled: symbols.length > 0,
    staleTime: 24 * 60 * 60 * 1000,
  })

  if (!openPositions.length) {
    return (
      <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
        <span className="text-2xl">🧭</span>
        <span>Sin posiciones abiertas para mostrar exposición por sector</span>
      </div>
    )
  }

  if (isLoading) {
    return <div className="h-32 flex items-center justify-center text-gray-500 text-sm">Calculando exposición…</div>
  }

  // Valor de mercado (precio en vivo si está disponible, si no el costo de
  // compra -- mismo fallback que el resto del modal) agrupado por sector.
  // "Sin clasificar" junta los símbolos que get_sector no reconoce todavía.
  const bySector = new Map<string, number>()
  for (const [sym, pos] of openPositions) {
    const price = priceBySymbol[sym] ?? pos.avg_cost
    const value = pos.quantity * price
    const sector = sectorBySymbol?.[sym] ?? 'Sin clasificar'
    bySector.set(sector, (bySector.get(sector) ?? 0) + value)
  }
  const data = [...bySector.entries()]
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
  const total = data.reduce((a, d) => a + d.value, 0) || 1

  return (
    <div className="flex items-center gap-4">
      <div className="w-28 h-28 shrink-0">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              innerRadius="65%"
              outerRadius="100%"
              paddingAngle={2}
              strokeWidth={0}
            >
              {data.map((_, i) => <Cell key={i} fill={FUND_COLORS[i % FUND_COLORS.length]} />)}
            </Pie>
            <Tooltip content={<DonutTooltip />} />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="flex-1 min-w-0 space-y-1.5 max-h-48 overflow-y-auto scrollbar-thin">
        {data.map((d, i) => {
          const pct = (d.value / total) * 100
          return (
            <div key={d.name} className="flex items-center gap-2 text-xs">
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: FUND_COLORS[i % FUND_COLORS.length] }} />
              <span className="text-gray-300 truncate flex-1">{d.name}</span>
              <span className="text-gray-500 nums shrink-0">{pct.toFixed(0)}%</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
