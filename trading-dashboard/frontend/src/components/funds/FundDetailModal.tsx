import type { Fund } from '../../api/types'
import { Modal } from '../ui/Modal'
import { fmtUsd, fmtAge } from '../../lib/format'

interface Props { fund: Fund | null; onClose: () => void }

export function FundDetailModal({ fund, onClose }: Props) {
  if (!fund) return null

  const trades = [...fund.trades].sort(
    (a, b) => new Date(b.executed_at).getTime() - new Date(a.executed_at).getTime(),
  )

  return (
    <Modal open={!!fund} onClose={onClose} title={fund.name} width="max-w-2xl">
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
          <div className="max-h-64 overflow-y-auto space-y-1">
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
