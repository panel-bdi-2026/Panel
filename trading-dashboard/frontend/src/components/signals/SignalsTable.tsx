import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchSignals, fetchLivePrices } from '../../api/signals'
import type { Signal } from '../../api/types'

const STRATEGIES = [
  { key: 'opportunistic' as const, label: 'Opor.' },
  { key: 'momentum'      as const, label: 'Mom.' },
  { key: 'long_term'     as const, label: 'LT'   },
  { key: 'dividend'      as const, label: 'Div.' },
]

function MiniBar({ value }: { value: number }) {
  const color =
    value >= 75 ? 'bg-green-500' :
    value >= 50 ? 'bg-blue-500' :
    value >= 30 ? 'bg-yellow-500' : 'bg-gray-700'
  return (
    <div className="flex items-center gap-1.5 min-w-0">
      <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div className={`h-full ${color} rounded-full`} style={{ width: `${value}%` }} />
      </div>
      <span className="text-[10px] tabular-nums text-gray-500 w-7 text-right">{value.toFixed(0)}</span>
    </div>
  )
}

interface Props {
  onOrder?: (symbol: string) => void
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

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 border-b border-gray-800">
              <th className="text-left pb-2.5 pr-4">Símbolo</th>
              <th className="text-right pb-2.5 pr-4 hidden md:table-cell">Precio</th>
              {STRATEGIES.map((s) => (
                <th key={s.key} className="pb-2.5 pr-3 text-left w-24 hidden sm:table-cell">
                  {s.label}
                </th>
              ))}
              <th className="pb-2.5 text-left w-24 sm:hidden">Score</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {signals.map((s) => {
              const lp = livePrices?.[s.symbol]
              const isActive = selected?.symbol === s.symbol
              const bestScore = Math.max(...Object.values(s.scores))
              return (
                <tr
                  key={s.symbol}
                  onClick={() => setSelected(isActive ? null : s)}
                  className={`cursor-pointer transition-colors ${
                    isActive ? 'bg-brand-950/40' : 'hover:bg-gray-900/60'
                  }`}
                >
                  <td className="py-2.5 pr-4">
                    <div className="flex flex-col">
                      <span className="font-semibold text-gray-100">{s.symbol}</span>
                      {s.sector && (
                        <span className="text-gray-600 text-xs hidden sm:block">{s.sector}</span>
                      )}
                    </div>
                  </td>
                  <td className="py-2.5 pr-4 text-right text-gray-300 tabular-nums hidden md:table-cell">
                    {lp
                      ? <span className="flex items-center justify-end gap-1">
                          ${lp.last_price.toFixed(2)}
                          <span className="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0" title="live" />
                        </span>
                      : <span className="text-gray-600">—</span>
                    }
                  </td>
                  {/* Desktop: individual strategy bars */}
                  {STRATEGIES.map((st) => (
                    <td key={st.key} className="py-2.5 pr-3 hidden sm:table-cell">
                      <MiniBar value={s.scores[st.key]} />
                    </td>
                  ))}
                  {/* Mobile: best score only */}
                  <td className="py-2.5 sm:hidden">
                    <MiniBar value={bestScore} />
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

      {/* Detail panel */}
      {selected && (
        <>
          <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setSelected(null)} />
          <div className="fixed inset-y-0 right-0 z-40 w-80 bg-gray-900 border-l border-gray-800 shadow-2xl flex flex-col">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800 shrink-0">
              <div>
                <p className="font-bold text-gray-100 text-lg">{selected.symbol}</p>
                {selected.sector && <p className="text-xs text-gray-500">{selected.sector}</p>}
              </div>
              <div className="flex items-center gap-2">
                {onOrder && (
                  <button
                    onClick={() => { onOrder(selected.symbol); setSelected(null) }}
                    className="px-3 py-1.5 bg-brand-600 hover:bg-brand-500 text-white text-xs font-semibold rounded-lg transition-colors"
                  >
                    + Orden
                  </button>
                )}
                <button
                  onClick={() => setSelected(null)}
                  className="w-7 h-7 flex items-center justify-center rounded-full text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors"
                >
                  ×
                </button>
              </div>
            </div>
            {/* Scores */}
            <div className="px-4 py-4 space-y-4 overflow-y-auto flex-1">
              <p className="text-xs font-semibold text-gray-500 uppercase">Scores por estrategia</p>
              {STRATEGIES.map((st) => (
                <div key={st.key}>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-gray-400 capitalize">{st.key.replace('_', ' ')}</span>
                    <span className={`font-semibold ${
                      selected.scores[st.key] >= 75 ? 'text-green-400' :
                      selected.scores[st.key] >= 50 ? 'text-blue-400' :
                      selected.scores[st.key] >= 30 ? 'text-yellow-400' : 'text-gray-500'
                    }`}>{selected.scores[st.key].toFixed(1)}</span>
                  </div>
                  <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${
                        selected.scores[st.key] >= 75 ? 'bg-green-500' :
                        selected.scores[st.key] >= 50 ? 'bg-blue-500' :
                        selected.scores[st.key] >= 30 ? 'bg-yellow-500' : 'bg-gray-700'
                      }`}
                      style={{ width: `${selected.scores[st.key]}%` }}
                    />
                  </div>
                </div>
              ))}
              {livePrices?.[selected.symbol] && (
                <div className="pt-2 border-t border-gray-800">
                  <p className="text-xs font-semibold text-gray-500 uppercase mb-2">Precio live</p>
                  <p className="text-xl font-bold text-gray-100">
                    ${livePrices[selected.symbol].last_price.toFixed(2)}
                  </p>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}
