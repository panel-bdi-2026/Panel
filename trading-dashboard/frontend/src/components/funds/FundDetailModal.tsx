import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { Fund } from '../../api/types'
import { addCapitalFlow, setAutoTrading } from '../../api/funds'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { EquityChart } from './EquityChart'
import { fmtUsd, fmtAge } from '../../lib/format'
import { useToastStore } from '../ui/Toast'

interface Props { fund: Fund | null; onClose: () => void }

export function FundDetailModal({ fund, onClose }: Props) {
  const [flowAmount, setFlowAmount] = useState('')
  const [flowNote, setFlowNote] = useState('')
  const [showFlow, setShowFlow] = useState(false)
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

  if (!fund) return null

  const trades = [...fund.trades].sort(
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

      {/* Positions */}
      {Object.keys(fund.positions).length > 0 && (
        <section className="mb-4">
          <h4 className="text-xs font-semibold text-gray-500 uppercase mb-2">Posiciones abiertas</h4>
          <div className="space-y-1">
            {Object.entries(fund.positions).map(([sym, pos]) => (
              <div key={sym} className="flex items-center justify-between text-sm bg-gray-800 rounded px-3 py-2">
                <span className="font-medium text-gray-200">{sym}</span>
                <span className="text-gray-400">{pos.quantity} @ {fmtUsd(pos.avg_cost)}</span>
                {pos.stop_loss_price && (
                  <span className="text-red-400 text-xs">SL {fmtUsd(pos.stop_loss_price)}</span>
                )}
                {pos.opened_at && (
                  <span className="text-gray-500 text-xs">{fmtAge(pos.opened_at)}</span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Recent trades */}
      {trades.length > 0 && (
        <section>
          <h4 className="text-xs font-semibold text-gray-500 uppercase mb-2">
            Trades recientes ({trades.length})
          </h4>
          <div className="max-h-52 overflow-y-auto space-y-1">
            {trades.slice(0, 20).map((t) => (
              <div key={t.id} className="flex items-center justify-between text-xs bg-gray-800 rounded px-3 py-1.5">
                <span className={t.side === 'BUY' ? 'text-green-400' : 'text-red-400'}>{t.side}</span>
                <span className="text-gray-200 font-medium">{t.symbol}</span>
                <span className="text-gray-400">{t.quantity} @ {fmtUsd(t.price)}</span>
                {t.realized_pnl != null && (
                  <span className={t.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                    {fmtUsd(t.realized_pnl)}
                  </span>
                )}
                <span className="text-gray-500">{fmtAge(t.executed_at)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </Modal>
  )
}
