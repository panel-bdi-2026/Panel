import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Mismo patron que test_signal_engine.py: apuntar las rutas de config a un
# directorio temporal ANTES de importar app.main, para no tocar los archivos
# reales del repo (rules.yaml, screener.yaml, audit.db, state.json, funds.json).
_tmp_dir = tempfile.mkdtemp(prefix="auto_trading_test_")
os.environ.setdefault("API_KEY", "test-key")
os.environ["RULES_PATH"] = str(Path(_tmp_dir) / "rules.yaml")
os.environ["SCREENER_PATH"] = str(Path(_tmp_dir) / "screener.yaml")
os.environ["AUDIT_DB_PATH"] = str(Path(_tmp_dir) / "audit.db")
os.environ["STATE_PATH"] = str(Path(_tmp_dir) / "state.json")
os.environ["FUNDS_PATH"] = str(Path(_tmp_dir) / "funds.json")

import pandas as pd
import pytest

from app import main as main_module
from app.broker import StopLossRejectedError
from app.funds import FundsStore
from app.models import AccountSummary, SignalResult
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig


def make_account(net_liq: float = 100_000, daily_pnl_pct: float = 0.0) -> AccountSummary:
    return AccountSummary(
        net_liquidation=net_liq,
        cash=net_liq,
        buying_power=net_liq,
        daily_pnl=net_liq * daily_pnl_pct / 100,
        daily_pnl_pct=daily_pnl_pct,
    )


def make_signal(symbol="AAPL", last_price=100.0, stop=95.0, passes=True, score=1.0) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        as_of=datetime.now(timezone.utc),
        last_price=last_price,
        score=score,
        momentum_3m_pct=10.0,
        momentum_1m_pct=5.0,
        trend_ok=True,
        rsi=55.0,
        avg_volume=1_000_000,
        pct_from_52w_high=-5.0,
        suggested_stop_loss_price=stop,
        suggested_stop_loss_pct=5.0,
        passes_filters=passes,
        notes=[],
    )


async def _fake_place_order(order):
    return {"order_id": 1, "status": "Filled", "filled_qty": order.quantity, "avg_fill_price": order.limit_price}


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    main_module.state["mode"] = "paper"
    main_module.state["halted"] = False
    main_module.state["connected"] = True
    main_module.state["pending_orders"] = {}
    monkeypatch.setattr(main_module, "funds_store", FundsStore(tmp_path / "funds.json"))
    # Algunos tests mutan atributos de screener_config directamente (ej.
    # max_holding_days, sma_fast) sin pasar por monkeypatch. Sin aislar el
    # objeto, esa mutacion persiste mas alla del test (y del archivo, ya que
    # screener_config es un singleton de modulo compartido con
    # test_signal_engine.py), filtrando estado entre tests. Una instancia
    # nueva por test, swapeada con monkeypatch, se revierte sola al terminar.
    monkeypatch.setattr(main_module, "screener_config", ScreenerConfig())
    main_module.rules_engine.reload(RulesConfig(
        symbol_whitelist=["AAPL", "MSFT"],
        allow_extended_hours=True,
        manual_approval_threshold_usd=1_000_000,
    ))
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)

    async def fake_get_account_summary():
        return make_account()

    monkeypatch.setattr(main_module.broker, "get_account_summary", fake_get_account_summary)
    monkeypatch.setattr(main_module, "_persist_state", lambda: None)
    yield


# ---------------------------------------------------------------------------
# _try_auto_trade_entry
# ---------------------------------------------------------------------------

def test_auto_trade_entry_skips_when_not_paper_mode():
    main_module.state["mode"] = "live"
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.cash_usd == 10_000
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_skips_when_signal_has_no_price():
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=0)))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_skips_when_no_funds_have_auto_trading_enabled():
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=False)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_skips_when_fund_already_owns_symbol():
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 90)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 5  # no se sumo una segunda compra


def test_auto_trade_entry_skips_when_sizing_is_zero():
    # Sizing se dimensiona contra el equity del propio fondo: un fondo sin
    # capital (equity_estimate() == 0) da sizing cero sin importar el equity
    # de la cuenta consolidada.
    fund = main_module.funds_store.create("Fondo", 0, auto_trading_enabled=True)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_skips_when_rules_engine_rejects():
    main_module.rules_engine.reload(RulesConfig(symbol_whitelist=[]))  # rechaza todo
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_executes_buy_and_records_fill(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0)))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") > 0
    pos = fund.positions["AAPL"]
    assert pos.avg_cost == 100.0
    assert pos.stop_loss_price == 95.0
    assert pos.opened_at is not None


def test_auto_trade_entry_records_signal_rationale_in_audit(monkeypatch):
    # El audit trail tiene que conservar el POR QUE el motor de señales
    # decidio esta compra (score/momentum/RSI/notas), no solo el QUE se
    # compro: sin esto no hay forma de auditar despues si el auto-trading
    # entro por una señal razonable o por un bug del screener.
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)
    signal = make_signal(last_price=100.0, stop=95.0, score=42.0)

    asyncio.run(main_module._try_auto_trade_entry(signal))

    entries = main_module.audit.recent(1)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "auto_trade_executed"
    assert entry["result"]["fund_id"] == fund.id
    recorded_signal = entry["result"]["signal"]
    assert recorded_signal["symbol"] == "AAPL"
    assert recorded_signal["score"] == 42.0
    assert recorded_signal["momentum_3m_pct"] == signal.momentum_3m_pct
    assert recorded_signal["momentum_1m_pct"] == signal.momentum_1m_pct
    assert recorded_signal["rsi"] == signal.rsi
    assert recorded_signal["notes"] == signal.notes


def test_auto_trade_entry_does_not_buy_when_broker_rejects_stop_loss(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)

    async def fail(order):
        raise StopLossRejectedError("rechazado")

    monkeypatch.setattr(main_module.broker, "place_order", fail)
    asyncio.run(main_module._try_auto_trade_entry(make_signal()))
    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 10_000


def test_auto_trade_entry_skips_fund_without_enough_cash_and_uses_next(monkeypatch):
    poor_fund = main_module.funds_store.create("Pobre", 50, auto_trading_enabled=True)
    rich_fund = main_module.funds_store.create("Rico", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0)))

    poor_fund = main_module.funds_store.get(poor_fund.id)
    rich_fund = main_module.funds_store.get(rich_fund.id)
    assert poor_fund.owned_quantity("AAPL") == 0
    assert rich_fund.owned_quantity("AAPL") > 0


def test_auto_trade_entry_assigns_signal_to_only_one_fund(monkeypatch):
    """Decision del usuario: una senal nunca se reparte entre varios fondos
    auto-trading, va al primero con cupo (por orden de creacion)."""
    fund1 = main_module.funds_store.create("Fondo 1", 10_000, auto_trading_enabled=True)
    fund2 = main_module.funds_store.create("Fondo 2", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0)))

    fund1 = main_module.funds_store.get(fund1.id)
    fund2 = main_module.funds_store.get(fund2.id)
    bought = [f for f in (fund1, fund2) if f.owned_quantity("AAPL") > 0]
    assert len(bought) == 1
    assert fund1.owned_quantity("AAPL") > 0
    assert fund2.owned_quantity("AAPL") == 0


# ---------------------------------------------------------------------------
# _check_fund_exit
# ---------------------------------------------------------------------------

def test_check_fund_exit_noop_when_fund_unknown():
    asyncio.run(main_module._check_fund_exit("no-existe", "AAPL"))


def test_check_fund_exit_noop_when_no_position():
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))


def test_check_fund_exit_reconciles_full_stop_loss_fill(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)

    async def fail_if_called(order):
        raise AssertionError("no deberia intentar vender: ya se reconcilio todo")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 9_950  # 10000 - 10*100 (compra) + 10*95 (stop reconciliado)


def test_check_fund_exit_reconciles_partial_stop_loss_fill_and_keeps_remainder(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 4)  # se vendieron 6 por el stop

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 4


def test_check_fund_exit_closes_on_max_holding_days(monkeypatch):
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    fund = main_module.funds_store.get(fund.id)
    fund.positions["AAPL"].opened_at = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    async def fake_reference_price(symbol):
        return 105.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 10_050  # 10000 - 10*100 (compra) + 10*105 (salida)


def test_check_fund_exit_closes_on_trend_break(monkeypatch):
    main_module.screener_config.sma_fast = 3
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    bars = pd.DataFrame({"Close": pd.Series([110.0, 108.0, 90.0])})  # ultimo cierre bien por debajo de la sma(3)
    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: bars)

    async def fake_reference_price(symbol):
        return 90.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_check_fund_exit_does_nothing_when_neither_condition_met(monkeypatch):
    main_module.screener_config.max_holding_days = 20
    main_module.screener_config.sma_fast = 3
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    bars = pd.DataFrame({"Close": pd.Series([95.0, 100.0, 110.0])})  # tendencia alcista, cierre por encima de la sma
    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: bars)

    async def fail_if_called(order):
        raise AssertionError("no deberia vender")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 10


def test_check_fund_exit_keeps_position_when_reference_price_unavailable(monkeypatch):
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    fund = main_module.funds_store.get(fund.id)
    fund.positions["AAPL"].opened_at = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    async def no_price(symbol):
        return None

    monkeypatch.setattr(main_module.broker, "get_reference_price", no_price)

    async def fail_if_called(order):
        raise AssertionError("no deberia vender sin precio de referencia")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 10


def test_check_fund_exit_keeps_position_when_exit_order_fails(monkeypatch):
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    fund = main_module.funds_store.get(fund.id)
    fund.positions["AAPL"].opened_at = datetime.now(timezone.utc) - timedelta(days=10)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    async def fake_reference_price(symbol):
        return 105.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)

    async def fail(order):
        raise StopLossRejectedError("rechazado")

    monkeypatch.setattr(main_module.broker, "place_order", fail)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 10


# ---------------------------------------------------------------------------
# _check_fund_trailing_stop
# ---------------------------------------------------------------------------

def _trending_bars() -> pd.DataFrame:
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    return pd.DataFrame({"High": close + 1, "Low": close - 1, "Close": close})


def test_check_fund_trailing_stop_noop_when_disabled():
    main_module.screener_config.trailing_stop_enabled = False
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90, stop_order_id=1
    )

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 90


def test_check_fund_trailing_stop_noop_when_no_position():
    main_module.screener_config.trailing_stop_enabled = True
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))  # no debe lanzar


def test_check_fund_trailing_stop_noop_when_no_stop_order_id(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90)

    def fail_if_called(symbol, days):
        raise AssertionError("no deberia pedir datos de mercado sin stop_order_id")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_if_called)
    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 90


def test_check_fund_trailing_stop_raises_stop_when_price_moved_favorably(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    main_module.screener_config.atr_period = 3
    main_module.screener_config.stop_loss_atr_multiplier = 1.0
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90, stop_order_id=1
    )

    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: _trending_bars())
    modify_calls = []
    monkeypatch.setattr(
        main_module.broker, "modify_stop_price", lambda order_id, price: modify_calls.append((order_id, price)) or True
    )

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    assert modify_calls == [(1, 103.0)]
    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 103.0
    entries = main_module.audit.recent(1)
    assert entries[0]["action"] == "auto_trade_trailing_stop_updated"
    assert entries[0]["result"] == {"old_stop": 90, "new_stop": 103.0}


def test_check_fund_trailing_stop_does_not_lower_an_already_better_stop(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    main_module.screener_config.atr_period = 3
    main_module.screener_config.stop_loss_atr_multiplier = 1.0
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=110, stop_order_id=1
    )

    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: _trending_bars())

    def fail_if_called(order_id, price):
        raise AssertionError("no deberia bajar un stop ya mas favorable")

    monkeypatch.setattr(main_module.broker, "modify_stop_price", fail_if_called)

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 110


def test_check_fund_trailing_stop_keeps_ledger_stop_when_broker_modify_fails(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    main_module.screener_config.atr_period = 3
    main_module.screener_config.stop_loss_atr_multiplier = 1.0
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90, stop_order_id=1
    )

    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: _trending_bars())
    monkeypatch.setattr(main_module.broker, "modify_stop_price", lambda order_id, price: False)

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 90  # el ledger no se adelanta al broker


def test_check_fund_trailing_stop_noop_when_market_data_unavailable(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90, stop_order_id=1
    )

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))  # no debe propagar

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 90


# ---------------------------------------------------------------------------
# _run_auto_exit_monitor_cycle
# ---------------------------------------------------------------------------

def test_run_auto_exit_monitor_cycle_skips_when_not_paper(monkeypatch):
    main_module.state["mode"] = "live"

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear salidas si no esta en paper")

    monkeypatch.setattr(main_module, "_check_fund_exit", fail_if_called)
    asyncio.run(main_module._run_auto_exit_monitor_cycle())


def test_run_auto_exit_monitor_cycle_skips_when_halted(monkeypatch):
    main_module.state["halted"] = True

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear salidas si esta halted")

    monkeypatch.setattr(main_module, "_check_fund_exit", fail_if_called)
    asyncio.run(main_module._run_auto_exit_monitor_cycle())


def test_run_auto_exit_monitor_cycle_skips_when_disconnected(monkeypatch):
    main_module.state["connected"] = False

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear salidas si no esta conectado")

    monkeypatch.setattr(main_module, "_check_fund_exit", fail_if_called)
    asyncio.run(main_module._run_auto_exit_monitor_cycle())


def test_run_auto_exit_monitor_cycle_only_checks_auto_trading_funds_with_positions(monkeypatch):
    fund_off = main_module.funds_store.create("Sin auto", 10_000, auto_trading_enabled=False)
    main_module.funds_store.record_fill(fund_off.id, "AAPL", main_module.Side.BUY, 5, 100)
    fund_on = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund_on.id, "MSFT", main_module.Side.BUY, 5, 100)

    checked = []

    async def fake_check(fund_id, symbol):
        checked.append((fund_id, symbol))

    monkeypatch.setattr(main_module, "_check_fund_exit", fake_check)

    asyncio.run(main_module._run_auto_exit_monitor_cycle())

    assert checked == [(fund_on.id, "MSFT")]


def test_run_auto_exit_monitor_cycle_checks_trailing_stop_before_exit(monkeypatch):
    fund = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 100)

    calls = []

    async def fake_trailing(fund_id, symbol):
        calls.append(("trailing", symbol))

    async def fake_exit(fund_id, symbol):
        calls.append(("exit", symbol))

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fake_trailing)
    monkeypatch.setattr(main_module, "_check_fund_exit", fake_exit)

    asyncio.run(main_module._run_auto_exit_monitor_cycle())

    assert calls == [("trailing", "AAPL"), ("exit", "AAPL")]


def test_run_auto_exit_monitor_cycle_continues_after_trailing_stop_check_fails(monkeypatch):
    fund = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 100)

    async def failing_trailing(fund_id, symbol):
        raise RuntimeError("fallo de datos de mercado")

    exit_calls = []

    async def fake_exit(fund_id, symbol):
        exit_calls.append(symbol)

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", failing_trailing)
    monkeypatch.setattr(main_module, "_check_fund_exit", fake_exit)

    asyncio.run(main_module._run_auto_exit_monitor_cycle())  # no debe propagar la excepcion

    assert exit_calls == ["AAPL"]
    entries = main_module.audit.recent(1)
    assert entries[0]["action"] == "auto_trade_trailing_stop_check_failed"


# ---------------------------------------------------------------------------
# _funds_order_lock (cierre de la carrera validar -> enviar -> aplicar fill)
# ---------------------------------------------------------------------------

def test_concurrent_submit_order_does_not_overspend_fund_cash(monkeypatch):
    """Reproduce la carrera que _funds_order_lock existe para cerrar: dos
    compras concurrentes sobre el MISMO fondo, cada una individualmente
    dentro del cash disponible (4500 contra 8000), pero juntas no (9000 >
    8000). Sin el lock, ambas validarian contra el mismo cash_usd
    desactualizado (ninguna ve el record_fill de la otra hasta que ya
    corrio) y el fondo terminaria con cash_usd negativo. Con el lock, la
    segunda se valida DESPUES de que la primera ya aplico su fill y debe
    ser rechazada."""
    fund = main_module.funds_store.create("Fondo", 8_000, auto_trading_enabled=False)

    async def slow_place_order(order):
        await asyncio.sleep(0.05)  # ensancha la ventana de carrera si el lock fallara
        return {"order_id": 1, "status": "Filled", "filled_qty": order.quantity, "avg_fill_price": order.limit_price}

    monkeypatch.setattr(main_module.broker, "place_order", slow_place_order)

    def make_order():
        return main_module.OrderRequest(
            symbol="AAPL",
            side=main_module.Side.BUY,
            quantity=45,
            order_type="LMT",
            limit_price=100.0,
            stop_loss_price=96.0,
            fund_id=fund.id,
        )

    async def run_both():
        return await asyncio.gather(
            main_module.submit_order(make_order(), None),
            main_module.submit_order(make_order(), None),
            return_exceptions=True,
        )

    results = asyncio.run(run_both())

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, main_module.HTTPException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 422

    fund = main_module.funds_store.get(fund.id)
    assert fund.cash_usd == 3_500  # 8000 - 4500: la segunda compra se rechazo
    assert fund.owned_quantity("AAPL") == 45


def test_run_auto_exit_monitor_cycle_continues_after_a_check_fails(monkeypatch):
    fund = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 100)
    main_module.funds_store.record_fill(fund.id, "MSFT", main_module.Side.BUY, 5, 100)

    checked = []

    async def fake_check(fund_id, symbol):
        checked.append(symbol)
        if symbol == "AAPL":
            raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module, "_check_fund_exit", fake_check)

    asyncio.run(main_module._run_auto_exit_monitor_cycle())  # no debe propagar la excepcion

    assert set(checked) == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# order_size_suggestion: el simbolo de query param tambien debe pasar por la
# misma validacion que OrderRequest.symbol (ver app/models.py validate_symbol).
# ---------------------------------------------------------------------------

def test_order_size_suggestion_rejects_invalid_symbol():
    with pytest.raises(main_module.HTTPException) as exc_info:
        asyncio.run(main_module.order_size_suggestion(
            symbol="<script>alert(1)</script>", entry_price=100.0, stop_loss_price=90.0
        ))
    assert exc_info.value.status_code == 422


def test_order_size_suggestion_normalizes_valid_symbol():
    suggestion = asyncio.run(main_module.order_size_suggestion(symbol="  aapl ", entry_price=100.0, stop_loss_price=90.0))
    assert suggestion.quantity > 0
