import { useQuery } from '@tanstack/react-query'
import { fetchAccount } from '../../api/account'
import { fmtUsd } from '../../lib/format'

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-sm font-semibold text-gray-100">{value}</span>
      {sub && <span className="text-xs text-gray-500">{sub}</span>}
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

  const pnlColor = (data.unrealized_pnl ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'

  return (
    <div className="flex items-center gap-4 sm:gap-6 px-4 py-2 bg-gray-900/60 border-b border-gray-800 text-sm overflow-x-auto scrollbar-none shrink-0">
      <Stat label="Net Liq" value={fmtUsd(data.net_liquidation)} />
      <Stat label="Cash" value={fmtUsd(data.total_cash)} />
      <Stat
        label="Unrealized P&L"
        value={<span className={pnlColor}>{fmtUsd(data.unrealized_pnl)}</span> as unknown as string}
      />
      <Stat label="Realized P&L" value={fmtUsd(data.realized_pnl)} />
      <Stat label="Buying Power" value={fmtUsd(data.buying_power)} />
    </div>
  )
}
