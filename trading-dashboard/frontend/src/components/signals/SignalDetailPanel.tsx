import type { Signal } from '../../api/types'
import { useRealtimeStore } from '../../store/realtime'
import { fmtUsd, fmtPct } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { ScoreBar } from './ScoreBar'

interface Props {
  signal: Signal
  onClose: () => void
  onOrder?: (symbol: string) => void
}

function Gate({ label, ok }: { label: string; ok: boolean | null }) {
  if (ok === null) return null
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full ${ok ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
      {ok ? '✓' : '✗'} {label}
    </span>
  )
}

function Row({ label, value }: { label: string; value: string | null }) {
  if (!value) return null
  return (
    <div className="flex justify-between text-sm py-1 border-b border-gray-800">
      <span className="text-gray-500">{label}</span>
      <span className="text-gray-200">{value}</span>
    </div>
  )
}

export function SignalDetailPanel({ signal: s, onClose, onOrder }: Props) {
  const prices = useRealtimeStore((state) => state.prices)
  const livePrice = prices[s.symbol]?.price ?? s.price

  const scoreComponents = Object.entries(s.score_components ?? {})

  return (
    <div className="fixed inset-y-0 right-0 w-80 bg-gray-900 border-l border-gray-800 flex flex-col shadow-2xl z-40">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <div>
          <h2 className="font-bold text-gray-100 text-lg">{s.symbol}</h2>
          <div className="flex items-center gap-2 mt-0.5">
            <Badge variant="blue">{s.strategy_id}</Badge>
            {s.sector && <span className="text-xs text-gray-500">{s.sector}</span>}
          </div>
        </div>
        <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none">×</button>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {/* Price + score */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-2xl font-bold text-gray-100">
              {livePrice != null ? fmtUsd(livePrice) : '—'}
            </p>
            {s.momentum_1m_pct != null && (
              <p className={`text-xs mt-0.5 ${s.momentum_1m_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {fmtPct(s.momentum_1m_pct)} (1m)
              </p>
            )}
          </div>
          <div className="text-right">
            <p className="text-xs text-gray-500 mb-1">Score</p>
            <p className="text-2xl font-bold text-blue-400">{s.score.toFixed(2)}</p>
          </div>
        </div>

        {/* Gates */}
        <div className="flex flex-wrap gap-1.5">
          <Gate label="Liquidez" ok={s.liquidity_ok ?? null} />
          <Gate label="Earnings" ok={s.earnings_ok ?? null} />
          <Gate label="Régimen" ok={s.regime_ok ?? null} />
          <Gate label="Near high" ok={s.near_high_ok ?? null} />
          <Gate label="Trend" ok={s.trend_ok ?? null} />
        </div>

        {/* Score components */}
        {scoreComponents.length > 0 && (
          <div>
            <p className="text-xs text-gray-500 uppercase mb-2">Componentes del score</p>
            <div className="space-y-2">
              {scoreComponents.map(([key, val]) => (
                <div key={key}>
                  <div className="flex justify-between text-xs text-gray-400 mb-0.5">
                    <span>{key.replace(/_/g, ' ')}</span>
                  </div>
                  <ScoreBar score={val} max={Math.max(...scoreComponents.map(([, v]) => v), 1)} />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Technical */}
        <div>
          <p className="text-xs text-gray-500 uppercase mb-1">Técnico</p>
          <Row label="RSI" value={s.rsi != null ? s.rsi.toFixed(1) : null} />
          <Row label="ATR%" value={s.atr_pct != null ? fmtPct(s.atr_pct) : null} />
          <Row label="Desde máx 52s" value={s.pct_from_52w_high != null ? fmtPct(s.pct_from_52w_high) : null} />
          <Row label="Momentum 3m" value={s.momentum_3m_pct != null ? fmtPct(s.momentum_3m_pct) : null} />
          <Row label="MACD hist%" value={s.macd_histogram_pct != null ? fmtPct(s.macd_histogram_pct) : null} />
          <Row label="Bollinger %B" value={s.bollinger_pct_b != null ? s.bollinger_pct_b.toFixed(2) : null} />
          <Row label="Sector RS" value={s.sector_relative_strength_pct != null ? fmtPct(s.sector_relative_strength_pct) : null} />
        </div>

        {/* Stop suggestion */}
        {s.suggested_stop_loss_price != null && (
          <div className="bg-gray-800 rounded-lg px-3 py-2">
            <p className="text-xs text-gray-500 mb-1">Stop sugerido</p>
            <div className="flex justify-between text-sm">
              <span className="text-red-400 font-semibold">{fmtUsd(s.suggested_stop_loss_price)}</span>
              {s.suggested_stop_loss_pct != null && (
                <span className="text-gray-400">{fmtPct(Math.abs(s.suggested_stop_loss_pct))} bajo entrada</span>
              )}
            </div>
          </div>
        )}

        {/* Fundamentals (if any) */}
        {(s.pe_ratio != null || s.dividend_yield_pct != null || s.beta != null) && (
          <div>
            <p className="text-xs text-gray-500 uppercase mb-1">Fundamentales</p>
            <Row label="P/E" value={s.pe_ratio?.toFixed(1) ?? null} />
            <Row label="P/B" value={s.price_to_book?.toFixed(2) ?? null} />
            <Row label="Div yield" value={s.dividend_yield_pct != null ? fmtPct(s.dividend_yield_pct) : null} />
            <Row label="Beta" value={s.beta?.toFixed(2) ?? null} />
            <Row label="Analista" value={s.analyst_recommendation} />
          </div>
        )}

        {/* News sentiment */}
        {s.news_sentiment && (
          <div className="bg-gray-800 rounded-lg px-3 py-2">
            <div className="flex items-center gap-2 mb-1">
              <p className="text-xs text-gray-500">Sentimiento</p>
              <Badge variant={s.news_sentiment === 'positive' ? 'green' : s.news_sentiment === 'negative' ? 'red' : 'gray'}>
                {s.news_sentiment}
              </Badge>
            </div>
            {s.news_summary && <p className="text-xs text-gray-400">{s.news_summary}</p>}
          </div>
        )}

        {/* Notes */}
        {s.notes && s.notes.length > 0 && (
          <div>
            <p className="text-xs text-gray-500 uppercase mb-1">Notas</p>
            <ul className="space-y-0.5">
              {s.notes.map((n, i) => (
                <li key={i} className="text-xs text-gray-400">• {n}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Footer */}
      {onOrder && (
        <div className="px-4 py-3 border-t border-gray-800">
          <button
            onClick={() => onOrder(s.symbol)}
            className="w-full bg-brand-600 hover:bg-brand-500 text-white font-semibold rounded-lg py-2.5 text-sm transition-colors"
          >
            + Crear orden para {s.symbol}
          </button>
        </div>
      )}
    </div>
  )
}
