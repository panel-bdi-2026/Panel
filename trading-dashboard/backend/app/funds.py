from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from .atomic_io import atomic_write_text
from .models import Side

logger = logging.getLogger(__name__)


class FundPosition(BaseModel):
    quantity: float = 0.0
    avg_cost: float = 0.0
    # Se completan al ABRIR la posicion (primera compra desde quantity == 0) y
    # se limpian al cerrarla del todo; compras adicionales sobre una posicion
    # ya abierta no las modifican (se conserva la apertura original). Las usa
    # el monitor de salida en vivo del motor de auto-trading (ver main.py)
    # para max_holding_days y para aproximar el fill de un stop-loss que IBKR
    # ya ejecuto del lado del broker sin pasar por record_fill.
    opened_at: Optional[datetime] = None
    stop_loss_price: Optional[float] = None


class FundTrade(BaseModel):
    id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    executed_at: datetime
    realized_pnl: Optional[float] = None  # solo se completa en ventas


class CapitalFlow(BaseModel):
    """Un aporte (amount > 0) o retiro (amount < 0) de capital virtual de un
    fondo. No mueve nada en IBKR: el dinero real ya esta en la cuenta
    (depositado/retirado por fuera de esta herramienta), esto solo registra
    cuanto de ese cash le "asignas" al fondo. La creacion de un fondo es, en
    este modelo, simplemente su primer aporte."""

    id: str
    amount: float
    created_at: datetime
    note: Optional[str] = None


class Fund(BaseModel):
    """Una porcion de capital con su propia contabilidad, separada de la vista
    consolidada de IBKR y de cualquier otro fondo.

    cash_usd es puramente virtual: el backend nunca lee el cash real de IBKR
    para un fondo, solo lo mueve internamente con cada compra/venta atada a
    ese fund_id, y con cada aporte/retiro (capital_flows). `positions` es la
    unica fuente de verdad de "cuanto de cada simbolo es de este fondo": una
    venta atada a un fund_id nunca puede superar la cantidad que figura aqui,
    sin importar cuanto haya en la cuenta real de IBKR. Eso es lo que protege
    holdings preexistentes (o de otro fondo) que nunca se registraron en este
    ledger: aunque comparta simbolo, este fondo no puede tocarlos.

    El PnL/ROI se mide contra `net_contributed_capital()` (la suma de
    capital_flows), no contra un capital inicial fijo: asi, aportar o retirar
    plata mas adelante no infla ni desinfla artificialmente el rendimiento.
    Es una medida "dollar-weighted" simple: no pondera por cuanto tiempo
    estuvo cada peso invertido (a diferencia de un time-weighted return, que
    podria agregarse mas adelante si se necesita mas rigor).
    """

    id: str
    name: str
    cash_usd: float
    # Si esta en True, el motor proactivo puede abrir y cerrar posiciones en
    # este fondo sin pasar por aprobacion manual (ver _try_auto_trade_entry y
    # _check_fund_exit en main.py). Sigue paper-only sin excepcion: el motor
    # nunca opera en real sin importar este toggle (ver state["mode"]).
    auto_trading_enabled: bool = False
    created_at: datetime
    positions: dict[str, FundPosition] = Field(default_factory=dict)
    trades: list[FundTrade] = Field(default_factory=list)
    capital_flows: list[CapitalFlow] = Field(default_factory=list)

    def owned_quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0.0

    def can_afford(self, estimated_cost_usd: float) -> bool:
        return estimated_cost_usd <= self.cash_usd

    def equity_estimate(self) -> float:
        """Estima el capital total del fondo (cash + valor de sus posiciones
        abiertas) sin consultar precios de mercado en vivo: usa el costo
        promedio de cada posicion como aproximacion. Es la base que usa el
        motor de auto-trading para dimensionar nuevas entradas en proporcion
        al capital propio de ESTE fondo (ver RulesEngine.suggested_quantity),
        en vez del equity de toda la cuenta de IBKR -- un fondo chico no debe
        recibir una posicion sizeada como si tuviera detras el capital de
        todos los demas fondos juntos. No reemplaza fundMarketValue() del
        frontend, que usa precios en vivo cuando los tiene para mostrar PnL
        no realizado; aca alcanza con una aproximacion para el sizing."""
        positions_value = sum(p.quantity * p.avg_cost for p in self.positions.values())
        return self.cash_usd + positions_value

    def realized_pnl_total(self) -> float:
        return sum(t.realized_pnl for t in self.trades if t.realized_pnl is not None)

    def net_contributed_capital(self) -> float:
        return sum(f.amount for f in self.capital_flows)

    def apply_capital_flow(self, amount: float, note: Optional[str] = None) -> CapitalFlow:
        """Aporta (amount > 0) o retira (amount < 0) capital virtual. No
        valida nada (cash real disponible en la cuenta, cash_usd suficiente
        para retirar): esas validaciones corren ANTES, en main.py -- mismo
        patron que record_fill()."""
        self.cash_usd += amount
        flow = CapitalFlow(
            id=str(uuid.uuid4()),
            amount=amount,
            created_at=datetime.now(timezone.utc),
            note=note,
        )
        self.capital_flows.append(flow)
        return flow

    def record_fill(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        price: float,
        stop_loss_price: Optional[float] = None,
    ) -> FundTrade:
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
            if pos.quantity == 0:
                pos.opened_at = datetime.now(timezone.utc)
                pos.stop_loss_price = stop_loss_price
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
                pos.opened_at = None
                pos.stop_loss_price = None
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
        # Protege la secuencia leer-mutar-guardar de cada metodo publico de
        # mas abajo: sin esto, dos llamadas concurrentes desde threads
        # distintos (ej. dos requests sincronas del API atendidas por el
        # threadpool de FastAPI a la vez) pueden interleavear sus lecturas y
        # escrituras sobre el mismo Fund y perder una actualizacion. No
        # protege por si sola la ventana de carrera "validar afuera, mutar
        # despues" (ver _funds_order_lock en main.py), que abarca codigo
        # fuera de esta clase.
        self._lock = threading.Lock()
        self.funds: dict[str, Fund] = self._load()

    def _backup_corrupt_file(self) -> None:
        """Copia funds.json a un .bak con timestamp antes de descartar lo
        que no se pudo leer. Sin esto, el primer save() posterior a un
        arranque con datos corruptos sobreescribe el original con el
        estado parcial en memoria y lo corrupto se pierde para siempre."""
        if not self.path.exists():
            return
        backup_path = self.path.with_suffix(
            f".corrupt.{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}.bak"
        )
        try:
            shutil.copy2(self.path, backup_path)
            logger.error("funds store corrupto, respaldado en %s", backup_path)
        except OSError:
            logger.exception("no se pudo respaldar el archivo de fondos corrupto")

    def _load(self) -> dict[str, Fund]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._backup_corrupt_file()
            return {}

        if not isinstance(raw, dict):
            self._backup_corrupt_file()
            return {}

        # Cada fondo se valida por separado: un solo fondo malformado (ej.
        # editado a mano, o de una version vieja del esquema) no debe tirar
        # abajo el resto de los fondos validos ni el arranque del backend.
        funds: dict[str, Fund] = {}
        any_corrupt = False
        for fid, f in raw.items():
            try:
                funds[fid] = Fund(**f)
            except ValidationError:
                any_corrupt = True
                logger.error("fondo %s corrupto, se descarta (ver backup)", fid)
        if any_corrupt:
            self._backup_corrupt_file()
        return funds

    def save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps({fid: f.model_dump() for fid, f in self.funds.items()}, default=str, indent=2),
            encoding="utf-8",
        )

    def create(self, name: str, initial_capital_usd: float, auto_trading_enabled: bool = False) -> Fund:
        fund = Fund(
            id=str(uuid.uuid4()),
            name=name,
            cash_usd=0.0,
            auto_trading_enabled=auto_trading_enabled,
            created_at=datetime.now(timezone.utc),
        )
        fund.apply_capital_flow(initial_capital_usd, note="Capital inicial")
        with self._lock:
            self.funds[fund.id] = fund
            self.save()
        return fund

    def get(self, fund_id: str) -> Fund | None:
        return self.funds.get(fund_id)

    def list(self) -> list[Fund]:
        return list(self.funds.values())

    def total_allocated_cash(self, exclude_fund_id: str | None = None) -> float:
        """Suma de cash_usd de todos los fondos (opcionalmente excluyendo
        uno) -- se usa para validar que un aporte no haga que la suma de
        todos los fondos supere el cash real de la cuenta de IBKR."""
        return sum(f.cash_usd for fid, f in self.funds.items() if fid != exclude_fund_id)

    def set_auto_trading(self, fund_id: str, enabled: bool) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.auto_trading_enabled = enabled
            self.save()
            return fund

    def apply_capital_flow(self, fund_id: str, amount: float, note: Optional[str] = None) -> CapitalFlow | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            flow = fund.apply_capital_flow(amount, note)
            self.save()
            return flow

    def record_fill(
        self,
        fund_id: str,
        symbol: str,
        side: Side,
        quantity: float,
        price: float,
        stop_loss_price: Optional[float] = None,
    ) -> FundTrade | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            trade = fund.record_fill(symbol, side, quantity, price, stop_loss_price)
            self.save()
            return trade
