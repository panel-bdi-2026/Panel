import { useQuery } from '@tanstack/react-query'
import { fetchTradeContext } from '../../api/funds'
import { Modal } from '../ui/Modal'
import { fmtUsd, fmtPct, fmtAge } from '../../lib/format'

interface Props {
  fundId: string
  tradeId: string | null
  onClose: () => void
}

const REASON_LABELS: Record<string, string> = {
  take_profit: 'Take-profit alcanzado',
  max_holding_days: 'Días máximos de tenencia alcanzados',
  trend_break: 'Ruptura de tendencia (precio bajo la SMA rápida)',
  sector_exit: 'Salida por debilidad del sector',
}

const SENTIMENT_LABELS: Record<string, { label: string; className: string }> = {
  positive: { label: 'Positivo', className: 'text-profit' },
  negative: { label: 'Negativo', className: 'text-loss' },
  neutral: { label: 'Neutral', className: 'text-gray-400' },
}

const ACTION_LABELS: Record<string, string> = {
  auto_trade_executed: 'Auto-trading',
  auto_trade_stop_loss_rejected: 'Auto-trading (stop-loss inicial rechazado)',
  auto_trade_fill_late: 'Auto-trading (fill tardío)',
  order_executed: 'Orden manual',
  order_executed_after_approval: 'Borrador aprobado a mano',
  order_fill_late: 'Orden manual (fill tardío)',
  auto_trade_exit: 'Salida automática',
  auto_trade_scale_out: 'Salida parcial por convicción',
  auto_trade_stop_loss_reconciled: 'Stop-loss ejecutado en IBKR',
}

export function TradeContextModal({ fundId, tradeId, onClose }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ['trade-context', fundId, tradeId],
    queryFn: () => fetchTradeContext(fundId, tradeId!),
    enabled: !!tradeId,
  })

  const title = data ? `${data.trade.symbol} · ${data.trade.side === 'BUY' ? 'Compra' : 'Venta'}` : 'Detalle de la operación'

  return (
    <Modal open={!!tradeId} onClose={onClose} title={title} width="max-w-md">
      {isLoading && <p className="text-sm text-gray-500">Cargando…</p>}
      {!isLoading && data && (
        <div className="space-y-3 text-sm">
          <div className="flex items-center justify-between text-xs text-gray-500">
            <span>{data.action ? (ACTION_LABELS[data.action] ?? data.action) : '—'}</span>
            <span>{fmtAge(data.trade.executed_at)}</span>
          </div>

          <div className="grid grid-cols-3 gap-3 bg-gray-800 rounded-lg p-3">
            <div>
              <p className="text-gray-500 text-xs">Cantidad</p>
              <p className="text-gray-200 font-medium nums">{data.trade.quantity}</p>
            </div>
            <div>
              <p className="text-gray-500 text-xs">Precio</p>
              <p className="text-gray-200 font-medium nums">{fmtUsd(data.trade.price)}</p>
            </div>
            {data.trade.realized_pnl != null && (
              <div>
                <p className="text-gray-500 text-xs">P&L</p>
                <p className={`font-medium nums ${data.trade.realized_pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                  {fmtUsd(data.trade.realized_pnl)}
                </p>
              </div>
            )}
          </div>

          {/* Motivo de la venta */}
          {data.reason && (
            <div className="bg-gray-800 rounded-lg p-3">
              <p className="text-xs text-gray-500 uppercase mb-1">Motivo</p>
              <p className="text-gray-200">{REASON_LABELS[data.reason] ?? data.reason}</p>
              {data.r_multiple != null && (
                <p className="text-xs text-gray-500 mt-1">Alcanzó {data.r_multiple}R de ganancia sobre el riesgo inicial.</p>
              )}
              {data.approximate && (
                <p className="text-xs text-gray-500 mt-1">Precio aproximado: IBKR no devolvió el fill exacto del stop.</p>
              )}
            </div>
          )}

          {/* Señal que originó la compra */}
          {data.signal && (
            <>
              <div className="bg-gray-800 rounded-lg p-3">
                <p className="text-xs text-gray-500 uppercase mb-2">Por qué se compró</p>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  <div>
                    <p className="text-gray-500">Estrategia</p>
                    <p className="text-gray-200 font-medium">{data.signal.strategy_id}</p>
                  </div>
                  <div>
                    <p className="text-gray-500">Score</p>
                    <p className="text-gray-200 font-medium nums">{data.signal.score.toFixed(1)}</p>
                  </div>
                  {data.signal.sector && (
                    <div>
                      <p className="text-gray-500">Sector</p>
                      <p className="text-gray-200 font-medium">{data.signal.sector}</p>
                    </div>
                  )}
                  <div>
                    <p className="text-gray-500">Momentum 3m</p>
                    <p className="text-gray-200 font-medium nums">{fmtPct(data.signal.momentum_3m_pct)}</p>
                  </div>
                  <div>
                    <p className="text-gray-500">RSI</p>
                    <p className="text-gray-200 font-medium nums">{data.signal.rsi.toFixed(1)}</p>
                  </div>
                  <div>
                    <p className="text-gray-500">Stop sugerido</p>
                    <p className="text-gray-200 font-medium nums">-{data.signal.suggested_stop_loss_pct.toFixed(1)}%</p>
                  </div>
                </div>
                {data.signal.notes.length > 0 && (
                  <ul className="mt-2 space-y-0.5 text-xs text-gray-400 list-disc list-inside">
                    {data.signal.notes.map((n, i) => <li key={i}>{n}</li>)}
                  </ul>
                )}
              </div>

              {data.signal.news_sentiment && (
                <div className="bg-gray-800 rounded-lg p-3">
                  <p className="text-xs text-gray-500 uppercase mb-1">Sentimiento de noticias</p>
                  <p className={`font-medium ${SENTIMENT_LABELS[data.signal.news_sentiment]?.className ?? 'text-gray-200'}`}>
                    {SENTIMENT_LABELS[data.signal.news_sentiment]?.label ?? data.signal.news_sentiment}
                  </p>
                  {data.signal.news_summary && (
                    <p className="text-xs text-gray-400 mt-1">{data.signal.news_summary}</p>
                  )}
                </div>
              )}
            </>
          )}

          {!data.reason && !data.signal && (
            <p className="text-sm text-gray-500">
              {data.action
                ? `No se guardó más detalle para esta operación (${ACTION_LABELS[data.action] ?? data.action}).`
                : 'No se encontró información adicional — puede ser una operación anterior a esta función.'}
            </p>
          )}
        </div>
      )}
    </Modal>
  )
}
