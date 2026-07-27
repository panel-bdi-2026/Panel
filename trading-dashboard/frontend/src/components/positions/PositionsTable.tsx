import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchPositions } from '../../api/account'
import { fmtUsd, fmtPct } from '../../lib/format'
import { Skeleton } from '../ui/Skeleton'
import { EmptyState } from '../ui/EmptyState'
import { useFlashOnChange } from '../../hooks/useFlashOnChange'
import { ArrowUp, ArrowDown, Info } from 'lucide-react'

const LONG_HELP  = 'LONG: tenés la acción comprada — ganás si el precio sube.'
const SHORT_HELP = 'SHORT: vendida en corto (pedida prestada) — ganás si el precio baja.'

interface Row {
  symbol: string
  quantity: number
  avg_cost: number
  live: number | null
  liveValue: number
  livePnl: number
  livePnlPct: number
  weight: number
}

type SortKey = 'symbol' | 'live' | 'quantity' | 'liveValue' | 'livePnl'

const pnlClass = (v: number | null) =>
  v == null ? 'text-gray-500' : v >= 0 ? 'text-profit' : 'text-loss'

function LivePrice({ value }: { value: number | null }) {
  const flash = useFlashOnChange(value)
  return (
    <span className={`nums rounded px-1 ${flash}`}>{value !== null ? fmtUsd(value) : '—'}</span>
  )
}

function MobileCard({ r }: { r: Row }) {
  const flash = useFlashOnChange(r.live)
  const up = r.livePnl >= 0
  return (
    <div className={`rounded-xl2 px-4 py-3 space-y-2 border ${
      up ? 'bg-profit/[0.06] border-profit/20' : 'bg-loss/[0.06] border-loss/20'
    }`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-bold text-gray-100 text-base">{r.symbol}</span>
          <span
            title={r.quantity >= 0 ? LONG_HELP : SHORT_HELP}
            className={`text-[10px] font-semibold px-1.5 py-0.5 rounded ${
              r.quantity >= 0 ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss'
            }`}
          >{r.quantity >= 0 ? 'LONG' : 'SHORT'}</span>
        </div>
        <span className={`font-semibold text-sm nums ${pnlClass(r.livePnl)}`}>
          {fmtUsd(r.livePnl)} <span className="text-xs">({fmtPct(r.livePnlPct)})</span>
        </span>
      </div>
      <div className="grid grid-cols-4 gap-2 text-xs">
        <div><p className="text-gray-500">Qty</p><p className="text-gray-300 nums">{Math.abs(r.quantity)}</p></div>
        <div><p className="text-gray-500">Costo</p><p className="text-gray-300 nums">{fmtUsd(r.avg_cost)}</p></div>
        <div><p className="text-gray-500">Live</p><p className={`text-gray-300 nums rounded ${flash}`}>{r.live !== null ? fmtUsd(r.live) : '—'}</p></div>
        <div><p className="text-gray-500">% cart.</p><p className="text-gray-300 nums">{r.weight.toFixed(1)}%</p></div>
      </div>
      <div className="flex items-center justify-between text-xs border-t border-surface-3 pt-2">
        <span className="text-gray-500">Valor mercado</span>
        <span className="text-gray-300 nums">{fmtUsd(r.liveValue)}</span>
      </div>
    </div>
  )
}

export function PositionsTable() {
  const { data: positions, isLoading } = useQuery({
    queryKey: ['positions'],
    queryFn: fetchPositions,
    refetchInterval: 10000,
  })
  const [sortKey, setSortKey] = useState<SortKey>('liveValue')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  if (isLoading) return <Skeleton className="h-16 rounded-xl2" count={4} />
  if (!positions?.length) return <EmptyState icon="📊" title="Sin posiciones abiertas" subtitle="Cuando el sistema o vos abran una posición, aparecerá acá con su P&L en vivo." />

  const rows: Row[] = positions.map((p) => {
    const live = p.market_price ?? null
    const liveValue = live !== null ? live * p.quantity : NaN
    const livePnl = live !== null ? (live - p.avg_cost) * p.quantity : (p.unrealized_pnl ?? NaN)
    const livePnlPct = live !== null && p.avg_cost !== 0 ? ((live - p.avg_cost) / p.avg_cost) * 100 : NaN
    return { symbol: p.symbol, quantity: p.quantity, avg_cost: p.avg_cost, live, liveValue, livePnl, livePnlPct, weight: 0 }
  })
  const totalValue = rows.reduce((a, r) => a + Math.abs(r.liveValue), 0) || 1
  rows.forEach((r) => { r.weight = (Math.abs(r.liveValue) / totalValue) * 100 })

  const sorted = [...rows].sort((a, b) => {
    const dir = sortDir === 'asc' ? 1 : -1
    if (sortKey === 'symbol') return a.symbol.localeCompare(b.symbol) * dir
    return ((a[sortKey] ?? 0) - (b[sortKey] ?? 0)) * dir
  })

  const toggleSort = (k: SortKey) => {
    if (k === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(k); setSortDir('desc') }
  }

  const Th = ({ k, children, className = '' }: { k: SortKey; children: React.ReactNode; className?: string }) => (
    <th className={`pb-2 pr-4 cursor-pointer select-none ${className}`} onClick={() => toggleSort(k)}>
      <span className="inline-flex items-center gap-1">
        {children}
        {sortKey === k && (sortDir === 'asc' ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
      </span>
    </th>
  )

  return (
    <>
      {/* Leyenda LONG/SHORT — significado no es obvio para todos los usuarios */}
      <div className="flex items-center gap-1.5 text-[11px] text-gray-500 mb-2">
        <Info size={12} className="shrink-0" />
        <span><span className="text-profit font-medium">LONG</span> = comprada, gana si sube ·{' '}
          <span className="text-loss font-medium">SHORT</span> = vendida en corto, gana si baja</span>
      </div>

      {/* Móvil: tarjetas tintadas */}
      <div className="sm:hidden space-y-2">
        {sorted.map((r) => <MobileCard key={r.symbol} r={r} />)}
      </div>

      {/* Desktop: tabla ordenable */}
      <div className="hidden sm:block overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase border-b border-surface-3">
              <Th k="symbol" className="text-left">Instrumento</Th>
              <Th k="quantity" className="text-right">Pos</Th>
              <th className="text-right pb-2 pr-4">Costo avg.</th>
              <Th k="live" className="text-right">Precio</Th>
              <Th k="liveValue" className="text-right">Valor</Th>
              <Th k="livePnl" className="text-right">P&L</Th>
              <th className="text-right pb-2">%</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-3">
            {sorted.map((r) => (
              <tr key={r.symbol} className="hover:bg-surface-2/50">
                <td className="py-2 pr-4">
                  <span className="font-semibold text-gray-100">{r.symbol}</span>
                  <span
                    title={r.quantity >= 0 ? LONG_HELP : SHORT_HELP}
                    className={`ml-2 text-[10px] font-semibold ${r.quantity >= 0 ? 'text-profit' : 'text-loss'}`}
                  >
                    {r.quantity >= 0 ? 'LONG' : 'SHORT'}
                  </span>
                </td>
                <td className="py-2 pr-4 text-right text-gray-300 nums">{r.quantity}</td>
                <td className="py-2 pr-4 text-right text-gray-400 nums">{fmtUsd(r.avg_cost)}</td>
                <td className="py-2 pr-4 text-right text-gray-300"><LivePrice value={r.live} /></td>
                <td className="py-2 pr-4 text-right text-gray-300 nums">{fmtUsd(r.liveValue)}</td>
                <td className={`py-2 pr-4 text-right nums ${pnlClass(r.livePnl)}`}>
                  {fmtUsd(r.livePnl)} <span className="text-xs">({fmtPct(r.livePnlPct)})</span>
                </td>
                <td className="py-2 text-right text-gray-500 nums">{r.weight.toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
