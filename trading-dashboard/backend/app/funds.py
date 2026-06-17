from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .models import Side


class FundPosition(BaseModel):
    quantity: float = 0.0
    avg_cost: float = 0.0


class FundTrade(BaseModel):
    id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    executed_at: datetime
    realized_pnl: Optional[float] = None  # solo se completa en ventas


class Fund(BaseModel):
    """Una porcion de capital con su propia contabilidad, separada de la vista
    consolidada de IBKR y de cualquier otro fondo.

    cash_usd es puramente virtual: el backend nunca lee el cash real de IBKR
    para un fondo, solo lo mueve internamente con cada compra/venta atada a
    ese fund_id (arranca en initial_capital_usd). `positions` es la unica
    fuente de verdad de "cuanto de cada simbolo es de este fondo": una venta
    atada a un fund_id nunca puede superar la cantidad que figura aqui, sin
    importar cuanto haya en la cuenta real de IBKR. Eso es lo que protege
    holdings preexistentes (o de otro fondo) que nunca se registraron en este
    ledger: aunque comparta simbolo, este fondo no puede tocarlos.
    """

    id: str
    name: str
    initial_capital_usd: float
    cash_usd: float
    # Si esta en True, el motor proactivo podra ejecutar compras/ventas en
    # este fondo sin pasar por aprobacion manual. Por ahora el campo solo se
    # persiste: ningun motor lo lee todavia (eso llega en una fase posterior,
    # ver README). Lo expone desde ya para que el toggle exista en la UI.
    auto_trading_enabled: bool = False
    created_at: datetime
    positions: dict[str, FundPosition] = Field(default_factory=dict)
    trades: list[FundTrade] = Field(default_factory=list)

    def owned_quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0.0

    def can_afford(self, estimated_cost_usd: float) -> bool:
        return estimated_cost_usd <= self.cash_usd

    def realized_pnl_total(self) -> float:
        return sum(t.realized_pnl for t in self.trades if t.realized_pnl is not None)

    def record_fill(self, symbol: str, side: Side, quantity: float, price: float) -> FundTrade:
        """Aplica una compra/venta ya ejecutada en el broker a la contabilidad
        del fondo: mueve cash_usd, actualiza la posicion (costo promedio en
        compras, PnL realizado en ventas) y la agrega al historial.

        No valida nada (cash suficiente, cantidad disponible): esas
        validaciones corren ANTES de enviar la orden al broker (ver
        main.py). Llamar a esto con una venta que deja quantity negativa
        indicaria un bug en esa validacion previa, no algo que este metodo
        deba intentar corregir silenciosamente.
        """
        pos = self.positions.setdefault(symbol, FundPosition())
        realized_pnl = None
        if side == Side.BUY:
            new_qty = pos.quantity + quantity
            pos.avg_cost = (
                (pos.avg_cost * pos.quantity + price * quantity) / new_qty if new_qty else 0.0
            )
            pos.quantity = new_qty
            self.cash_usd -= price * quantity
        else:
            realized_pnl = (price - pos.avg_cost) * quantity
            pos.quantity -= quantity
            if pos.quantity <= 0:
                pos.quantity = 0.0
                pos.avg_cost = 0.0
            self.cash_usd += price * quantity

        trade = FundTrade(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            executed_at=datetime.now(timezone.utc),
            realized_pnl=round(realized_pnl, 2) if realized_pnl is not None else None,
        )
        self.trades.append(trade)
        return trade


class FundsStore:
    """Persiste los fondos en un JSON simple (mismo patron que state_store.py:
    sin esto, crear un fondo y reiniciar el backend lo perdia)."""

    def __init__(self, path: Path):
        self.path = path
        self.funds: dict[str, Fund] = self._load()

    def _load(self) -> dict[str, Fund]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {fid: Fund(**f) for fid, f in data.items()}

    def save(self) -> None:
        self.path.write_text(
            json.dumps({fid: f.model_dump() for fid, f in self.funds.items()}, default=str, indent=2),
            encoding="utf-8",
        )

    def create(self, name: str, initial_capital_usd: float, auto_trading_enabled: bool = False) -> Fund:
        fund = Fund(
            id=str(uuid.uuid4()),
            name=name,
            initial_capital_usd=initial_capital_usd,
            cash_usd=initial_capital_usd,
            auto_trading_enabled=auto_trading_enabled,
            created_at=datetime.now(timezone.utc),
        )
        self.funds[fund.id] = fund
        self.save()
        return fund

    def get(self, fund_id: str) -> Fund | None:
        return self.funds.get(fund_id)

    def list(self) -> list[Fund]:
        return list(self.funds.values())

    def set_auto_trading(self, fund_id: str, enabled: bool) -> Fund | None:
        fund = self.funds.get(fund_id)
        if fund is None:
            return None
        fund.auto_trading_enabled = enabled
        self.save()
        return fund

    def record_fill(
        self, fund_id: str, symbol: str, side: Side, quantity: float, price: float
    ) -> FundTrade | None:
        fund = self.funds.get(fund_id)
        if fund is None:
            return None
        trade = fund.record_fill(symbol, side, quantity, price)
        self.save()
        return trade
