import pytest

from app.models import AccountSummary, OrderRequest, OrderType, Side
from app.rules import RulesConfig, RulesEngine


def make_account(net_liq: float = 100_000, daily_pnl_pct: float = 0.0) -> AccountSummary:
    return AccountSummary(
        net_liquidation=net_liq,
        cash=net_liq,
        buying_power=net_liq,
        daily_pnl=net_liq * daily_pnl_pct / 100,
        daily_pnl_pct=daily_pnl_pct,
    )


@pytest.fixture
def engine() -> RulesEngine:
    config = RulesConfig(symbol_whitelist=["AAPL"], allow_extended_hours=True)
    return RulesEngine(config)


def buy_order(**overrides) -> OrderRequest:
    defaults = dict(
        symbol="AAPL",
        side=Side.BUY,
        quantity=1,
        order_type=OrderType.LMT,
        limit_price=200,
        stop_loss_price=190,
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


def test_rejects_symbol_not_in_whitelist(engine):
    order = buy_order(symbol="TSLA")
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "symbol_whitelist" for v in decision.violations)


def test_rejects_empty_whitelist():
    engine = RulesEngine(RulesConfig(allow_extended_hours=True))
    order = buy_order()
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "symbol_whitelist" for v in decision.violations)


def test_requires_stop_loss_on_buy(engine):
    order = buy_order(stop_loss_price=None)
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "require_stop_loss_on_buy" for v in decision.violations)


def test_rejects_stop_loss_too_wide(engine):
    order = buy_order(stop_loss_price=150)  # 25% por debajo del precio de entrada
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "max_stop_loss_pct" for v in decision.violations)


def test_rejects_when_halted(engine):
    order = buy_order()
    decision = engine.evaluate(order, make_account(), 0, 200, 0, True)
    assert not decision.approved
    assert any(v.rule == "halted" for v in decision.violations)


def test_rejects_when_daily_loss_limit_hit(engine):
    order = buy_order()
    decision = engine.evaluate(order, make_account(daily_pnl_pct=-3), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "daily_loss_limit_pct" for v in decision.violations)


def test_rejects_order_value_over_limit(engine):
    order = buy_order(quantity=1000)  # 1000 * 200 = 200,000 > max_order_value_usd
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "max_order_value_usd" for v in decision.violations)


def test_rejects_position_concentration(engine):
    order = buy_order(quantity=49)  # 49*200=9800 -> 9.8% del equity, ya hay 1 share
    decision = engine.evaluate(order, make_account(net_liq=10_000), 1, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "max_position_pct_of_equity" for v in decision.violations)


def test_approved_small_order_within_rules(engine):
    order = buy_order(quantity=1)
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert decision.approved
    assert not decision.requires_manual_approval


def test_requires_manual_approval_above_threshold(engine):
    order = buy_order(quantity=10)  # 10*200=2000 > manual_approval_threshold_usd (1000)
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert decision.approved
    assert decision.requires_manual_approval


def test_max_trades_per_day(engine):
    order = buy_order()
    trades_today = engine.config.max_trades_per_day
    decision = engine.evaluate(order, make_account(), 0, 200, trades_today, False)
    assert not decision.approved
    assert any(v.rule == "max_trades_per_day" for v in decision.violations)


def test_sell_does_not_require_stop_loss(engine):
    order = buy_order(side=Side.SELL, stop_loss_price=None, quantity=1)
    decision = engine.evaluate(order, make_account(), 1, 200, 0, False)
    assert decision.approved


def test_rejects_short_selling_by_default(engine):
    # No hay posicion previa: vender 1 dejaria la posicion en -1 (short).
    order = buy_order(side=Side.SELL, stop_loss_price=None, quantity=1)
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert not decision.approved
    assert any(v.rule == "allow_short_selling" for v in decision.violations)


def test_allows_short_selling_when_enabled():
    config = RulesConfig(symbol_whitelist=["AAPL"], allow_extended_hours=True, allow_short_selling=True)
    engine = RulesEngine(config)
    order = buy_order(side=Side.SELL, stop_loss_price=None, quantity=1)
    decision = engine.evaluate(order, make_account(), 0, 200, 0, False)
    assert decision.approved
