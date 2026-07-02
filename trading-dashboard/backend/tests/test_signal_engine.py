import asyncio
import os
import tempfile
import time
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
from app import market_data as market_data_module
from app.funds import FundsStore
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


def make_signal(symbol="AAPL", last_price=100.0, stop=95.0, passes=True, score=65.0) -> SignalResult:
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
def reset_state(monkeypatch, tmp_path):
    main_module.state["pending_orders"] = {}
    main_module.state["halted"] = False
    main_module.state["connected"] = True
    main_module.state["peak_equity_usd"] = None
    main_module.state["market_data_degraded"] = False
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
    # _draft_fund_order_from_signal necesita un fondo aislado por test (mismo
    # motivo que screener_config arriba): sin esto, los fondos creados por un
    # test persistirian en el mismo funds_store compartido y contaminarian
    # los candidatos de otro test de este archivo.
    monkeypatch.setattr(main_module, "funds_store", FundsStore(tmp_path / "funds.json"))
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)

    async def fake_get_account_summary():
        return make_account()

    async def fake_get_reference_price(symbol):
        return None

    async def fake_get_positions():
        return []

    async def fake_place_order(order):
        # _run_score_recompute_cycle ahora intenta _try_auto_trade_entry
        # ANTES del draft manual para cualquier fondo candidato (ver
        # _draft_fund_order_from_signal): sin este mock, un fondo creado por
        # un test para servir de candidato del DRAFT tambien calificaria
        # para el auto-trade, y este intentaria un placeOrder real contra un
        # IB() sin conexion. Rechazarlo silenciosamente (mismo camino que un
        # StopLossRejectedError real) deja get_position_qty en 0, para que
        # el draft se pueda evaluar tal como este archivo espera probarlo.
        raise main_module.StopLossRejectedError("test: sin ejecucion real en test_signal_engine.py", order_id=9999, stop_order_id=9998)

    monkeypatch.setattr(main_module.broker, "get_account_summary", fake_get_account_summary)
    # Por defecto no hay precio en vivo de IBKR: cae al last_price de la
    # señal (igual que el comportamiento previo a usar precio en vivo), salvo
    # que un test override este mock puntualmente.
    monkeypatch.setattr(main_module.broker, "get_reference_price", fake_get_reference_price)
    monkeypatch.setattr(main_module.broker, "get_positions", fake_get_positions)
    monkeypatch.setattr(main_module.broker, "place_order", fake_place_order)
    monkeypatch.setattr(main_module, "_persist_state", lambda: None)
    yield


def make_auto_trading_fund(strategy_id="momentum", cash=100_000.0):
    """Fondo candidato para _draft_fund_order_from_signal: auto_trading_
    enabled=True y con la estrategia indicada, para que aparezca en la lista
    de candidatos igual que en produccion."""
    return main_module.funds_store.create(
        "Fondo de prueba", cash, auto_trading_enabled=True, strategy_id=strategy_id,
    )


def test_draft_fund_order_from_signal_creates_pending_order_with_signal_source():
    fund = make_auto_trading_fund()
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is not None
    assert pending.source == "signal_engine"
    assert pending.strategy_id == "momentum"
    assert pending.order.symbol == "AAPL"
    assert pending.order.fund_id == fund.id
    assert pending.order.quantity > 0
    assert pending.id in main_module.state["pending_orders"]


def test_draft_fund_order_from_signal_sizes_against_fund_equity_not_whole_account():
    # Antes (_draft_order_from_signal, retirada), el sizing usaba
    # account.net_liquidation (100,000 en make_account()) sin importar el
    # fondo: un fondo bien mas chico ($2,000) debe producir una cantidad
    # sugerida mucho menor a la que hubiera dado dimensionar contra toda la
    # cuenta.
    make_auto_trading_fund(cash=2_000.0)
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is not None
    # risk_per_trade_pct default 1% de $2,000 = $20 de riesgo; con
    # stop a $5 de distancia (100 vs 95), la cantidad sugerida es ~4, muy
    # por debajo de lo que hubiera dado sizear contra $100,000 (~200).
    assert pending.order.quantity < 20


def test_draft_fund_order_from_signal_records_signal_rationale_in_audit():
    # Igual que en auto_trade_executed: el audit trail del draft manual tiene
    # que guardar la señal completa (score/momentum/RSI/notas), no solo el
    # score suelto, para poder revisar despues por que el motor sugirio esta
    # orden.
    make_auto_trading_fund()
    signal = make_signal(score=7.5)
    pending = asyncio.run(main_module._draft_fund_order_from_signal(signal, "momentum"))
    assert pending is not None

    entries = main_module.audit.recent(1)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["action"] == "signal_order_drafted"
    assert entry["result"]["id"] == pending.id
    assert entry["result"]["strategy_id"] == "momentum"
    recorded_signal = entry["result"]["signal"]
    assert recorded_signal["symbol"] == "AAPL"
    assert recorded_signal["score"] == 7.5
    assert recorded_signal["momentum_3m_pct"] == signal.momentum_3m_pct
    assert recorded_signal["rsi"] == signal.rsi
    assert recorded_signal["notes"] == signal.notes


def test_draft_fund_order_from_signal_skips_when_no_fund_follows_strategy():
    # Ningun fondo creado: sin candidatos, no se genera ningun borrador --
    # asi una estrategia que ningun fondo sigue (ej. Momentum, si el/los
    # fondos activos eligieron Oportunista) deja de inundar la cola sin
    # necesitar un chequeo aparte.
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_draft_fund_order_from_signal_skips_when_existing_position(monkeypatch):
    make_auto_trading_fund()
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 10)
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_draft_fund_order_from_signal_skips_when_pending_order_already_exists_for_symbol():
    make_auto_trading_fund()
    existing = PendingOrder(
        id="existing-1",
        order=main_module.OrderRequest(symbol="AAPL", side=main_module.Side.BUY, quantity=1, stop_loss_price=90),
        decision=OrderDecision(approved=True, requires_manual_approval=True, estimated_value_usd=100),
        created_at=datetime.now(timezone.utc),
        source="user",
    )
    main_module.state["pending_orders"]["existing-1"] = existing

    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is None
    assert len(main_module.state["pending_orders"]) == 1


def test_draft_fund_order_from_signal_skips_when_sizing_zero():
    make_auto_trading_fund(cash=0.01)  # cash insuficiente para ni una accion
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_draft_fund_order_from_signal_skips_when_rules_engine_rejects():
    make_auto_trading_fund()
    main_module.rules_engine.reload(RulesConfig(symbol_whitelist=[]))  # rechaza todo
    pending = asyncio.run(main_module._draft_fund_order_from_signal(make_signal(), "momentum"))
    assert pending is None
    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_skips_when_auto_scan_disabled(monkeypatch):
    main_module.screener_config.auto_scan_enabled = False

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si auto_scan_enabled=False")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_score_recompute_cycle())


def test_signal_scan_cycle_skips_when_halted(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.state["halted"] = True

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si esta halted")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_score_recompute_cycle())


def test_signal_scan_cycle_skips_when_disconnected(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module.state["connected"] = False

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia escanear si no esta conectado")

    monkeypatch.setattr(main_module.screener, "scan", fail_if_called)
    asyncio.run(main_module._run_score_recompute_cycle())


def test_signal_scan_cycle_first_run_establishes_baseline_without_drafting(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [make_signal(passes=True)])

    async def fail_if_called(result, strategy_id):
        raise AssertionError("el primer ciclo no deberia draftear ordenes")

    monkeypatch.setattr(main_module, "_draft_fund_order_from_signal", fail_if_called)

    asyncio.run(main_module._run_score_recompute_cycle())

    assert main_module._signal_state["previously_passing"] == {"AAPL"}
    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_drafts_order_for_new_passing_symbol(monkeypatch):
    make_auto_trading_fund()
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = set()  # baseline ya establecida, nada pasaba
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [make_signal(symbol="AAPL", passes=True)])

    asyncio.run(main_module._run_score_recompute_cycle())

    assert main_module._signal_state["previously_passing"] == {"AAPL"}
    assert len(main_module.state["pending_orders"]) == 1
    drafted = list(main_module.state["pending_orders"].values())[0]
    assert drafted.source == "signal_engine"
    assert drafted.order.symbol == "AAPL"
    assert drafted.strategy_id == "momentum"


def test_signal_scan_cycle_does_not_redraft_symbol_already_passing(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = {"AAPL"}  # ya estaba pasando antes
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [make_signal(symbol="AAPL", passes=True)])

    asyncio.run(main_module._run_score_recompute_cycle())

    assert main_module.state["pending_orders"] == {}


def test_signal_scan_cycle_handles_scan_failure_gracefully(monkeypatch):
    main_module.screener_config.auto_scan_enabled = True
    main_module._signal_state["previously_passing"] = set()

    def failing_scan(*a, **kw):
        raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module.screener, "scan", failing_scan)

    asyncio.run(main_module._run_score_recompute_cycle())  # no debe propagar la excepcion

    assert main_module._signal_state["previously_passing"] == set()


def test_signal_scan_cycle_holds_screener_config_lock_around_signal_state_update(monkeypatch):
    """update_screener_config corre en un thread del pool (es un endpoint sync,
    no async) y resetea _signal_state bajo _screener_config_lock (ver M8 del
    audit); este ciclo, que corre en el event loop, debe tomar el MISMO lock
    al leer y escribir previously_passing -- si no, un reset de config en
    pleno vuelo de un ciclo se podia perder (el ciclo leia el valor previo al
    reset y lo pisaba de nuevo al escribir, devolviendo intacta la base vieja
    que el reset queria descartar). Se verifica espiando los accesos al dict
    en vez de con una raza real entre threads (no deterministica): el lock
    debe estar tomado en cada get/set de 'previously_passing'."""
    main_module.screener_config.auto_scan_enabled = True
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: [make_signal(passes=True)])

    lock_held_on_access = []

    class _SpyDict(dict):
        def __getitem__(self, key):
            if key == "previously_passing":
                lock_held_on_access.append(main_module._screener_config_lock.locked())
            return super().__getitem__(key)

        def __setitem__(self, key, value):
            if key == "previously_passing":
                lock_held_on_access.append(main_module._screener_config_lock.locked())
            return super().__setitem__(key, value)

    monkeypatch.setattr(main_module, "_signal_state", _SpyDict(main_module._signal_state))

    asyncio.run(main_module._run_score_recompute_cycle())

    assert lock_held_on_access  # se accedio al menos una vez (get + set)
    assert all(lock_held_on_access)


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


def test_update_screener_config_partial_nested_update_preserves_other_fields():
    before = main_module.screener_config.model_dump()
    new_max_pe = (before["long_term"]["max_pe_ratio"] or 0) + 1
    body = main_module.ScreenerUpdate(config={"long_term": {"max_pe_ratio": new_max_pe}})

    main_module.update_screener_config(body, None)

    after = main_module.screener_config.model_dump()
    assert after["long_term"]["max_pe_ratio"] == new_max_pe
    # Un PUT que solo toca un campo de un sub-objeto anidado no debe perder ni
    # el resto de los campos de ese sub-objeto, ni las otras estrategias, ni
    # los campos de nivel superior.
    other_long_term_before = {k: v for k, v in before["long_term"].items() if k != "max_pe_ratio"}
    other_long_term_after = {k: v for k, v in after["long_term"].items() if k != "max_pe_ratio"}
    assert other_long_term_after == other_long_term_before
    assert after["opportunistic"] == before["opportunistic"]
    assert after["dividend"] == before["dividend"]
    assert after["universe"] == before["universe"]
    assert after["sma_fast"] == before["sma_fast"]


def test_update_screener_config_partial_top_level_update_preserves_nested_subconfigs():
    before = main_module.screener_config.model_dump()
    new_sma_fast = before["sma_fast"] + 1
    body = main_module.ScreenerUpdate(config={"sma_fast": new_sma_fast})

    main_module.update_screener_config(body, None)

    after = main_module.screener_config.model_dump()
    assert after["sma_fast"] == new_sma_fast
    assert after["opportunistic"] == before["opportunistic"]
    assert after["long_term"] == before["long_term"]
    assert after["dividend"] == before["dividend"]
    assert after["universe"] == before["universe"]


def test_sync_whitelist_with_universe_is_noop_when_already_synced(monkeypatch):
    main_module.rules_config.symbol_whitelist = list(main_module.screener_config.universe)
    save_calls = []
    monkeypatch.setattr(main_module.RulesConfig, "save", lambda self, path: save_calls.append(path))

    main_module._sync_whitelist_with_universe()

    assert save_calls == []


def test_signal_scan_cycle_caps_drafts_per_cycle(monkeypatch):
    make_auto_trading_fund()
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
    signals = [make_signal(symbol=f"S{i}", score=100 - i, passes=True) for i in range(5)]
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: signals)

    asyncio.run(main_module._run_score_recompute_cycle())

    drafted = list(main_module.state["pending_orders"].values())
    assert len(drafted) == 3
    assert {p.order.symbol for p in drafted} == {"S0", "S1", "S2"}


def test_signal_scan_cycle_respects_free_slots_vs_top_n(monkeypatch):
    make_auto_trading_fund()
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

    signals = [make_signal(symbol=f"S{i}", score=100 - i, passes=True) for i in range(3)]
    monkeypatch.setattr(main_module.screener, "scan", lambda *a, **kw: signals)

    asyncio.run(main_module._run_score_recompute_cycle())

    drafted = [p for p in main_module.state["pending_orders"].values() if p.source == "signal_engine"]
    assert len(drafted) == 1
    assert drafted[0].order.symbol == "S0"


# ---------------------------------------------------------------------------
# _check_market_data_degradation
# ---------------------------------------------------------------------------

def _set_universe_with_failures(failed_symbols, ok_symbols, lookback_days=400):
    market_data_module._bars_failure_cache.clear()
    market_data_module._cache.clear()
    now = time.time()
    for s in failed_symbols:
        market_data_module._bars_failure_cache[(s, lookback_days)] = (now, "fallo simulado")
    for s in ok_symbols:
        market_data_module._cache[(s, lookback_days)] = (now, object())
    main_module.screener_config.universe = failed_symbols + ok_symbols
    main_module.screener_config.lookback_days = lookback_days


def test_check_market_data_degradation_noop_when_below_threshold():
    main_module.screener_config.market_data_degradation_alert_pct = 20
    _set_universe_with_failures(failed_symbols=["S0"], ok_symbols=[f"S{i}" for i in range(1, 10)])  # 10%

    asyncio.run(main_module._check_market_data_degradation())

    assert main_module.state["market_data_degraded"] is False
    assert all(e["action"] != "market_data_degraded" for e in main_module.audit.recent(10))


def test_check_market_data_degradation_flags_and_audits_on_breach():
    main_module.screener_config.market_data_degradation_alert_pct = 20
    _set_universe_with_failures(failed_symbols=["S0", "S1", "S2"], ok_symbols=["S3", "S4"])  # 60%

    asyncio.run(main_module._check_market_data_degradation())

    assert main_module.state["market_data_degraded"] is True
    entry = main_module.audit.recent(1)[0]
    assert entry["action"] == "market_data_degraded"
    assert entry["result"]["failed"] == 3
    assert entry["result"]["total"] == 5


def test_check_market_data_degradation_does_not_repeat_audit_while_still_degraded():
    main_module.screener_config.market_data_degradation_alert_pct = 20
    _set_universe_with_failures(failed_symbols=["S0", "S1", "S2"], ok_symbols=["S3", "S4"])

    asyncio.run(main_module._check_market_data_degradation())
    count_after_first = len(main_module.audit.recent(10))

    asyncio.run(main_module._check_market_data_degradation())  # sigue degradado, mismo % de fallo
    count_after_second = len(main_module.audit.recent(10))

    assert count_after_second == count_after_first  # sin nueva entrada de audit


def test_check_market_data_degradation_recovers():
    main_module.screener_config.market_data_degradation_alert_pct = 20
    _set_universe_with_failures(failed_symbols=["S0", "S1", "S2"], ok_symbols=["S3", "S4"])
    asyncio.run(main_module._check_market_data_degradation())
    assert main_module.state["market_data_degraded"] is True

    _set_universe_with_failures(failed_symbols=[], ok_symbols=["S0", "S1", "S2", "S3", "S4"])
    asyncio.run(main_module._check_market_data_degradation())

    assert main_module.state["market_data_degraded"] is False
    entry = main_module.audit.recent(1)[0]
    assert entry["action"] == "market_data_recovered"


def test_check_market_data_degradation_noop_when_universe_empty():
    main_module.screener_config.universe = []
    asyncio.run(main_module._check_market_data_degradation())  # no debe lanzar
    assert main_module.state["market_data_degraded"] is False
