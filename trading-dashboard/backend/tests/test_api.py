"""Tests de la capa HTTP (FastAPI TestClient): autenticacion por API key,
autenticacion del WebSocket, el kill switch automatico por perdida diaria,
el cambio de modo paper/live, y las trabas de asignacion de capital entre
fondos. El resto de la logica de negocio de estas areas ya tiene cobertura
a nivel de funcion en otros archivos (test_auto_trading.py, test_funds.py,
test_rules.py); este archivo cubre especificamente lo que solo se puede
probar pasando por la capa HTTP/ASGI real (headers, query params del
WebSocket, codigos de status) en vez de llamar las funciones de Python
directamente.
"""
import asyncio
import os
import tempfile
from pathlib import Path

# Mismo patron que test_auto_trading.py / test_signal_engine.py: apuntar las
# rutas de config a un directorio temporal ANTES de importar app.main, para
# no tocar los archivos reales del repo.
_tmp_dir = tempfile.mkdtemp(prefix="api_test_")
os.environ.setdefault("API_KEY", "test-key")
os.environ["RULES_PATH"] = str(Path(_tmp_dir) / "rules.yaml")
os.environ["SCREENER_PATH"] = str(Path(_tmp_dir) / "screener.yaml")
os.environ["AUDIT_DB_PATH"] = str(Path(_tmp_dir) / "audit.db")
os.environ["STATE_PATH"] = str(Path(_tmp_dir) / "state.json")
os.environ["FUNDS_PATH"] = str(Path(_tmp_dir) / "funds.json")

from datetime import datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main as main_module
from app.funds import CapitalFlow, Fund, FundsStore, FundTrade
from app.market_data import MarketDataError
from app.models import AccountSummary, Side
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig

# Sin entrar como context manager ("with TestClient(...) as client"), el
# lifespan de la app NO corre: no intenta conectar a IBKR de verdad ni
# arranca los loops en background (_broadcast_loop, _risk_monitor_loop,
# etc). Eso es lo que queremos: estos tests ejercitan los endpoints/objetos
# directamente, no el ciclo de vida completo del proceso.
client = TestClient(main_module.app)


def make_account(net_liq: float = 100_000, cash: float = 100_000, daily_pnl_pct: float = 0.0) -> AccountSummary:
    return AccountSummary(
        net_liquidation=net_liq,
        cash=cash,
        buying_power=net_liq,
        daily_pnl=net_liq * daily_pnl_pct / 100,
        daily_pnl_pct=daily_pnl_pct,
    )


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    main_module.state["mode"] = "paper"
    main_module.state["halted"] = False
    main_module.state["connected"] = False
    main_module.state["pending_orders"] = {}
    monkeypatch.setattr(main_module, "funds_store", FundsStore(tmp_path / "funds.json"))
    monkeypatch.setattr(main_module, "screener_config", ScreenerConfig())
    main_module.rules_engine.reload(RulesConfig())
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account())
    monkeypatch.setattr(main_module, "_persist_state", lambda: None)
    yield


# ---------------------------------------------------------------------------
# Autenticacion por API key (require_api_key)
# ---------------------------------------------------------------------------

def test_status_does_not_require_api_key():
    # /api/status no expone nada sensible (solo mode/connected/halted): a
    # proposito no tiene Depends(require_api_key) para que el dashboard
    # pueda mostrar el estado de conexion antes de tener configurada la key.
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_protected_endpoint_rejects_missing_api_key():
    resp = client.get("/api/rules")
    assert resp.status_code == 401


def test_protected_endpoint_rejects_wrong_api_key():
    resp = client.get("/api/rules", headers={"X-API-Key": "clave-incorrecta"})
    assert resp.status_code == 401


def test_protected_endpoint_accepts_correct_api_key():
    resp = client.get("/api/rules", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200


def test_halt_endpoint_rejects_request_without_api_key():
    resp = client.post("/api/halt", params={"value": True})
    assert resp.status_code == 401
    assert main_module.state["halted"] is False  # no se aplico el cambio


# ---------------------------------------------------------------------------
# Autenticacion del WebSocket (la API key viaja como query param, no header)
# ---------------------------------------------------------------------------

def test_websocket_rejects_missing_api_key():
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_websocket_rejects_wrong_api_key():
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws?api_key=clave-incorrecta") as ws:
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_websocket_accepts_correct_api_key():
    assert main_module.clients == []
    with client.websocket_connect("/ws?api_key=test-key"):
        assert len(main_module.clients) == 1
    assert main_module.clients == []  # se limpia al desconectar


# ---------------------------------------------------------------------------
# Kill switch automatico por perdida diaria (_risk_monitor_loop)
# ---------------------------------------------------------------------------

async def _run_one_cycle(monkeypatch):
    # El loop real es "while True: sleep(poll_interval); chequear". Bajamos
    # el intervalo a casi cero y lo corremos con un timeout corto: como nunca
    # termina solo, un TimeoutError tras al menos un ciclo es el resultado
    # esperado (no un fallo), y para entonces ya debe haber aplicado el
    # efecto del primer chequeo.
    monkeypatch.setattr(main_module.settings, "poll_interval_seconds", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(main_module._risk_monitor_loop(), timeout=0.05)


def test_risk_monitor_loop_halts_when_daily_loss_limit_breached(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(daily_pnl_pct=-3.0))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is True


def test_risk_monitor_loop_keeps_running_within_limit(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(daily_pnl_pct=-1.0))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is False


def test_risk_monitor_loop_skips_check_when_disconnected(monkeypatch):
    main_module.state["connected"] = False

    def fail_if_called():
        raise AssertionError("no deberia consultar la cuenta si no esta conectado")

    monkeypatch.setattr(main_module.broker, "get_account_summary", fail_if_called)
    asyncio.run(_run_one_cycle(monkeypatch))


def test_risk_monitor_loop_skips_check_when_already_halted(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = True

    def fail_if_called():
        raise AssertionError("no deberia consultar la cuenta si ya esta halted")

    monkeypatch.setattr(main_module.broker, "get_account_summary", fail_if_called)
    asyncio.run(_run_one_cycle(monkeypatch))


# ---------------------------------------------------------------------------
# Cambio de modo paper/live (/api/mode)
# ---------------------------------------------------------------------------

def test_set_mode_rejects_invalid_mode_value():
    resp = client.post("/api/mode", json={"mode": "turbo"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422


def test_set_mode_noop_when_already_in_target_mode(monkeypatch):
    main_module.state["mode"] = "paper"

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no deberia reconectar si el modo no cambia")

    monkeypatch.setattr(main_module.broker, "reconnect", fail_if_called)
    resp = client.post("/api/mode", json={"mode": "paper"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "paper"


def test_set_mode_rejects_live_without_live_confirm(monkeypatch):
    monkeypatch.setattr(main_module.settings, "live_confirm", "")
    resp = client.post("/api/mode", json={"mode": "live"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 403
    assert main_module.state["mode"] == "paper"  # no se aplico el cambio


def test_set_mode_rejects_live_without_ib_port_live_configured(monkeypatch):
    monkeypatch.setattr(main_module.settings, "live_confirm", "I-UNDERSTAND-THIS-USES-REAL-MONEY")
    monkeypatch.setattr(main_module.settings, "ib_port_live", None)
    resp = client.post("/api/mode", json={"mode": "live"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422
    assert main_module.state["mode"] == "paper"


def test_set_mode_switches_and_forces_halt_on_success(monkeypatch):
    monkeypatch.setattr(main_module.settings, "live_confirm", "I-UNDERSTAND-THIS-USES-REAL-MONEY")
    monkeypatch.setattr(main_module.settings, "ib_port_live", 4001)
    main_module.state["halted"] = False

    async def fake_reconnect(host, port, client_id):
        return None

    monkeypatch.setattr(main_module.broker, "reconnect", fake_reconnect)
    resp = client.post("/api/mode", json={"mode": "live"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "live"
    assert body["connected"] is True
    assert body["halted"] is True  # todo cambio de modo deja el trading pausado por seguridad
    assert main_module.state["mode"] == "live"


def test_set_mode_returns_502_and_keeps_previous_mode_when_reconnect_fails(monkeypatch):
    monkeypatch.setattr(main_module.settings, "live_confirm", "I-UNDERSTAND-THIS-USES-REAL-MONEY")
    monkeypatch.setattr(main_module.settings, "ib_port_live", 4001)
    main_module.state["mode"] = "paper"

    async def fail_reconnect(host, port, client_id):
        raise main_module.IBKRConnectionError("no se pudo conectar")

    monkeypatch.setattr(main_module.broker, "reconnect", fail_reconnect)
    resp = client.post("/api/mode", json={"mode": "live"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 502
    assert main_module.state["mode"] == "paper"
    assert main_module.state["connected"] is False


# ---------------------------------------------------------------------------
# Trabas de asignacion de capital entre fondos (_validate_capital_allocation)
# ---------------------------------------------------------------------------

def test_create_fund_rejects_when_not_connected_to_ibkr():
    main_module.state["connected"] = False
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 1000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 503


def test_create_fund_rejects_non_positive_initial_capital():
    main_module.state["connected"] = True
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 0},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


def test_create_fund_rejects_amount_exceeding_real_ibkr_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=1_000))
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 5_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422
    assert "cash real" in resp.json()["detail"]


def test_create_fund_succeeds_within_available_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=10_000))
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 4_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["cash_usd"] == 4_000


def test_create_second_fund_rejected_when_combined_allocation_exceeds_real_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=10_000))
    first = client.post(
        "/api/funds",
        json={"name": "Fondo 1", "initial_capital_usd": 7_000},
        headers={"X-API-Key": "test-key"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/funds",
        json={"name": "Fondo 2", "initial_capital_usd": 4_000},
        headers={"X-API-Key": "test-key"},
    )
    assert second.status_code == 422
    assert "cash real" in second.json()["detail"]


def test_capital_flow_deposit_rejected_when_exceeds_remaining_real_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=10_000))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 6_000},
        headers={"X-API-Key": "test-key"},
    ).json()

    resp = client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": 5_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422
    assert "cash real" in resp.json()["detail"]


def test_capital_flow_withdrawal_rejected_when_exceeds_fund_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=10_000))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 3_000},
        headers={"X-API-Key": "test-key"},
    ).json()

    resp = client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": -5_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


def test_capital_flow_withdrawal_allowed_even_when_disconnected(monkeypatch):
    # Un retiro no necesita validar contra el cash real de IBKR (solo contra
    # el propio ledger del fondo), asi que a diferencia de un deposito no
    # exige estar conectado.
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", lambda: make_account(cash=10_000))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 3_000},
        headers={"X-API-Key": "test-key"},
    ).json()

    main_module.state["connected"] = False
    resp = client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": -1_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["cash_usd"] == 2_000


# ---------------------------------------------------------------------------
# Comparativa de retorno acumulado vs. benchmark (/api/funds/roi-history)
# ---------------------------------------------------------------------------


def _bars_from_closes(dates: list[str], closes: list[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    close = pd.Series(closes, index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1_000_000},
        index=idx,
    )


def test_roi_history_rejects_missing_api_key():
    resp = client.get("/api/funds/roi-history")
    assert resp.status_code == 401


def test_roi_history_empty_when_no_funds():
    resp = client.get("/api/funds/roi-history", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json() == {
        "dates": [],
        "fund_cumulative_return_pct": [],
        "benchmark_cumulative_return_pct": [],
    }


def test_roi_history_empty_when_fund_has_no_capital_flows(monkeypatch):
    # Un fondo sin ningun aporte registrado (no deberia poder existir via la
    # API real, pero el endpoint no debe asumirlo) tampoco tiene fecha de
    # arranque para la comparacion: misma forma vacia que "sin fondos".
    fund = Fund(id="f1", name="Vacio", cash_usd=0.0, created_at=datetime.now(timezone.utc))
    main_module.funds_store.funds["f1"] = fund

    def fail_if_called(symbol, lookback_days):
        raise AssertionError("no deberia consultar market data sin capital_flows")

    monkeypatch.setattr(main_module, "get_daily_bars", fail_if_called)
    resp = client.get("/api/funds/roi-history", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["dates"] == []


def test_roi_history_empty_when_spy_data_unavailable(monkeypatch):
    flow = CapitalFlow(id="cf1", amount=10_000, created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    fund = Fund(id="f1", name="Test", cash_usd=10_000, created_at=flow.created_at, capital_flows=[flow])
    main_module.funds_store.funds["f1"] = fund

    def fake_get_daily_bars(symbol, lookback_days):
        raise MarketDataError("sin datos")

    monkeypatch.setattr(main_module, "get_daily_bars", fake_get_daily_bars)
    resp = client.get("/api/funds/roi-history", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json() == {
        "dates": [],
        "fund_cumulative_return_pct": [],
        "benchmark_cumulative_return_pct": [],
    }


def test_roi_history_computes_time_weighted_return_vs_benchmark(monkeypatch):
    """Un fondo, un aporte y una compra el mismo dia (el dia de arranque de
    la comparacion), seguido de dos dias de mark-to-market sin nuevos flujos.

    Valores hand-computed a partir de los precios sinteticos de abajo:
      - dia 1 (2024-01-02, dia del aporte+compra): cash=10000-1000=9000,
        10 AAPL @ cierre 100 -> equity=10000. Primer dia con equity>0: cum=0%.
        SPY cierra 400 (mismo dia, indexa el benchmark en 0%).
      - dia 2 (2024-01-03): AAPL cierra 110 -> equity=9000+1100=10100.
        r = (10100-10000)/10000 = 1% -> cum=1.00%. SPY cierra 404 -> +1.00%.
      - dia 3 (2024-01-04): AAPL cierra 121 -> equity=9000+1210=10210.
        r = (10210-10100)/10100 = 1.089...% -> cum=(1.01*1.0108910891)-1=2.10%.
        SPY cierra 412 -> 412/400-1 = 3.00%.
    El dia previo (2024-01-01) tiene barra de SPY pero es anterior al aporte:
    no debe aparecer en la respuesta.
    """
    flow = CapitalFlow(id="cf1", amount=10_000, created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    trade = FundTrade(
        id="t1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=10,
        price=100,
        executed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    fund = Fund(
        id="f1",
        name="Test",
        cash_usd=9_000,
        created_at=flow.created_at,
        capital_flows=[flow],
        trades=[trade],
    )
    main_module.funds_store.funds["f1"] = fund

    spy_bars = _bars_from_closes(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        [395.0, 400.0, 404.0, 412.0],
    )
    aapl_bars = _bars_from_closes(
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        [99.0, 100.0, 110.0, 121.0],
    )

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return spy_bars
        if symbol == "AAPL":
            return aapl_bars
        raise MarketDataError(f"sin datos sinteticos para {symbol}")

    monkeypatch.setattr(main_module, "get_daily_bars", fake_get_daily_bars)
    resp = client.get("/api/funds/roi-history", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert body["fund_cumulative_return_pct"] == [0.0, 1.0, 2.1]
    assert body["benchmark_cumulative_return_pct"] == [0.0, 1.0, 3.0]


def test_roi_history_falls_back_to_last_trade_price_when_symbol_data_unavailable(monkeypatch):
    """Si falla la descarga de precios del simbolo operado (pero no la de
    SPY), el endpoint no debe romperse: usa el ultimo precio de fill conocido
    como aproximacion plana del valor de esa posicion en mark-to-market."""
    flow = CapitalFlow(id="cf1", amount=10_000, created_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    trade = FundTrade(
        id="t1",
        symbol="ZZZZ",
        side=Side.BUY,
        quantity=10,
        price=100,
        executed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    fund = Fund(
        id="f1",
        name="Test",
        cash_usd=9_000,
        created_at=flow.created_at,
        capital_flows=[flow],
        trades=[trade],
    )
    main_module.funds_store.funds["f1"] = fund

    spy_bars = _bars_from_closes(
        ["2024-01-02", "2024-01-03"],
        [400.0, 404.0],
    )

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return spy_bars
        raise MarketDataError("sin datos para ZZZZ")

    monkeypatch.setattr(main_module, "get_daily_bars", fake_get_daily_bars)
    resp = client.get("/api/funds/roi-history", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    # Con ZZZZ marcado siempre al precio de fill (100), el equity no cambia
    # de un dia al otro (9000 + 10*100 = 10000 ambos dias) -> retorno 0%.
    assert body["dates"] == ["2024-01-02", "2024-01-03"]
    assert body["fund_cumulative_return_pct"] == [0.0, 0.0]
    assert body["benchmark_cumulative_return_pct"] == [0.0, 1.0]
