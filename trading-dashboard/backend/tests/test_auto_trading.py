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
from app.audit import AuditLog
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
    main_module.state["peak_equity_usd"] = None
    main_module.state["market_data_degraded"] = False
    main_module._signal_state["last_attempt_at"] = {}
    main_module._signal_state["last_attempt_at_by_strategy"] = {}
    monkeypatch.setattr(main_module, "funds_store", FundsStore(tmp_path / "funds.json"))
    # audit es un AuditLog real compartido a nivel de modulo con
    # test_signal_engine.py (y el resto de la suite): sin aislarlo, los
    # "auto_trade_executed" que este archivo registra de verdad se acumulan en
    # el mismo audit.db durante toda la sesion de pytest y pueden hacer que
    # count_trades_today() llegue a max_trades_per_day (10) por la cuenta de
    # OTROS archivos, rechazando ordenes en tests que no tienen nada que ver
    # con el limite diario. Una instancia nueva por test evita ese acople.
    monkeypatch.setattr(main_module, "audit", AuditLog(tmp_path / "audit.db"))
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
    # asyncio.Lock se ata al event loop la primera vez que alguien tiene que
    # esperarlo (contencion real), no a la creacion. Como cada test que usa
    # asyncio.run() corre en un loop nuevo, reusar el _funds_order_lock del
    # modulo entre tests con contencion real revienta con "is bound to a
    # different event loop" (o peor, deja de bloquear silenciosamente) en
    # cuanto un segundo test lo contiende desde otro loop. Una instancia
    # nueva por test evita que el binding se filtre entre tests.
    monkeypatch.setattr(main_module, "_funds_order_lock", asyncio.Lock())
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)

    async def fake_get_account_summary():
        return make_account()

    monkeypatch.setattr(main_module.broker, "get_account_summary", fake_get_account_summary)

    async def fake_get_reference_price(symbol):
        return None

    # Default: sin precio en vivo (cae al last_price de la señal, ver
    # main._try_auto_trade_entry/_draft_order_from_signal). Los tests que
    # quieren probar el precio en vivo lo overridean localmente.
    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_get_reference_price)
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


def test_auto_trade_entry_deducts_commission_from_configured_screener_config(monkeypatch):
    """El fill de entrada tiene que descontar la comision configurada en
    screener_config (no un valor fijo): si esto se rompe, el ledger del fondo
    deja de coincidir con el costo que el backtest modela para la misma
    config."""
    main_module.screener_config.commission_per_trade_usd = 2.5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0)))

    fund = main_module.funds_store.get(fund.id)
    qty = fund.owned_quantity("AAPL")
    assert fund.cash_usd == 10_000 - qty * 100.0 - 2.5


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
        raise StopLossRejectedError("rechazado", order_id=9999, stop_order_id=9998)

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


def test_auto_trade_entry_ignores_fund_with_different_strategy(monkeypatch):
    """Un fondo que elige su propia estrategia (fund.strategy_id) no debe
    recibir señales de una estrategia distinta, aunque tenga auto-trading
    activado y cupo de cash."""
    fund = main_module.funds_store.create(
        "Fondo dividendos", 10_000, auto_trading_enabled=True, strategy_id="dividend"
    )
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0), "momentum"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_auto_trade_entry_matches_fund_with_explicit_strategy(monkeypatch):
    fund = main_module.funds_store.create(
        "Fondo dividendos", 10_000, auto_trading_enabled=True, strategy_id="dividend"
    )
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0), "dividend"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") > 0


def test_auto_trade_entry_default_strategy_id_uses_global_active_strategy(monkeypatch):
    """Sin pasar strategy_id explicito (igual al comportamiento previo a este
    cambio), un fondo sin strategy_id propio sigue la estrategia activa
    global -- el llamador no necesita saber que existe esta opcion para que
    todo siga funcionando como antes."""
    main_module.screener_config.strategy_id = "momentum"
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._try_auto_trade_entry(make_signal(last_price=100.0, stop=95.0)))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") > 0


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
    assert fund.cash_usd == 9_949  # 10000 - 10*100 (compra) + 10*95 (stop reconciliado) - 1 (comision venta)


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


def test_check_fund_exit_reconciles_via_specific_stop_order_even_if_broker_aggregate_unchanged(monkeypatch):
    """Si otro fondo tiene posicion en el mismo simbolo, el agregado de toda
    la cuenta (broker.get_position_qty) puede no reflejar que ESTE fondo ya
    vendio por stop-loss (bug de reconciliacion por agregado en vez de por
    fondo). La reconciliacion por la orden especifica de este fondo
    (get_trade_fill(stop_order_id)) tiene que detectarlo igual, sin mirar el
    agregado de cuenta para nada."""
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95, stop_order_id=501
    )

    def fail_if_called(symbol):
        raise AssertionError(
            "no deberia consultar el agregado de cuenta: la orden especifica ya responde"
        )

    monkeypatch.setattr(main_module.broker, "get_position_qty", fail_if_called)
    monkeypatch.setattr(
        main_module.broker,
        "get_trade_fill",
        lambda order_id: ("Filled", 10.0, 94.5, 0.0) if order_id == 501 else None,
    )

    async def fail_if_called_order(order):
        raise AssertionError("no deberia intentar vender: ya se reconcilio todo via la orden")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called_order)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 9_944  # 10000 - 10*100 (compra) + 10*94.5 (stop reconciliado) - 1 (comision venta)


def test_check_fund_exit_reconciles_partial_fill_via_specific_stop_order(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95, stop_order_id=501
    )

    def fail_if_called(symbol):
        raise AssertionError("no deberia consultar el agregado de cuenta")

    monkeypatch.setattr(main_module.broker, "get_position_qty", fail_if_called)
    monkeypatch.setattr(
        main_module.broker,
        "get_trade_fill",
        lambda order_id: ("Submitted", 6.0, 95.0, 4.0) if order_id == 501 else None,
    )

    def fail_bars(symbol, days):
        raise main_module.MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_bars)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 4


def test_check_fund_exit_falls_back_to_aggregate_when_specific_order_not_found(monkeypatch):
    """Si la orden del stop ya no esta en self.ib.trades() de esta sesion
    (reconexion entre sesiones, o posicion abierta antes de que existiera
    stop_order_id), get_trade_fill devuelve None: cae al agregado de toda la
    cuenta como unico dato disponible, igual que el camino legado."""
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95, stop_order_id=501
    )
    monkeypatch.setattr(main_module.broker, "get_trade_fill", lambda order_id: None)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)

    async def fail_if_called(order):
        raise AssertionError("no deberia intentar vender: ya se reconcilio todo")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 9_949  # fallback usa stop_loss_price (95) como aproximacion, - 1 (comision venta)


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
    assert fund.cash_usd == 10_049  # 10000 - 10*100 (compra) + 10*105 (salida) - 1 (comision venta)


def test_check_fund_exit_cancels_orphaned_stop_loss_on_full_close(monkeypatch):
    # Bug real 2026-07-24 (AEHR/TAP): al cerrar la posicion entera por
    # sector_exit/take_profit/trend_break/max_holding_days, el stop-loss
    # protector que se habia colocado al abrir la posicion quedaba vivo en
    # IBKR. Si el precio lo tocaba mas tarde, vendia de mas sobre una
    # posicion que ya no existia -- generando un short pese a
    # allow_short_selling=false. Este test verifica que ahora se cancela.
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95, stop_order_id=777
    )
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

    cancel_calls = []

    async def fake_cancel(order_id):
        cancel_calls.append(order_id)
        return True

    monkeypatch.setattr(main_module.broker, "cancel_resting_order", fake_cancel)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert cancel_calls == [777]


def test_check_fund_exit_skips_cancel_when_position_had_no_stop_order_id(monkeypatch):
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    # Sin stop_order_id (ej. posicion reconciliada sin stop conocido).
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

    async def fail_if_called(order_id):
        raise AssertionError("no deberia intentar cancelar sin stop_order_id")

    monkeypatch.setattr(main_module.broker, "cancel_resting_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))  # no debe lanzar

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


def test_check_fund_exit_survives_cancel_resting_order_failure(monkeypatch):
    # Best-effort: si la cancelacion del stop huerfano falla (ej. error de
    # red con IBKR), el cierre de la posicion en el ledger del fondo ya
    # ocurrio y no debe revertirse ni propagar la excepcion.
    main_module.screener_config.max_holding_days = 5
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95, stop_order_id=777
    )
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

    async def fake_cancel(order_id):
        raise ConnectionError("IBKR no responde")

    monkeypatch.setattr(main_module.broker, "cancel_resting_order", fake_cancel)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))  # no debe propagar

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0


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


def test_check_fund_exit_opportunistic_ignores_trend_break(monkeypatch):
    """Oportunista no exige tendencia en la entrada (ver
    strategies/opportunistic.py), asi que salir por romper una SMA que nunca
    formo parte de su señal no tiene tesis detras: _strategy_exit_params
    debe dejarla sin chequeo de tendencia para esta estrategia, sin importar
    cuan roto este el precio."""
    main_module.screener_config.opportunistic.max_holding_days = 30
    fund = main_module.funds_store.create(
        "Fondo oportunista", 10_000, auto_trading_enabled=True, strategy_id="opportunistic"
    )
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    # Cierre muy por debajo de la sma_fast global de Momentum: si el fix no
    # aplicara, esto cerraria la posicion igual que en Momentum.
    bars = pd.DataFrame({"Close": pd.Series([110.0, 108.0, 90.0])})
    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: bars)

    async def fail_if_called(order):
        raise AssertionError("Oportunista no deberia salir por ruptura de tendencia")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 10


def test_check_fund_exit_opportunistic_closes_on_its_own_max_holding_days(monkeypatch):
    """Oportunista si tiene su propio limite de tiempo (opp.max_holding_days),
    distinto del global de Momentum."""
    main_module.screener_config.max_holding_days = 999  # el global de Momentum no deberia usarse
    main_module.screener_config.opportunistic.max_holding_days = 5
    fund = main_module.funds_store.create(
        "Fondo oportunista", 10_000, auto_trading_enabled=True, strategy_id="opportunistic"
    )
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


def test_check_fund_exit_long_term_never_times_out_or_trend_breaks(monkeypatch):
    """Largo Plazo/Dividendos son fundamentals-first (tesis a meses/año): sin
    limite de tiempo ni ruptura de tendencia, solo salen por stop-loss."""
    fund = main_module.funds_store.create(
        "Fondo largo plazo", 10_000, auto_trading_enabled=True, strategy_id="long_term"
    )
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=95)
    fund = main_module.funds_store.get(fund.id)
    fund.positions["AAPL"].opened_at = datetime.now(timezone.utc) - timedelta(days=1000)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    bars = pd.DataFrame({"Close": pd.Series([110.0, 108.0, 90.0])})  # cierre muy roto igual

    def fail_if_bars_requested(symbol, days):
        raise AssertionError(
            "Largo Plazo no chequea ruptura de tendencia: no deberia pedir barras para eso"
        )

    monkeypatch.setattr(main_module, "get_daily_bars", fail_if_bars_requested)

    async def fail_if_called(order):
        raise AssertionError("Largo Plazo no deberia salir por tiempo ni tendencia")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 10


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
        raise StopLossRejectedError("rechazado", order_id=9999, stop_order_id=9998)

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

    async def fake_reference_price(symbol):
        return 105.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
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

    async def fake_reference_price(symbol):
        return 105.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)

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

    async def fake_reference_price(symbol):
        return 105.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
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


def test_check_fund_trailing_stop_noop_when_live_price_unavailable(monkeypatch):
    main_module.screener_config.trailing_stop_enabled = True
    main_module.screener_config.atr_period = 3
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, 10, 100, stop_loss_price=90, stop_order_id=1
    )

    monkeypatch.setattr(main_module, "get_daily_bars", lambda symbol, days: _trending_bars())
    # get_reference_price ya devuelve None por defecto via el fixture reset_state.

    def fail_if_called(order_id, price):
        raise AssertionError("no deberia modificar el stop sin precio en vivo")

    monkeypatch.setattr(main_module.broker, "modify_stop_price", fail_if_called)

    asyncio.run(main_module._check_fund_trailing_stop(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 90


# ---------------------------------------------------------------------------
# _check_fund_scale_out
# ---------------------------------------------------------------------------

def _setup_scale_out_fund(stop_loss_price=90, qty=10, avg_cost=100):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(
        fund.id, "AAPL", main_module.Side.BUY, qty, avg_cost, stop_loss_price=stop_loss_price, stop_order_id=1
    )
    return fund


def test_check_fund_scale_out_noop_when_disabled():
    main_module.screener_config.scale_out_enabled = False
    fund = _setup_scale_out_fund()

    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].quantity == 10
    assert fund.positions["AAPL"].scaled_out_at is None


def test_check_fund_scale_out_noop_when_no_position():
    main_module.screener_config.scale_out_enabled = True
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))  # no debe lanzar


def test_check_fund_scale_out_noop_when_already_scaled_out(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    fund = _setup_scale_out_fund()
    main_module.funds_store.mark_scaled_out(fund.id, "AAPL")

    async def fail_if_called(symbol):
        raise AssertionError("no deberia pedir precio si ya se hizo scale-out")

    monkeypatch.setattr(main_module.broker, "get_reference_price", fail_if_called)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))


def test_check_fund_scale_out_noop_when_r_multiple_below_threshold(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    fund = _setup_scale_out_fund()  # avg_cost=100, stop=90 -> riesgo=10

    async def fake_reference_price(symbol):
        return 105.0  # R=0.5, por debajo del umbral 1.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)

    def fail_if_called(order):
        raise AssertionError("no deberia vender si no se alcanzo el R minimo")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].quantity == 10


def test_check_fund_scale_out_noop_when_rounded_sell_qty_is_zero(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    main_module.screener_config.scale_out_pct = 50
    fund = _setup_scale_out_fund(qty=1)  # floor(1 * 0.5) == 0

    async def fake_reference_price(symbol):
        return 110.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)

    def fail_if_called(order):
        raise AssertionError("no deberia vender una cantidad redondeada a 0")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))


def test_check_fund_scale_out_sells_partial_and_moves_stop_to_breakeven(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    main_module.screener_config.scale_out_pct = 50
    fund = _setup_scale_out_fund()  # avg_cost=100, stop=90, qty=10

    async def fake_reference_price(symbol):
        return 110.0  # R = (110-100)/10 = 1.0, cumple el umbral

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)
    modify_calls = []
    monkeypatch.setattr(
        main_module.broker,
        "modify_stop_price",
        lambda order_id, price, new_quantity=None: modify_calls.append((order_id, price, new_quantity)) or True,
    )

    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.quantity == 5  # 10 - floor(10*0.5)
    assert pos.avg_cost == 100  # no se toca en una venta
    assert pos.stop_loss_price == 100  # movido a breakeven
    assert pos.scaled_out_at is not None
    # new_quantity=5 (el remanente): sin esto el stop-loss que ya estaba
    # colocado en IBKR queda dimensionado para las 10 acciones originales, y
    # si se dispara mas tarde vende de mas (ver incidente AEHR/TAP 2026-07-24).
    assert modify_calls == [(1, 100, 5)]
    entries = main_module.audit.recent(1)
    assert entries[0]["action"] == "auto_trade_scale_out"


def test_check_fund_scale_out_does_not_repeat_after_first_trigger(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    main_module.screener_config.scale_out_pct = 50
    fund = _setup_scale_out_fund()

    async def fake_reference_price(symbol):
        return 110.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)
    monkeypatch.setattr(main_module.broker, "modify_stop_price", lambda order_id, price, new_quantity=None: True)

    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))
    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].quantity == 5

    def fail_if_called(order):
        raise AssertionError("no deberia repetir la venta parcial")

    monkeypatch.setattr(main_module.broker, "place_order", fail_if_called)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))
    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].quantity == 5


def test_check_fund_scale_out_keeps_position_when_order_fails(monkeypatch):
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    fund = _setup_scale_out_fund()

    async def fake_reference_price(symbol):
        return 110.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)

    async def fail_place_order(order):
        raise StopLossRejectedError("rechazada", order_id=9999, stop_order_id=9998)

    monkeypatch.setattr(main_module.broker, "place_order", fail_place_order)
    asyncio.run(main_module._check_fund_scale_out(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.quantity == 10
    assert pos.scaled_out_at is None
    entries = main_module.audit.recent(1)
    assert entries[0]["action"] == "auto_trade_scale_out_failed"


def test_check_fund_exit_scale_out_leaves_position_open_when_no_full_exit_condition(monkeypatch):
    # Integracion: _check_fund_exit llama a _check_fund_scale_out antes de
    # evaluar cierre total. Con max_holding_days/trend_break sin cumplirse,
    # la posicion debe quedar abierta (mas chica) tras el scale-out, no
    # cerrada del todo.
    main_module.screener_config.scale_out_enabled = True
    main_module.screener_config.scale_out_at_r_multiple = 1.0
    main_module.screener_config.scale_out_pct = 50
    main_module.screener_config.max_holding_days = 999
    main_module.screener_config.strategy_id = "long_term"  # sin trend_break ni timeout cercano
    fund = _setup_scale_out_fund()

    async def fake_reference_price(symbol):
        return 110.0

    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_reference_price)
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)
    monkeypatch.setattr(main_module.broker, "modify_stop_price", lambda order_id, price, new_quantity=None: True)
    monkeypatch.setattr(main_module.broker, "get_trade_fill", lambda order_id: None)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)

    asyncio.run(main_module._check_fund_exit(fund.id, "AAPL"))

    fund = main_module.funds_store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.quantity == 5
    assert pos.scaled_out_at is not None


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


def test_run_auto_exit_monitor_cycle_does_not_check_trailing_stop(monkeypatch):
    """El trailing stop corre por separado en _run_trailing_stop_monitor_cycle,
    a una cadencia mas rapida (ver ese comentario): este ciclo solo evalua
    _check_fund_exit."""
    fund = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 100)

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("el trailing stop no deberia correr en este ciclo")

    async def fake_exit(fund_id, symbol):
        pass

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fail_if_called)
    monkeypatch.setattr(main_module, "_check_fund_exit", fake_exit)

    asyncio.run(main_module._run_auto_exit_monitor_cycle())


# ---------------------------------------------------------------------------
# _run_trailing_stop_monitor_cycle
# ---------------------------------------------------------------------------

def test_run_trailing_stop_monitor_cycle_skips_when_not_paper(monkeypatch):
    main_module.state["mode"] = "live"

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear trailing stop si no esta en paper")

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fail_if_called)
    asyncio.run(main_module._run_trailing_stop_monitor_cycle())


def test_run_trailing_stop_monitor_cycle_skips_when_halted(monkeypatch):
    main_module.state["halted"] = True

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear trailing stop si esta halted")

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fail_if_called)
    asyncio.run(main_module._run_trailing_stop_monitor_cycle())


def test_run_trailing_stop_monitor_cycle_skips_when_disconnected(monkeypatch):
    main_module.state["connected"] = False

    async def fail_if_called(fund_id, symbol):
        raise AssertionError("no deberia chequear trailing stop si no esta conectado")

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fail_if_called)
    asyncio.run(main_module._run_trailing_stop_monitor_cycle())


def test_run_trailing_stop_monitor_cycle_only_checks_auto_trading_funds_with_positions(monkeypatch):
    fund_off = main_module.funds_store.create("Sin auto", 10_000, auto_trading_enabled=False)
    main_module.funds_store.record_fill(fund_off.id, "AAPL", main_module.Side.BUY, 5, 100)
    fund_on = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund_on.id, "MSFT", main_module.Side.BUY, 5, 100)

    checked = []

    async def fake_check(fund_id, symbol):
        checked.append((fund_id, symbol))

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fake_check)

    asyncio.run(main_module._run_trailing_stop_monitor_cycle())

    assert checked == [(fund_on.id, "MSFT")]


def test_run_trailing_stop_monitor_cycle_continues_after_a_check_fails(monkeypatch):
    fund = main_module.funds_store.create("Con auto", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 5, 100)
    main_module.funds_store.record_fill(fund.id, "MSFT", main_module.Side.BUY, 5, 100)

    checked = []

    async def fake_check(fund_id, symbol):
        checked.append(symbol)
        if symbol == "AAPL":
            raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module, "_check_fund_trailing_stop", fake_check)

    asyncio.run(main_module._run_trailing_stop_monitor_cycle())  # no debe propagar la excepcion

    assert set(checked) == {"AAPL", "MSFT"}
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
    assert fund.cash_usd == 3_499  # 8000 - 4500 - 1 (comision compra): la segunda compra se rechazo
    assert fund.owned_quantity("AAPL") == 45


def test_concurrent_submit_order_does_not_exceed_max_position_pct(monkeypatch):
    """Reproduce la otra carrera de _funds_order_lock: account_summary y
    position_qty se leian ANTES de adquirir el lock, asi que dos ordenes
    concurrentes sobre el MISMO simbolo evaluaban max_position_pct_of_equity
    contra el mismo current_position_qty desactualizado (0 las dos), aunque
    juntas excedan el limite. Con el fix, la segunda orden lee el
    position_qty YA actualizado por el fill de la primera (simulado via
    filled_state) y debe ser rechazada."""
    main_module.rules_engine.reload(RulesConfig(
        symbol_whitelist=["AAPL"],
        allow_extended_hours=True,
        manual_approval_threshold_usd=1_000_000,
        max_order_value_usd=100_000,
        max_position_pct_of_equity=10,
    ))

    async def fake_get_account_summary():
        return make_account(net_liq=20_000)

    monkeypatch.setattr(main_module.broker, "get_account_summary", fake_get_account_summary)

    filled_state = {"qty": 0.0}

    async def slow_place_order(order):
        await asyncio.sleep(0.05)  # ensancha la ventana de carrera si el lock fallara
        filled_state["qty"] += order.quantity
        return {"order_id": 1, "status": "Filled", "filled_qty": order.quantity, "avg_fill_price": order.limit_price}

    monkeypatch.setattr(main_module.broker, "place_order", slow_place_order)
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: filled_state["qty"])

    def make_order():
        return main_module.OrderRequest(
            symbol="AAPL",
            side=main_module.Side.BUY,
            quantity=11,
            order_type="LMT",
            limit_price=100.0,
            stop_loss_price=95.0,
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
    assert filled_state["qty"] == 11  # solo la primera orden se ejecuto


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
# fund.strategy_id: cada fondo en auto-trading puede elegir una estrategia
# distinta a la activa global (ver _run_score_recompute_cycle /
# _run_fund_strategy_auto_trade_scan en main.py).
# ---------------------------------------------------------------------------

def test_signal_scan_cycle_triggers_extra_scan_for_fund_with_distinct_strategy(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.screener_config.strategy_id = "momentum"
    main_module._signal_state["previously_passing"] = set()
    main_module.funds_store.create(
        "Fondo dividendos", 10_000, auto_trading_enabled=True, strategy_id="dividend"
    )
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [])

    called_with = []

    async def fake_extra_scan(strategy_id):
        called_with.append(strategy_id)

    monkeypatch.setattr(main_module, "_run_fund_strategy_auto_trade_scan", fake_extra_scan)

    asyncio.run(main_module._run_score_recompute_cycle())

    assert called_with == ["dividend"]


def test_signal_scan_cycle_skips_extra_scan_for_fund_matching_global_strategy(monkeypatch):
    """Si el fondo elige la misma estrategia que ya es la activa global, no
    hace falta escanearla dos veces."""
    main_module.screener_config.auto_scan_enabled = True
    main_module.screener_config.strategy_id = "momentum"
    main_module._signal_state["previously_passing"] = set()
    main_module.funds_store.create(
        "Fondo", 10_000, auto_trading_enabled=True, strategy_id="momentum"
    )
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [])

    called = []

    async def fake_extra_scan(strategy_id):
        called.append(strategy_id)

    monkeypatch.setattr(main_module, "_run_fund_strategy_auto_trade_scan", fake_extra_scan)

    asyncio.run(main_module._run_score_recompute_cycle())

    assert called == []


def test_signal_scan_cycle_skips_extra_scan_for_fund_without_auto_trading(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.screener_config.strategy_id = "momentum"
    main_module._signal_state["previously_passing"] = set()
    main_module.funds_store.create(
        "Fondo dividendos pausado", 10_000, auto_trading_enabled=False, strategy_id="dividend"
    )
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [])

    called = []

    async def fake_extra_scan(strategy_id):
        called.append(strategy_id)

    monkeypatch.setattr(main_module, "_run_fund_strategy_auto_trade_scan", fake_extra_scan)

    asyncio.run(main_module._run_score_recompute_cycle())

    assert called == []


def test_run_fund_strategy_auto_trade_scan_first_run_establishes_baseline(monkeypatch):
    signal = make_signal(symbol="DIV1", score=90.0)
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda *a, **kw: [signal])

    asyncio.run(main_module._run_fund_strategy_auto_trade_scan("dividend"))

    assert main_module._signal_state["previously_passing_by_strategy"]["dividend"] == {"DIV1"}
    assert main_module.state["pending_orders"] == {}  # nunca arma drafts manuales


def test_run_fund_strategy_auto_trade_scan_executes_auto_trade_for_matching_fund(monkeypatch):
    fund = main_module.funds_store.create(
        "Fondo dividendos", 10_000, auto_trading_enabled=True, strategy_id="dividend"
    )
    main_module._signal_state["previously_passing_by_strategy"]["dividend"] = set()
    # AAPL (no DIV1): debe estar en symbol_whitelist (ver fixture reset_state)
    # para que rules_engine.evaluate apruebe la orden.
    signal = make_signal(symbol="AAPL", score=90.0, last_price=100.0, stop=95.0)
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda *a, **kw: [signal])
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._run_fund_strategy_auto_trade_scan("dividend"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("AAPL") > 0


def test_run_fund_strategy_auto_trade_scan_holds_screener_config_lock_around_signal_state(monkeypatch):
    """Mismo motivo que el test analogo en test_signal_engine.py para
    _run_score_recompute_cycle (ver M8 del audit): update_screener_config
    REEMPLAZA _signal_state["previously_passing_by_strategy"] por un dict
    nuevo (no lo muta in place), asi que sin compartir _screener_config_lock
    este ciclo podia guardarse una referencia al dict VIEJO antes del
    reemplazo y escribir ahi, perdiendo la escritura sin que _signal_state la
    vea nunca."""
    signal = make_signal(symbol="DIV1", score=90.0)
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda *a, **kw: [signal])

    lock_held_on_access = []

    class _SpyDict(dict):
        def __getitem__(self, key):
            if key == "previously_passing_by_strategy":
                lock_held_on_access.append(main_module._screener_config_lock.locked())
            return super().__getitem__(key)

    monkeypatch.setattr(main_module, "_signal_state", _SpyDict(main_module._signal_state))

    asyncio.run(main_module._run_fund_strategy_auto_trade_scan("dividend"))

    assert lock_held_on_access
    assert all(lock_held_on_access)


def test_run_fund_strategy_auto_trade_scan_ignores_fund_with_other_strategy(monkeypatch):
    fund = main_module.funds_store.create(
        "Fondo momentum", 10_000, auto_trading_enabled=True, strategy_id="momentum"
    )
    main_module._signal_state["previously_passing_by_strategy"]["dividend"] = set()
    signal = make_signal(symbol="DIV1", score=90.0, last_price=100.0, stop=95.0)
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda *a, **kw: [signal])
    monkeypatch.setattr(main_module.broker, "place_order", _fake_place_order)

    asyncio.run(main_module._run_fund_strategy_auto_trade_scan("dividend"))

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("DIV1") == 0


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


# ---------------------------------------------------------------------------
# _ensure_protective_stop / _reconcile_unfilled_on_startup: reconciliacion
# robusta ante el incidente de MAMA (orden que parece "Cancelled" a los 5s en
# paper trading, cancela su stop, pero en realidad sigue viva y llena horas
# despues -- posiblemente cruzando un reinicio del backend en el medio).
# ---------------------------------------------------------------------------

def test_ensure_protective_stop_places_new_stop_when_none_exists(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100)

    # get_live_protective_stops usa reqAllOpenOrdersAsync, no trades(): simular
    # el escenario post-reconnect donde trades() estaria vacio pero queremos
    # asegurarnos de que no hay stop vivo.
    async def fake_get_live(symbol, side="SELL"):
        return []

    monkeypatch.setattr(main_module.broker, "get_live_protective_stops", fake_get_live)

    async def fake_place_protective_stop(symbol, quantity, stop_price, side="SELL"):
        assert symbol == "AAPL"
        assert quantity == 10
        assert stop_price == 95.0
        assert side == "SELL"
        return 555

    monkeypatch.setattr(main_module.broker, "place_protective_stop", fake_place_protective_stop)

    asyncio.run(main_module._ensure_protective_stop(fund.id, "AAPL", 95.0))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_order_id == 555


def test_ensure_protective_stop_does_nothing_when_stop_already_live(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100)

    async def fake_get_live(symbol, side="SELL"):
        return [123]  # hay un stop vivo

    monkeypatch.setattr(main_module.broker, "get_live_protective_stops", fake_get_live)

    async def fake_cancel_duplicates(symbol, side="SELL", keep_order_id=None):
        return []  # un solo stop, nada que cancelar

    monkeypatch.setattr(main_module.broker, "cancel_duplicate_protective_stops", fake_cancel_duplicates)

    async def fail_if_called(symbol, quantity, stop_price, side="SELL"):
        raise AssertionError("no deberia colocar un stop si ya hay uno vivo")

    monkeypatch.setattr(main_module.broker, "place_protective_stop", fail_if_called)

    asyncio.run(main_module._ensure_protective_stop(fund.id, "AAPL", 95.0))

    fund = main_module.funds_store.get(fund.id)
    assert fund.positions["AAPL"].stop_order_id is None  # no se toco


def test_ensure_protective_stop_noop_when_fund_has_no_position(monkeypatch):
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    # Sin posicion, qty==0: retorna antes de consultar al broker.

    async def fail_if_called(symbol, quantity, stop_price, side="SELL"):
        raise AssertionError("no deberia colocar un stop sin posicion que proteger")

    monkeypatch.setattr(main_module.broker, "place_protective_stop", fail_if_called)

    asyncio.run(main_module._ensure_protective_stop(fund.id, "AAPL", 95.0))  # no debe explotar


def test_reconcile_unfilled_on_startup_places_stop_after_reconciling(monkeypatch):
    # Escenario MAMA: IBKR ya muestra la posicion (llenó), pero el fondo
    # todavia no la tiene registrada, y el stop original ya no esta vivo.
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.audit.record(
        "auto_trade_submitted_unfilled",
        {"symbol": "MAMA", "quantity": 27.0, "stop_loss_price": 16.91, "fund_id": fund.id},
        {"fund_id": fund.id, "order_id": 277781, "filled_qty": 0.0},
    )

    class _FakePosition:
        symbol = "MAMA"
        quantity = 27.0
        avg_cost = 18.417
        market_price = 18.21
        unrealized_pnl = None

    async def fake_get_positions():
        return [_FakePosition()]

    monkeypatch.setattr(main_module.broker, "get_positions", fake_get_positions)

    async def fake_get_live(symbol, side="SELL"):
        return []  # sin stops vivos: corresponde colocar uno nuevo

    monkeypatch.setattr(main_module.broker, "get_live_protective_stops", fake_get_live)

    stop_calls = []

    async def fake_place_protective_stop(symbol, quantity, stop_price, side="SELL"):
        stop_calls.append((symbol, quantity, stop_price, side))
        return 999

    monkeypatch.setattr(main_module.broker, "place_protective_stop", fake_place_protective_stop)

    asyncio.run(main_module._reconcile_unfilled_on_startup())

    fund = main_module.funds_store.get(fund.id)
    assert fund.owned_quantity("MAMA") == 27.0
    assert fund.positions["MAMA"].stop_order_id == 999
    assert stop_calls == [("MAMA", 27.0, 16.91, "SELL")]


def test_reconcile_unfilled_on_startup_subscribes_to_late_fill_when_still_pending(monkeypatch):
    # La orden todavia NO muestra posicion en IBKR (sigue pendiente) -- antes
    # de este fix, se descartaba para siempre; ahora debe suscribirse al
    # fill tardio via broker.subscribe_fill.
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.audit.record(
        "auto_trade_submitted_unfilled",
        {"symbol": "MAMA", "quantity": 27.0, "stop_loss_price": 16.91, "fund_id": fund.id},
        {"fund_id": fund.id, "order_id": 277781, "filled_qty": 0.0},
    )

    async def fake_get_positions():
        return []  # todavia sin posicion en IBKR

    monkeypatch.setattr(main_module.broker, "get_positions", fake_get_positions)

    subscribe_calls = []

    def fake_subscribe_fill(order_id, callback):
        subscribe_calls.append(order_id)
        return True

    monkeypatch.setattr(main_module.broker, "subscribe_fill", fake_subscribe_fill)

    asyncio.run(main_module._reconcile_unfilled_on_startup())

    assert subscribe_calls == [277781]
    # Todavia no se registro nada en el fondo -- recien cuando el callback
    # de subscribe_fill dispare (simulado en otros tests de
    # _register_fund_fill_reconciliation).
    assert fund.owned_quantity("MAMA") == 0


# ---------------------------------------------------------------------------
# Test de regresion del incidente AEHR/TAP 2026-07-24: stops duplicados tras
# reconnect. has_live_protective_stop usaba self.ib.trades() (vacio post-
# reconnect), permitiendo a _ensure_protective_stop colocar un segundo stop
# sobre uno que ya existia en IBKR. Al dispararse ambos se vendia el doble
# -> short desnudo. (Ver C1 del NUEVO_INFORME y project_orphan_stop_short_bug)
# ---------------------------------------------------------------------------

def test_ensure_protective_stop_never_places_twice_when_ibkr_reports_live_stop(monkeypatch):
    """Simula el escenario post-reconnect: self.ib.trades() esta vacio (como si
    never hubiera ocurrido en esta sesion), pero reqAllOpenOrdersAsync (la fuente
    real de verdad) reporta el stop como vivo. _ensure_protective_stop debe
    detectarlo y NO colocar un segundo stop, incluso si se llama dos veces."""
    fund = main_module.funds_store.create("Fondo", 10_000, auto_trading_enabled=True)
    main_module.funds_store.record_fill(fund.id, "AAPL", main_module.Side.BUY, 10, 100)
    # Registrar el stop_order_id existente (como si ya se hubiera colocado antes
    # del restart del backend).
    main_module.funds_store.set_stop_order_id(fund.id, "AAPL", 777)

    # reqAllOpenOrdersAsync dice que el stop 777 esta vivo (post-reconnect);
    # trades() devolveria [] (no se mockea aqui -- el fix es que ya no se usa).
    async def fake_get_live(symbol, side="SELL"):
        return [777]

    monkeypatch.setattr(main_module.broker, "get_live_protective_stops", fake_get_live)

    async def fake_cancel_duplicates(symbol, side="SELL", keep_order_id=None):
        return []  # un solo stop, nada que cancelar

    monkeypatch.setattr(main_module.broker, "cancel_duplicate_protective_stops", fake_cancel_duplicates)

    place_calls = []

    async def fail_if_place_called(symbol, quantity, stop_price, side="SELL"):
        place_calls.append((symbol, quantity, stop_price))
        raise AssertionError("no debe colocar un stop si ya hay uno vivo en IBKR")

    monkeypatch.setattr(main_module.broker, "place_protective_stop", fail_if_place_called)

    # Llamar dos veces seguidas (simula el polling de _ensure_missing_protective_stops
    # corriendo antes y despues del reconnect).
    asyncio.run(main_module._ensure_protective_stop(fund.id, "AAPL", 95.0))
    asyncio.run(main_module._ensure_protective_stop(fund.id, "AAPL", 95.0))

    assert place_calls == [], "place_protective_stop no debia llamarse ni una vez"
