from __future__ import annotations

import asyncio

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

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

    def get_account_summary(self) -> AccountSummary:
        # Lectura de cache mantenido en background por ib_async; no requiere await.
        tags = self.ib.accountSummary()
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
        if pnl_list:
            raw = pnl_list[0].dailyPnL
            daily_pnl = float(raw) if raw is not None and raw == raw else 0.0  # filtra NaN
        daily_pnl_pct = (daily_pnl / net_liq * 100) if net_liq else 0.0
        return AccountSummary(
            net_liquidation=net_liq,
            cash=cash,
            buying_power=buying_power,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
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
            await asyncio.sleep(0.5)

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
            return {
                "order_id": parent.orderId,
                "stop_order_id": stop.orderId,
                "status": parent_trade.orderStatus.status,
                "stop_status": stop_status,
            }

        trade = self.ib.placeOrder(contract, parent)
        await asyncio.sleep(0.5)
        return {"order_id": parent.orderId, "status": trade.orderStatus.status}
