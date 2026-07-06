import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchSignals } from '../../api/signals'
import { useRealtimeStore } from '../../store/realtime'
import { fmtPct } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { ScoreBar } from './ScoreBar'
import { SignalDetailPanel } from './SignalDetailPanel'
import type { Signal } from '../../api/types'

interface Props {
  onOrder?: (symbol: string) => void
}

function PassingDot({ passing }: { passing: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${passing ? 'bg-green-500' : 'bg-gray-600'}`}
      title={passing ? 'Pasa todos los filtros' : 'Filtros parciales'}
    />
  )
}

export function SignalsTable({ onOrder }: Props) {
  const { data: signals, isLoading } = useQuery({
    queryKey: ['signals'],
    queryFn: fetchSignals,
    refetchInterval: 30000,
  })
  const prices = useRealtimeStore((s) => s.prices)
  const [selected, setSelected] = useState<Signal | null>(null)

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando señales…</div>
  if (!signals?.length) return <div className="text-gray-500 text-sm">Sin señales activas.</div>

  const maxScore = Math.max(...signals.map((s) => s.score), 1)

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase border-b border-gray-800">
              <th className="text-left pb-2 pr-3 w-4"></th>
              <th className="text-left pb-2 pr-4">Símbolo</th>
              <th className="text-left pb-2 pr-4 hidden sm:table-cell">Estrategia</th>
              <th className="pb-2 pr-4 w-28">Score</th>
              <th className="text-right pb-2 pr-4 hidden md:table-cell">Precio</th>
              <th className="text-right pb-2 pr-4 hidden lg:table-cell">ATR%</th>
              <th className="text-right pb-2 hidden lg:table-cell">Desde máx.</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {signals.map((s) => {
              const livePrice = prices[s.symbol]?.price ?? null
              const isActive = selected?.symbol === s.symbol
              return (
                <tr
                  key={`${s.symbol}-${s.strategy_id}`}
                  onClick={() => setSelected(isActive ? null : s)}
                  className={`cursor-pointer transition-colors ${isActive ? 'bg-brand-900/30' : 'hover:bg-gray-900/50'}`}
                >
                  <td className="py-2 pr-3">
                    <PassingDot passing={s.passes_filters ?? s.passing} />
                  </td>
                  <td className="py-2 pr-4 font-semibold text-gray-100">{s.symbol}</td>
                  <td className="py-2 pr-4 hidden sm:table-cell">
                    <Badge variant="blue">{s.strategy_id}</Badge>
                  </td>
                  <td className="py-2 pr-4">
                    <ScoreBar score={s.score} max={maxScore} />
                  </td>
                  <td className="py-2 pr-4 text-right text-gray-200 hidden md:table-cell">
                    {livePrice != null
                      ? `$${livePrice.toFixed(2)}`
                      : s.price != null ? `$${s.price.toFixed(2)}` : '—'}
                  </td>
                  <td className="py-2 pr-4 text-right text-gray-400 hidden lg:table-cell">
                    {s.atr_pct != null ? fmtPct(s.atr_pct) : '—'}
                  </td>
                  <td className="py-2 text-right text-gray-400 hidden lg:table-cell">
                    {s.from_high_pct != null ? fmtPct(s.from_high_pct) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {selected && (
        <>
          <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setSelected(null)} />
          <SignalDetailPanel
            signal={selected}
            onClose={() => setSelected(null)}
            onOrder={onOrder}
          />
        </>
      )}
    </>
  )
}
