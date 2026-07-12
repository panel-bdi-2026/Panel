import { useQuery } from '@tanstack/react-query'
import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip } from 'recharts'
import { fetchFunds } from '../../api/funds'
import { fmtUsd } from '../../lib/format'
import { FUND_COLORS } from './EquityChart'

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

/**
 * Donut de asignación de capital entre fondos activos (capital aportado neto,
 * no valor de mercado — evita depender de precios live para algo que solo
 * necesita mostrar cómo se repartió el capital entre estrategias).
 */
export function AllocationDonut() {
  const { data: funds } = useQuery({
    queryKey: ['funds'],
    queryFn: fetchFunds,
    refetchInterval: 15000,
  })

  const active = (funds ?? []).filter((f) => !f.closed && f.net_contributed_capital > 0)
  if (active.length < 2) return null // con 0-1 fondo, un donut no aporta nada

  const total = active.reduce((a, f) => a + f.net_contributed_capital, 0)
  const data = active.map((f) => ({ name: f.name, value: f.net_contributed_capital }))

  return (
    <div className="bg-surface-1 border border-surface-3 rounded-xl2 p-4 flex items-center gap-4">
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
      <div className="flex-1 min-w-0 space-y-1.5">
        <p className="text-[11px] uppercase tracking-wide text-gray-500 mb-1.5">Asignación de capital</p>
        {active.map((f, i) => {
          const pct = total ? (f.net_contributed_capital / total) * 100 : 0
          return (
            <div key={f.id} className="flex items-center gap-2 text-xs">
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: FUND_COLORS[i % FUND_COLORS.length] }} />
              <span className="text-gray-300 truncate flex-1">{f.name}</span>
              <span className="text-gray-500 nums shrink-0">{pct.toFixed(0)}%</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
