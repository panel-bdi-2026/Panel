from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    order_type: OrderType = OrderType.MKT
    limit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class RuleViolation(BaseModel):
    rule: str
    message: str


class OrderDecision(BaseModel):
    approved: bool
    requires_manual_approval: bool
    violations: list[RuleViolation] = []
    estimated_value_usd: Optional[float] = None


class PositionSizeSuggestion(BaseModel):
    quantity: float
    risk_usd: float
    limited_by: Optional[str] = None


class AccountSummary(BaseModel):
    net_liquidation: float
    cash: float
    buying_power: float
    daily_pnl: float
    daily_pnl_pct: float
    currency: str = "USD"


class Position(BaseModel):
    symbol: str
    quantity: float
    avg_cost: float
    market_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class PendingOrder(BaseModel):
    id: str
    order: OrderRequest
    decision: OrderDecision
    created_at: datetime
    status: str = "pending"  # pending | approved | rejected | executed
    source: str = "user"  # user | signal_engine


class SignalResult(BaseModel):
    symbol: str
    as_of: datetime
    last_price: float
    score: float
    momentum_3m_pct: float
    momentum_1m_pct: float
    trend_ok: bool
    rsi: float
    avg_volume: float
    pct_from_52w_high: Optional[float] = None
    suggested_stop_loss_price: float
    suggested_stop_loss_pct: float
    passes_filters: bool
    notes: list[str] = []


class BacktestTrade(BaseModel):
    symbol: str
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str  # stop_loss | max_holding_days | trend_break


class BacktestSummary(BaseModel):
    start_date: datetime
    end_date: datetime
    total_trades: int
    win_rate_pct: float
    avg_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: Optional[float] = None
    expectancy_pct: float
    strategy_cumulative_return_pct: float
    benchmark_cumulative_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float] = None
    trades: list[BacktestTrade] = []
