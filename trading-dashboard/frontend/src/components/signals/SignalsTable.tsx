import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchSignals, fetchLivePrices } from '../../api/signals'
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
      title={passing ? 'Pasa todos los filtros' : 'Filtros parciales'}
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${passing ? 'bg-green-500' : 'bg-gray-600'}`}
    />
  )
}

export function SignalsTable({ onOrder }: Props) {
  const { data: signals, isLoading } = useQuery({
    queryKey: ['signals'],
    queryFn: fetchSignals,
    refetchInterval: 30000,
  })

  const symbolList = signals?.map((s) => s.symbol) ?? []

  const { data: livePrices } = useQuery({
    queryKey: ['live-prices', symbolList],
    queryFn: () => fetchLivePrices(symbolList),
    enabled: symbolList.length > 0,
    refetchInterval: 5000,
  })

  const [selected, setSelected] = useState<Signal | null>(null)

  if (isLoading) {
    return (
      <div className="flex flex-col gap-2 animate-pulse">
        {[...Array(5)].map((_, i) => (
          <div key={i} className="h-10 bg-gray-800 rounded-lg" />
        ))}
      </div>
    )
  }

  if (!signals?.length) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="text-2xl mb-2">📡</p>
        <p className="text-gray-400 font-medium">Sin señales activas</p>
        <p className="text-gray-600 text-sm mt-1">El escáner no encontró candidatos con el filtro actual</p>
      </div>
    )
  }

  const maxScore = Math.max(...signals.map((s) => s.score), 1)

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 border-b border-gray-800">
              <th className="text-left pb-2.5 pr-3 w-4" />
              <th className="text-left pb-2.5 pr-4">Símbolo</th>
              <th className="text-left pb-2.5 pr-4 hidden sm:table-cell">Estrategia</th>
              <th className="pb-2.5 pr-4 w-28 text-left">Score</th>
              <th className="text-right pb-2.5 pr-4 hidden md:table-cell">Precio</th>
              <th className="text-right pb-2.5 pr-4 hidden lg:table-cell">ATR%</th>
              <th className="text-right pb-2.5 hidden lg:table-cell">Desde máx.</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {signals.map((s) => {
              const lp = livePrices?.[s.symbol]
              const displayPrice = lp?.last_price ?? s.price
              const isActive = selected?.symbol === s.symbol
              return (
                <tr
                  key={`${s.symbol}-${s.strategy_id}`}
                  onClick={() => setSelected(isActive ? null : s)}
                  className={`cursor-pointer transition-colors group ${
                    isActive ? 'bg-brand-950/50 border-brand-900/30' : 'hover:bg-gray-900/60'
                  }`}
                >
                  <td className="py-2.5 pr-3">
                    <PassingDot passing={s.passes_filters ?? s.passing} />
                  </td>
                  <td className="py-2.5 pr-4">
                    <span className="font-semibold text-gray-100">{s.symbol}</span>
                    {s.sector && (
                      <span className="text-gray-600 text-xs ml-2 hidden sm:inline">{s.sector}</span>
                    )}
                  </td>
                  <td className="py-2.5 pr-4 hidden sm:table-cell">
                    <Badge variant="blue">{s.strategy_id}</Badge>
                  </td>
                  <td className="py-2.5 pr-4">
                    <ScoreBar score={s.score} max={maxScore} />
                  </td>
                  <td className="py-2.5 pr-4 text-right text-gray-200 tabular-nums hidden md:table-cell">
                    {displayPrice != null ? `$${displayPrice.toFixed(2)}` : '—'}
                    {lp && <span className="w-1.5 h-1.5 rounded-full bg-green-500 inline-block ml-1.5 mb-0.5" title="Precio live" />}
                  </td>
                  <td className="py-2.5 pr-4 text-right text-gray-400 hidden lg:table-cell">
                    {s.atr_pct != null ? fmtPct(s.atr_pct) : '—'}
                  </td>
                  <td className="py-2.5 text-right text-gray-400 hidden lg:table-cell">
                    {s.from_high_pct != null ? fmtPct(s.from_high_pct) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="text-xs text-gray-600 mt-3 text-right">
          {signals.length} señal{signals.length !== 1 ? 'es' : ''} · precios live c/5s
        </p>
      </div>

      {selected && (
        <>
          <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setSelected(null)} />
          <SignalDetailPanel
            signal={selected}
            onClose={() => setSelected(null)}
            onOrder={onOrder ? (sym) => { onOrder(sym); setSelected(null) } : undefined}
          />
        </>
      )}
    </>
  )
}
