import asyncio

import pytest

from ib_async.order import OrderStatus

from app.broker import (
    IBKRBroker,
    StopLossRejectedError,
    _from_ib_symbol,
    _is_spurious_cancel,
    _to_ib_symbol,
)
from app.models import OrderRequest, OrderType, Side


class FakeContract:
    def __init__(self, con_id, symbol):
        self.conId = con_id
        self.symbol = symbol


class FakePosition:
    def __init__(self, con_id, symbol, position, avg_cost):
        self.contract = FakeContract(con_id, symbol)
        self.position = position
        self.avgCost = avg_cost


class FakeTicker:
    def __init__(self, contract, price):
        self.contract = contract
        self._price = price

    def marketPrice(self):
        return self._price


@pytest.fixture
def broker() -> IBKRBroker:
    return IBKRBroker("127.0.0.1", 7497, 17)


def test_market_data_type_defaults_to_delayed():
    broker = IBKRBroker("127.0.0.1", 7497, 17)
    assert broker.market_data_type == 3


def test_market_data_type_configurable_via_constructor():
    broker = IBKRBroker("127.0.0.1", 7497, 17, market_data_type=1)
    assert broker.market_data_type == 1


def test_connect_requests_configured_market_data_type(monkeypatch):
    broker = IBKRBroker("127.0.0.1", 7497, 17, market_data_type=1)

    async def fake_connect_async(*args, **kwargs):
        return None

    calls = []
    monkeypatch.setattr(broker.ib, "connectAsync", fake_connect_async)
    monkeypatch.setattr(broker.ib, "reqMarketDataType", lambda t: calls.append(t))
    monkeypatch.setattr(broker.ib, "managedAccounts", lambda: [])

    asyncio.run(broker.connect())

    assert calls == [1]


def test_get_positions_returns_empty_list_without_calling_ib(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "positions", lambda: [])

    async def fail_if_called(*contracts, **kwargs):
        raise AssertionError("no deberia pedir tickers si no hay posiciones")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fail_if_called)
    assert asyncio.run(broker.get_positions()) == []


def test_get_positions_batches_a_single_ticker_request(broker, monkeypatch):
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    pos2 = FakePosition(2, "MSFT", 5, 300.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1, pos2])

    calls = []

    async def fake_req_tickers(*contracts, **kwargs):
        calls.append(contracts)
        return [FakeTicker(pos1.contract, 155.0), FakeTicker(pos2.contract, 290.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_positions())

    assert len(calls) == 1  # una sola llamada batched, no una por posicion
    assert len(calls[0]) == 2

    by_symbol = {p.symbol: p for p in out}
    assert by_symbol["AAPL"].market_price == 155.0
    assert by_symbol["AAPL"].unrealized_pnl == pytest.approx((155.0 - 150.0) * 10)
    assert by_symbol["MSFT"].market_price == 290.0
    assert by_symbol["MSFT"].unrealized_pnl == pytest.approx((290.0 - 300.0) * 5)


def test_get_positions_handles_ticker_request_failure(broker, monkeypatch):
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1])

    async def failing_req_tickers(*contracts, **kwargs):
        raise RuntimeError("pacing violation")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", failing_req_tickers)

    out = asyncio.run(broker.get_positions())
    assert len(out) == 1
    assert out[0].symbol == "AAPL"
    assert out[0].market_price is None
    assert out[0].unrealized_pnl is None


def test_get_positions_treats_nan_price_as_missing(broker, monkeypatch):
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1])

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(pos1.contract, float("nan"))]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_positions())
    assert out[0].market_price is None
    assert out[0].unrealized_pnl is None


def test_get_positions_treats_negative_price_as_missing(broker, monkeypatch):
    """Con datos demorados (reqMarketDataType(3)) IBKR puede mandar -1 como
    valor real de last/close para indicar "sin dato disponible" en vez de
    NaN: esto pasaba el viejo filtro de NaN sin chequeo aparte."""
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1])

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(pos1.contract, -1.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_positions())
    assert out[0].market_price is None
    assert out[0].unrealized_pnl is None


def test_get_position_qty_returns_zero_when_no_match(broker, monkeypatch):
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1])
    assert broker.get_position_qty("MSFT") == 0.0
    assert broker.get_position_qty("AAPL") == 10


class FakeOrder:
    def __init__(self, order_id, aux_price, total_quantity=0.0):
        self.orderId = order_id
        self.auxPrice = aux_price
        self.totalQuantity = total_quantity


class FakeOrderStatus:
    def __init__(self, status, filled=0.0, remaining=0.0, avg_fill_price=0.0):
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = avg_fill_price


class _LogEntry:
    """Como TradeLogEntry de ib_async: solo los dos campos que mira
    _is_spurious_cancel."""

    def __init__(self, status, error_code=0):
        self.status = status
        self.errorCode = error_code


class FakeTrade:
    def __init__(
        self,
        order_id,
        status,
        aux_price=90.0,
        contract="AAPL contract",
        filled=0.0,
        remaining=0.0,
        avg_fill_price=0.0,
        total_quantity=0.0,
        log=None,
    ):
        self.order = FakeOrder(order_id, aux_price, total_quantity)
        self.orderStatus = FakeOrderStatus(status, filled, remaining, avg_fill_price)
        self.contract = contract
        self.log = log if log is not None else []


def test_modify_stop_price_resubmits_same_order_id_with_new_aux_price(broker, monkeypatch):
    trade = FakeTrade(order_id=42, status="Submitted", aux_price=90.0)
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    placed = []
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: placed.append((contract, order)))

    assert broker.modify_stop_price(42, 95.0) is True
    assert trade.order.auxPrice == 95.0
    assert len(placed) == 1
    assert placed[0][1].orderId == 42  # misma orden, no una nueva


def test_modify_stop_price_returns_false_when_order_not_found(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "trades", lambda: [])

    def fail_if_called(contract, order):
        raise AssertionError("no deberia colocar ninguna orden")

    monkeypatch.setattr(broker.ib, "placeOrder", fail_if_called)
    assert broker.modify_stop_price(42, 95.0) is False


def test_modify_stop_price_returns_false_when_order_already_done(broker, monkeypatch):
    trade = FakeTrade(order_id=42, status="Filled", aux_price=90.0)
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    def fail_if_called(contract, order):
        raise AssertionError("no deberia modificar una orden ya terminada")

    monkeypatch.setattr(broker.ib, "placeOrder", fail_if_called)
    assert broker.modify_stop_price(42, 95.0) is False
    assert trade.order.auxPrice == 90.0  # no se toco


def test_modify_stop_price_resizes_quantity_when_new_quantity_given(broker, monkeypatch):
    # Necesario tras un scale-out parcial: si solo se ajustara el precio, el
    # stop quedaria dimensionado para la cantidad ORIGINAL de la posicion, no
    # para lo que realmente queda -- si se dispara despues, vende de mas (ver
    # incidente AEHR/TAP 2026-07-24).
    trade = FakeTrade(order_id=42, status="Submitted", aux_price=90.0, total_quantity=10.0)
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: None)

    assert broker.modify_stop_price(42, 95.0, new_quantity=5.0) is True
    assert trade.order.totalQuantity == 5.0


def test_modify_stop_price_leaves_quantity_untouched_when_not_given(broker, monkeypatch):
    trade = FakeTrade(order_id=42, status="Submitted", aux_price=90.0, total_quantity=10.0)
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: None)

    assert broker.modify_stop_price(42, 95.0) is True
    assert trade.order.totalQuantity == 10.0


def test_cancel_resting_order_cancels_when_found(broker, monkeypatch):
    trade = FakeTrade(order_id=42, status="PreSubmitted")

    async def fake_req_all_open_orders():
        return [trade]

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all_open_orders)
    cancelled = []
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order))

    assert asyncio.run(broker.cancel_resting_order(42)) is True
    assert cancelled == [trade.order]


def test_cancel_resting_order_returns_false_when_not_found(broker, monkeypatch):
    # Caso esperado la mayoria de las veces: la orden ya se ejecuto/cancelo
    # sola antes de que hiciera falta cancelarla a mano -- no debe tratarse
    # como un error.
    async def fake_req_all_open_orders():
        return []

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all_open_orders)

    def fail_if_called(order):
        raise AssertionError("no deberia intentar cancelar nada")

    monkeypatch.setattr(broker.ib, "cancelOrder", fail_if_called)

    assert asyncio.run(broker.cancel_resting_order(42)) is False


def test_cancel_resting_order_finds_orders_from_a_previous_session(broker, monkeypatch):
    # A diferencia de modify_stop_price/has_live_protective_stop (que miran
    # self.ib.trades(), vacio tras un restart), cancel_resting_order usa
    # reqAllOpenOrdersAsync -- debe encontrar la orden aunque self.ib.trades()
    # este vacio, como pasaria justo despues de reconectar.
    trade = FakeTrade(order_id=99, status="PreSubmitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [])  # sesion actual vacia

    async def fake_req_all_open_orders():
        return [trade]

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all_open_orders)
    cancelled = []
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order))

    assert asyncio.run(broker.cancel_resting_order(99)) is True
    assert cancelled == [trade.order]


class _StopCheckContract:
    def __init__(self, symbol, con_id=1):
        self.symbol = symbol
        self.conId = con_id


class _StopCheckOrder:
    def __init__(self, action, order_type):
        self.action = action
        self.orderType = order_type


class _StopCheckTrade:
    def __init__(self, symbol, action, order_type, status):
        self.contract = _StopCheckContract(symbol)
        self.order = _StopCheckOrder(action, order_type)
        self.orderStatus = FakeOrderStatus(status)


def test_has_live_protective_stop_true_when_live_sell_stop_exists(broker, monkeypatch):
    trade = _StopCheckTrade("AAPL", "SELL", "STOP", "Submitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("AAPL") is True


def test_has_live_protective_stop_false_when_stop_already_done(broker, monkeypatch):
    trade = _StopCheckTrade("AAPL", "SELL", "STOP", "Filled")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("AAPL") is False


def test_has_live_protective_stop_false_when_no_matching_symbol(broker, monkeypatch):
    trade = _StopCheckTrade("MSFT", "SELL", "STOP", "Submitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("AAPL") is False


def test_has_live_protective_stop_false_when_order_is_not_a_stop(broker, monkeypatch):
    trade = _StopCheckTrade("AAPL", "SELL", "LMT", "Submitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("AAPL") is False


def test_has_live_protective_stop_false_when_order_is_a_buy(broker, monkeypatch):
    # Un STOP de COMPRA (ej. para cerrar un short) no protege una posicion
    # larga -- solo cuenta un stop de VENTA.
    trade = _StopCheckTrade("AAPL", "BUY", "STOP", "Submitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("AAPL") is False


def test_has_live_protective_stop_matches_class_share_symbol_despite_space(broker, monkeypatch):
    # IBKR reporta el contrato con espacio ("BRK B"), no guion: has_live_
    # protective_stop debe convertir el simbolo de entrada de la misma forma
    # que el resto del broker (ver _to_ib_symbol).
    trade = _StopCheckTrade("BRK B", "SELL", "STOP", "Submitted")
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])
    assert broker.has_live_protective_stop("BRK-B") is True


# ---------------------------------------------------------------------------
# get_live_protective_stops: fuente de verdad post-reconnect.
# A diferencia de has_live_protective_stop (que usa trades(), vacio tras un
# reconnect), este metodo usa reqAllOpenOrdersAsync y ve TODAS las ordenes
# vivas del cliente. Es la clave del fix de C1 del NUEVO_INFORME.
# ---------------------------------------------------------------------------

class _LiveStopTrade:
    """Trade con los campos que lee get_live_protective_stops."""
    def __init__(self, symbol, order_id, action, order_type, status):
        self.contract = _StopCheckContract(symbol)
        self.order = type("Order", (), {
            "orderId": order_id, "action": action, "orderType": order_type
        })()
        self.orderStatus = FakeOrderStatus(status)


def test_get_live_protective_stops_finds_orders_invisible_to_trades(broker, monkeypatch):
    # El escenario del incidente: trades() esta vacio tras un reconnect,
    # pero el stop sigue vivo en IBKR y reqAllOpenOrdersAsync lo ve.
    # ESTE TEST DEBIA FALLAR contra el codigo anterior (has_live_protective_stop).
    monkeypatch.setattr(broker.ib, "trades", lambda: [])  # trades() vacio: post-reconnect

    live_trade = _LiveStopTrade("AAPL", order_id=42, action="SELL", order_type="STP", status="Submitted")

    async def fake_req_all():
        return [live_trade]

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all)

    result = asyncio.run(broker.get_live_protective_stops("AAPL"))
    assert result == [42], "debe encontrar el stop aunque trades() este vacio"


def test_get_live_protective_stops_detects_buy_stop_for_short_position(broker, monkeypatch):
    # Fix de A4: el stop protector de una posicion corta es una orden BUY.
    buy_stop = _LiveStopTrade("TSLA", order_id=88, action="BUY", order_type="STP", status="Submitted")

    async def fake_req_all():
        return [buy_stop]

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all)

    # Con side="BUY" debe encontrarlo; con "SELL" no.
    assert asyncio.run(broker.get_live_protective_stops("TSLA", side="BUY")) == [88]
    assert asyncio.run(broker.get_live_protective_stops("TSLA", side="SELL")) == []


def test_cancel_duplicate_protective_stops_leaves_exactly_one_and_cancels_rest(broker, monkeypatch):
    # 3 stops duplicados para AAPL -> debe quedar 1 y cancelar 2.
    stops = [
        _LiveStopTrade("AAPL", order_id=10, action="SELL", order_type="STP", status="Submitted"),
        _LiveStopTrade("AAPL", order_id=20, action="SELL", order_type="STP", status="Submitted"),
        _LiveStopTrade("AAPL", order_id=30, action="SELL", order_type="STP", status="Submitted"),
    ]

    async def fake_req_all():
        return stops

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all)
    cancelled = []
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order.orderId))

    result = asyncio.run(broker.cancel_duplicate_protective_stops("AAPL"))

    # Conserva el max orderId (30), cancela los otros dos.
    assert 30 not in result, "el sobreviviente no debe aparecer en la lista de cancelados"
    assert set(result) == {10, 20}
    assert len(cancelled) == 2


def test_cancel_duplicate_protective_stops_does_nothing_with_single_stop(broker, monkeypatch):
    stop = _LiveStopTrade("AAPL", order_id=55, action="SELL", order_type="STP", status="Submitted")

    async def fake_req_all():
        return [stop]

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all)

    def fail_if_called(order):
        raise AssertionError("no debe cancelar nada si solo hay un stop")

    monkeypatch.setattr(broker.ib, "cancelOrder", fail_if_called)

    result = asyncio.run(broker.cancel_duplicate_protective_stops("AAPL"))
    assert result == []


def test_cancel_duplicate_protective_stops_respects_keep_order_id(broker, monkeypatch):
    # Si keep_order_id esta en la lista, debe conservarse ESE, aunque no sea el maximo.
    stops = [
        _LiveStopTrade("AAPL", order_id=100, action="SELL", order_type="STP", status="Submitted"),
        _LiveStopTrade("AAPL", order_id=200, action="SELL", order_type="STP", status="Submitted"),
    ]

    async def fake_req_all():
        return stops

    monkeypatch.setattr(broker.ib, "reqAllOpenOrdersAsync", fake_req_all)
    cancelled = []
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order.orderId))

    # keep=100: debe conservarse 100 y cancelarse 200 (aunque 200 > 100).
    result = asyncio.run(broker.cancel_duplicate_protective_stops("AAPL", keep_order_id=100))
    assert result == [200]
    assert cancelled == [200]


async def _noop_qualify(*contracts, **kwargs):
    """Simula una calificacion exitosa: IBKR asigna un conId real a cada
    contrato (a diferencia de un contrato recien construido, que arranca con
    conId=0/sin calificar)."""
    for i, c in enumerate(contracts, start=1):
        c.conId = i
    return list(contracts)


def test_place_protective_stop_places_standalone_sell_stop_and_returns_order_id(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)
    placed = []

    def fake_place_order(contract, order):
        order.orderId = 999
        placed.append((contract, order))

    monkeypatch.setattr(broker.ib, "placeOrder", fake_place_order)

    order_id = asyncio.run(broker.place_protective_stop("AAPL", 10, 145.0))

    assert order_id == 999
    assert len(placed) == 1
    contract, order = placed[0]
    assert contract.symbol == "AAPL"
    assert order.action == "SELL"
    assert order.totalQuantity == 10
    assert order.auxPrice == 145.0
    # Standalone: sin padre, sin bracket.
    assert getattr(order, "parentId", 0) == 0


def test_place_protective_stop_returns_none_when_contract_fails_to_qualify(broker, monkeypatch):
    async def fail_to_qualify(*contracts, **kwargs):
        return list(contracts)  # conId se queda en 0

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", fail_to_qualify)

    def fail_if_called(contract, order):
        raise AssertionError("no deberia colocar una orden para un contrato sin calificar")

    monkeypatch.setattr(broker.ib, "placeOrder", fail_if_called)

    order_id = asyncio.run(broker.place_protective_stop("BADSYM", 10, 145.0))
    assert order_id is None


def test_stream_subscribe_opens_streaming_only_for_new_symbols(broker, monkeypatch):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), 150.0)
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    req_calls = []

    def fake_req_mkt_data(contract, generic_ticks, snapshot, regulatory):
        req_calls.append(contract)
        return FakeTicker(contract, None)

    monkeypatch.setattr(broker.ib, "reqMktData", fake_req_mkt_data)

    asyncio.run(broker.stream_subscribe(["AAPL", "MSFT"]))

    assert len(req_calls) == 1  # AAPL ya estaba suscripto, no se vuelve a pedir
    assert req_calls[0].symbol == "MSFT"
    assert set(broker._live_tickers.keys()) == {"AAPL", "MSFT"}


def test_stream_subscribe_skips_symbols_that_fail_to_qualify(broker, monkeypatch):
    """Un simbolo que IBKR no puede calificar (ej. TERN) queda con conId=0 y
    no se le puede pedir reqMktData (revienta con un error de hash). Antes
    esto abortaba la suscripcion de TODO el lote, incluidos los simbolos
    validos; ahora se omite solo el que falla."""
    async def qualify_all_but_bad(*contracts, **kwargs):
        for i, c in enumerate(contracts, start=1):
            if c.symbol != "BAD":
                c.conId = i
        return list(contracts)

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", qualify_all_but_bad)

    req_calls = []

    def fake_req_mkt_data(contract, generic_ticks, snapshot, regulatory):
        req_calls.append(contract)
        return FakeTicker(contract, None)

    monkeypatch.setattr(broker.ib, "reqMktData", fake_req_mkt_data)

    asyncio.run(broker.stream_subscribe(["AAPL", "BAD", "MSFT"]))

    assert {c.symbol for c in req_calls} == {"AAPL", "MSFT"}
    assert set(broker._live_tickers.keys()) == {"AAPL", "MSFT"}


def test_stream_subscribe_skips_network_call_when_nothing_new(broker, monkeypatch):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), 150.0)

    async def fail_if_called(*contracts, **kwargs):
        raise AssertionError("no deberia llamar a IBKR si no hay simbolos nuevos")

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", fail_if_called)
    asyncio.run(broker.stream_subscribe(["AAPL"]))


def test_stream_unsubscribe_cancels_market_data_and_forgets_symbol(broker, monkeypatch):
    contract = FakeContract(1, "AAPL")
    broker._live_tickers["AAPL"] = FakeTicker(contract, 150.0)

    cancelled = []
    monkeypatch.setattr(broker.ib, "cancelMktData", lambda c: cancelled.append(c))

    broker.stream_unsubscribe(["AAPL", "MSFT"])  # MSFT nunca estuvo suscripto

    assert cancelled == [contract]
    assert "AAPL" not in broker._live_tickers


def test_get_live_price_reads_cached_ticker_without_network(broker, monkeypatch):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), 155.5)

    async def fail_if_called(*contracts, **kwargs):
        raise AssertionError("get_live_price no deberia pedir nada a IBKR")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fail_if_called)

    assert broker.get_live_price("AAPL") == 155.5
    assert broker.get_live_price("MSFT") is None  # no esta en el hot-set


def test_get_live_price_treats_nan_as_missing(broker):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), float("nan"))
    assert broker.get_live_price("AAPL") is None


def test_get_live_price_treats_negative_as_missing(broker):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), -1.0)
    assert broker.get_live_price("AAPL") is None


def test_get_snapshot_prices_returns_empty_dict_without_calling_ib(broker, monkeypatch):
    async def fail_if_called(*contracts, **kwargs):
        raise AssertionError("no deberia pedir nada a IBKR con una lista vacia")

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", fail_if_called)
    assert asyncio.run(broker.get_snapshot_prices([])) == {}


def test_get_snapshot_prices_returns_price_by_symbol(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [
            FakeTicker(FakeContract(1, "AAPL"), 155.0),
            FakeTicker(FakeContract(2, "MSFT"), 290.0),
        ]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_snapshot_prices(["AAPL", "MSFT"]))
    assert out == {"AAPL": 155.0, "MSFT": 290.0}


def test_get_snapshot_prices_filters_nan_prices(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(FakeContract(1, "AAPL"), float("nan"))]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)
    assert asyncio.run(broker.get_snapshot_prices(["AAPL"])) == {}


def test_get_snapshot_prices_filters_negative_prices(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(FakeContract(1, "AAPL"), -1.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)
    assert asyncio.run(broker.get_snapshot_prices(["AAPL"])) == {}


def test_get_snapshot_prices_skips_symbols_that_fail_to_qualify(broker, monkeypatch):
    """Mismo problema que en stream_subscribe: un simbolo sin conId no
    deberia tirar abajo el precio de TODO el lote (ver
    test_stream_subscribe_skips_symbols_that_fail_to_qualify)."""
    async def qualify_all_but_bad(*contracts, **kwargs):
        for i, c in enumerate(contracts, start=1):
            if c.symbol != "BAD":
                c.conId = i
        return list(contracts)

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", qualify_all_but_bad)

    seen_symbols = []

    async def fake_req_tickers(*contracts, **kwargs):
        seen_symbols.extend(c.symbol for c in contracts)
        return [
            FakeTicker(FakeContract(1, "AAPL"), 155.0),
            FakeTicker(FakeContract(2, "MSFT"), 290.0),
        ]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_snapshot_prices(["AAPL", "BAD", "MSFT"]))

    assert seen_symbols == ["AAPL", "MSFT"]  # BAD nunca se pide
    assert out == {"AAPL": 155.0, "MSFT": 290.0}


def test_get_snapshot_prices_returns_empty_dict_when_all_symbols_fail_to_qualify(broker, monkeypatch):
    async def qualify_none(*contracts, **kwargs):
        return list(contracts)  # ningun conId asignado

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", qualify_none)

    async def fail_if_called(*contracts, **kwargs):
        raise AssertionError("no deberia pedir tickers si ningun contrato califico")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fail_if_called)

    assert asyncio.run(broker.get_snapshot_prices(["BAD"])) == {}


def test_get_snapshot_prices_handles_request_failure(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def failing_req_tickers(*contracts, **kwargs):
        raise RuntimeError("pacing violation")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", failing_req_tickers)
    assert asyncio.run(broker.get_snapshot_prices(["AAPL"])) == {}


def test_to_ib_symbol_converts_hyphen_and_dot_class_suffix_to_space():
    assert _to_ib_symbol("BRK-B") == "BRK B"
    assert _to_ib_symbol("BF-B") == "BF B"
    assert _to_ib_symbol("BRK.B") == "BRK B"
    assert _to_ib_symbol("AAPL") == "AAPL"  # sin clase de accion: sin cambios


def test_from_ib_symbol_converts_space_back_to_hyphen():
    assert _from_ib_symbol("BRK B") == "BRK-B"
    assert _from_ib_symbol("BF B") == "BF-B"
    assert _from_ib_symbol("AAPL") == "AAPL"


def test_get_reference_price_qualifies_class_share_symbol_with_space(broker, monkeypatch):
    seen_contracts = []

    async def fake_qualify(*contracts, **kwargs):
        seen_contracts.extend(contracts)
        return list(contracts)

    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", fake_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(contracts[0], 450.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    price = asyncio.run(broker.get_reference_price("BRK-B"))

    assert price == 450.0
    assert seen_contracts[0].symbol == "BRK B"  # formato que espera IBKR


def test_get_reference_price_treats_negative_as_missing(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(contracts[0], -1.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    assert asyncio.run(broker.get_reference_price("AAPL")) is None


def test_get_positions_translates_class_share_symbol_back_to_hyphen(broker, monkeypatch):
    pos = FakePosition(1, "BRK B", 10, 300.0)  # IBKR devuelve el simbolo con espacio
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos])

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(pos.contract, 310.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_positions())

    assert out[0].symbol == "BRK-B"  # forma canonica usada por el resto del sistema


def test_get_position_qty_matches_class_share_symbol_despite_space(broker, monkeypatch):
    pos = FakePosition(1, "BRK B", 7, 300.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos])
    assert broker.get_position_qty("BRK-B") == 7


def test_get_snapshot_prices_keys_result_by_canonical_hyphen_symbol(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def fake_req_tickers(*contracts, **kwargs):
        return [FakeTicker(FakeContract(1, "BRK B"), 450.0)]

    monkeypatch.setattr(broker.ib, "reqTickersAsync", fake_req_tickers)

    out = asyncio.run(broker.get_snapshot_prices(["BRK-B"]))
    assert out == {"BRK-B": 450.0}


def test_disconnect_clears_live_tickers(broker, monkeypatch):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), 150.0)
    monkeypatch.setattr(broker.ib, "isConnected", lambda: True)
    monkeypatch.setattr(broker.ib, "disconnect", lambda: None)

    broker.disconnect()

    assert broker._live_tickers == {}


def test_get_trade_fill_returns_status_filled_price_and_remaining(broker, monkeypatch):
    trade = FakeTrade(order_id=42, status="Submitted", filled=4.0, remaining=6.0, avg_fill_price=101.5)
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    assert broker.get_trade_fill(42) == ("Submitted", 4.0, 101.5, 6.0)


def test_get_trade_fill_returns_none_when_order_not_found(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "trades", lambda: [])
    assert broker.get_trade_fill(42) is None


class _StubOrderStatus:
    def __init__(self, status, filled=0.0, remaining=0.0, avg_fill_price=0.0):
        self.status = status
        self.filled = filled
        self.remaining = remaining
        self.avgFillPrice = avg_fill_price


class _StubTrade:
    def __init__(self, order, status, log=None):
        self.order = order
        self.orderStatus = status
        self.log = log if log is not None else []


class _StubIB:
    """Fake minimal de self.ib para tests de place_order: asigna un orderId
    nuevo la primera vez que ve una orden (orderId == 0, como un Order recien
    creado de ib_async) y reusa el Trade existente en llamadas posteriores
    con el mismo orderId, igual que IBKR trata un placeOrder sobre un orderId
    vivo como una modificacion in-place (mismo patron que modify_stop_price).
    El status de la orden colocada PRIMERO (la orden padre, en place_order)
    se puede preseedear via `next_status` antes de llamar a place_order."""

    def __init__(self):
        self._next_id = 1000
        self.statuses: dict[int, _StubOrderStatus] = {}
        self.placed: list = []
        self.cancelled: list = []
        self.next_status: _StubOrderStatus | None = None

    async def qualifyContractsAsync(self, *contracts, **kwargs):
        return list(contracts)

    def placeOrder(self, contract, order):
        self.placed.append(order)
        if order.orderId == 0:
            order.orderId = self._next_id
            self._next_id += 1
            if self.next_status is not None:
                self.statuses[order.orderId] = self.next_status
                self.next_status = None
        status = self.statuses.get(order.orderId)
        if status is None:
            status = _StubOrderStatus("Submitted", filled=0.0, remaining=order.totalQuantity)
            self.statuses[order.orderId] = status
        return _StubTrade(order, status)

    def trades(self):
        seen: dict[int, object] = {}
        for order in self.placed:
            seen[order.orderId] = order
        return [_StubTrade(o, self.statuses[o.orderId]) for o in seen.values()]

    def cancelOrder(self, order):
        self.cancelled.append(order)


def _install_stub_ib(broker, monkeypatch, parent_status: _StubOrderStatus) -> _StubIB:
    stub = _StubIB()
    stub.next_status = parent_status
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", stub.qualifyContractsAsync)
    monkeypatch.setattr(broker.ib, "placeOrder", stub.placeOrder)
    monkeypatch.setattr(broker.ib, "trades", stub.trades)
    monkeypatch.setattr(broker.ib, "cancelOrder", stub.cancelOrder)
    return stub


def test_place_order_resizes_stop_when_parent_partially_fills(broker, monkeypatch):
    """El stop se crea con la cantidad PEDIDA (order.quantity) antes de saber
    cuanto llena realmente el padre. Si el padre solo llena una parte, el
    stop tiene que resizearse a la cantidad real: un stop sobredimensionado
    que se dispara puede vender mas de lo que efectivamente se compro."""
    parent_status = _StubOrderStatus("Filled", filled=5.0, remaining=0.0, avg_fill_price=99.5)
    stub = _install_stub_ib(broker, monkeypatch, parent_status)

    order = OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10, stop_loss_price=90.0)
    result = asyncio.run(broker.place_order(order))

    assert result["filled_qty"] == 5.0
    stop_order_id = result["stop_order_id"]
    stop_order = next(o for o in stub.placed if o.orderId == stop_order_id)
    assert stop_order.totalQuantity == 5.0
    # La orden de stop se reenvio (mismo orderId) para aplicar el resize.
    assert sum(1 for o in stub.placed if o.orderId == stop_order_id) >= 2
    assert stub.cancelled == []


def test_place_order_cancels_stop_when_parent_does_not_fill_at_all(broker, monkeypatch):
    parent_status = _StubOrderStatus("Cancelled", filled=0.0, remaining=10.0, avg_fill_price=0.0)
    stub = _install_stub_ib(broker, monkeypatch, parent_status)

    order = OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10, stop_loss_price=90.0)
    result = asyncio.run(broker.place_order(order))

    assert result["filled_qty"] == 0.0
    stop_order_id = result["stop_order_id"]
    assert len(stub.cancelled) == 1
    assert stub.cancelled[0].orderId == stop_order_id


def test_place_order_does_not_resize_stop_on_full_fill(broker, monkeypatch):
    parent_status = _StubOrderStatus("Filled", filled=10.0, remaining=0.0, avg_fill_price=101.0)
    stub = _install_stub_ib(broker, monkeypatch, parent_status)

    order = OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10, stop_loss_price=90.0)
    result = asyncio.run(broker.place_order(order))

    assert result["filled_qty"] == 10.0
    stop_order_id = result["stop_order_id"]
    assert sum(1 for o in stub.placed if o.orderId == stop_order_id) == 1  # sin resize
    assert stub.cancelled == []


def test_place_order_raises_before_resizing_when_stop_is_rejected(broker, monkeypatch):
    parent_status = _StubOrderStatus("Filled", filled=3.0, remaining=0.0, avg_fill_price=99.0)
    stub = _install_stub_ib(broker, monkeypatch, parent_status)

    # Fuerza el rechazo del stop: la siguiente orden que reciba un orderId
    # (la del stop) queda con status "Cancelled" desde el arranque.
    real_place_order = stub.placeOrder

    def place_order_then_reject_stop(contract, order):
        trade = real_place_order(contract, order)
        if order.parentId:  # es la orden hija (stop)
            trade.orderStatus.status = "Cancelled"
        return trade

    monkeypatch.setattr(broker.ib, "placeOrder", place_order_then_reject_stop)

    order = OrderRequest(symbol="AAPL", side=Side.BUY, quantity=10, stop_loss_price=90.0)
    with pytest.raises(StopLossRejectedError):
        asyncio.run(broker.place_order(order))

    # No se intenta resizear/cancelar un stop que ya esta rechazado.
    assert stub.cancelled == []


class _FakeEvent:
    """Minimo de ib_async.Event: soporta `+=` para suscribirse y `.emit()`
    para disparar los handlers registrados, sin depender de la libreria real."""

    def __init__(self):
        self._handlers: list = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def emit(self, *args):
        for handler in list(self._handlers):
            handler(*args)


class _EventTrade:
    """Como _StubTrade, pero con filledEvent/cancelledEvent reales (Event-like)
    para simular un fill tardio que llega DESPUES de _register_stop_reconciliation,
    no resuelto de antemano como en _StubTrade."""

    def __init__(self, order, status, log=None):
        self.order = order
        self.orderStatus = status
        self.log = log if log is not None else []
        self.filledEvent = _FakeEvent()
        self.cancelledEvent = _FakeEvent()


def test_register_stop_reconciliation_waits_for_late_fill_event(broker, monkeypatch):
    """Si el padre todavia esta vivo (no llego a un estado terminal) al
    momento de registrar, el ajuste del stop NO debe correr de inmediato --
    debe esperar al evento real, sin importar cuanto tarde en llegar. Esto es
    lo que evita que un timeout de _wait_for_fill (5s) se confunda con "la
    orden ya no va a llenar nada" y cancele un stop que en realidad sigue
    protegiendo una orden viva (ver docstring de _register_stop_reconciliation)."""
    parent_status = _StubOrderStatus("Submitted", filled=0.0, remaining=10.0)
    parent_order = FakeOrder(order_id=1, aux_price=None, total_quantity=10.0)
    parent_trade = _EventTrade(parent_order, parent_status)

    stop_status = _StubOrderStatus("PreSubmitted", filled=0.0, remaining=10.0)
    stop_order = FakeOrder(order_id=2, aux_price=90.0, total_quantity=10.0)
    stop_trade = _StubTrade(stop_order, stop_status)

    placed: list = []
    cancelled: list = []
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: placed.append(order))
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order))

    broker._register_stop_reconciliation(parent_trade, stop_trade, contract=object(), requested_qty=10.0)

    # El padre todavia esta "vivo": no se toca el stop todavia.
    assert placed == []
    assert cancelled == []

    # Fill tardio parcial (4 de 10), llega mucho despues via el evento real.
    parent_status.filled = 4.0
    parent_status.status = "Filled"
    parent_trade.filledEvent.emit()

    assert cancelled == []
    assert placed == [stop_order]
    assert stop_order.totalQuantity == 4.0


def test_register_stop_reconciliation_cancels_stop_on_late_zero_fill(broker, monkeypatch):
    """Si el padre finalmente se cancela sin haber llenado nada (tras seguir
    vivo un buen rato), el stop huerfano se cancela recien en ese momento --
    nunca antes, mientras el padre todavia podia llenar."""
    parent_status = _StubOrderStatus("Submitted", filled=0.0, remaining=10.0)
    parent_order = FakeOrder(order_id=1, aux_price=None, total_quantity=10.0)
    parent_trade = _EventTrade(parent_order, parent_status)

    stop_status = _StubOrderStatus("PreSubmitted", filled=0.0, remaining=10.0)
    stop_order = FakeOrder(order_id=2, aux_price=90.0, total_quantity=10.0)
    stop_trade = _StubTrade(stop_order, stop_status)

    cancelled: list = []
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: (_ for _ in ()).throw(
        AssertionError("no deberia resizear el stop si el padre nunca lleno nada")
    ))
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order))

    broker._register_stop_reconciliation(parent_trade, stop_trade, contract=object(), requested_qty=10.0)
    assert cancelled == []

    parent_status.status = "Cancelled"
    parent_trade.cancelledEvent.emit()

    assert cancelled == [stop_order]


# ---------------------------------------------------------------------------
# Cancel espurio por aviso 10349 de IBKR (incidente del 2026-07-29/30).
#
# ib_async solo trata como benignos los codigos de su lista `warningCodes`;
# cualquier otro (10349 = "Order TIF was set to DAY based on order preset",
# que es un AVISO, no un rechazo) lo manda a la rama de error, que marca la
# orden Cancelled y emite cancelledEvent aunque siga viva en el broker. IBKR
# manda el status real ~300ms despues. Ver _is_spurious_cancel en broker.py.
# ---------------------------------------------------------------------------

def test_is_spurious_cancel_true_for_informational_ibkr_code():
    trade = FakeTrade(
        order_id=1,
        status="Cancelled",
        log=[
            _LogEntry("PendingSubmit"),
            _LogEntry("Cancelled", error_code=10349),
        ],
    )
    assert _is_spurious_cancel(trade) is True


def test_is_spurious_cancel_false_for_a_real_cancellation():
    """202 ("Order Canceled") es una cancelacion de verdad: no debe enmascararse."""
    trade = FakeTrade(
        order_id=1,
        status="Cancelled",
        log=[_LogEntry("PendingSubmit"), _LogEntry("Cancelled", error_code=202)],
    )
    assert _is_spurious_cancel(trade) is False


def test_is_spurious_cancel_false_when_order_is_not_cancelled():
    trade = FakeTrade(
        order_id=1,
        status="Submitted",
        log=[_LogEntry("Cancelled", error_code=10349), _LogEntry("Submitted")],
    )
    assert _is_spurious_cancel(trade) is False


def test_is_spurious_cancel_respects_a_real_cancel_after_a_spurious_one():
    """Una orden puede sobrevivir al aviso 10349 y ser cancelada de verdad mas
    tarde. Manda la ULTIMA entrada de cancelacion del log, no la primera."""
    trade = FakeTrade(
        order_id=1,
        status="Cancelled",
        log=[
            _LogEntry("Cancelled", error_code=10349),
            _LogEntry("Submitted"),
            _LogEntry("Cancelled", error_code=202),
        ],
    )
    assert _is_spurious_cancel(trade) is False


def test_get_trade_fill_does_not_report_a_spurious_cancel_as_terminal(broker, monkeypatch):
    trade = FakeTrade(
        order_id=42,
        status="Cancelled",
        filled=0.0,
        remaining=10.0,
        log=[_LogEntry("Cancelled", error_code=10349)],
    )
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    status, filled, _avg, remaining = broker.get_trade_fill(42)

    assert status not in OrderStatus.DoneStates
    assert (filled, remaining) == (0.0, 10.0)


def test_get_trade_fill_still_reports_a_real_cancel_as_terminal(broker, monkeypatch):
    trade = FakeTrade(
        order_id=42,
        status="Cancelled",
        log=[_LogEntry("Cancelled", error_code=202)],
    )
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    assert broker.get_trade_fill(42)[0] == "Cancelled"


def test_wait_for_fill_keeps_waiting_through_a_spurious_cancel(broker, monkeypatch):
    """El bug de fondo: _wait_for_fill volvia AL INSTANTE con filled_qty=0
    porque el Cancelled falso es un DoneState. Debe seguir poleando hasta ver
    el estado real (la orden llena unos ciclos despues)."""
    trade = FakeTrade(
        order_id=42,
        status="Cancelled",
        filled=0.0,
        remaining=10.0,
        log=[_LogEntry("Cancelled", error_code=10349)],
    )
    monkeypatch.setattr(broker.ib, "trades", lambda: [trade])

    polls = {"n": 0}
    real_get = broker.get_trade_fill

    def counting_get(order_id):
        polls["n"] += 1
        if polls["n"] == 3:  # IBKR manda el status real recien en el 3er poll
            trade.orderStatus.status = "Filled"
            trade.orderStatus.filled = 10.0
            trade.orderStatus.remaining = 0.0
            trade.orderStatus.avgFillPrice = 27.76
            trade.log.append(_LogEntry("Filled"))
        return real_get(order_id)

    monkeypatch.setattr(broker, "get_trade_fill", counting_get)

    status, filled, avg, _remaining = asyncio.run(
        broker._wait_for_fill(42, timeout=5.0, interval=0.01)
    )

    assert polls["n"] >= 3
    assert (status, filled, avg) == ("Filled", 10.0, 27.76)


def test_register_stop_reconciliation_ignores_a_spurious_parent_cancel(broker, monkeypatch):
    """El corazon del incidente: al ver el Cancelled falso del padre, el
    sistema cancelaba SU PROPIO stop-loss y dejaba la compra desprotegida."""
    parent_status = _StubOrderStatus("Cancelled", filled=0.0, remaining=10.0)
    parent_order = FakeOrder(order_id=1, aux_price=None, total_quantity=10.0)
    parent_trade = _EventTrade(
        parent_order, parent_status, log=[_LogEntry("Cancelled", error_code=10349)]
    )

    stop_status = _StubOrderStatus("PreSubmitted", filled=0.0, remaining=10.0)
    stop_order = FakeOrder(order_id=2, aux_price=90.0, total_quantity=10.0)
    stop_trade = _StubTrade(stop_order, stop_status)

    cancelled: list = []
    monkeypatch.setattr(broker.ib, "cancelOrder", lambda order: cancelled.append(order))
    monkeypatch.setattr(broker.ib, "placeOrder", lambda contract, order: None)

    broker._register_stop_reconciliation(
        parent_trade, stop_trade, contract=object(), requested_qty=10.0
    )

    # Ni al registrar, ni cuando ib_async emite el cancelledEvent del aviso.
    assert cancelled == []
    parent_trade.cancelledEvent.emit()
    assert cancelled == []

    # Y el guard NO quedo consumido: el fill real posterior si se atiende.
    parent_status.status = "Filled"
    parent_status.filled = 4.0
    parent_trade.log.append(_LogEntry("Filled"))
    parent_trade.filledEvent.emit()

    assert stop_order.totalQuantity == 4.0


def test_place_order_sets_an_explicit_tif_on_parent_and_stop(broker, monkeypatch):
    """Sin TIF explicito IBKR aplica el preset de la cuenta y lo avisa con el
    codigo 10349 -- el disparador del cancel espurio. Fijarlo a mano elimina
    el aviso en el origen (DAY es lo que el preset ya venia poniendo)."""
    parent_status = _StubOrderStatus(
        "Filled", filled=10.0, remaining=0.0, avg_fill_price=27.76
    )
    stub = _install_stub_ib(broker, monkeypatch, parent_status)

    asyncio.run(
        broker.place_order(
            OrderRequest(
                symbol="CCL",
                side=Side.BUY,
                quantity=10,
                order_type=OrderType.LMT,
                limit_price=27.76,
                stop_loss_price=25.54,
            )
        )
    )

    assert len(stub.placed) >= 2
    assert {o.tif for o in stub.placed} == {"DAY"}
