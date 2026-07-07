import { useQuery } from '@tanstack/react-query'
import { fetchAccount } from '../../api/account'
import { fmtUsd } from '../../lib/format'

interface StatProps { label: string; value: string; colorClass?: string }

function Stat({ label, value, colorClass }: StatProps) {
  return (
    <div className="flex flex-col shrink-0">
      <span className="text-xs text-gray-500 whitespace-nowrap">{label}</span>
      <span className={`text-sm font-semibold whitespace-nowrap ${colorClass ?? 'text-gray-100'}`}>{value}</span>
    </div>
  )
}

export function AccountSummaryPanel() {
  const { data } = useQuery({
    queryKey: ['account'],
    queryFn: fetchAccount,
    refetchInterval: 15000,
  })

  if (!data) return null

  const dpnl = data.daily_pnl ?? 0
  const dpnlPct = data.daily_pnl_pct ?? 0

  return (
    <div className="flex items-center gap-5 sm:gap-7 px-4 py-2.5 bg-gray-900 border-b border-gray-800 overflow-x-auto scrollbar-none shrink-0">
      <Stat label="Net Liq" value={fmtUsd(data.net_liquidation)} />
      <div className="w-px h-6 bg-gray-800 shrink-0" />
      <Stat label="Cash" value={fmtUsd(data.cash)} />
      {data.pnl_data_available && (
        <Stat
          label="P&L del día"
          value={`${fmtUsd(dpnl)} (${dpnlPct >= 0 ? '+' : ''}${(dpnlPct * 100).toFixed(2)}%)`}
          colorClass={dpnl >= 0 ? 'text-green-400' : 'text-red-400'}
        />
      )}
      <div className="w-px h-6 bg-gray-800 shrink-0" />
      <Stat label="Buying Power" value={fmtUsd(data.buying_power)} />
    </div>
  )
}
