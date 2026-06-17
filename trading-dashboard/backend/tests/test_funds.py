import pytest

from app.funds import FundsStore
from app.models import Side


@pytest.fixture
def store(tmp_path) -> FundsStore:
    return FundsStore(tmp_path / "funds.json")


def test_create_fund_starts_with_full_cash_and_no_positions(store):
    fund = store.create("Test 1 semana", 5000)
    assert fund.cash_usd == 5000
    assert fund.net_contributed_capital() == 5000
    assert fund.auto_trading_enabled is False
    assert fund.positions == {}
    assert fund.owned_quantity("AAPL") == 0.0


def test_create_fund_registers_initial_capital_as_first_flow(store):
    fund = store.create("Test", 5000)
    assert len(fund.capital_flows) == 1
    assert fund.capital_flows[0].amount == 5000
    assert fund.capital_flows[0].note == "Capital inicial"


def test_buy_reduces_cash_and_opens_position(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    fund = store.get(fund.id)
    assert fund.cash_usd == 4000
    assert fund.owned_quantity("AAPL") == 10
    assert fund.positions["AAPL"].avg_cost == 100


def test_buy_twice_averages_cost(store):
    fund = store.create("Test", 10_000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 120)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 20
    assert fund.positions["AAPL"].avg_cost == 110


def test_sell_increases_cash_and_records_realized_pnl(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.SELL, 10, 120)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 5000 + (120 - 100) * 10
    assert fund.realized_pnl_total() == 200.0


def test_partial_sell_keeps_remaining_position_and_avg_cost(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.SELL, 4, 150)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 6
    assert fund.positions["AAPL"].avg_cost == 100


def test_owned_quantity_does_not_see_holdings_never_bought_by_this_fund(store):
    """El guardrail central: un fondo que nunca compro un simbolo no figura
    con cantidad en su ledger, sin importar lo que haya en la cuenta real de
    IBKR (holdings preexistentes o de otro fondo)."""
    fund = store.create("Test", 5000)
    assert fund.owned_quantity("TSLA") == 0.0
    assert fund.can_afford(4999)
    assert not fund.can_afford(5001)


def test_set_auto_trading_toggle(store):
    fund = store.create("Test", 5000)
    assert fund.auto_trading_enabled is False
    updated = store.set_auto_trading(fund.id, True)
    assert updated.auto_trading_enabled is True
    assert store.get(fund.id).auto_trading_enabled is True


def test_set_auto_trading_unknown_fund_returns_none(store):
    assert store.set_auto_trading("no-existe", True) is None


def test_record_fill_unknown_fund_returns_none(store):
    assert store.record_fill("no-existe", "AAPL", Side.BUY, 1, 100) is None


def test_persists_across_store_reload(tmp_path):
    path = tmp_path / "funds.json"
    store1 = FundsStore(path)
    fund = store1.create("Test", 5000)
    store1.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store1.apply_capital_flow(fund.id, 1000, note="Aporte extra")

    store2 = FundsStore(path)
    reloaded = store2.get(fund.id)
    assert reloaded is not None
    assert reloaded.cash_usd == 5000
    assert reloaded.owned_quantity("AAPL") == 10
    assert len(reloaded.trades) == 1
    assert reloaded.net_contributed_capital() == 6000
    assert len(reloaded.capital_flows) == 2


def test_apply_capital_flow_increases_cash_and_net_contributed(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, 2000, note="Aporte adicional")
    fund = store.get(fund.id)
    assert fund.cash_usd == 7000
    assert fund.net_contributed_capital() == 7000
    assert len(fund.capital_flows) == 2


def test_apply_capital_flow_negative_amount_withdraws_cash(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, -1000, note="Retiro")
    fund = store.get(fund.id)
    assert fund.cash_usd == 4000
    assert fund.net_contributed_capital() == 4000


def test_apply_capital_flow_unknown_fund_returns_none(store):
    assert store.apply_capital_flow("no-existe", 1000) is None


def test_total_allocated_cash_sums_across_funds(store):
    fund1 = store.create("Fondo 1", 5000)
    fund2 = store.create("Fondo 2", 3000)
    assert store.total_allocated_cash() == 8000
    assert store.total_allocated_cash(exclude_fund_id=fund1.id) == 3000
    assert store.total_allocated_cash(exclude_fund_id=fund2.id) == 5000
