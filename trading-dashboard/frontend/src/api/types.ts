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
  score: number
  strategy_id: string
  price: number | null
  price_ts: string | null
  rsi: number | null
  atr_pct: number | null
  from_high_pct: number | null
  momentum_pct: number | null
  suggested_stop: number | null
  sector: string | null
  notes: string[] | null
  passing: boolean
  last_scan_at: string | null
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
