import { useQuery } from '@tanstack/react-query'
import { fetchPositions } from '../../api/account'
import { useRealtimeStore } from '../../store/realtime'
import { fmtUsd, fmtPct } from '../../lib/format'

export function PositionsTable() {
  const { data: positions, isLoading } = useQuery({
    queryKey: ['positions'],
    queryFn: fetchPositions,
    refetchInterval: 10000,
  })
  const priceMap = useRealtimeStore((s) => s.prices)

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando posiciones…</div>
  if (!positions?.length) return <div className="text-gray-500 text-sm">Sin posiciones abiertas.</div>

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 uppercase border-b border-gray-800">
            <th className="text-left pb-2 pr-4">Símbolo</th>
            <th className="text-right pb-2 pr-4">Qty</th>
            <th className="text-right pb-2 pr-4">Costo avg.</th>
            <th className="text-right pb-2 pr-4">Precio live</th>
            <th className="text-right pb-2 pr-4">Valor mercado</th>
            <th className="text-right pb-2 pr-4">P&L no real.</th>
            <th className="text-right pb-2">P&L %</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {positions.map((p) => {
            const live = priceMap[p.symbol]?.price ?? null
            const liveValue = live !== null ? live * p.quantity : p.market_value
            const livePnl = live !== null ? (live - p.avg_cost) * p.quantity : p.unrealized_pnl
            const livePnlPct = live !== null ? ((live - p.avg_cost) / p.avg_cost) * 100 : p.unrealized_pnl_pct
            return (
              <tr key={p.symbol} className="hover:bg-gray-900/50">
                <td className="py-2 pr-4 font-semibold text-gray-100">{p.symbol}</td>
                <td className="py-2 pr-4 text-right text-gray-300">{p.quantity}</td>
                <td className="py-2 pr-4 text-right text-gray-400">{fmtUsd(p.avg_cost)}</td>
                <td className="py-2 pr-4 text-right text-gray-300">
                  {live !== null ? fmtUsd(live) : '—'}
                </td>
                <td className="py-2 pr-4 text-right text-gray-300">{fmtUsd(liveValue)}</td>
                <td className={`py-2 pr-4 text-right ${livePnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {fmtUsd(livePnl)}
                </td>
                <td className={`py-2 text-right ${livePnlPct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {fmtPct(livePnlPct)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
