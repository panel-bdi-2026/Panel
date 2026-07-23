import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Fund } from '../../api/types'
import { addCapitalFlow, setAutoTrading } from '../../api/funds'
import { fetchPositions } from '../../api/account'
import { fetchCompanyNames } from '../../api/signals'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { EquityChart } from './EquityChart'
import { TradeContextModal } from './TradeContextModal'
import { fmtUsd, fmtPct, fmtAge } from '../../lib/format'
import { useToastStore } from '../ui/Toast'

interface Props { fund: Fund | null; onClose: () => void }

// Una posicion abierta no tiene un solo "trade" -- puede haberse construido
// de varias compras (scaling in). "Por qué se compró" se refiere siempre a
// la compra que ABRIÓ la posición (la única que fija pos.opened_at, ver
// FundPosition en funds.py): se busca el trade BUY de ese símbolo cuyo
// executed_at está más cerca de pos.opened_at.
function findOpeningTradeId(fund: Fund, symbol: string, openedAt: string | null): string | null {
  const buys = fund.trades.filter((t) => t.symbol === symbol && t.side === 'BUY')
  if (!buys.length) return null
  if (!openedAt) return buys[buys.length - 1].id
  const openedMs = new Date(openedAt).getTime()
  return buys.reduce((best, t) => {
    const bestDiff = Math.abs(new Date(best.executed_at).getTime() - openedMs)
    const diff = Math.abs(new Date(t.executed_at).getTime() - openedMs)
    return diff < bestDiff ? t : best
  }, buys[0]).id
}

export function FundDetailModal({ fund, onClose }: Props) {
  const [flowAmount, setFlowAmount] = useState('')
  const [flowNote, setFlowNote] = useState('')
  const [showFlow, setShowFlow] = useState(false)
  const [contextTradeId, setContextTradeId] = useState<string | null>(null)
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const flowMutation = useMutation({
    mutationFn: () => addCapitalFlow(fund!.id, Number(flowAmount), flowNote || undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['funds'] })
      qc.invalidateQueries({ queryKey: ['roi-history'] })
      addToast('Flujo de capital registrado', 'success')
      setFlowAmount(''); setFlowNote(''); setShowFlow(false)
    },
    onError: (e: Error) => addToast(e.message || 'Error', 'error'),
  })

  const autoMutation = useMutation({
    mutationFn: () => setAutoTrading(fund!.id, !fund!.auto_trading_enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['funds'] }),
    onError: () => addToast('Error al cambiar auto-trading', 'error'),
  })

  // Los fondos no traen precio de mercado propio (fund.positions solo tiene
  // cantidad/costo de compra, ver FundPosition): se toma el precio actual
  // por símbolo de /api/positions (posiciones reales de la cuenta de IBKR,
  // ya usada por PositionsTable con la misma queryKey, así que no genera un
  // fetch extra) para calcular valor de mercado y P&L no realizado acá.
  const { data: positions } = useQuery({
    queryKey: ['positions'],
    queryFn: fetchPositions,
    refetchInterval: 10000,
  })
  const priceBySymbol: Record<string, number | null> = {}
  for (const p of positions ?? []) priceBySymbol[p.symbol] = p.market_price

  // Nombre comercial al lado del ticker (ej. "Apple Inc." junto a AAPL) —
  // cache de 24hs en el backend y acá: el nombre de una empresa no cambia.
  const openSymbols = Object.entries(fund?.positions ?? {})
    .filter(([, pos]) => pos.quantity !== 0)
    .map(([sym]) => sym)
    .sort()
  const { data: companyNames } = useQuery({
    queryKey: ['company-names', openSymbols.join(',')],
    queryFn: () => fetchCompanyNames(openSymbols),
    enabled: openSymbols.length > 0,
    staleTime: 24 * 60 * 60 * 1000,
  })

  if (!fund) return null

  const trades = [...(fund.trades ?? [])].sort(
    (a, b) => new Date(b.executed_at).getTime() - new Date(a.executed_at).getTime(),
  )

  return (
    <Modal open={!!fund} onClose={onClose} title={fund.name} width="max-w-2xl">
      {/* Header stats + actions */}
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex gap-4 text-sm">
          <div>
            <p className="text-xs text-gray-500">Cash</p>
            <p className="font-semibold text-gray-200">{fmtUsd(fund.cash_usd)}</p>
          </div>
          <div>
            <p className="text-xs text-gray-500">P&L realizado</p>
            <p className={`font-semibold ${fund.realized_pnl_total >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {fmtUsd(fund.realized_pnl_total)}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <button onClick={() => autoMutation.mutate()} disabled={autoMutation.isPending}>
            <Badge variant={fund.auto_trading_enabled ? 'green' : 'gray'}>
              AUTO {fund.auto_trading_enabled ? 'ON' : 'OFF'}
            </Badge>
          </button>
          <Button variant="secondary" size="sm" onClick={() => setShowFlow((v) => !v)}>
            {showFlow ? 'Cancelar' : '± Capital'}
          </Button>
        </div>
      </div>

      {/* Capital flow form */}
      {showFlow && (
        <div className="bg-gray-800 rounded-lg p-3 mb-4 flex gap-2 items-end flex-wrap">
          <div className="flex-1 min-w-28">
            <label className="block text-xs text-gray-500 mb-1">Monto (+/-)</label>
            <input
              type="number"
              value={flowAmount}
              onChange={(e) => setFlowAmount(e.target.value)}
              placeholder="500 o -200"
              className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none"
            />
          </div>
          <div className="flex-1 min-w-32">
            <label className="block text-xs text-gray-500 mb-1">Nota</label>
            <input
              value={flowNote}
              onChange={(e) => setFlowNote(e.target.value)}
              placeholder="Depósito inicial"
              className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none"
            />
          </div>
          <Button
            variant="primary"
            size="sm"
            onClick={() => flowMutation.mutate()}
            disabled={!flowAmount || flowMutation.isPending}
          >
            Aplicar
          </Button>
        </div>
      )}

      {/* Equity chart */}
      <div className="mb-4">
        <p className="text-xs text-gray-500 uppercase mb-2">Curva de equity</p>
        <EquityChart fundId={fund.id} />
      </div>

      {/* Positions — solo las que tienen acciones; el ledger conserva registros
          en 0 de posiciones ya cerradas que no deben mostrarse como abiertas. */}
      {Object.values(fund.positions).some((p) => p.quantity !== 0) && (
        <section className="mb-4">
          <h4 className="text-xs font-semibold text-gray-500 uppercase mb-2">Posiciones abiertas</h4>
          <div className="space-y-1">
            {Object.entries(fund.positions).filter(([, pos]) => pos.quantity !== 0).map(([sym, pos]) => {
              const costTotal = pos.quantity * pos.avg_cost
              const marketPrice = priceBySymbol[sym] ?? null
              const marketValue = marketPrice !== null ? pos.quantity * marketPrice : null
              const unrealizedPnl = marketPrice !== null ? (marketPrice - pos.avg_cost) * pos.quantity : null
              const unrealizedPnlPct = marketPrice !== null && pos.avg_cost !== 0
                ? ((marketPrice - pos.avg_cost) / pos.avg_cost) * 100
                : null
              const openingTradeId = findOpeningTradeId(fund, sym, pos.opened_at)
              return (
                <div
                  key={sym}
                  className={`bg-gray-800 rounded-lg px-3 py-2.5 ${openingTradeId ? 'cursor-pointer hover:bg-gray-700 transition-colors' : ''}`}
                  role={openingTradeId ? 'button' : undefined}
                  tabIndex={openingTradeId ? 0 : undefined}
                  onClick={openingTradeId ? () => setContextTradeId(openingTradeId) : undefined}
                  onKeyDown={openingTradeId ? (e) => { if (e.key === 'Enter') setContextTradeId(openingTradeId) } : undefined}
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-baseline gap-1.5 min-w-0">
                      <span className="font-semibold text-gray-100 shrink-0">{sym}</span>
                      {companyNames?.[sym] && (
                        <span className="text-gray-500 text-xs truncate">{companyNames[sym]}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {pos.stop_loss_price && (
                        <span className="text-red-400 text-xs font-medium">SL {fmtUsd(pos.stop_loss_price)}</span>
                      )}
                      {pos.opened_at && (
                        <span className="text-gray-500 text-xs">{fmtAge(pos.opened_at)}</span>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 sm:grid-cols-6 gap-x-3 gap-y-1.5 mt-2 text-xs">
                    <div>
                      <p className="text-gray-500">Cantidad</p>
                      <p className="text-gray-200 font-medium nums">{pos.quantity}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Precio compra</p>
                      <p className="text-gray-200 font-medium nums">{fmtUsd(pos.avg_cost)}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Costo total</p>
                      <p className="text-gray-200 font-medium nums">{fmtUsd(costTotal)}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Precio actual</p>
                      <p className="text-gray-200 font-medium nums">{fmtUsd(marketPrice)}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">Valor mercado</p>
                      <p className="text-gray-200 font-medium nums">{fmtUsd(marketValue)}</p>
                    </div>
                    <div>
                      <p className="text-gray-500">P&L no realizado</p>
                      <p className={`font-medium nums ${unrealizedPnl == null ? 'text-gray-200' : unrealizedPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {fmtUsd(unrealizedPnl)}{unrealizedPnlPct != null && <span className="ml-1">({fmtPct(unrealizedPnlPct)})</span>}
                      </p>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      )}

      {/* Recent trades */}
      {trades.length > 0 && (
        <section>
          <h4 className="text-xs font-semibold text-gray-500 uppercase mb-2">
            Trades recientes ({trades.length})
          </h4>
          <div className="max-h-52 overflow-y-auto scrollbar-thin space-y-1">
            {trades.slice(0, 20).map((t) => (
              <div
                key={t.id}
                className="flex items-center gap-2 text-xs bg-gray-800 rounded px-3 py-1.5 cursor-pointer hover:bg-gray-700 transition-colors"
                role="button"
                tabIndex={0}
                onClick={() => setContextTradeId(t.id)}
                onKeyDown={(e) => { if (e.key === 'Enter') setContextTradeId(t.id) }}
              >
                <span className={`font-semibold shrink-0 w-7 ${t.side === 'BUY' ? 'text-green-400' : 'text-red-400'}`}>{t.side}</span>
                <span className="text-gray-200 font-semibold shrink-0">{t.symbol}</span>
                <span className="text-gray-500 hidden sm:inline">{t.quantity} @ {fmtUsd(t.price)}</span>
                {t.realized_pnl != null && (
                  <span className={`ml-auto font-semibold tabular-nums shrink-0 ${t.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {t.realized_pnl >= 0 ? '+' : ''}{fmtUsd(t.realized_pnl)}
                  </span>
                )}
                {t.realized_pnl == null && <span className="ml-auto" />}
                <span className="text-gray-600 shrink-0">{fmtAge(t.executed_at)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <TradeContextModal
        fundId={fund.id}
        tradeId={contextTradeId}
        onClose={() => setContextTradeId(null)}
      />
    </Modal>
  )
}
