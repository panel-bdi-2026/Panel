from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker
from ib_async.order import OrderStatus

from .models import AccountSummary, OrderRequest, OrderType, Position, Side

logger = logging.getLogger(__name__)


def _to_ib_symbol(symbol: str) -> str:
    """IBKR identifica las acciones con clase de accion con un espacio (ej.
    'BRK B'), no con el guion ni el punto que usan los proveedores de datos
    y el resto de este sistema (BRK-B en screener_config.py/sectors.py/
    rules.yaml). Sin esto, qualifyContractsAsync no encuentra el contrato
    para simbolos como BRK-B o BF-B: queda sin conId y todo lo que dependa
    de el (precio, ordenes) falla en silencio para esos dos tickers."""
    return symbol.replace("-", " ").replace(".", " ")


def _from_ib_symbol(symbol: str) -> str:
    """Inverso de _to_ib_symbol: el contrato que devuelve IBKR trae el
    simbolo con espacio. Hay que volver a la forma canonica con guion para
    que coincida con el universo del screener y el resto del sistema (sin
    esto, una posicion en BRK-B volveria como "BRK B" y nunca calzaria con
    los lookups por simbolo de RulesEngine, sectors.py, etc.)."""
    return symbol.replace(" ", "-")


def _clean_price(price: float) -> float | None:
    """Ticker.marketPrice() de ib_async devuelve NaN cuando todavia no hay
    ningun tick (filtrado con price == price). Pero con datos demorados/
    congelados (reqMarketDataType(3), el default de este backend) IBKR
    tambien puede mandar -1 como valor real de last/close para indicar
    "sin dato disponible" en vez de omitir el campo, y eso SI es un float
    valido que pasa el filtro de NaN sin este chequeo aparte -- se vio
    como precio "-$1.00" en el radar para simbolos sin datos demorados
    entitleados en ese momento."""
    return price if price == price and price > 0 else None


# IBKR manda avisos meramente informativos por el mismo canal que los rechazos
# reales, y ib_async solo reconoce como benignos los codigos de su lista
# `warningCodes` (105, 110, 165, 321, 329, 399, 404, 434, 492, 10167 -- ver
# ib_async/wrapper.py:1608). Cualquier otro codigo cae en la rama de error, que
# marca la orden como Cancelled y emite cancelledEvent AUNQUE la orden siga
# viva en el broker: ~300ms despues IBKR manda el status real y la orden vuelve
# a PreSubmitted/Submitted.
#
# 10349 ("Order TIF was set to DAY based on order preset") es exactamente eso:
# un aviso, no un rechazo. Esa ventana de ~300ms rompia el flujo entero de
# auto-trading (incidente del 2026-07-29/30: 62 ordenes en dos dias, ninguna
# contabilizada). Secuencia real de la orden 2477436 (CCL) del 30/07:
#
#   18:48:06.832  PendingSubmit
#   18:48:06.850  Cancelled   <- ib_async, por el aviso 10349
#   18:48:07.126  PreSubmitted
#   18:48:07.135  Submitted   <- estado real: la orden estaba viva todo el tiempo
#
# Dentro de esa ventana, _register_stop_reconciliation veia el Cancelled falso
# y cancelaba nuestro propio stop-loss, _wait_for_fill volvia al instante con
# filled_qty=0, y place_order lanzaba StopLossRejectedError -- con lo cual la
# compra quedaba viva en IBKR, sin proteccion y sin registrar en el fondo, y el
# scanner la reintentaba en cada ciclo (CSGP se intento 9 veces en dos dias).
_SPURIOUS_CANCEL_CODES = frozenset({10349})

# Time-in-force explicito en toda orden que colocamos. Si se deja vacio, IBKR
# aplica el preset de la cuenta y lo AVISA con el codigo 10349 -- que es
# justamente el que dispara el cancel espurio de arriba. Poner "DAY" a mano no
# cambia el comportamiento (es lo que el preset ya venia aplicando), solo
# elimina el aviso en el origen. El guard de _is_spurious_cancel se mantiene
# igual: es la defensa contra CUALQUIER otro codigo informativo que ib_async
# clasifique mal, no solo el 10349.
_DEFAULT_TIF = "DAY"


def _is_spurious_cancel(trade) -> bool:
    """True si el 'Cancelled' actual de `trade` lo escribio ib_async al
    malinterpretar un aviso informativo de IBKR (ver _SPURIOUS_CANCEL_CODES).
    La orden sigue viva en el broker y su status real llega poco despues.

    Se mira la ULTIMA entrada de cancelacion del log, no cualquiera: una orden
    puede acumular un cancel espurio temprano y despues ser cancelada de
    verdad, y en ese caso hay que respetar la cancelacion real."""
    if trade.orderStatus.status not in (OrderStatus.Cancelled, OrderStatus.ApiCancelled):
        return False
    for entry in reversed(trade.log):
        if entry.status in (OrderStatus.Cancelled, OrderStatus.ApiCancelled):
            return entry.errorCode in _SPURIOUS_CANCEL_CODES
    return False


class IBKRConnectionError(RuntimeError):
    pass


class StopLossRejectedError(RuntimeError):
    """La orden padre se transmitio pero IBKR rechazo/cancelo el stop-loss
    asociado. La posicion puede haber quedado abierta sin proteccion --
    quien llama debe tratar esto como una falla critica, no como exito.

    Lleva el fill de la orden PADRE (si ya se ejecutó) para que quien capture
    esta excepción pueda registrar el fill en el fondo y colocar un stop
    protector de emergencia, sin perder la posición abierta."""

    def __init__(
        self,
        msg: str,
        *,
        order_id: int,
        stop_order_id: int,
        filled_qty: float = 0.0,
        avg_fill_price: "float | None" = None,
    ) -> None:
        super().__init__(msg)
        self.order_id = order_id
        self.stop_order_id = stop_order_id
        self.filled_qty = filled_qty
        self.avg_fill_price = avg_fill_price


class IBKRBroker:
    """Capa fina sobre ib_async (fork mantenido de ib_insync).

    Requiere TWS o IB Gateway corriendo y accesible en host:port con la API
    habilitada (Configure > API > Settings > "Enable ActiveX and Socket Clients").
    No incluye logica de estrategia: solo conecta, lee cuenta/posiciones, y
    coloca las ordenes que ya pasaron por el RulesEngine.

    Importante: ib_async es asyncio-nativo. Como este backend corre dentro del
    event loop de uvicorn, usamos siempre los metodos *Async (connectAsync,
    qualifyContractsAsync, reqTickersAsync) en vez de los wrappers sincronos
    (connect, ib.sleep, etc.), que internamente intentan correr su propio loop
    y chocan con el de FastAPI ("event loop is already running").
    """

    def __init__(self, host: str, port: int, client_id: int, market_data_type: int = 3):
        self.host = host
        self.port = port
        self.client_id = client_id
        # 1=real-time, 2=frozen, 3=delayed (default, funciona sin ninguna
        # suscripcion de datos), 4=delayed-frozen. Ver Settings.ib_market_data_type
        # (app/config.py) para el detalle de cuando conviene cambiarlo.
        self.market_data_type = market_data_type
        self.account_id: str | None = None
        self.ib = IB()
        # Suscripciones de streaming persistente (Nivel 1) para el hot-set del
        # radar de oportunidades (ver _hot_set_loop en main.py): a diferencia
        # de get_reference_price/get_snapshot_prices (que ocupan una linea
        # unos segundos y la liberan), estas quedan abiertas y se actualizan
        # solas via callbacks de ib_async -- get_live_price solo lee el ultimo
        # valor cacheado, sin pedirle nada nuevo a IBKR.
        self._live_tickers: dict[str, Ticker] = {}

    async def connect(self) -> None:
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=10)
            self.ib.reqMarketDataType(self.market_data_type)
            accounts = self.ib.managedAccounts()
            if accounts:
                self.account_id = accounts[0]
                # Suscripcion al PnL diario real de IBKR (dailyPnL). Sin esto no
                # hay forma confiable de saber la perdida del DIA: accountSummary
                # solo trae el no-realizado acumulado desde que se abrio cada
                # posicion, no el movimiento de hoy.
                self.ib.reqPnL(self.account_id)
        except Exception as exc:
            raise IBKRConnectionError(
                f"No se pudo conectar a IBKR en {self.host}:{self.port}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
        # Los Ticker quedan atados a la conexion que se esta cerrando: tras un
        # reconnect (que crea un IB() nuevo) ya no se actualizarian solos, asi
        # que get_live_price no debe seguir devolviendo ese valor congelado.
        # El proximo ciclo de _hot_set_loop vuelve a suscribir lo que haga
        # falta sobre la conexion nueva.
        self._live_tickers.clear()

    async def reconnect(self, host: str, port: int, client_id: int) -> None:
        """Cierra la conexion actual y abre una nueva en otro host/puerto.

        Usado para cambiar entre paper y live sin reiniciar el backend: cada
        modo corre como una sesion distinta de TWS/IB Gateway en su propio
        puerto, asi que cambiar de modo implica reconectar, no solo cambiar
        una bandera en memoria.
        """
        self.disconnect()
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        await self.connect()

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    async def get_account_summary(self) -> AccountSummary:
        # accountSummaryAsync espera a que el snapshot inicial este cargado en
        # vez de leer el cache sincrono: self.ib.accountSummary() (sync) llama
        # internamente a self._run(...) -> loop.run_until_complete() sobre el
        # MISMO loop de uvicorn que ya esta corriendo, lo cual revienta con
        # "This event loop is already running" en cada llamada desde un
        # endpoint async. accountSummaryAsync corre en el loop existente.
        tags = await self.ib.accountSummaryAsync()
        values = {t.tag: t.value for t in tags}
        net_liq = float(values.get("NetLiquidation", 0) or 0)
        cash = float(values.get("TotalCashValue", 0) or 0)
        buying_power = float(values.get("BuyingPower", 0) or 0)

        # dailyPnL es el P&L real DEL DIA que reporta IBKR (requiere reqPnL,
        # suscripto en connect()). RealizedPnL/UnrealizedPnL de accountSummary
        # NO son del dia: el no-realizado es acumulado desde que se abrio cada
        # posicion, asi que usarlos subestima/sobreestima la perdida diaria real.
        daily_pnl = 0.0
        pnl_list = self.ib.pnl()
        # Sin pnl_list (recien conectado, antes del primer callback de reqPnL)
        # o con NetLiquidation 0 (cuenta sin datos), dailyPnL real es
        # indeterminado: no hay forma de distinguir "perdida real de 0" de
        # "todavia no llego el dato". pnl_data_available=False le indica a
        # quien consuma esto (kill switch, RulesEngine) que no confie en
        # daily_pnl/daily_pnl_pct.
        pnl_data_available = bool(pnl_list) and net_liq != 0
        if pnl_data_available:
            raw = pnl_list[0].dailyPnL
            if raw is None or raw != raw:  # NaN: callback de reqPnL todavia no llego
                pnl_data_available = False
            else:
                daily_pnl = float(raw)
        daily_pnl_pct = (daily_pnl / net_liq * 100) if net_liq else 0.0
        return AccountSummary(
            net_liquidation=net_liq,
            cash=cash,
            buying_power=buying_power,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
            pnl_data_available=pnl_data_available,
        )

    async def get_positions(self) -> list[Position]:
        positions = self.ib.positions()
        if not positions:
            return []

        # Un solo reqTickersAsync con todos los contratos en vez de uno por
        # posicion: N llamadas secuenciales en cada ciclo del broadcast (cada
        # poll_interval_seconds) desperdicia cuota y puede gatillar pacing
        # violations de IBKR a medida que crece la cantidad de posiciones.
        try:
            tickers = await self.ib.reqTickersAsync(*(p.contract for p in positions))
        except Exception as exc:
            logger.warning("No se pudo pedir precios de mercado para las posiciones abiertas: %s", exc)
            tickers = []
        price_by_conid: dict[int, float] = {}
        for t in tickers:
            if not t.contract:
                continue
            price = _clean_price(t.marketPrice())
            if price is not None:
                price_by_conid[t.contract.conId] = price

        out: list[Position] = []
        for p in positions:
            market_price = price_by_conid.get(p.contract.conId)
            unrealized = (market_price - p.avgCost) * p.position if market_price is not None else None
            out.append(Position(
                symbol=_from_ib_symbol(p.contract.symbol),
                quantity=p.position,
                avg_cost=p.avgCost,
                market_price=market_price,
                unrealized_pnl=unrealized,
            ))
        return out

    def get_position_qty(self, symbol: str) -> float:
        for p in self.ib.positions():
            if _from_ib_symbol(p.contract.symbol) == symbol:
                return p.position
        return 0.0

    async def get_reference_price(self, symbol: str) -> float | None:
        contract = Stock(_to_ib_symbol(symbol), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        tickers = await self.ib.reqTickersAsync(contract)
        if not tickers:
            return None
        return _clean_price(tickers[0].marketPrice())

    async def stream_subscribe(self, symbols: list[str]) -> None:
        """Abre suscripciones de streaming persistente para `symbols` que
        todavia no la tengan (ignora los que ya estan suscriptos: reqMktData
        de nuevo sobre el mismo contrato duplicaria la linea sin necesidad).
        No hay limite de "refrescos": una vez suscripto, Ticker.marketPrice()
        (ver get_live_price) se mantiene actualizado solo mientras dure la
        conexion, ocupando una sola linea de market data por simbolo.

        Si IBKR no puede calificar el contrato de algun simbolo (queda sin
        conId), se lo omite en vez de pedirle reqMktData: ese contrato no se
        puede hashear y aborta TODO el lote, dejando sin suscribir incluso a
        los simbolos validos."""
        new_symbols = [s for s in symbols if s not in self._live_tickers]
        if not new_symbols:
            return
        contracts = [Stock(_to_ib_symbol(s), "SMART", "USD") for s in new_symbols]
        await self.ib.qualifyContractsAsync(*contracts)
        for symbol, contract in zip(new_symbols, contracts):
            if not contract.conId:
                continue
            self._live_tickers[symbol] = self.ib.reqMktData(contract, "", False, False)

    def stream_unsubscribe(self, symbols: list[str]) -> None:
        """Libera lineas de streaming de simbolos que ya no estan en el
        hot-set. Ignora en silencio los que no estaban suscriptos."""
        for symbol in symbols:
            ticker = self._live_tickers.pop(symbol, None)
            if ticker is not None:
                self.ib.cancelMktData(ticker.contract)

    def get_live_price(self, symbol: str) -> float | None:
        """Ultimo precio cacheado de una suscripcion de streaming activa, sin
        red: Ticker.marketPrice() ya viene actualizado por los callbacks de
        ib_async en background. None si `symbol` no esta en el hot-set en
        este momento (o si el callback todavia no trajo un primer precio)."""
        ticker = self._live_tickers.get(symbol)
        if ticker is None:
            return None
        return _clean_price(ticker.marketPrice())

    async def get_snapshot_prices(self, symbols: list[str]) -> dict[str, float]:
        """Snapshot de precio para un lote de simbolos en una sola llamada
        (igual que get_positions: reqTickersAsync libera cada linea apenas
        llega el snapshot, a diferencia de stream_subscribe). Pensado para
        rotar el resto del universo -- el que no esta en el hot-set -- con las
        lineas de market data que el hot-set deja libres (ver
        _price_rotation_loop en main.py). Si algun simbolo del lote no
        califica en IBKR (sin conId), se lo descarta antes de pedir tickers:
        un solo contrato sin conId no deberia tirar abajo el precio de todo
        el resto del lote."""
        if not symbols:
            return {}
        contracts = [Stock(_to_ib_symbol(s), "SMART", "USD") for s in symbols]
        await self.ib.qualifyContractsAsync(*contracts)
        contracts = [c for c in contracts if c.conId]
        if not contracts:
            return {}
        try:
            tickers = await self.ib.reqTickersAsync(*contracts)
        except Exception as exc:
            logger.warning("No se pudo pedir snapshot de precios para %s: %s", symbols, exc)
            return {}
        out: dict[str, float] = {}
        for t in tickers:
            if t.contract is None:
                continue
            price = _clean_price(t.marketPrice())
            if price is not None:
                out[_from_ib_symbol(t.contract.symbol)] = price
        return out

    def modify_stop_price(
        self, stop_order_id: int, new_stop_price: float, new_quantity: "float | None" = None,
    ) -> bool:
        """Sube (o ajusta) el precio de un stop-loss ya colocado, reenviando
        la MISMA orden (mismo orderId) con auxPrice actualizado: la API de
        IBKR trata un placeOrder sobre el orderId de una orden viva como una
        modificacion in-place, no como una orden nueva.

        new_quantity (opcional) resizea la orden a la vez -- necesario tras un
        scale-out parcial: si solo se ajusta el precio y se deja la cantidad
        original, el stop queda sobredimensionado para la posicion que
        realmente queda, y si se dispara mas tarde vende de mas (mismo patron
        que el incidente AEHR/TAP 2026-07-24, aca por cantidad en vez de por
        no cancelar).

        Devuelve False sin lanzar si la orden no se encuentra viva (ya se
        ejecuto/cancelo, o es de otra sesion -- self.ib.trades() solo cubre
        la sesion actual, misma limitacion que get_trade_fill): quien llama
        no debe tratar eso como una falla, el proximo chequeo de salida
        reconciliara la posicion si el stop ya se ejecuto del lado del
        broker."""
        for trade in self.ib.trades():
            if trade.order.orderId == stop_order_id:
                if trade.orderStatus.status in OrderStatus.DoneStates:
                    return False
                trade.order.auxPrice = new_stop_price
                if new_quantity is not None:
                    trade.order.totalQuantity = new_quantity
                self.ib.placeOrder(trade.contract, trade.order)
                return True
        return False

    async def cancel_resting_order(self, order_id: int) -> bool:
        """Cancela una orden viva en IBKR por su order_id, via
        reqAllOpenOrdersAsync en vez de buscar en self.ib.trades().

        self.ib.trades() (usado por has_live_protective_stop/modify_stop_price/
        get_trade_fill) solo ve ordenes que ESTA sesion vio colocarse o
        actualizarse; reqAllOpenOrdersAsync pide a IBKR el estado real de todas
        las ordenes vivas del cliente, incluidas las que quedaron resting de
        una sesion anterior (ej. tras un restart del backend). Necesario para
        poder cancelar con confianza el stop-loss protector de una posicion
        que se cierra por otro camino (ver _check_fund_exit): sin esto, un
        stop viejo puede seguir vivo en IBKR y dispararse mas tarde sobre una
        posicion que ya no existe, dejando una posicion corta no intencional
        (ver incidente AEHR/TAP 2026-07-24).

        Best-effort: si la orden ya no esta viva (ya se ejecuto o cancelo),
        no hace nada y devuelve False -- es el resultado esperado la mayoria
        de las veces, no un error."""
        for trade in await self.ib.reqAllOpenOrdersAsync():
            if trade.order.orderId == order_id:
                self.ib.cancelOrder(trade.order)
                return True
        return False

    async def get_live_protective_stops(self, symbol: str, side: str = "SELL") -> "list[int]":
        """Ids de todas las ordenes STOP protectoras vivas para `symbol`,
        consultando IBKR directamente via reqAllOpenOrdersAsync.

        A diferencia de has_live_protective_stop (que usa self.ib.trades() y
        queda ciego tras un reconnect que hace self.ib = IB()), este metodo
        consulta IBKR por el estado real de todas las ordenes vivas del cliente,
        incluidas las de sesiones anteriores al restart (mismo razonamiento que
        cancel_resting_order -- ver su docstring para la explicacion completa).

        `side` es la accion de la orden protectora: "SELL" para cubrir una
        posicion larga, "BUY" para cubrir una posicion corta (fix de A4 del
        NUEVO_INFORME: el stop protector de un short es una orden de compra).

        Devuelve lista de orderId; puede contener mas de 1 si hay stops
        duplicados creados tras un reconnect (ver cancel_duplicate_protective_
        stops para limpiarlos, y C1 del NUEVO_INFORME para la causa raiz)."""
        ib_symbol = _to_ib_symbol(symbol)
        result = []
        for trade in await self.ib.reqAllOpenOrdersAsync():
            if (
                trade.contract.symbol == ib_symbol
                and trade.order.action == side
                # IBKR reporta StopOrder como "STP" en ib_insync, no "STOP"
                and trade.order.orderType in ("STOP", "STP")
                and trade.orderStatus.status not in OrderStatus.DoneStates
            ):
                result.append(trade.order.orderId)
        return result

    def has_live_protective_stop(self, symbol: str) -> bool:
        """True si hay una orden STOP de venta viva para `symbol` en
        self.ib.trades() de esta sesion.

        OBSOLETO: usa self.ib.trades(), que queda vacio tras reconnect()
        (self.ib = IB()). Usar get_live_protective_stops() en su lugar, que
        consulta IBKR directamente y ve ordenes de sesiones anteriores.
        Conservado porque los tests existentes lo mockean directamente."""
        ib_symbol = _to_ib_symbol(symbol)
        for trade in self.ib.trades():
            if (
                trade.contract.symbol == ib_symbol
                and trade.order.action == "SELL"
                # IBKR reporta StopOrder como "STP" en ib_insync, no "STOP"
                and trade.order.orderType in ("STOP", "STP")
                and trade.orderStatus.status not in OrderStatus.DoneStates
            ):
                return True
        return False

    async def cancel_duplicate_protective_stops(
        self, symbol: str, side: str = "SELL", keep_order_id: "int | None" = None
    ) -> "list[int]":
        """Cancela stops protectores duplicados para `symbol`, conservando uno.

        Invariante central: 1 posicion <-> 1 stop protector. Si hay mas de uno
        (creados tras un reconnect que vacio self.ib.trades() y rompio el chequeo
        de idempotencia de has_live_protective_stop), cancela todos menos uno.

        keep_order_id: si esta en la lista de stops vivos, se conserva ese (para
        preservar el que ya tiene registrado el ledger del fondo). Si no se
        especifica o no esta en la lista viva, conserva el mas reciente (max
        orderId). Devuelve los ids cancelados."""
        live = await self.get_live_protective_stops(symbol, side)
        if len(live) <= 1:
            return []
        if keep_order_id is not None and keep_order_id in live:
            survivor = keep_order_id
        else:
            survivor = max(live)
        to_cancel = [oid for oid in live if oid != survivor]
        for order_id in to_cancel:
            await self.cancel_resting_order(order_id)
        return to_cancel

    async def place_protective_stop(
        self, symbol: str, quantity: float, stop_price: float, side: str = "SELL"
    ) -> "int | None":
        """Coloca un stop-loss STANDALONE (sin padre, sin bracket) para proteger
        una posicion existente que no tiene ningun stop vivo.

        `side` determina el tipo de orden: "SELL" para una posicion larga,
        "BUY" para una posicion corta (fix de A4: el stop protector de un
        short es una compra de cierre). quantity se pasa en valor absoluto.

        Devuelve el order_id de la nueva orden, o None si el contrato no
        califica en IBKR."""
        contract = Stock(_to_ib_symbol(symbol), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        if not contract.conId:
            return None
        stop = StopOrder(side, abs(quantity), stop_price)
        stop.tif = _DEFAULT_TIF
        self.ib.placeOrder(contract, stop)
        return stop.orderId

    async def close_short_position(self, symbol: str, quantity: float) -> dict:
        """Cierra una posición corta (short fantasma) con BUY MKT sin pasar por
        place_order ni por rules_engine. Solo para remediar shorts no intencionales
        creados por stops duplicados. Devuelve {"filled_qty": ..., "avg_fill_price": ...}."""
        contract = Stock(_to_ib_symbol(symbol), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        if not contract.conId:
            return {"filled_qty": 0.0, "avg_fill_price": None}
        order = MarketOrder("BUY", abs(quantity))
        order.tif = _DEFAULT_TIF
        trade = self.ib.placeOrder(contract, order)
        result = await self._wait_for_fill(trade.order.orderId, timeout=30.0)
        if result is None:
            return {"filled_qty": 0.0, "avg_fill_price": None}
        _status, filled, avg_price, _remaining = result
        return {"filled_qty": filled or 0.0, "avg_fill_price": avg_price}

    def subscribe_fill(
        self,
        order_id: int,
        on_fill: "Callable[[float, float], None]",
    ) -> bool:
        """Suscribe `on_fill(filled_qty, avg_fill_price)` al evento de fill de
        la orden `order_id`. Se invoca una sola vez cuando la orden llega a
        estado terminal con cantidad llenada > 0 (filledEvent si se llena
        completa, o cancelledEvent si IBKR la cancela tras un fill parcial).

        Cubre el caso en que _wait_for_fill venció el timeout antes de que
        llegara el fill real: la orden sigue viva en IBKR y este callback la
        captura cuando finalmente se ejecuta, dentro de la misma sesion de
        proceso. No sobrevive reinicios del backend (ver la reconciliacion de
        startup en main.py para cubrir ese escenario).

        Devuelve True si la orden se encontro en trades(), False si no (ej.
        ya no esta en la sesion actual o nunca se transmitio).
        """
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                _called = [False]  # guard para que el handler corra una sola vez

                def _handler(t=trade):
                    if _called[0]:
                        return
                    filled = t.orderStatus.filled
                    price = t.orderStatus.avgFillPrice
                    if filled > 0 and price:
                        _called[0] = True
                        on_fill(filled, price)

                trade.filledEvent += _handler
                trade.cancelledEvent += _handler
                return True
        return False

    def get_trade_fill(self, order_id: int) -> tuple[str, float, float | None, float] | None:
        """Estado y fill de una orden colocada esta sesion, por order_id.

        self.ib.trades() solo cubre la sesion actual del proceso (se pierde en
        un reconnect): limitacion aceptada, el seguimiento de fills es
        best-effort dentro de la misma sesion en la que se coloco la orden.
        Devuelve (status, filled_qty, avg_fill_price, remaining) o None si no
        se encuentra la orden. `remaining` (OrderStatus.remaining: lo que le
        falta llenar a ESTA orden puntual) es lo que permite reconciliar un
        stop-loss por fondo en vez de por el agregado de toda la cuenta -- ver
        _check_fund_exit en main.py.
        """
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                avg_price = trade.orderStatus.avgFillPrice
                status = trade.orderStatus.status
                if _is_spurious_cancel(trade):
                    # No es un estado terminal: se reporta como activo para que
                    # _wait_for_fill (y _check_fund_exit en main.py) sigan
                    # esperando el status real en vez de dar la orden por
                    # cancelada. Ver _is_spurious_cancel.
                    status = OrderStatus.PendingSubmit
                return (
                    status,
                    trade.orderStatus.filled,
                    avg_price if avg_price else None,
                    trade.orderStatus.remaining,
                )
        return None

    async def _wait_for_fill(
        self, order_id: int, timeout: float = 5.0, interval: float = 0.25
    ) -> tuple[str, float, float | None, float] | None:
        """Polea get_trade_fill(order_id) hasta que llegue a un estado
        terminal (OrderStatus.DoneStates) o venza `timeout`.

        Reemplaza los sleeps a ciegas que habia antes: con un sleep fijo, una
        orden que tardaba mas en llenar (o se llenaba mas rapido) quedaba
        registrada con el status/filled leido en un instante arbitrario, no
        el del fill real. Si vence el timeout sin llegar a un estado
        terminal, devuelve el ultimo estado visto (puede ser parcial o
        todavia en curso) en vez de bloquear indefinidamente.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        result = None
        while True:
            result = self.get_trade_fill(order_id)
            if result is not None and result[0] in OrderStatus.DoneStates:
                return result
            if loop.time() >= deadline:
                return result
            await asyncio.sleep(interval)

    def _register_stop_reconciliation(
        self, parent_trade, stop_trade, contract, requested_qty: float
    ) -> None:
        """Ajusta el stop-loss a la cantidad FINAL que el padre efectivamente
        llene, sin importar si eso pasa dentro de los 5s de _wait_for_fill o
        mucho despues (fill tardio).

        Reemplaza la logica vieja, que resolvia esto una sola vez con el
        snapshot de los 5s de _wait_for_fill: si el padre todavia no habia
        llenado nada en ese instante, cancelaba el stop asumiendo que la
        orden ya no iba a llenar -- pero una orden LMT puede seguir viva y
        llenar minutos despues (ver subscribe_fill/_on_late_fill en main.py),
        y en ese caso la posicion quedaba abierta sin ninguna proteccion real
        en IBKR (el ledger del fondo seguia mostrando un stop_order_id que
        para entonces ya estaba cancelado). Este callback se dispara recien
        cuando el PADRE llega a su propio estado terminal (lleno del todo o
        cancelado), sea cuando sea que eso ocurra, y es la UNICA fuente de
        verdad para cancelar/resizear el stop -- place_order ya no lo hace
        con el snapshot de la espera sincronica.

        Si el padre nunca llena nada, cancela el stop (no debe quedar un stop
        huerfano sin posicion que proteger). Si llena parcialmente, resizea
        el stop a la cantidad real. Si llena exactamente lo pedido, no hace
        falta tocarlo (ya quedo bien dimensionado desde que se coloco).

        Chequea el estado ACTUAL antes de suscribirse: si el padre ya esta en
        un estado terminal en el momento de registrar (ej. lleno casi al
        instante, dentro del asyncio.sleep(0.2) previo), el evento ya pudo
        haber disparado antes de que este codigo se suscribiera y nunca
        volveria a hacerlo -- correr el ajuste de inmediato en ese caso evita
        esa ventana de carrera, ademas de cubrir el caso normal (sincronico)
        en el que _wait_for_fill ya vio el estado final."""
        _done = [False]  # guard para que el ajuste corra una sola vez

        def _handler(t=parent_trade):
            if _done[0]:
                return
            if _is_spurious_cancel(t):
                # cancelledEvent tambien se emite en el cancel falso (ib_async/
                # wrapper.py:1668). Salir SIN consumir el guard: la orden sigue
                # viva y su evento terminal real todavia va a llegar.
                return
            _done[0] = True
            filled = t.orderStatus.filled
            if stop_trade.orderStatus.status in OrderStatus.DoneStates:
                return  # el stop ya termino solo (se disparo, o lo cancelaron a mano)
            if filled <= 0:
                self.ib.cancelOrder(stop_trade.order)
            elif filled != requested_qty:
                stop_trade.order.totalQuantity = filled
                self.ib.placeOrder(contract, stop_trade.order)

        if (
            parent_trade.orderStatus.status in OrderStatus.DoneStates
            and not _is_spurious_cancel(parent_trade)
        ):
            _handler()
            return
        parent_trade.filledEvent += _handler
        parent_trade.cancelledEvent += _handler

    async def place_order(self, order: OrderRequest) -> dict:
        contract = Stock(_to_ib_symbol(order.symbol), "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        if order.order_type == OrderType.LMT:
            parent = LimitOrder(order.side.value, order.quantity, order.limit_price)
        else:
            parent = MarketOrder(order.side.value, order.quantity)
        parent.tif = _DEFAULT_TIF

        if order.stop_loss_price:
            # Orden padre + stop-loss encadenado: el padre no se transmite hasta
            # que el hijo (stop-loss) esta listo, asi nunca queda una posicion
            # abierta sin su proteccion.
            parent.transmit = False
            parent_trade = self.ib.placeOrder(contract, parent)
            await asyncio.sleep(0.2)

            protective_side = Side.SELL if order.side == Side.BUY else Side.BUY
            stop = StopOrder(protective_side.value, order.quantity, order.stop_loss_price)
            stop.tif = _DEFAULT_TIF
            stop.parentId = parent.orderId
            stop.transmit = True
            stop_trade = self.ib.placeOrder(contract, stop)

            # Registrado ANTES de esperar: el ajuste final del stop (cancelar
            # si el padre nunca llena, resizear si llena parcial) se resuelve
            # por evento cuando el padre llegue a SU PROPIO estado terminal,
            # no con el snapshot de la espera sincronica de abajo (ver
            # _register_stop_reconciliation para el detalle de por que esto
            # importa con un fill tardio).
            self._register_stop_reconciliation(parent_trade, stop_trade, contract, order.quantity)

            # Espera el fill real del padre (hasta DoneStates) en vez de un
            # sleep a ciegas: ver _wait_for_fill.
            fill = await self._wait_for_fill(parent.orderId)

            # IBKR puede rechazar/cancelar el stop (precio invalido, regla del
            # mercado, etc). Si eso pasa, NO devolvemos "ok": la posicion padre
            # puede haber quedado abierta sin proteccion y eso hay que tratarlo
            # como una falla critica, no silenciarlo.
            bad_statuses = {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
            stop_status = stop_trade.orderStatus.status
            # Calculamos el fill del padre ANTES de decidir si lanzar la excepcion:
            # si el stop fallo pero el padre ya lleno (total o parcialmente), quien
            # capture StopLossRejectedError necesita saber cuanto se compro para
            # poder registrar la posicion en el fondo y poner un stop de emergencia.
            _fill_for_exc = fill if fill is not None else (
                parent_trade.orderStatus.status,
                parent_trade.orderStatus.filled,
                parent_trade.orderStatus.avgFillPrice or None,
                parent_trade.orderStatus.remaining,
            )
            if stop_status in bad_statuses:
                raise StopLossRejectedError(
                    f"El stop-loss fue rechazado/cancelado por IBKR (estado: {stop_status}). "
                    f"La orden principal (id {parent.orderId}) puede haber quedado activa SIN "
                    f"proteccion. Revisa la posicion manualmente en TWS antes de seguir operando.",
                    order_id=parent.orderId,
                    stop_order_id=stop.orderId,
                    filled_qty=_fill_for_exc[1] if _fill_for_exc else 0.0,
                    avg_fill_price=_fill_for_exc[2] if _fill_for_exc else None,
                )

            status, filled_qty, avg_fill_price, _remaining = fill if fill is not None else (
                parent_trade.orderStatus.status,
                parent_trade.orderStatus.filled,
                parent_trade.orderStatus.avgFillPrice or None,
                parent_trade.orderStatus.remaining,
            )

            return {
                "order_id": parent.orderId,
                "stop_order_id": stop.orderId,
                "status": status,
                "stop_status": stop_status,
                "filled_qty": filled_qty,
                "avg_fill_price": avg_fill_price,
            }

        trade = self.ib.placeOrder(contract, parent)
        fill = await self._wait_for_fill(parent.orderId)
        status, filled_qty, avg_fill_price, _remaining = fill if fill is not None else (
            trade.orderStatus.status,
            trade.orderStatus.filled,
            trade.orderStatus.avgFillPrice or None,
            trade.orderStatus.remaining,
        )
        return {
            "order_id": parent.orderId,
            "status": status,
            "filled_qty": filled_qty,
            "avg_fill_price": avg_fill_price,
        }
