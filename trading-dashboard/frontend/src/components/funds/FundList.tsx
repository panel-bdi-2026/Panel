import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchFunds } from '../../api/funds'
import { fmtUsd, fmtPct } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { FundDetailModal } from './FundDetailModal'
import type { Fund } from '../../api/types'

function FundCard({ fund, onClick }: { fund: Fund; onClick: () => void }) {
  const pnl = fund.realized_pnl_total
  const capital = fund.net_contributed_capital
  const roi = capital ? (pnl / capital) * 100 : null

  // Market value of open positions (approx: we don't have live prices here)
  const posCount = Object.keys(fund.positions).length

  return (
    <div
      onClick={onClick}
      className="bg-gray-900 border border-gray-800 rounded-xl p-4 cursor-pointer hover:border-gray-600 transition-colors"
    >
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-gray-100">{fund.name}</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            {fund.strategy_id ?? 'Sin estrategia'}
          </p>
        </div>
        <div className="flex gap-2">
          {fund.auto_trading_enabled && <Badge variant="green">AUTO</Badge>}
          {fund.closed && <Badge variant="gray">CERRADO</Badge>}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-xs text-gray-500">Cash disponible</p>
          <p className="font-medium text-gray-200">{fmtUsd(fund.cash_usd)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">Capital aportado</p>
          <p className="font-medium text-gray-200">{fmtUsd(capital)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">P&L realizado</p>
          <p className={`font-medium ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {fmtUsd(pnl)}
          </p>
        </div>
        <div>
          <p className="text-xs text-gray-500">ROI</p>
          <p className={`font-medium ${(roi ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {fmtPct(roi)}
          </p>
        </div>
      </div>

      {posCount > 0 && (
        <p className="text-xs text-gray-500 mt-3">
          {posCount} posición{posCount !== 1 ? 'es' : ''} abiertas
        </p>
      )}
    </div>
  )
}

export function FundList() {
  const { data: funds, isLoading } = useQuery({
    queryKey: ['funds'],
    queryFn: fetchFunds,
    refetchInterval: 15000,
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = funds?.find((f) => f.id === selectedId) ?? null

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando fondos…</div>
  if (!funds?.length) return <div className="text-gray-500 text-sm">Sin fondos. Crea uno desde el panel.</div>

  return (
    <>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {funds.map((f) => (
          <FundCard key={f.id} fund={f} onClick={() => setSelectedId(f.id)} />
        ))}
      </div>
      <FundDetailModal fund={selected} onClose={() => setSelectedId(null)} />
    </>
  )
}
