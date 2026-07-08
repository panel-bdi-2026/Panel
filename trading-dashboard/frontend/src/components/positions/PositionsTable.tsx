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
  if (!positions?.length) return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <p className="text-2xl mb-2">📊</p>
      <p className="text-gray-400 font-medium">Sin posiciones abiertas</p>
    </div>
  )

  const rows = positions.map((p) => {
    const live = priceMap[p.symbol]?.price ?? null
    const liveValue = live !== null ? live * p.quantity : p.market_value
    const livePnl = live !== null ? (live - p.avg_cost) * p.quantity : p.unrealized_pnl
    const livePnlPct = live !== null ? ((live - p.avg_cost) / p.avg_cost) * 100 : p.unrealized_pnl_pct
    return { p, live, liveValue, livePnl, livePnlPct }
  })

  const pnlClass = (v: number | null) =>
    v == null ? 'text-gray-500' : v >= 0 ? 'text-green-400' : 'text-red-400'

  return (
    <>
      {/* Mobile: card view */}
      <div className="sm:hidden space-y-2">
        {rows.map(({ p, live, liveValue, livePnl, livePnlPct }) => (
          <div key={p.symbol} className="bg-gray-900 rounded-xl px-4 py-3 space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-bold text-gray-100 text-base">{p.symbol}</span>
              <span className={`font-semibold text-sm tabular-nums ${pnlClass(livePnl)}`}>
                {livePnl != null ? fmtUsd(livePnl) : '—'}
                <span className="text-xs ml-1">({livePnlPct != null ? fmtPct(livePnlPct) : '—'})</span>
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <div>
                <p className="text-gray-500">Qty</p>
                <p className="text-gray-300 tabular-nums">{p.quantity}</p>
              </div>
              <div>
                <p className="text-gray-500">Costo avg</p>
                <p className="text-gray-300 tabular-nums">{fmtUsd(p.avg_cost)}</p>
              </div>
              <div>
                <p className="text-gray-500">Precio live</p>
                <p className="text-gray-300 tabular-nums">{live !== null ? fmtUsd(live) : '—'}</p>
              </div>
            </div>
            <div className="flex items-center justify-between text-xs border-t border-gray-800 pt-2">
              <span className="text-gray-500">Valor mercado</span>
              <span className="text-gray-300 tabular-nums">{fmtUsd(liveValue)}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Desktop: table */}
      <div className="hidden sm:block overflow-x-auto">
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
            {rows.map(({ p, live, liveValue, livePnl, livePnlPct }) => (
              <tr key={p.symbol} className="hover:bg-gray-900/50">
                <td className="py-2 pr-4 font-semibold text-gray-100">{p.symbol}</td>
                <td className="py-2 pr-4 text-right text-gray-300 tabular-nums">{p.quantity}</td>
                <td className="py-2 pr-4 text-right text-gray-400 tabular-nums">{fmtUsd(p.avg_cost)}</td>
                <td className="py-2 pr-4 text-right text-gray-300 tabular-nums">
                  {live !== null ? fmtUsd(live) : '—'}
                </td>
                <td className="py-2 pr-4 text-right text-gray-300 tabular-nums">{fmtUsd(liveValue)}</td>
                <td className={`py-2 pr-4 text-right tabular-nums ${pnlClass(livePnl)}`}>
                  {livePnl != null ? fmtUsd(livePnl) : '—'}
                </td>
                <td className={`py-2 text-right tabular-nums ${pnlClass(livePnlPct)}`}>
                  {livePnlPct != null ? fmtPct(livePnlPct) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
