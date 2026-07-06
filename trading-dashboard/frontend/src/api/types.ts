// ── Funds ─────────────────────────────────────────────────────────────────────

export interface FundPosition {
  quantity: number
  avg_cost: number
  opened_at: string | null
  stop_loss_price: number | null
  initial_stop_loss_price: number | null
  scaled_out_at: string | null
}

export interface FundTrade {
  id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  price: number
  executed_at: string
  realized_pnl: number | null
}

export interface CapitalFlow {
  id: string
  amount: number
  note: string | null
  created_at: string
}

export interface Fund {
  id: string
  name: string
  cash_usd: number
  auto_trading_enabled: boolean
  strategy_id: string | null
  created_at: string
  positions: Record<string, FundPosition>
  trades: FundTrade[]
  capital_flows: CapitalFlow[]
  closed: boolean
  closed_at: string | null
  realized_pnl_total: number
  net_contributed_capital: number
}

// ── Orders ────────────────────────────────────────────────────────────────────

export interface PendingOrder {
  id: string
  fund_id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  order_type: string
  limit_price: number | null
  stop_price: number | null
  status: 'pending' | 'approved' | 'rejected' | 'filled' | 'cancelled'
  created_at: string
  score: number | null
  strategy_id: string | null
  notes: string | null
}

// ── Signals ───────────────────────────────────────────────────────────────────

export interface Signal {
  symbol: string
  strategy_id: string
  score: number
  price: number | null
  price_ts: string | null

  // Price action
  momentum_3m_pct: number | null
  momentum_1m_pct: number | null
  rsi: number | null
  atr_pct: number | null
  from_high_pct: number | null
  pct_from_52w_high: number | null
  macd_histogram_pct: number | null
  bollinger_pct_b: number | null
  trend_ok: boolean | null
  near_high_ok: boolean | null
  regime_ok: boolean | null
  earnings_ok: boolean | null
  liquidity_ok: boolean | null

  // Sizing
  suggested_stop_loss_price: number | null
  suggested_stop_loss_pct: number | null

  // Meta
  sector: string | null
  sector_relative_strength_pct: number | null
  notes: string[] | null
  passing: boolean
  passes_filters: boolean | null
  last_scan_at: string | null
  score_components: Record<string, number>

  // Fundamentals (long-term / dividend strategies)
  pe_ratio: number | null
  dividend_yield_pct: number | null
  payout_ratio_pct: number | null
  price_to_book: number | null
  peg_ratio: number | null
  beta: number | null
  analyst_recommendation: string | null
  news_sentiment: string | null
  news_summary: string | null
}

// ── Account ───────────────────────────────────────────────────────────────────

export interface AccountSummary {
  net_liquidation: number
  total_cash: number
  unrealized_pnl: number
  realized_pnl: number
  buying_power: number
}

export interface Position {
  symbol: string
  quantity: number
  avg_cost: number
  market_value: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
}

// ── Status ────────────────────────────────────────────────────────────────────

export interface AppStatus {
  connected: boolean
  mode: 'paper' | 'live'
  halted: boolean
  uptime: number
  last_scan_at: string | null
  scan_stale: boolean
}

// ── Audit ─────────────────────────────────────────────────────────────────────

export interface AuditEntry {
  id: string
  ts: string
  event: string
  detail: string | null
  fund_id: string | null
  symbol: string | null
}
