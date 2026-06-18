from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Tickers reales son letras, digitos, punto y guion (ej. BRK.B, RDS-A). Se
# valida estrictamente el simbolo de toda orden entrante: sin esto, el campo
# aceptaba cualquier texto (solo se hacia strip().upper()), que despues se
# persistia en audit.db / ordenes pendientes y se renderizaba en el dashboard.
# Un "simbolo" tipo '<img src=x onerror=...>' quedaba como XSS almacenado
# capaz de robar la API key del localStorage de quien abriera el panel.
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


def validate_symbol(value: str) -> str:
    """Normaliza y valida un ticker contra _SYMBOL_RE. Punto de entrada
    compartido para cualquier input de simbolo (ordenes, universo del
    screener, query params), no solo OrderRequest: cualquier otro lugar
    que reciba un simbolo en texto libre y lo persista o lo use para
    construir una whitelist necesita la misma sanitizacion, o queda como
    una puerta de entrada sin el chequeo que protege contra XSS almacenado
    e inyeccion de datos malformados."""
    v = value.strip().upper()
    if not _SYMBOL_RE.match(v):
        raise ValueError(
            "Simbolo invalido: solo se permiten letras, numeros, punto y "
            "guion (1 a 12 caracteres)."
        )
    return v


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
    # Si se especifica, la orden queda atada a un fondo (ver funds.py): la
    # contabilidad (cash_usd, posiciones, PnL realizado) se actualiza en ese
    # fondo en vez de mezclarse con el resto. None = cuenta general (sin fondo).
    fund_id: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return validate_symbol(v)


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
