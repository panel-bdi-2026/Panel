import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# app.main importa app.config, que instancia Settings() (y por lo tanto exige
# API_KEY) al momento del import, y crea AuditLog/state.json apuntando a
# archivos reales del proyecto. Apuntamos esas rutas a un directorio temporal
# ANTES de importar app.main para no tocar audit.db/state.json/rules.yaml
# reales del repo con datos de tests.
_tmp_dir = tempfile.mkdtemp(prefix="signal_engine_test_")
os.environ.setdefault("API_KEY", "test-key")
os.environ["RULES_PATH"] = str(Path(_tmp_dir) / "rules.yaml")
os.environ["SCREENER_PATH"] = str(Path(_tmp_dir) / "screener.yaml")
os.environ["AUDIT_DB_PATH"] = str(Path(_tmp_dir) / "audit.db")
os.environ["STATE_PATH"] = str(Path(_tmp_dir) / "state.json")

import pytest

from app import main as main_module
from app.models import AccountSummary, OrderDecision, PendingOrder, SignalResult
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


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    main_module.state["pending_orders"] = {}
    main_module.state["halted"] = False
    main_module.state["connected"] = True
    main_module._signal_state["previously_passing"] = None
    # Varios tests mutan atributos de screener_config directamente (ej.
    # auto_scan_enabled, top_n) sin pasar por monkeypatch. Sin aislar el
    # objeto, esa mutacion persiste mas alla del test (y del archivo, ya que
    # screener_config es un singleton de modulo compartido con
    # test_auto_trading.py), filtrando estado entre tests. Una instancia
    # nueva por test, swapeada con monkeypatch, se revierte sola al terminar.
    monkeypatch.setattr(main_module, "screener_config", ScreenerConfig())
    main_module.rules_engine.reload(RulesConfig(
        symbol_whitelist=["AAPL", "MSFT"],
        allow_extended_hours=True,
        manual_approval_threshold_usd=1_000_000,
    ))
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account())
    monkeypatch.setattr(main_module, "_persist_state", lambda: None)
    yield


def test_draft_order_from_signal_creates_pending_order_with_signal_source():
    pending = main_module._draft_order_from_signal(make_signal())
    assert pending is not None
    assert pending.source == "signal_engine"
    assert pending.order.symbol == "AAPL"
    assert pending.order.quantity > 0
    assert pending.id in main_module.state["pending_orders"]


def test_draft_order_from_signal_skips_when_existing_position(monkeypatch):
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)
    pending = main_module._draft_order_from_signal(make_signal())
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_draft_order_from_signal_skips_when_pending_order_already_exists_for_symbol():
    existing = PendingOrder(
        id="existing-1",
        order=main_module.OrderRequest(symbol="AAPL", side=main_module.Side.BUY, quantity=1, stop_loss_price=90),
        decision=OrderDecision(approved=True, requires_manual_approval=True, estimated_value_usd=100),
        created_at=datetime.now(timezone.utc),
        source="user",
    )
    main_module.state["pending_orders"]["existing-1"] = existing

    pending = main_module._draft_order_from_signal(make_signal())
    assert pending is None
    assert len(main_module.state["pending_orders"]) == 1


def test_draft_order_from_signal_skips_when_sizing_zero(monkeypatch):
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(net_liq=0))
    pending = main_module._draft_order_from_signal(make_signal())
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_draft_order_from_signal_skips_when_rules_engine_rejects():
    main_module.rules_engine.reload(RulesConfig(symbol_whitelist=[]))  # rechaza todo
    pending = main_module._draft_order_from_signal(make_signal())
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_skips_when_auto_scan_disabled(monkeypatch):
    main_module.screener_config.auto_scan_enabled = False

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si auto_scan_enabled=False")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_signal_scan_cycle())


def test_signal_scan_cycle_skips_when_halted(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.state["halted"] = True

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si esta halted")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_signal_scan_cycle())


def test_signal_scan_cycle_skips_when_disconnected(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.state["connected"] = False

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si no esta conectado")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_signal_scan_cycle())


def test_signal_scan_cycle_first_run_establishes_baseline_without_drafting(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    monkeypatch.setattr(main_module.screener, "scan", lambda: [make_signal(passes=True)])

    async def fail_if_called(result):
        raise AssertionError("el primer ciclo no deberia draftear ordenes")

    monkeypatch.setattr(main_module, "_draft_order_from_signal", fail_if_called)

    asyncio.run(main_module._run_signal_scan_cycle())

    assert main_module._signal_state["previously_passing"] == {"AAPL"}
    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_drafts_order_for_new_passing_symbol(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = set()  # baseline ya establecida, nada pasaba
    monkeypatch.setattr(main_module.screener, "scan", lambda: [make_signal(symbol="AAPL", passes=True)])

    asyncio.run(main_module._run_signal_scan_cycle())

    assert main_module._signal_state["previously_passing"] == {"AAPL"}
    assert len(main_module.state["pending_orders"]) == 1
    drafted = list(main_module.state["pending_orders"].values())[0]
    assert drafted.source == "signal_engine"
    assert drafted.order.symbol == "AAPL"


def test_signal_scan_cycle_does_not_redraft_symbol_already_passing(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = {"AAPL"}  # ya estaba pasando antes
    monkeypatch.setattr(main_module.screener, "scan", lambda: [make_signal(symbol="AAPL", passes=True)])

    asyncio.run(main_module._run_signal_scan_cycle())

    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_handles_scan_failure_gracefully(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = set()

    def failing_scan():
        raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module.screener, "scan", failing_scan)

    asyncio.run(main_module._run_signal_scan_cycle())  # no debe propagar la excepcion

    assert main_module._signal_state["previously_passing"] == set()


def test_update_screener_config_resets_signal_baseline():
    main_module._signal_state["previously_passing"] = {"AAPL"}
    body = main_module.ScreenerUpdate(config=main_module.screener_config.model_dump())
    main_module.update_screener_config(body, None)
    assert main_module._signal_state["previously_passing"] is None


def test_update_screener_config_syncs_whitelist_with_universe():
    new_universe = ["ZZZ", "YYY"]
    config = main_module.screener_config.model_dump()
    config["universe"] = new_universe
    body = main_module.ScreenerUpdate(config=config)

    main_module.update_screener_config(body, None)

    assert main_module.rules_config.symbol_whitelist == new_universe
    assert main_module.rules_engine.config.symbol_whitelist == new_universe


def test_sync_whitelist_with_universe_is_noop_when_already_synced(monkeypatch):
    main_module.rules_config.symbol_whitelist = list(main_module.screener_config.universe)
    save_calls = []
    monkeypatch.setattr(main_module.RulesConfig, "save", lambda self, path: save_calls.append(path))

    main_module._sync_whitelist_with_universe()

    assert save_calls == []


def test_signal_scan_cycle_caps_drafts_per_cycle(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.screener_config.top_n = 10
    main_module.screener_config.max_auto_drafts_per_cycle = 3
    main_module._signal_state["previously_passing"] = set()  # base ya establecida
    main_module.rules_engine.reload(RulesConfig(
        symbol_whitelist=["S0", "S1", "S2", "S3", "S4"],
        allow_extended_hours=True,
        manual_approval_threshold_usd=1_000_000,
    ))
    # 5 simbolos transicionan a "pasa" en el mismo ciclo, ordenados por score
    # descendente (S0 el mas fuerte). Con el tope de 3, solo se draftean los 3
    # mejores.
    signals = [make_signal(symbol=f"S{i}", score=10 - i, passes=True) for i in range(5)]
    monkeypatch.setattr(main_module.screener, "scan", lambda: signals)

    asyncio.run(main_module._run_signal_scan_cycle())

    drafted = list(main_module.state["pending_orders"].values())
    assert len(drafted) == 3
    assert {p.order.symbol for p in drafted} == {"S0", "S1", "S2"}


def test_signal_scan_cycle_respects_free_slots_vs_top_n(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.screener_config.top_n = 2
    main_module.screener_config.max_auto_drafts_per_cycle = 5
    main_module._signal_state["previously_passing"] = set()
    main_module.rules_engine.reload(RulesConfig(
        symbol_whitelist=["S0", "S1", "S2"],
        allow_extended_hours=True,
        manual_approval_threshold_usd=1_000_000,
    ))
    # Ya hay 1 orden pendiente: con top_n=2 solo queda 1 cupo libre, asi que
    # aunque el tope por ciclo sea 5 y haya 3 señales nuevas, se draftea 1 sola.
    existing = PendingOrder(
        id="pending-x",
        order=main_module.OrderRequest(symbol="ZZ", side=main_module.Side.BUY, quantity=1, stop_loss_price=90),
        decision=OrderDecision(approved=True, requires_manual_approval=True, estimated_value_usd=100),
        created_at=datetime.now(timezone.utc),
        source="user",
    )
    main_module.state["pending_orders"]["pending-x"] = existing

    signals = [make_signal(symbol=f"S{i}", score=10 - i, passes=True) for i in range(3)]
    monkeypatch.setattr(main_module.screener, "scan", lambda: signals)

    asyncio.run(main_module._run_signal_scan_cycle())

    drafted = [p for p in main_module.state["pending_orders"].values() if p.source == "signal_engine"]
    assert len(drafted) == 1
    assert drafted[0].order.symbol == "S0"
