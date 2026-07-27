from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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
    # order_id de la orden stop-loss bracket colocada al ABRIR esta posicion
    # (ver broker.place_order). Permite consultar broker.get_trade_fill() para
    # reconciliar el precio de fill REAL cuando IBKR ejecuta el stop del lado
    # del broker sin pasar por record_fill, en vez de aproximarlo solo con
    # stop_loss_price (ver _check_fund_exit en main.py).
    stop_order_id: Optional[int] = None
    # Comision de COMPRA acumulada, todavia no realizada, de la cantidad
    # actualmente abierta (ver record_fill). Igual que avg_cost, no incluye
    # la comision (se mantiene "precio limpio" a proposito), pero eso
    # significaba que realized_pnl de una venta solo restaba la comision de
    # SALIDA, nunca la de entrada -- realized_pnl_total() quedaba inflado por
    # la suma de todas las comisiones de compra de posiciones ya cerradas.
    # Al vender, se descuenta la porcion proporcional a la cantidad vendida
    # (ver record_fill) del realized_pnl de esa venta, sin tocar avg_cost.
    cost_basis_commission: float = 0.0
    # Stop-loss ORIGINAL al abrir la posicion, congelado (a diferencia de
    # stop_loss_price, que el trailing stop puede subir con el tiempo -- ver
    # _check_fund_trailing_stop en main.py). Es la base para calcular el "R"
    # de la salida parcial por conviccion (ver _check_fund_exit en main.py y
    # ScreenerConfig.scale_out_at_r_multiple): 1R = avg_cost - este valor.
    initial_stop_loss_price: Optional[float] = None
    # Marca que ya se ejecuto la salida parcial de esta posicion (ver
    # ScreenerConfig.scale_out_enabled): evita repetir la venta parcial en
    # cada ciclo del monitor de salida una vez disparada.
    scaled_out_at: Optional[datetime] = None


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
    # Estrategia que el motor de auto-trading usa para decidir que señales
    # tomar en ESTE fondo (ver _try_auto_trade_entry en main.py). None = sigue
    # la estrategia activa global (screener_config.strategy_id), que es el
    # comportamiento que tenian todos los fondos antes de que existiera este
    # campo (un solo fondo "ganador" por orden de creacion, sin importar la
    # estrategia que disparo la señal).
    strategy_id: Optional[str] = None
    created_at: datetime
    positions: dict[str, FundPosition] = Field(default_factory=dict)
    trades: list[FundTrade] = Field(default_factory=list)
    capital_flows: list[CapitalFlow] = Field(default_factory=list)
    # True una vez cerrado (ver FundsStore.close()): el fondo queda de solo
    # lectura, sin nuevas ordenes, aportes/retiros ni auto-trading. No hay
    # operacion inversa: cerrar un fondo no se puede deshacer.
    closed: bool = False
    closed_at: Optional[datetime] = None

    def owned_quantity(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else 0.0

    def has_open_positions(self) -> bool:
        return any(p.quantity != 0 for p in self.positions.values())

    def _reset_position(self, pos: "FundPosition") -> None:
        """Limpia todos los campos de estado de una posicion cerrada."""
        pos.quantity = 0.0
        pos.avg_cost = 0.0
        pos.opened_at = None
        pos.stop_loss_price = None
        pos.stop_order_id = None
        pos.cost_basis_commission = 0.0
        pos.initial_stop_loss_price = None
        pos.scaled_out_at = None

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
        stop_order_id: Optional[int] = None,
        commission: float = 0.0,
    ) -> FundTrade:
        """Aplica una compra/venta ya ejecutada en el broker a la contabilidad
        del fondo: mueve cash_usd, actualiza la posicion (costo promedio en
        compras, PnL realizado en ventas) y la agrega al historial.

        `commission` se descuenta de cash_usd ademas del precio*cantidad, en
        ambos lados (compra y venta): sin esto, el ledger del fondo asumia
        operar gratis, mientras que el backtest (ver commission_per_trade_usd
        en screener_config.py) si modela ese costo -- inflando el P&L en vivo
        respecto al de un backtest comparable. No se suma a avg_cost (el
        costo de la posicion queda en precio puro): se resta directo del cash
        al comprar, asi equity_estimate() siempre refleja el costo total
        pagado sin importar en que pierna se cobro la comision.

        La comision de COMPRA se acumula en pos.cost_basis_commission (sin
        tocar avg_cost) y se descuenta PRO-RATA del realized_pnl de cada
        venta posterior, ademas de la comision de esa venta: sin esto,
        realized_pnl_total() solo restaba la comision de salida, nunca la de
        entrada, e inflaba el PnL realizado reportado por fondo (aunque
        equity_estimate()/cash_usd ya eran correctos, porque la comision de
        compra si se descuenta del cash en el momento de comprar).

        No valida nada (cash suficiente): esa validacion corre ANTES de
        enviar la orden al broker (ver main.py). La cantidad vendida si se
        acota a lo que la posicion realmente tiene: un caller con un bug (ej.
        una reconciliacion que calculo mal closed_qty) no debe poder inflar
        cash_usd con dinero virtual ni registrar un PnL irreal vendiendo mas
        de lo que el fondo posee.
        """
        pos = self.positions.setdefault(symbol, FundPosition())
        realized_pnl = None
        if side == Side.BUY:
            if pos.quantity < 0:
                # Cobertura de posición corta: BUY cierra (parcial o totalmente) un short
                qty_covered = min(quantity, abs(pos.quantity))
                entry_commission_share = (
                    pos.cost_basis_commission / abs(pos.quantity) * qty_covered
                    if pos.quantity else 0.0
                )
                realized_pnl = (pos.avg_cost - price) * qty_covered - commission - entry_commission_share
                pos.cost_basis_commission -= entry_commission_share
                pos.quantity += qty_covered
                if pos.quantity == 0:
                    self._reset_position(pos)
                quantity = qty_covered
                self.cash_usd -= price * qty_covered + commission
            else:
                if pos.quantity == 0:
                    pos.opened_at = datetime.now(timezone.utc)
                    pos.stop_loss_price = stop_loss_price
                    pos.stop_order_id = stop_order_id
                    pos.initial_stop_loss_price = stop_loss_price
                    pos.scaled_out_at = None
                new_qty = pos.quantity + quantity
                pos.avg_cost = (
                    (pos.avg_cost * pos.quantity + price * quantity) / new_qty if new_qty else 0.0
                )
                pos.quantity = new_qty
                pos.cost_basis_commission += commission
                self.cash_usd -= price * quantity + commission
        else:
            if quantity > pos.quantity:
                logger.warning(
                    "record_fill: venta de %.6f %s en fondo %s supera la posicion "
                    "registrada (%.6f); se acota a lo disponible.",
                    quantity, symbol, self.id, pos.quantity,
                )
                quantity = pos.quantity
            entry_commission_share = (
                pos.cost_basis_commission / pos.quantity * quantity if pos.quantity else 0.0
            )
            realized_pnl = (price - pos.avg_cost) * quantity - commission - entry_commission_share
            pos.cost_basis_commission -= entry_commission_share
            pos.quantity -= quantity
            if pos.quantity <= 0:
                self._reset_position(pos)
            self.cash_usd += price * quantity - commission

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

    def update_stop_loss(self, symbol: str, new_stop_price: float) -> None:
        """Actualiza el stop_loss_price registrado de una posicion abierta,
        sin tocar cash/quantity/trades. Usado por el trailing stop (ver
        _check_fund_trailing_stop en main.py) DESPUES de confirmar que IBKR ya
        modifico la orden stop-loss real al nuevo precio (broker.modify_stop_price):
        este ledger nunca debe registrar un stop mas favorable que el que de
        verdad protege la posicion en el broker."""
        pos = self.positions.get(symbol)
        if pos is not None and pos.quantity > 0:
            pos.stop_loss_price = new_stop_price

    def set_stop_order_id(self, symbol: str, stop_order_id: int) -> None:
        """Registra el stop_order_id de un stop-loss recien colocado sobre
        una posicion ya abierta, sin tocar price/cash/quantity. Usado por la
        reconciliacion de arranque (ver _reconcile_unfilled_on_startup en
        main.py) DESPUES de llamar a broker.place_protective_stop() para una
        posicion reconciliada que no tenia ningun stop vivo protegiendola
        (ver broker.has_live_protective_stop) -- el stop original de esa
        posicion probablemente se cancelo por error cuando la orden padre
        parecia "Cancelled" transitoriamente en IBKR."""
        pos = self.positions.get(symbol)
        if pos is not None and pos.quantity > 0:
            pos.stop_order_id = stop_order_id

    def mark_scaled_out(self, symbol: str) -> None:
        """Marca que la salida parcial (scale-out) de esta posicion ya se
        ejecuto, sin tocar price/cash/quantity. Usado por el monitor de
        salida (ver _check_fund_exit en main.py) DESPUES de confirmar la
        venta parcial en el broker, para no repetirla en el proximo ciclo."""
        pos = self.positions.get(symbol)
        if pos is not None and pos.quantity > 0:
            pos.scaled_out_at = datetime.now(timezone.utc)


class FundValidationError(ValueError):
    """Una validacion de `allocation_check` (ver FundsStore.create()/
    apply_capital_flow()) fallo. Se levanta DENTRO de self._lock para que la
    decision se tome sobre el estado mas actualizado posible, en vez de sobre
    una lectura externa que otra llamada concurrente ya pudo haber invalidado."""
    pass


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

    def create(
        self,
        name: str,
        initial_capital_usd: float,
        auto_trading_enabled: bool = False,
        strategy_id: Optional[str] = None,
        allocation_check: Optional[Callable[[float], None]] = None,
    ) -> Fund:
        """Crea un fondo. `allocation_check`, si se pasa, recibe la suma de
        cash_usd ya asignado a OTROS fondos (calculada DENTRO de self._lock,
        justo antes de crear) y puede levantar FundValidationError para
        abortar la creacion sin tocar self.funds.

        Validar dentro del lock (en vez de afuera, antes de llamar a create())
        es lo que cierra la carrera: dos creaciones concurrentes que leyeran la
        misma suma "ya asignada" desactualizada podian pasar la validacion
        ambas y terminar asignando entre las dos mas cash a fondos que el cash
        real disponible en la cuenta de IBKR.
        """
        fund = Fund(
            id=str(uuid.uuid4()),
            name=name,
            cash_usd=0.0,
            auto_trading_enabled=auto_trading_enabled,
            strategy_id=strategy_id,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            if allocation_check is not None:
                allocation_check(self.total_allocated_cash())
            fund.apply_capital_flow(initial_capital_usd, note="Capital inicial")
            self.funds[fund.id] = fund
            self.save()
        return fund

    def get(self, fund_id: str) -> Fund | None:
        return self.funds.get(fund_id)

    def list(self) -> list[Fund]:
        return list(self.funds.values())

    def total_allocated_cash(self, exclude_fund_id: str | None = None) -> float:
        """Suma de cash_usd + costo de posiciones abiertas de todos los fondos
        (opcionalmente excluyendo uno). Se compara contra net_liquidation de
        IBKR (no solo TotalCashValue) para cubrir el gap de timing entre el
        momento en que el fondo registra el fill y el momento en que IBKR
        actualiza su TotalCashValue: si solo sumáramos cash_usd, entre el fill
        y el update de IBKR el sistema vería más 'asignable' del que hay."""
        total = 0.0
        for fid, f in self.funds.items():
            if fid == exclude_fund_id:
                continue
            total += f.cash_usd
            for pos in f.positions.values():
                total += pos.quantity * pos.avg_cost
        return total

    def set_auto_trading(self, fund_id: str, enabled: bool) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.auto_trading_enabled = enabled
            self.save()
            return fund

    def set_strategy(self, fund_id: str, strategy_id: Optional[str]) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.strategy_id = strategy_id
            self.save()
            return fund

    def apply_capital_flow(
        self,
        fund_id: str,
        amount: float,
        note: Optional[str] = None,
        allocation_check: Optional[Callable[[Fund, float], None]] = None,
    ) -> CapitalFlow | None:
        """Aporta/retira capital. `allocation_check`, si se pasa, recibe el
        Fund (ya bajo el lock, con su cash_usd actual) y la suma de cash_usd
        ya asignado a OTROS fondos, y puede levantar FundValidationError --
        misma razon que en create(): validar dentro del lock evita la carrera
        con otra llamada concurrente sobre el mismo o otro fondo."""
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            if allocation_check is not None:
                allocation_check(fund, self.total_allocated_cash(exclude_fund_id=fund_id))
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
        stop_order_id: Optional[int] = None,
        commission: float = 0.0,
    ) -> FundTrade | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            trade = fund.record_fill(
                symbol, side, quantity, price, stop_loss_price, stop_order_id, commission
            )
            self.save()
            return trade

    def update_stop_loss(self, fund_id: str, symbol: str, new_stop_price: float) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.update_stop_loss(symbol, new_stop_price)
            self.save()
            return fund

    def set_stop_order_id(self, fund_id: str, symbol: str, stop_order_id: int) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.set_stop_order_id(symbol, stop_order_id)
            self.save()
            return fund

    def mark_scaled_out(self, fund_id: str, symbol: str) -> Fund | None:
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            fund.mark_scaled_out(symbol)
            self.save()
            return fund

    def close(self, fund_id: str) -> Fund | None:
        """Cierra un fondo de forma definitiva (no hay operacion inversa).
        Exige cash_usd en 0 y ninguna posicion abierta: cerrar un fondo que
        todavia tiene plata o activos asignados los dejaria atrapados en un
        fondo de solo lectura, sin forma de retirarlos ni venderlos despues.

        La tolerancia de 0.005 (medio centavo) en la comparacion de cash_usd
        es para no bloquear un retiro total legitimo por arrastre de punto
        flotante (sumas/restas de muchos fills) que deje, por ejemplo,
        -1e-10 en vez de un 0.0 exacto."""
        with self._lock:
            fund = self.funds.get(fund_id)
            if fund is None:
                return None
            if fund.closed:
                raise FundValidationError("El fondo ya esta cerrado.")
            if abs(fund.cash_usd) > 0.005:
                raise FundValidationError(
                    f"El fondo todavia tiene ${fund.cash_usd:,.2f} de cash: retira el saldo "
                    "completo antes de cerrarlo."
                )
            if fund.has_open_positions():
                raise FundValidationError(
                    "El fondo todavia tiene posiciones abiertas: vendelas antes de cerrarlo."
                )
            fund.closed = True
            fund.closed_at = datetime.now(timezone.utc)
            fund.auto_trading_enabled = False
            self.save()
            return fund
