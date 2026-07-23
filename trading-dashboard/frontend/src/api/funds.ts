import { apiFetch } from './client'
import type { Fund } from './types'

export const fetchFunds = () => apiFetch<Fund[]>('/api/funds')

export const fetchFund = (id: string) => apiFetch<Fund>(`/api/funds/${id}`)

export const createFund = (body: {
  name: string
  initial_capital_usd: number
  auto_trading_enabled?: boolean
  strategy_id?: string | null
}) => apiFetch<Fund>('/api/funds', { method: 'POST', body: JSON.stringify(body) })

export const closeFund = (id: string) =>
  apiFetch<Fund>(`/api/funds/${id}/close`, { method: 'POST' })

export const addCapitalFlow = (id: string, amount: number, note?: string) =>
  apiFetch<Fund>(`/api/funds/${id}/capital-flows`, {
    method: 'POST',
    body: JSON.stringify({ amount, note }),
  })

export const setAutoTrading = (id: string, enabled: boolean) =>
  apiFetch<Fund>(`/api/funds/${id}/auto-trading`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })

export interface PerFundHistory {
  id: string
  name: string
  closed: boolean
  dates: string[]
  cumulative_return_pct: number[]
  benchmark_cumulative_return_pct: number[]
}

export interface RoiHistory {
  dates: string[]
  fund_cumulative_return_pct: number[]
  benchmark_cumulative_return_pct: number[]
  per_fund: PerFundHistory[]
}

export const fetchRoiHistory = () => apiFetch<RoiHistory>('/api/funds/roi-history')

// Subconjunto de SignalResult (backend/app/models.py) que interesa mostrar
// en el contexto de un trade -- el resto de los ~25 campos del modelo real
// no se necesitan acá (ya se resumen en "rationale", ver trade_rationale.py).
export interface TradeSignal {
  score: number
  strategy_id: string
  sector: string | null
  news_sentiment: 'positive' | 'negative' | 'neutral' | null
  news_summary: string | null
}

export interface TradeContext {
  trade: {
    id: string
    symbol: string
    side: 'BUY' | 'SELL'
    quantity: number
    price: number
    executed_at: string
    realized_pnl: number | null
  }
  action: string | null
  signal: TradeSignal | null
  rationale: string | null
  reason: string | null
  r_multiple: number | null
  approximate: boolean | null
}

export const fetchTradeContext = (fundId: string, tradeId: string) =>
  apiFetch<TradeContext>(`/api/funds/${fundId}/trades/${tradeId}/context`)
