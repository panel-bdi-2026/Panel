import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchFunds } from '../../api/funds'
import { fmtUsd, fmtPct } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { Card } from '../ui/Card'
import { Skeleton } from '../ui/Skeleton'
import { EmptyState } from '../ui/EmptyState'
import { Sparkline } from '../ui/Sparkline'
import { MetricDelta } from '../ui/MetricDelta'
import { Toggle } from '../ui/Toggle'
import { FundDetailModal } from './FundDetailModal'
import type { Fund } from '../../api/types'

// Preferencia persistida: por defecto los fondos cerrados quedan ocultos
// (son de solo lectura y con el tiempo se acumulan), pero el usuario puede
// volver a mostrarlos sin perder la eleccion entre recargas de pagina.
const HIDE_CLOSED_KEY = 'funds.hideClosed'

function getHideClosedDefault(): boolean {
  const stored = localStorage.getItem(HIDE_CLOSED_KEY)
  return stored === null ? true : stored === 'true'
}

// Serie de P&L realizado acumulado a partir de los trades (dato real del fondo)
function cumulativePnl(fund: Fund): number[] {
  const withPnl = fund.trades
    .filter((t) => t.realized_pnl != null)
    .sort((a, b) => a.executed_at.localeCompare(b.executed_at))
  if (!withPnl.length) return []
  let acc = 0
  const series = [0]
  for (const t of withPnl) { acc += t.realized_pnl ?? 0; series.push(acc) }
  return series
}

function FundCard({ fund, onClick }: { fund: Fund; onClick: () => void }) {
  const pnl = fund.realized_pnl_total
  const capital = fund.net_contributed_capital
  const roi = capital ? (pnl / capital) * 100 : null
  // El ledger conserva un registro por cada símbolo que el fondo alguna vez
  // tuvo, incluso tras cerrarlo (quantity=0). Contar solo las que tienen
  // acciones — si no, muestra posiciones "abiertas" que en realidad están en 0.
  const posCount = Object.values(fund.positions).filter((p) => p.quantity !== 0).length
  const series = cumulativePnl(fund)

  return (
    <Card interactive onClick={onClick}>
      <div className="flex items-start justify-between mb-3 gap-2">
        <div className="min-w-0">
          <h3 className="font-semibold text-gray-100 truncate">{fund.name}</h3>
          <p className="text-xs text-gray-500 mt-0.5">{fund.strategy_id ?? 'Sin estrategia'}</p>
        </div>
        <div className="flex gap-1.5 shrink-0">
          {fund.auto_trading_enabled && <Badge variant="green">AUTO</Badge>}
          {fund.closed && <Badge variant="gray">CERRADO</Badge>}
        </div>
      </div>

      {/* P&L destacado + sparkline */}
      <div className="flex items-end justify-between mb-3">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-gray-500">P&L realizado</p>
          <div className="flex items-baseline gap-2">
            <span className={`text-lg font-bold nums ${pnl >= 0 ? 'text-profit' : 'text-loss'}`}>{fmtUsd(pnl)}</span>
            {roi != null && <MetricDelta value={fmtPct(roi)} raw={roi} size="sm" />}
          </div>
        </div>
        {series.length >= 2 && <Sparkline data={series} width={80} height={30} />}
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm border-t border-surface-3 pt-3">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-gray-500">Cash disponible</p>
          <p className="font-medium text-gray-200 nums">{fmtUsd(fund.cash_usd)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-gray-500">Capital aportado</p>
          <p className="font-medium text-gray-200 nums">{fmtUsd(capital)}</p>
        </div>
      </div>

      {posCount > 0 && (
        <p className="text-xs text-gray-500 mt-3">
          {posCount} posición{posCount !== 1 ? 'es' : ''} abierta{posCount !== 1 ? 's' : ''}
        </p>
      )}
    </Card>
  )
}

export function FundList() {
  const { data: funds, isLoading } = useQuery({
    queryKey: ['funds'],
    queryFn: fetchFunds,
    refetchInterval: 15000,
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [hideClosed, setHideClosed] = useState(getHideClosedDefault)
  const selected = funds?.find((f) => f.id === selectedId) ?? null

  if (isLoading) return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      {[...Array(3)].map((_, i) => <Skeleton key={i} className="h-44 rounded-xl2" />)}
    </div>
  )
  if (!funds?.length) return <EmptyState icon="💼" title="Sin fondos" subtitle="Creá un fondo para empezar a administrar capital con su propia estrategia y auto-trading." />

  const closedCount = funds.filter((f) => f.closed).length
  const visibleFunds = hideClosed ? funds.filter((f) => !f.closed) : funds

  function toggleHideClosed(v: boolean) {
    setHideClosed(v)
    localStorage.setItem(HIDE_CLOSED_KEY, String(v))
  }

  return (
    <>
      {closedCount > 0 && (
        <div className="flex items-center justify-end gap-2 mb-3">
          <span className="text-xs text-gray-500">
            Ocultar cerrados{closedCount > 0 ? ` (${closedCount})` : ''}
          </span>
          <Toggle checked={hideClosed} onChange={toggleHideClosed} />
        </div>
      )}
      {visibleFunds.length ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {visibleFunds.map((f) => (
            <FundCard key={f.id} fund={f} onClick={() => setSelectedId(f.id)} />
          ))}
        </div>
      ) : (
        <EmptyState icon="💼" title="Todos los fondos están cerrados" subtitle="Desactivá 'Ocultar cerrados' para verlos, o creá uno nuevo." />
      )}
      <FundDetailModal fund={selected} onClose={() => setSelectedId(null)} />
    </>
  )
}
