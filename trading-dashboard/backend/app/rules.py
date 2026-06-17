from __future__ import annotations

import math
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel

from .atomic_io import atomic_write_text
from .models import AccountSummary, OrderDecision, OrderRequest, PositionSizeSuggestion, RuleViolation, Side


class RulesConfig(BaseModel):
    symbol_whitelist: list[str] = []
    max_order_value_usd: float = 5000
    max_position_pct_of_equity: float = 10
    # Riesgo maximo a arriesgar por operacion, como % del equity, si se toca el
    # stop-loss. Se usa solo para sugerir un tamano de posicion (no rechaza
    # ordenes por si solo): el tamano final igual queda limitado tambien por
    # max_position_pct_of_equity y max_order_value_usd.
    risk_per_trade_pct: float = 1
    daily_loss_limit_pct: float = 2
    max_trades_per_day: int = 10
    require_stop_loss_on_buy: bool = True
    max_stop_loss_pct: float = 5
    allow_short_selling: bool = False
    manual_approval_threshold_usd: float = 1000
    allow_extended_hours: bool = False
    trading_hours_start: str = "09:30"
    trading_hours_end: str = "16:00"
    trading_hours_timezone: str = "America/New_York"

    @classmethod
    def load(cls, path: Path) -> "RulesConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = yaml.safe_load(path.read_text()) or {}
        return cls(**data)

    def save(self, path: Path) -> None:
        atomic_write_text(path, yaml.safe_dump(self.model_dump(), sort_keys=False))


def _parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


class RulesEngine:
    """Evalua una orden contra las reglas de riesgo configuradas.

    Una orden se aprueba solo si NINGUNA regla la rechaza. Si se aprueba pero su
    valor supera manual_approval_threshold_usd, queda marcada como pendiente de
    aprobacion manual aunque el modo de ejecucion sea automatico.
    """

    def __init__(self, config: RulesConfig):
        self.config = config

    def reload(self, config: RulesConfig) -> None:
        self.config = config

    def _within_trading_hours(self, now: datetime | None = None) -> bool:
        if self.config.allow_extended_hours:
            return True
        tz = ZoneInfo(self.config.trading_hours_timezone)
        current = (now or datetime.now(tz)).astimezone(tz)
        start = _parse_hhmm(self.config.trading_hours_start)
        end = _parse_hhmm(self.config.trading_hours_end)
        return start <= current.time() <= end

    def evaluate(
        self,
        order: OrderRequest,
        account: AccountSummary,
        current_position_qty: float,
        reference_price: float,
        trades_today: int,
        halted: bool,
    ) -> OrderDecision:
        violations: list[RuleViolation] = []

        if halted:
            violations.append(RuleViolation(
                rule="halted",
                message="El trading esta pausado (kill switch activo).",
            ))

        if self.config.symbol_whitelist:
            if order.symbol not in self.config.symbol_whitelist:
                violations.append(RuleViolation(
                    rule="symbol_whitelist",
                    message=f"{order.symbol} no esta en la lista blanca de simbolos permitidos.",
                ))
        else:
            violations.append(RuleViolation(
                rule="symbol_whitelist",
                message=(
                    "La lista blanca de simbolos esta vacia: por seguridad no se "
                    "permite ningun simbolo hasta agregarlo explicitamente."
                ),
            ))

        price = order.limit_price or reference_price
        estimated_value = price * order.quantity

        if estimated_value > self.config.max_order_value_usd:
            violations.append(RuleViolation(
                rule="max_order_value_usd",
                message=(
                    f"Valor estimado ${estimated_value:,.2f} excede el maximo por "
                    f"orden (${self.config.max_order_value_usd:,.2f})."
                ),
            ))

        signed_qty = order.quantity if order.side == Side.BUY else -order.quantity
        resulting_qty = current_position_qty + signed_qty

        if not self.config.allow_short_selling and resulting_qty < 0:
            violations.append(RuleViolation(
                rule="allow_short_selling",
                message=(
                    f"La orden dejaria una posicion corta ({resulting_qty:g}) en "
                    f"{order.symbol}. El short selling esta deshabilitado "
                    "(allow_short_selling=false)."
                ),
            ))

        if account.net_liquidation > 0:
            resulting_value = abs(resulting_qty) * price
            position_pct = (resulting_value / account.net_liquidation) * 100
            if position_pct > self.config.max_position_pct_of_equity:
                violations.append(RuleViolation(
                    rule="max_position_pct_of_equity",
                    message=(
                        f"La posicion resultante en {order.symbol} seria "
                        f"{position_pct:.1f}% del equity (maximo "
                        f"{self.config.max_position_pct_of_equity}%)."
                    ),
                ))

        if account.daily_pnl_pct <= -abs(self.config.daily_loss_limit_pct):
            violations.append(RuleViolation(
                rule="daily_loss_limit_pct",
                message=(
                    f"Perdida diaria {account.daily_pnl_pct:.2f}% alcanzo el limite "
                    f"(-{self.config.daily_loss_limit_pct}%). Trading detenido por hoy."
                ),
            ))

        if trades_today >= self.config.max_trades_per_day:
            violations.append(RuleViolation(
                rule="max_trades_per_day",
                message=f"Ya se alcanzo el maximo de {self.config.max_trades_per_day} operaciones hoy.",
            ))

        if order.side == Side.BUY and self.config.require_stop_loss_on_buy:
            if not order.stop_loss_price:
                violations.append(RuleViolation(
                    rule="require_stop_loss_on_buy",
                    message="Toda compra debe incluir un precio de stop-loss.",
                ))
            else:
                stop_pct = (price - order.stop_loss_price) / price * 100
                if stop_pct <= 0:
                    violations.append(RuleViolation(
                        rule="require_stop_loss_on_buy",
                        message="El stop-loss debe estar por debajo del precio de entrada.",
                    ))
                elif stop_pct > self.config.max_stop_loss_pct:
                    violations.append(RuleViolation(
                        rule="max_stop_loss_pct",
                        message=(
                            f"El stop-loss implica {stop_pct:.1f}% de riesgo, mayor "
                            f"al maximo permitido ({self.config.max_stop_loss_pct}%)."
                        ),
                    ))

        if not self._within_trading_hours():
            violations.append(RuleViolation(
                rule="trading_hours",
                message=(
                    f"Fuera del horario de trading permitido "
                    f"({self.config.trading_hours_start}-{self.config.trading_hours_end} "
                    f"{self.config.trading_hours_timezone})."
                ),
            ))

        approved = len(violations) == 0
        requires_manual_approval = approved and estimated_value > self.config.manual_approval_threshold_usd

        return OrderDecision(
            approved=approved,
            requires_manual_approval=requires_manual_approval,
            violations=violations,
            estimated_value_usd=estimated_value,
        )

    def suggested_quantity(
        self,
        equity: float,
        current_position_qty: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> PositionSizeSuggestion:
        """Sugiere una cantidad para una compra en base al riesgo, no a un monto
        fijo arbitrario: el tamano se calcula para que, si se toca el
        stop-loss, la perdida no supere risk_per_trade_pct del equity. Se
        recorta ademas por max_position_pct_of_equity y max_order_value_usd
        para no sugerir una cantidad que el motor de reglas rechazaria de
        todas formas al enviarla. Es solo una sugerencia editable, no se
        aplica sola.

        `equity` es el capital contra el que se dimensiona: el equity de toda
        la cuenta de IBKR para ordenes sin fondo asociado, o el
        equity_estimate() de un fondo puntual cuando la orden esta atada a
        uno (ver _try_auto_trade_entry y order_size_suggestion en main.py) --
        asi un fondo chico no recibe una posicion sizeada como si tuviera
        detras el capital de toda la cuenta.
        """
        risk_per_share = entry_price - stop_loss_price
        if entry_price <= 0 or equity <= 0 or risk_per_share <= 0:
            return PositionSizeSuggestion(quantity=0.0, risk_usd=0.0, limited_by=None)

        risk_budget_usd = equity * self.config.risk_per_trade_pct / 100
        qty_by_risk = risk_budget_usd / risk_per_share

        max_position_value = equity * self.config.max_position_pct_of_equity / 100
        remaining_value = max(0.0, max_position_value - current_position_qty * entry_price)
        qty_by_position_pct = remaining_value / entry_price

        qty_by_order_value = self.config.max_order_value_usd / entry_price

        qty_raw, limited_by = min(
            (qty_by_risk, None),
            (qty_by_position_pct, "max_position_pct_of_equity"),
            (qty_by_order_value, "max_order_value_usd"),
            key=lambda pair: pair[0],
        )
        qty = max(0.0, float(math.floor(qty_raw)))
        return PositionSizeSuggestion(
            quantity=qty,
            risk_usd=round(qty * risk_per_share, 2),
            limited_by=limited_by,
        )
