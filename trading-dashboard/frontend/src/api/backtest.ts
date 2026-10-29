import { apiFetch } from './client'

export interface BacktestSummary {
  start_date: string
  end_date: string
  total_trades: number
  win_rate_pct: number
  avg_return_pct: number
  avg_win_pct: number
  avg_loss_pct: number
  profit_factor: number | null
  profit_factor_is_infinite: boolean
  expectancy_pct: number
  strategy_cumulative_return_pct: number
  benchmark_cumulative_return_pct: number
  exposure_adjusted_benchmark_return_pct: number
  avg_alpha_pct: number | null
  max_drawdown_pct: number
  sharpe_ratio: number | null
  deflated_sharpe_ratio_pct: number | null
  avg_exposure_pct: number
  exit_reason_counts: Record<string, number>
  equity_curve: { date: string; equity_pct: number }[]
}

export interface WalkForwardFold {
  fold: number
  start_date: string
  end_date: string
  total_trades: number
  strategy_cumulative_return_pct: number
  benchmark_cumulative_return_pct: number
  sharpe_ratio: number | null
  max_drawdown_pct: number
  win_rate_pct: number
}

export interface WalkForwardResult {
  folds: WalkForwardFold[]
  n_positive_folds: number
}

export const runBacktest = (strategyId?: string) =>
  apiFetch<BacktestSummary>(
    `/api/signals/backtest${strategyId ? `?strategy_id=${strategyId}` : ''}`,
  )

export const runWalkForward = (strategyId?: string, nFolds = 3) =>
  apiFetch<WalkForwardResult>(
    `/api/signals/backtest/walk-forward?n_folds=${nFolds}${strategyId ? `&strategy_id=${strategyId}` : ''}`,
  )
