import pytest
from pydantic import ValidationError

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


def test_symbol_whitelist_rejects_malformed_symbol():
    # Mismo chequeo que OrderRequest.symbol y ScreenerConfig.universe: un PUT
    # directo a /api/rules no debe poder persistir un simbolo malformado/XSS
    # en la whitelist.
    with pytest.raises(ValidationError):
        RulesConfig(symbol_whitelist=["<script>alert(1)</script>"])


def test_symbol_whitelist_is_normalized_to_uppercase():
    config = RulesConfig(symbol_whitelist=["aapl"])
    assert config.symbol_whitelist == ["AAPL"]


def test_symbol_whitelist_rejects_oversized_list():
    with pytest.raises(ValidationError):
        RulesConfig(symbol_whitelist=[f"S{i}" for i in range(1001)])


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


def test_suggested_quantity_limited_by_risk():
    config = RulesConfig(max_order_value_usd=1_000_000, max_position_pct_of_equity=100, risk_per_trade_pct=1)
    engine = RulesEngine(config)
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=150)
    assert suggestion.quantity == 20
    assert suggestion.risk_usd == 1000.0
    assert suggestion.limited_by is None


def test_suggested_quantity_limited_by_max_position_pct_of_equity():
    config = RulesConfig(max_order_value_usd=1_000_000, max_position_pct_of_equity=5, risk_per_trade_pct=50)
    engine = RulesEngine(config)
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=190)
    assert suggestion.quantity == 25
    assert suggestion.limited_by == "max_position_pct_of_equity"


def test_suggested_quantity_limited_by_max_order_value_usd(engine):
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=190)
    assert suggestion.quantity == 25
    assert suggestion.limited_by == "max_order_value_usd"


def test_suggested_quantity_zero_when_no_equity(engine):
    suggestion = engine.suggested_quantity(0, 0, entry_price=200, stop_loss_price=190)
    assert suggestion.quantity == 0.0
    assert suggestion.risk_usd == 0.0
    assert suggestion.limited_by is None


def test_suggested_quantity_zero_when_stop_not_below_entry(engine):
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=200)
    assert suggestion.quantity == 0.0


# ---------------------------------------------------------------------------
# Cotas en los campos numericos de RulesConfig: sin ellas, PUT /api/rules
# podia recibir un valor absurdamente alto (ej. daily_loss_limit_pct=999999)
# que neutraliza el kill switch en la practica sin desactivarlo
# explicitamente, porque ninguna perdida diaria real llegaria nunca a ese
# umbral.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("daily_loss_limit_pct", 0),
    ("daily_loss_limit_pct", 999_999),
    ("max_order_value_usd", 0),
    ("max_order_value_usd", -1),
    ("max_position_pct_of_equity", 0),
    ("max_position_pct_of_equity", 101),
    ("max_sector_concentration_pct", 0),
    ("max_sector_concentration_pct", 101),
    ("risk_per_trade_pct", 0),
    ("risk_per_trade_pct", 101),
    ("max_trades_per_day", 0),
    ("max_trades_per_day", 1001),
    ("max_stop_loss_pct", 0),
    ("max_stop_loss_pct", 101),
    ("manual_approval_threshold_usd", -1),
])
def test_rules_config_rejects_out_of_range_values(field, value):
    with pytest.raises(ValidationError):
        RulesConfig(**{field: value})


def test_rules_config_accepts_values_at_the_bounds():
    config = RulesConfig(
        daily_loss_limit_pct=100,
        max_order_value_usd=10_000_000,
        max_position_pct_of_equity=100,
        max_sector_concentration_pct=100,
        risk_per_trade_pct=100,
        max_trades_per_day=1000,
        max_stop_loss_pct=100,
        manual_approval_threshold_usd=100_000_000,
    )
    assert config.daily_loss_limit_pct == 100
    assert config.manual_approval_threshold_usd == 100_000_000


# ---------------------------------------------------------------------------
# max_sector_concentration_pct: limite a la exposicion combinada (posiciones
# existentes + la orden en evaluacion) a un mismo sector GICS, como % del
# equity. Solo se evalua si se conoce el sector de la orden (order_sector);
# sin ese dato, la regla no bloquea (ver razonamiento en rules.py).
# ---------------------------------------------------------------------------

def test_rejects_order_exceeding_sector_concentration(engine):
    order = buy_order(quantity=10)  # resulting_value = 10*200 = 2,000 (2% del equity)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        order_sector="Information Technology",
        sector_exposure_usd={"Information Technology": 29_000},  # 29% ya expuesto -> 31% combinado
    )
    assert not decision.approved
    assert any(v.rule == "max_sector_concentration_pct" for v in decision.violations)


def test_approves_order_within_sector_concentration_limit(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        order_sector="Information Technology",
        sector_exposure_usd={"Information Technology": 10_000},  # 10% + 2% = 12%, bajo el limite (30%)
    )
    assert decision.approved


def test_order_sector_none_bypasses_sector_concentration_check(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        order_sector=None,  # sector desconocido: no hay forma de evaluar el limite
        sector_exposure_usd={"Information Technology": 1_000_000},
    )
    assert decision.approved
    assert not any(v.rule == "max_sector_concentration_pct" for v in decision.violations)


def test_sector_concentration_only_counts_matching_sector(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        order_sector="Information Technology",
        sector_exposure_usd={"Health Care": 1_000_000, "Information Technology": 1_000},
    )
    assert decision.approved
    assert not any(v.rule == "max_sector_concentration_pct" for v in decision.violations)


def test_sector_concentration_defaults_to_zero_exposure_when_not_provided(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        order_sector="Information Technology",
        sector_exposure_usd=None,
    )
    assert decision.approved
    assert not any(v.rule == "max_sector_concentration_pct" for v in decision.violations)
