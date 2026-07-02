from datetime import datetime, timezone

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


def test_rejects_when_fund_reaches_its_own_daily_cap():
    # Cupo global (max_trades_per_day) muy alto para no interferir: aislar
    # el chequeo por fondo.
    config = RulesConfig(
        symbol_whitelist=["AAPL"], allow_extended_hours=True,
        max_trades_per_day=1000, max_trades_per_day_per_fund=3,
    )
    engine = RulesEngine(config)
    order = buy_order(fund_id="fund-a")
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False, trades_today_for_fund=3,
    )
    assert not decision.approved
    assert any(v.rule == "max_trades_per_day_per_fund" for v in decision.violations)


def test_approves_when_fund_below_its_own_daily_cap():
    config = RulesConfig(
        symbol_whitelist=["AAPL"], allow_extended_hours=True,
        max_trades_per_day=1000, max_trades_per_day_per_fund=3,
    )
    engine = RulesEngine(config)
    order = buy_order(fund_id="fund-a")
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False, trades_today_for_fund=2,
    )
    assert decision.approved


def test_max_trades_per_day_per_fund_disabled_by_default(engine):
    # max_trades_per_day_per_fund=None (default): sin cupo adicional por
    # fondo, solo el global (max_trades_per_day) sigue aplicando.
    order = buy_order(fund_id="fund-a")
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False, trades_today_for_fund=999,
    )
    assert decision.approved
    assert not any(v.rule == "max_trades_per_day_per_fund" for v in decision.violations)


def test_max_trades_per_day_per_fund_ignores_orders_without_fund_id():
    # Una orden de la cuenta general (sin fund_id) no tiene fondo del que
    # contar: el cupo por fondo no debe aplicarle aunque se pase un conteo.
    config = RulesConfig(
        symbol_whitelist=["AAPL"], allow_extended_hours=True,
        max_trades_per_day=1000, max_trades_per_day_per_fund=3,
    )
    engine = RulesEngine(config)
    order = buy_order()  # sin fund_id
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False, trades_today_for_fund=99,
    )
    assert decision.approved
    assert not any(v.rule == "max_trades_per_day_per_fund" for v in decision.violations)


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


def test_suggested_quantity_score_50_matches_no_score_baseline():
    config = RulesConfig(max_order_value_usd=1_000_000, max_position_pct_of_equity=100, risk_per_trade_pct=1)
    engine = RulesEngine(config)
    baseline = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=150)
    with_score = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=150, score=50)
    assert with_score.quantity == baseline.quantity == 20


def test_suggested_quantity_higher_score_increases_risk_based_quantity():
    config = RulesConfig(max_order_value_usd=1_000_000, max_position_pct_of_equity=100, risk_per_trade_pct=1)
    engine = RulesEngine(config)
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=150, score=100)
    # multiplicador 1.5x sobre el baseline de 20 (score=None/50)
    assert suggestion.quantity == 30
    assert suggestion.risk_usd == 1500.0


def test_suggested_quantity_lower_score_decreases_risk_based_quantity():
    config = RulesConfig(max_order_value_usd=1_000_000, max_position_pct_of_equity=100, risk_per_trade_pct=1)
    engine = RulesEngine(config)
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=150, score=0)
    # multiplicador 0.5x sobre el baseline de 20
    assert suggestion.quantity == 10
    assert suggestion.risk_usd == 500.0


def test_suggested_quantity_score_multiplier_does_not_bypass_hard_caps():
    # Aun con score=100 (multiplicador maximo 1.5x), max_order_value_usd sigue
    # topeando la cantidad final -- la conviccion solo afecta la pata de riesgo.
    config = RulesConfig(max_order_value_usd=5_000, max_position_pct_of_equity=100, risk_per_trade_pct=50)
    engine = RulesEngine(config)
    suggestion = engine.suggested_quantity(100_000, 0, entry_price=200, stop_loss_price=190, score=100)
    assert suggestion.quantity == 25
    assert suggestion.limited_by == "max_order_value_usd"


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
    ("max_drawdown_pct", 0),
    ("max_drawdown_pct", 101),
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
        max_drawdown_pct=100,
    )
    assert config.daily_loss_limit_pct == 100
    assert config.max_drawdown_pct == 100
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


def test_rejects_new_position_when_sector_at_max_concurrent_positions(engine):
    # Antes solo se aplicaba en el backtest (cap_concurrent_positions), nunca
    # en el motor de auto-trading en vivo -- ver fix de paridad.
    order = buy_order()  # abre una posicion NUEVA (current_position_qty=0)
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False,
        order_sector="Information Technology",
        max_concurrent_positions_per_sector=2,
        sector_position_count={"Information Technology": 2},
    )
    assert not decision.approved
    assert any(v.rule == "max_concurrent_positions_per_sector" for v in decision.violations)


def test_approves_new_position_when_sector_below_max_concurrent_positions(engine):
    order = buy_order()
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False,
        order_sector="Information Technology",
        max_concurrent_positions_per_sector=2,
        sector_position_count={"Information Technology": 1},
    )
    assert decision.approved


def test_max_concurrent_positions_per_sector_disabled_when_zero_or_none(engine):
    order = buy_order()
    decision = engine.evaluate(
        order, make_account(), 0, 200, 0, False,
        order_sector="Information Technology",
        max_concurrent_positions_per_sector=0,  # 0 = desactivado
        sector_position_count={"Information Technology": 99},
    )
    assert decision.approved
    assert not any(v.rule == "max_concurrent_positions_per_sector" for v in decision.violations)


def test_max_concurrent_positions_per_sector_ignores_add_to_existing_position(engine):
    # current_position_qty != 0: la orden agrega a una posicion YA abierta en
    # ese simbolo, no aumenta la cantidad de simbolos distintos del sector.
    order = buy_order()
    decision = engine.evaluate(
        order, make_account(), 5, 200, 0, False,  # ya tiene 5 unidades de AAPL
        order_sector="Information Technology",
        max_concurrent_positions_per_sector=2,
        sector_position_count={"Information Technology": 2},
    )
    assert decision.approved
    assert not any(v.rule == "max_concurrent_positions_per_sector" for v in decision.violations)


def test_max_concurrent_positions_per_sector_does_not_apply_to_sells(engine):
    order = buy_order(side=Side.SELL, stop_loss_price=None)
    decision = engine.evaluate(
        order, make_account(), 10, 200, 0, False,
        order_sector="Information Technology",
        max_concurrent_positions_per_sector=2,
        sector_position_count={"Information Technology": 2},
    )
    assert not any(v.rule == "max_concurrent_positions_per_sector" for v in decision.violations)


def test_rejects_order_exceeding_total_exposure(engine):
    # resulting_value = 10*200 = 2,000 (2% del equity); 97% ya invertido en
    # el resto de la cartera -> 99% combinado, por debajo del limite (default
    # 100%) -- subir a 99_500 ya invertidos para superarlo.
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        total_position_value_usd=99_500,  # 99.5% ya invertido + 2% de esta orden = 101.5%
    )
    assert not decision.approved
    assert any(v.rule == "max_total_exposure_pct" for v in decision.violations)


def test_approves_order_within_total_exposure_limit(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        total_position_value_usd=50_000,  # 50% + 2% = 52%, bajo el limite (100%)
    )
    assert decision.approved


def test_total_position_value_none_bypasses_total_exposure_check(engine):
    order = buy_order(quantity=10)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        total_position_value_usd=None,
    )
    assert decision.approved
    assert not any(v.rule == "max_total_exposure_pct" for v in decision.violations)


def test_rejects_order_exceeding_portfolio_heat(engine):
    # riesgo de esta orden: (200-190)*1 = $10 de $100,000 = 0.01%. Con
    # $6,000 ya en riesgo abierto (6.0%) + este, el combinado (6.01%)
    # supera el limite default de 6%.
    order = buy_order(quantity=1)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        open_portfolio_risk_usd=6_000,
    )
    assert not decision.approved
    assert any(v.rule == "max_portfolio_heat_pct" for v in decision.violations)


def test_approves_order_within_portfolio_heat_limit(engine):
    order = buy_order(quantity=1)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        open_portfolio_risk_usd=1_000,  # 1% + 0.01% de esta orden, bajo el limite (6%)
    )
    assert decision.approved


def test_open_portfolio_risk_none_bypasses_portfolio_heat_check(engine):
    order = buy_order(quantity=1)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 0, 200, 0, False,
        open_portfolio_risk_usd=None,
    )
    assert decision.approved
    assert not any(v.rule == "max_portfolio_heat_pct" for v in decision.violations)


def test_portfolio_heat_check_ignored_for_sells(engine):
    order = buy_order(side=Side.SELL, stop_loss_price=None, quantity=1)
    decision = engine.evaluate(
        order, make_account(net_liq=100_000), 10, 200, 0, False,
        open_portfolio_risk_usd=50_000,  # muy por encima del limite, pero es una venta
    )
    assert not any(v.rule == "max_portfolio_heat_pct" for v in decision.violations)


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


# ---------------------------------------------------------------------------
# _within_trading_hours: fin de semana, feriados, limites y datetime naive
# ---------------------------------------------------------------------------


@pytest.fixture
def hours_engine() -> RulesEngine:
    return RulesEngine(RulesConfig(allow_extended_hours=False))


def test_within_trading_hours_true_on_a_normal_weekday(hours_engine):
    # martes 23/6/2026, 10:00 -- ni fin de semana ni feriado
    assert hours_engine._within_trading_hours(datetime(2026, 6, 23, 10, 0))


def test_within_trading_hours_false_on_saturday(hours_engine):
    assert not hours_engine._within_trading_hours(datetime(2026, 6, 20, 10, 0))


def test_within_trading_hours_false_on_sunday(hours_engine):
    assert not hours_engine._within_trading_hours(datetime(2026, 6, 21, 10, 0))


def test_within_trading_hours_false_on_observed_holiday(hours_engine):
    # Juneteenth 2026 cae viernes 19/6: feriado de mercado, no solo de fin de semana.
    assert not hours_engine._within_trading_hours(datetime(2026, 6, 19, 10, 0))


def test_within_trading_hours_false_on_holiday_observed_in_previous_calendar_year(hours_engine):
    # Ano Nuevo 2022 (1/1, sabado) se observa el 31/12/2021 (viernes): el
    # feriado de un anio puede "correrse" al anio calendario anterior.
    assert not hours_engine._within_trading_hours(datetime(2021, 12, 31, 10, 0))


def test_within_trading_hours_includes_start_and_end_boundary(hours_engine):
    assert hours_engine._within_trading_hours(datetime(2026, 6, 23, 9, 30))
    assert hours_engine._within_trading_hours(datetime(2026, 6, 23, 16, 0))


def test_within_trading_hours_false_just_before_open_and_just_after_close(hours_engine):
    assert not hours_engine._within_trading_hours(datetime(2026, 6, 23, 9, 29, 59))
    assert not hours_engine._within_trading_hours(datetime(2026, 6, 23, 16, 0, 1))


def test_within_trading_hours_treats_naive_datetime_as_configured_timezone(hours_engine):
    """Un `now` sin tzinfo debe interpretarse como ya expresado en
    trading_hours_timezone (hora de pared de NY), NO reinterpretado via
    astimezone() asumiendolo en la zona horaria del SISTEMA -- ese bug hacia
    que el resultado dependiera de en que TZ corriera el proceso (en un
    contenedor productivo, casi siempre UTC) en vez de la zona configurada."""
    naive_within_hours = datetime(2026, 6, 23, 10, 0)
    assert hours_engine._within_trading_hours(naive_within_hours)
    # Las mismas 10:00, pero marcadas explicitamente como UTC en vez de NY,
    # caen fuera de horario una vez convertidas a NY (-4hs en horario de
    # verano: 6:00 NY) -- prueba que la hora SI se interpreta segun su
    # tzinfo cuando lo tiene, y solo se asume `trading_hours_timezone`
    # cuando el datetime llega naive (sin tzinfo).
    same_wall_clock_marked_as_utc = naive_within_hours.replace(tzinfo=timezone.utc)
    assert not hours_engine._within_trading_hours(same_wall_clock_marked_as_utc)


def test_within_trading_hours_converts_aware_datetime_in_another_timezone(hours_engine):
    # 14:00 UTC en junio (horario de verano, NY = UTC-4) son las 10:00 NY:
    # dentro de horario, aunque el datetime llegue en otra zona horaria.
    aware_utc = datetime(2026, 6, 23, 14, 0, tzinfo=timezone.utc)
    assert hours_engine._within_trading_hours(aware_utc)


def test_within_trading_hours_true_when_extended_hours_allowed_on_weekend():
    engine = RulesEngine(RulesConfig(allow_extended_hours=True))
    assert engine._within_trading_hours(datetime(2026, 6, 20, 3, 0))
