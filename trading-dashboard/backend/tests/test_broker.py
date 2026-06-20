import asyncio

import pytest

from app.broker import IBKRBroker


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


def test_get_position_qty_returns_zero_when_no_match(broker, monkeypatch):
    pos1 = FakePosition(1, "AAPL", 10, 150.0)
    monkeypatch.setattr(broker.ib, "positions", lambda: [pos1])
    assert broker.get_position_qty("MSFT") == 0.0
    assert broker.get_position_qty("AAPL") == 10


class FakeOrder:
    def __init__(self, order_id, aux_price):
        self.orderId = order_id
        self.auxPrice = aux_price


class FakeOrderStatus:
    def __init__(self, status):
        self.status = status


class FakeTrade:
    def __init__(self, order_id, status, aux_price=90.0, contract="AAPL contract"):
        self.order = FakeOrder(order_id, aux_price)
        self.orderStatus = FakeOrderStatus(status)
        self.contract = contract


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


async def _noop_qualify(*contracts, **kwargs):
    return list(contracts)


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


def test_get_snapshot_prices_handles_request_failure(broker, monkeypatch):
    monkeypatch.setattr(broker.ib, "qualifyContractsAsync", _noop_qualify)

    async def failing_req_tickers(*contracts, **kwargs):
        raise RuntimeError("pacing violation")

    monkeypatch.setattr(broker.ib, "reqTickersAsync", failing_req_tickers)
    assert asyncio.run(broker.get_snapshot_prices(["AAPL"])) == {}


def test_disconnect_clears_live_tickers(broker, monkeypatch):
    broker._live_tickers["AAPL"] = FakeTicker(FakeContract(1, "AAPL"), 150.0)
    monkeypatch.setattr(broker.ib, "isConnected", lambda: True)
    monkeypatch.setattr(broker.ib, "disconnect", lambda: None)

    broker.disconnect()

    assert broker._live_tickers == {}
