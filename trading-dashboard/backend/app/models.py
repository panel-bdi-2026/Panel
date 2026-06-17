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
