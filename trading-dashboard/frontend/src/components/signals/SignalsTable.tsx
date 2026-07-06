import { useQuery } from '@tanstack/react-query'
import { fetchSignals } from '../../api/signals'
import { useRealtimeStore } from '../../store/realtime'
import { fmtPct } from '../../lib/format'
import { Badge } from '../ui/Badge'

export function SignalsTable() {
  const { data: signals, isLoading } = useQuery({
    queryKey: ['signals'],
    queryFn: fetchSignals,
    refetchInterval: 30000,
  })
  const prices = useRealtimeStore((s) => s.prices)

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando señales…</div>
  if (!signals?.length) return <div className="text-gray-500 text-sm">Sin señales activas.</div>

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 uppercase border-b border-gray-800">
            <th className="text-left pb-2 pr-4">Símbolo</th>
            <th className="text-left pb-2 pr-4">Estrategia</th>
            <th className="text-right pb-2 pr-4">Score</th>
            <th className="text-right pb-2 pr-4">Precio</th>
            <th className="text-right pb-2 pr-4">ATR%</th>
            <th className="text-right pb-2">Desde máx.</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {signals.map((s) => {
            const livePrice = prices[s.symbol]?.price ?? null
            return (
              <tr key={`${s.symbol}-${s.strategy_id}`} className="hover:bg-gray-900/50">
                <td className="py-2 pr-4 font-semibold text-gray-100">{s.symbol}</td>
                <td className="py-2 pr-4">
                  <Badge variant="blue">{s.strategy_id}</Badge>
                </td>
                <td className="py-2 pr-4 text-right text-gray-200">{s.score?.toFixed(2) ?? '—'}</td>
                <td className="py-2 pr-4 text-right text-gray-200">
                  {livePrice != null ? `$${livePrice.toFixed(2)}` : (s.price != null ? `$${s.price.toFixed(2)}` : '—')}
                </td>
                <td className="py-2 pr-4 text-right text-gray-400">
                  {s.atr_pct != null ? fmtPct(s.atr_pct) : '—'}
                </td>
                <td className="py-2 text-right text-gray-400">
                  {s.from_high_pct != null ? fmtPct(s.from_high_pct) : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
