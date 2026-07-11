import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchAccount } from '../../api/account'
import { fmtUsd, fmtPct } from '../../lib/format'
import { MetricDelta } from '../ui/MetricDelta'
import { Stat } from '../ui/Stat'
import { Skeleton } from '../ui/Skeleton'
import { useFlashOnChange } from '../../hooks/useFlashOnChange'

type View = 'value' | 'performance'

export function AccountSummaryPanel() {
  const { data, isLoading } = useQuery({
    queryKey: ['account'],
    queryFn: fetchAccount,
    refetchInterval: 15000,
  })
  const [view, setView] = useState<View>('value')
  const flash = useFlashOnChange(data?.net_liquidation)

  if (isLoading || !data) {
    return (
      <div className="px-4 py-3 bg-surface-1 border-b border-surface-3 shrink-0">
        <Skeleton className="h-3 w-24 mb-2" />
        <Skeleton className="h-9 w-48" />
      </div>
    )
  }

  const dpnl = data.daily_pnl ?? 0
  const dpnlPct = data.daily_pnl_pct ?? 0
  const hasPnl = data.pnl_data_available

  // Número protagonista con centavos atenuados (estilo IBKR)
  const primary = view === 'value' ? data.net_liquidation : dpnl
  const [intPart, decPart] = fmtUsd(primary).split('.')

  return (
    <div className={`px-4 py-3 bg-surface-1 border-b border-surface-3 shrink-0 ${flash}`}>
      <div className="flex items-start justify-between gap-4 max-w-6xl mx-auto">
        <div className="min-w-0">
          {/* Toggle Valor | Rendimiento */}
          <div className="flex items-center gap-3 mb-1">
            {(['value', 'performance'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`text-xs font-medium transition-colors ${
                  view === v ? 'text-gray-100' : 'text-gray-600 hover:text-gray-400'
                }`}
              >
                {v === 'value' ? 'Valor' : 'Rendimiento'}
              </button>
            ))}
          </div>

          {/* Hero number */}
          <div className="flex items-baseline gap-0.5">
            <span className={`text-3xl sm:text-4xl font-bold nums ${
              view === 'performance' ? (dpnl >= 0 ? 'text-profit' : 'text-loss') : 'text-gray-100'
            }`}>{intPart}</span>
            {decPart && (
              <span className={`text-xl sm:text-2xl font-semibold nums ${
                view === 'performance' ? (dpnl >= 0 ? 'text-profit/70' : 'text-loss/70') : 'text-gray-500'
              }`}>.{decPart}</span>
            )}
          </div>

          {/* Delta del día */}
          {hasPnl && (
            <div className="flex items-center gap-1.5 mt-0.5">
              <MetricDelta value={`${fmtUsd(dpnl)} (${fmtPct(dpnlPct * 100)})`} raw={dpnl} size="md" />
              <span className="text-xs text-gray-500">hoy</span>
            </div>
          )}
        </div>

        {/* Chips secundarios (desktop) */}
        <div className="hidden sm:flex items-center gap-6 shrink-0">
          <Stat label="Cash" value={fmtUsd(data.cash)} align="right" />
          <Stat label="Buying Power" value={fmtUsd(data.buying_power)} align="right" />
        </div>
      </div>

      {/* Chips (móvil) */}
      <div className="sm:hidden flex items-center gap-5 mt-3 overflow-x-auto scrollbar-none">
        <Stat label="Cash" value={fmtUsd(data.cash)} />
        <Stat label="Buying Power" value={fmtUsd(data.buying_power)} />
      </div>
    </div>
  )
}
