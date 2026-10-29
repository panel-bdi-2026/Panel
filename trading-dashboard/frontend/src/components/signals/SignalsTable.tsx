import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchSignals, fetchLivePrices } from '../../api/signals'
import { fetchStatus } from '../../api/account'
import { Skeleton } from '../ui/Skeleton'
import { EmptyState } from '../ui/EmptyState'
import type { Signal } from '../../api/types'

const STRATEGIES = [
  { key: 'opportunistic' as const, label: 'Oportunista', short: 'Opor.' },
  { key: 'momentum'      as const, label: 'Momentum',    short: 'Mom.'  },
  { key: 'long_term'     as const, label: 'Largo plazo', short: 'LT'    },
  { key: 'dividend'      as const, label: 'Dividendo',   short: 'Div.'  },
]
type StrategyKey = typeof STRATEGIES[number]['key']

function MiniBar({ value, emphasized = false }: { value: number | null; emphasized?: boolean }) {
  const v = value ?? 0
  const color =
    v >= 75 ? 'bg-green-500' :
    v >= 50 ? 'bg-blue-500' :
    v >= 30 ? 'bg-yellow-500' : 'bg-gray-700'
  return (
    <div className="flex items-center gap-1.5 min-w-0">
      <div className={`flex-1 bg-gray-800 rounded-full overflow-hidden ${emphasized ? 'h-2' : 'h-1.5'}`}>
        <div className={`h-full ${color} rounded-full`} style={{ width: `${v}%` }} />
      </div>
      <span className={`text-[10px] tabular-nums text-gray-500 w-7 text-right ${emphasized ? 'font-semibold text-gray-300' : ''}`}>
        {v.toFixed(0)}
      </span>
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
  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 5000,
  })

  const symbolList = signals?.map((s) => s.symbol) ?? []

  const { data: livePrices } = useQuery({
    queryKey: ['live-prices', symbolList],
    queryFn: () => fetchLivePrices(symbolList),
    enabled: symbolList.length > 0,
    refetchInterval: 5000,
  })

  const [selected, setSelected] = useState<Signal | null>(null)
  const [strategy, setStrategy] = useState<StrategyKey | 'all'>('all')

  const sorted = useMemo(() => {
    if (!signals) return []
    if (strategy === 'all') {
      return [...signals].sort((a, b) => Math.max(...Object.values(b.scores)) - Math.max(...Object.values(a.scores)))
    }
    return [...signals].sort((a, b) => (b.scores[strategy] ?? 0) - (a.scores[strategy] ?? 0))
  }, [signals, strategy])

  if (isLoading) {
    return <Skeleton className="h-10 rounded-lg" count={5} />
  }

  if (!signals?.length) {
    return <EmptyState icon="📡" title="Sin señales activas" subtitle="El escáner no encontró candidatos con el filtro actual" />
  }

  const liveCount = status?.live_hot_count ?? 0
  const liveCap = status?.live_hot_cap ?? 0

  return (
    <>
      {/* Selector de estrategia */}
      <div className="flex items-center gap-1.5 mb-3 overflow-x-auto scrollbar-none">
        <button
          onClick={() => setStrategy('all')}
          className={`px-3 py-1.5 rounded-full text-xs font-medium shrink-0 transition-colors border ${
            strategy === 'all'
              ? 'bg-brand-600 border-brand-600 text-white'
              : 'border-surface-3 text-gray-400 hover:bg-surface-2'
          }`}
        >Todas</button>
        {STRATEGIES.map((s) => (
          <button
            key={s.key}
            onClick={() => setStrategy(s.key)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium shrink-0 transition-colors border ${
              strategy === s.key
                ? 'bg-brand-600 border-brand-600 text-white'
                : 'border-surface-3 text-gray-400 hover:bg-surface-2'
            }`}
          >{s.label}</button>
        ))}
      </div>

      {/* Contexto de precios live */}
      <div className="flex items-center gap-1.5 mb-2 text-[11px] text-gray-500">
        <span className="w-1.5 h-1.5 rounded-full bg-green-500 shrink-0" />
        <span>{liveCount}/{liveCap} símbolos con streaming en vivo (los de mayor score) — el resto muestra el precio del último scan</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 border-b border-surface-3">
              <th className="text-left pb-2.5 pr-4">Símbolo</th>
              <th className="text-right pb-2.5 pr-4 hidden md:table-cell">Precio</th>
              {strategy === 'all' ? (
                STRATEGIES.map((s) => (
                  <th key={s.key} className="pb-2.5 pr-3 text-left w-24 hidden sm:table-cell">{s.short}</th>
                ))
              ) : (
                <th className="pb-2.5 pr-3 text-left w-32 hidden sm:table-cell">
                  {STRATEGIES.find((s) => s.key === strategy)?.label}
                </th>
              )}
              <th className="pb-2.5 text-left w-24 sm:hidden">Score</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {sorted.map((s) => {
              const lp = livePrices?.[s.symbol]
              const isLive = !!lp
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
                    {isLive
                      ? <span className="flex items-center justify-end gap-1">
                          ${lp.last_price.toFixed(2)}
                          <span className="w-2 h-2 rounded-full bg-green-500 shrink-0 animate-pulse" title="Precio en vivo (streaming IBKR)" />
                        </span>
                      : <span className="text-gray-600" title="Sin streaming en vivo — fuera del top con datos en tiempo real">—</span>
                    }
                  </td>
                  {strategy === 'all' ? (
                    STRATEGIES.map((st) => (
                      <td key={st.key} className="py-2.5 pr-3 hidden sm:table-cell">
                        <MiniBar value={s.scores[st.key]} />
                      </td>
                    ))
                  ) : (
                    <td className="py-2.5 pr-3 hidden sm:table-cell">
                      <MiniBar value={s.scores[strategy]} emphasized />
                    </td>
                  )}
                  <td className="py-2.5 sm:hidden">
                    <MiniBar value={strategy === 'all' ? bestScore : s.scores[strategy]} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="text-xs text-gray-600 mt-3 text-right">
          {signals.length} señal{signals.length !== 1 ? 'es' : ''}
        </p>
      </div>

      {/* Detail panel */}
      {selected && (
        <>
          <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setSelected(null)} />
          <div className="fixed inset-y-0 right-0 z-40 w-full max-w-xs sm:max-w-none sm:w-80 bg-gray-900 border-l border-gray-800 shadow-2xl flex flex-col">
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
              {STRATEGIES.map((st) => {
                const sc = selected.scores[st.key] ?? 0
                return (
                  <div key={st.key}>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-gray-400">{st.label}</span>
                      <span className={`font-semibold ${
                        sc >= 75 ? 'text-green-400' :
                        sc >= 50 ? 'text-blue-400' :
                        sc >= 30 ? 'text-yellow-400' : 'text-gray-500'
                      }`}>{sc.toFixed(1)}</span>
                    </div>
                    <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${
                          sc >= 75 ? 'bg-green-500' :
                          sc >= 50 ? 'bg-blue-500' :
                          sc >= 30 ? 'bg-yellow-500' : 'bg-gray-700'
                        }`}
                        style={{ width: `${sc}%` }}
                      />
                    </div>
                  </div>
                )
              })}
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
