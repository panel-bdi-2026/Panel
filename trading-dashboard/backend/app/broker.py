from __future__ import annotations

import asyncio

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, Ticker

from .models import AccountSummary, OrderRequest, OrderType, Position, Side


class IBKRConnectionError(RuntimeError):
    pass


class StopLossRejectedError(RuntimeError):
    """La orden padre se transmitio pero IBKR rechazo/cancelo el stop-loss
    asociado. La posicion puede haber quedado abierta sin proteccion --
    quien llama debe tratar esto como una falla critica, no como exito."""
    pass


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

    def __init__(self, host: str, port: int, client_id: int):
        self.host = host
        self.port = port
        self.client_id = client_id
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
            # Datos demorados por defecto: funcionan sin suscripcion de market data
            # en tiempo real. Cambia a reqMarketDataType(1) si tienes suscripciones.
            self.ib.reqMarketDataType(3)
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
        except Exception:
            tickers = []
        price_by_conid = {
            t.contract.conId: t.marketPrice()
            for t in tickers
            if t.contract and t.marketPrice() == t.marketPrice()  # not NaN
        }

        out: list[Position] = []
        for p in positions:
            market_price = price_by_conid.get(p.contract.conId)
            unrealized = (market_price - p.avgCost) * p.position if market_price is not None else None
            out.append(Position(
                symbol=p.contract.symbol,
                quantity=p.position,
                avg_cost=p.avgCost,
                market_price=market_price,
                unrealized_pnl=unrealized,
            ))
        return out

    def get_position_qty(self, symbol: str) -> float:
        for p in self.ib.positions():
            if p.contract.symbol == symbol:
                return p.position
        return 0.0

    async def get_reference_price(self, symbol: str) -> float | None:
        contract = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        tickers = await self.ib.reqTickersAsync(contract)
        if not tickers:
            return None
        price = tickers[0].marketPrice()
        return price if price == price else None  # filtra NaN

    async def stream_subscribe(self, symbols: list[str]) -> None:
        """Abre suscripciones de streaming persistente para `symbols` que
        todavia no la tengan (ignora los que ya estan suscriptos: reqMktData
        de nuevo sobre el mismo contrato duplicaria la linea sin necesidad).
        No hay limite de "refrescos": una vez suscripto, Ticker.marketPrice()
        (ver get_live_price) se mantiene actualizado solo mientras dure la
        conexion, ocupando una sola linea de market data por simbolo."""
        new_symbols = [s for s in symbols if s not in self._live_tickers]
        if not new_symbols:
            return
        contracts = [Stock(s, "SMART", "USD") for s in new_symbols]
        await self.ib.qualifyContractsAsync(*contracts)
        for symbol, contract in zip(new_symbols, contracts):
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
        price = ticker.marketPrice()
        return price if price == price else None  # filtra NaN

    async def get_snapshot_prices(self, symbols: list[str]) -> dict[str, float]:
        """Snapshot de precio para un lote de simbolos en una sola llamada
        (igual que get_positions: reqTickersAsync libera cada linea apenas
        llega el snapshot, a diferencia de stream_subscribe). Pensado para
        rotar el resto del universo -- el que no esta en el hot-set -- con las
        lineas de market data que el hot-set deja libres (ver
        _price_rotation_loop en main.py)."""
        if not symbols:
            return {}
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        await self.ib.qualifyContractsAsync(*contracts)
        try:
            tickers = await self.ib.reqTickersAsync(*contracts)
        except Exception:
            return {}
        out: dict[str, float] = {}
        for t in tickers:
            if t.contract is None:
                continue
            price = t.marketPrice()
            if price == price:  # filtra NaN
                out[t.contract.symbol] = price
        return out

    def modify_stop_price(self, stop_order_id: int, new_stop_price: float) -> bool:
        """Sube (o ajusta) el precio de un stop-loss ya colocado, reenviando
        la MISMA orden (mismo orderId) con auxPrice actualizado: la API de
        IBKR trata un placeOrder sobre el orderId de una orden viva como una
        modificacion in-place, no como una orden nueva.

        Devuelve False sin lanzar si la orden no se encuentra viva (ya se
        ejecuto/cancelo, o es de otra sesion -- self.ib.trades() solo cubre
        la sesion actual, misma limitacion que get_trade_fill): quien llama
        no debe tratar eso como una falla, el proximo chequeo de salida
        reconciliara la posicion si el stop ya se ejecuto del lado del
        broker."""
        from ib_async.order import OrderStatus

        for trade in self.ib.trades():
            if trade.order.orderId == stop_order_id:
                if trade.orderStatus.status in OrderStatus.DoneStates:
                    return False
                trade.order.auxPrice = new_stop_price
                self.ib.placeOrder(trade.contract, trade.order)
                return True
        return False

    def get_trade_fill(self, order_id: int) -> tuple[str, float, float | None] | None:
        """Estado y fill de una orden colocada esta sesion, por order_id.

        self.ib.trades() solo cubre la sesion actual del proceso (se pierde en
        un reconnect): limitacion aceptada, el seguimiento de fills es
        best-effort dentro de la misma sesion en la que se coloco la orden.
        Devuelve (status, filled_qty, avg_fill_price) o None si no se
        encuentra la orden.
        """
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                avg_price = trade.orderStatus.avgFillPrice
                return (
                    trade.orderStatus.status,
                    trade.orderStatus.filled,
                    avg_price if avg_price else None,
                )
        return None

    async def _wait_for_fill(
        self, order_id: int, timeout: float = 5.0, interval: float = 0.25
    ) -> tuple[str, float, float | None] | None:
        """Polea get_trade_fill(order_id) hasta que llegue a un estado
        terminal (OrderStatus.DoneStates) o venza `timeout`.

        Reemplaza los sleeps a ciegas que habia antes: con un sleep fijo, una
        orden que tardaba mas en llenar (o se llenaba mas rapido) quedaba
        registrada con el status/filled leido en un instante arbitrario, no
        el del fill real. Si vence el timeout sin llegar a un estado
        terminal, devuelve el ultimo estado visto (puede ser parcial o
        todavia en curso) en vez de bloquear indefinidamente.
        """
        from ib_async.order import OrderStatus

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        result = None
        while True:
            result = self.get_trade_fill(order_id)
            if result is not None and result[0] in OrderStatus.DoneStates:
                return result
            if loop.time() >= deadline:
                return result
            await asyncio.sleep(interval)

    async def place_order(self, order: OrderRequest) -> dict:
        contract = Stock(order.symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        if order.order_type == OrderType.LMT:
            parent = LimitOrder(order.side.value, order.quantity, order.limit_price)
        else:
            parent = MarketOrder(order.side.value, order.quantity)

        if order.stop_loss_price:
            # Orden padre + stop-loss encadenado: el padre no se transmite hasta
            # que el hijo (stop-loss) esta listo, asi nunca queda una posicion
            # abierta sin su proteccion.
            parent.transmit = False
            parent_trade = self.ib.placeOrder(contract, parent)
            await asyncio.sleep(0.2)

            protective_side = Side.SELL if order.side == Side.BUY else Side.BUY
            stop = StopOrder(protective_side.value, order.quantity, order.stop_loss_price)
            stop.parentId = parent.orderId
            stop.transmit = True
            stop_trade = self.ib.placeOrder(contract, stop)

            # Espera el fill real del padre (hasta DoneStates) en vez de un
            # sleep a ciegas: ver _wait_for_fill.
            fill = await self._wait_for_fill(parent.orderId)

            # IBKR puede rechazar/cancelar el stop (precio invalido, regla del
            # mercado, etc). Si eso pasa, NO devolvemos "ok": la posicion padre
            # puede haber quedado abierta sin proteccion y eso hay que tratarlo
            # como una falla critica, no silenciarlo.
            bad_statuses = {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
            stop_status = stop_trade.orderStatus.status
            if stop_status in bad_statuses:
                raise StopLossRejectedError(
                    f"El stop-loss fue rechazado/cancelado por IBKR (estado: {stop_status}). "
                    f"La orden principal (id {parent.orderId}) puede haber quedado activa SIN "
                    f"proteccion. Revisa la posicion manualmente en TWS antes de seguir operando."
                )

            status, filled_qty, avg_fill_price = fill if fill is not None else (
                parent_trade.orderStatus.status,
                parent_trade.orderStatus.filled,
                parent_trade.orderStatus.avgFillPrice or None,
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
        status, filled_qty, avg_fill_price = fill if fill is not None else (
            trade.orderStatus.status,
            trade.orderStatus.filled,
            trade.orderStatus.avgFillPrice or None,
        )
        return {
            "order_id": parent.orderId,
            "status": status,
            "filled_qty": filled_qty,
            "avg_fill_price": avg_fill_price,
        }
