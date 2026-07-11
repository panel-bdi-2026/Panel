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
  order: {
    symbol: string
    side: 'BUY' | 'SELL'
    quantity: number
    order_type: string
    limit_price: number | null
    stop_loss_price: number | null
    fund_id: string | null
  }
  decision: {
    approved: boolean
    requires_manual_approval: boolean
    violations: string[]
    estimated_value_usd: number | null
  } | null
  status: 'pending' | 'approved' | 'rejected' | 'filled' | 'cancelled'
  created_at: string
  source: string | null
  strategy_id: string | null
  notes: string | null
}

// ── Signals ───────────────────────────────────────────────────────────────────

export interface SignalScores {
  momentum: number
  opportunistic: number
  long_term: number
  dividend: number
}

export interface Signal {
  symbol: string
  sector: string | null
  scores: SignalScores
}

// ── Account ───────────────────────────────────────────────────────────────────

export interface AccountSummary {
  net_liquidation: number
  cash: number
  buying_power: number
  daily_pnl: number
  daily_pnl_pct: number
  pnl_data_available: boolean
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
  // uptime/last_scan_at/scan_stale no vienen de /api/status (viven en
  // /api/health) — se dejan opcionales para no romper si algún caller viejo
  // los espera acá.
  uptime?: number
  last_scan_at?: string | null
  scan_stale?: boolean
  market_scan_busy?: boolean
  live_radar_enabled?: boolean
  live_hot_count?: number
  live_hot_cap?: number
  live_radar_strategy_filter?: string | null
}

// ── Audit ─────────────────────────────────────────────────────────────────────

export interface AuditEntry {
  id: number
  ts: string
  action: string
  payload: Record<string, unknown>
  result: Record<string, unknown> | null
}
