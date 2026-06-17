from __future__ import annotations

import asyncio

from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder

from .models import AccountSummary, OrderRequest, OrderType, Position, Side


class IBKRConnectionError(RuntimeError):
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
        self.ib = IB()

    async def connect(self) -> None:
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=10)
            # Datos demorados por defecto: funcionan sin suscripcion de market data
            # en tiempo real. Cambia a reqMarketDataType(1) si tienes suscripciones.
            self.ib.reqMarketDataType(3)
        except Exception as exc:
            raise IBKRConnectionError(
                f"No se pudo conectar a IBKR en {self.host}:{self.port}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def get_account_summary(self) -> AccountSummary:
        # Lectura de cache mantenido en background por ib_async; no requiere await.
        tags = self.ib.accountSummary()
        values = {t.tag: t.value for t in tags}
        net_liq = float(values.get("NetLiquidation", 0) or 0)
        cash = float(values.get("TotalCashValue", 0) or 0)
        buying_power = float(values.get("BuyingPower", 0) or 0)
        realized = float(values.get("RealizedPnL", 0) or 0)
        unrealized = float(values.get("UnrealizedPnL", 0) or 0)
        daily_pnl = realized + unrealized
        daily_pnl_pct = (daily_pnl / net_liq * 100) if net_liq else 0.0
        return AccountSummary(
            net_liquidation=net_liq,
            cash=cash,
            buying_power=buying_power,
            daily_pnl=daily_pnl,
            daily_pnl_pct=daily_pnl_pct,
        )

    async def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for p in self.ib.positions():
            market_price = None
            unrealized = None
            try:
                tickers = await self.ib.reqTickersAsync(p.contract)
                if tickers and tickers[0].marketPrice() == tickers[0].marketPrice():  # not NaN
                    market_price = tickers[0].marketPrice()
                    unrealized = (market_price - p.avgCost) * p.position
            except Exception:
                pass
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
            self.ib.placeOrder(contract, stop)
            await asyncio.sleep(0.5)
            return {
                "order_id": parent.orderId,
                "stop_order_id": stop.orderId,
                "status": parent_trade.orderStatus.status,
            }

        trade = self.ib.placeOrder(contract, parent)
        await asyncio.sleep(0.5)
        return {"order_id": parent.orderId, "status": trade.orderStatus.status}
