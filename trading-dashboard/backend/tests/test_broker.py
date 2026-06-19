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
