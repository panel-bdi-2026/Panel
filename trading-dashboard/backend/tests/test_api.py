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
import threading
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
from app.models import AccountSummary, Side, SignalResult
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


def _async_account_summary(account: AccountSummary):
    """Wrapea un AccountSummary en una funcion async, para monkeypatchear
    broker.get_account_summary (que ahora es async, ver broker.py)."""
    async def fake() -> AccountSummary:
        return account
    return fake


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    main_module.state["mode"] = "paper"
    main_module.state["halted"] = False
    main_module.state["connected"] = False
    main_module.state["pending_orders"] = {}
    main_module.state["peak_equity_usd"] = None
    monkeypatch.setattr(main_module, "funds_store", FundsStore(tmp_path / "funds.json"))
    monkeypatch.setattr(main_module, "screener_config", ScreenerConfig())
    main_module.rules_engine.reload(RulesConfig())
    monkeypatch.setattr(main_module.broker, "get_position_qty", lambda symbol: 0)
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account()))
    monkeypatch.setattr(main_module, "_persist_state", lambda: None)
    # asyncio.Lock se ata al event loop la primera vez que alguien tiene que
    # esperarlo (contencion real), no a la creacion. Como cada test que usa
    # asyncio.run() corre en un loop nuevo, reusar el _funds_order_lock del
    # modulo entre tests con contencion real revienta con "is bound to a
    # different event loop" (o peor, deja de bloquear silenciosamente) en
    # cuanto un segundo test lo contiende desde otro loop. Una instancia
    # nueva por test evita que el binding se filtre entre tests.
    monkeypatch.setattr(main_module, "_funds_order_lock", asyncio.Lock())
    monkeypatch.setattr(main_module, "_screener_config_lock", asyncio.Lock())
    main_module._store.clear_sessions()
    main_module._roi_history_cache = None
    yield
    main_module._store.clear_sessions()
    main_module._roi_history_cache = None


def test_healthz_does_not_require_api_key():
    # A diferencia de todo lo demas bajo /api, /healthz es un liveness check
    # publico (ver docstring en main.py): no expone datos de cuenta/broker,
    # asi que no exige X-API-Key ni cookie de sesion.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Autenticacion por API key (require_api_key)
# ---------------------------------------------------------------------------

def test_status_requires_api_key():
    # /api/status expone ib_host/ib_port (info de conexion al broker), asi
    # que exige API key igual que el resto de los endpoints.
    resp = client.get("/api/status")
    assert resp.status_code == 401


def test_status_returns_data_with_valid_api_key():
    resp = client.get("/api/status", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert "ib_host" in resp.json()


def test_status_reports_live_hot_set_size_and_cap():
    # El frontend recalcula "X/Y streaming en vivo" en cada poll de
    # /api/status (no solo al escanear) para que ese texto no quede
    # mostrando un estado congelado de la ultima vez que el usuario escaneo.
    main_module._hot_symbols.update({"AAPL", "MSFT"})
    main_module.screener_config.live_hot_symbols_cap = 50

    resp = client.get("/api/status", headers={"X-API-Key": "test-key"})
    body = resp.json()
    assert body["live_hot_count"] == 2
    assert body["live_hot_cap"] == 50


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
# /api/login y /api/logout (sesion por cookie, alternativa a la API key cruda)
# ---------------------------------------------------------------------------

def test_login_rejects_wrong_password():
    resp = client.post("/api/login", json={"password": "clave-incorrecta"})
    assert resp.status_code == 401
    assert "session" not in resp.cookies


def test_login_sets_httponly_session_cookie_on_correct_password():
    resp = client.post("/api/login", json={"password": "test-key"})
    assert resp.status_code == 200
    assert "session" in resp.cookies
    set_cookie_header = resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie_header  # JS del frontend no debe poder leerla


def test_session_ttl_is_one_week_not_a_month():
    """M9 del audit: 30 dias era una ventana de exposicion innecesariamente
    larga para un token que viaja sin el flag Secure (ver comentario junto a
    SESSION_TTL_SECONDS en main.py: el deploy real es HTTP plano sobre
    Tailscale, asi que Secure no es viable hoy). 7 dias acota el riesgo de un
    token filtrado sin forzar un re-login diario."""
    assert main_module.SESSION_TTL_SECONDS == 7 * 24 * 60 * 60


def test_session_cookie_grants_access_to_protected_endpoint_without_api_key_header():
    login_resp = client.post("/api/login", json={"password": "test-key"})
    token = login_resp.cookies["session"]

    resp = client.get("/api/status", cookies={"session": token})
    assert resp.status_code == 200


def test_protected_endpoint_rejects_unknown_session_cookie():
    resp = client.get("/api/status", cookies={"session": "token-que-no-existe"})
    assert resp.status_code == 401


def test_logout_invalidates_the_session():
    login_resp = client.post("/api/login", json={"password": "test-key"})
    token = login_resp.cookies["session"]
    assert client.get("/api/status", cookies={"session": token}).status_code == 200

    logout_resp = client.post("/api/logout", cookies={"session": token})
    assert logout_resp.status_code == 200

    resp = client.get("/api/status", cookies={"session": token})
    assert resp.status_code == 401  # la sesion ya no existe del lado del servidor


def test_logout_without_a_session_cookie_is_a_harmless_noop():
    resp = client.post("/api/logout")
    assert resp.status_code == 200


def test_expired_session_is_rejected_and_purged(monkeypatch):
    login_resp = client.post("/api/login", json={"password": "test-key"})
    token = login_resp.cookies["session"]

    from datetime import datetime, timedelta, timezone
    expired_at = datetime.now(timezone.utc) - timedelta(
        seconds=main_module.SESSION_TTL_SECONDS + 1
    )
    main_module._store._insert_session_at(token, expired_at)

    resp = client.get("/api/status", cookies={"session": token})
    assert resp.status_code == 401
    assert not main_module._store.valid(token)  # se purgo, no solo se rechazo


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


def test_websocket_accepts_session_cookie_without_api_key_query_param():
    login_resp = client.post("/api/login", json={"password": "test-key"})
    token = login_resp.cookies["session"]

    assert main_module.clients == []
    with client.websocket_connect("/ws", cookies={"session": token}):
        assert len(main_module.clients) == 1
    assert main_module.clients == []


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
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(daily_pnl_pct=-3.0)))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is True


def test_risk_monitor_loop_keeps_running_within_limit(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(daily_pnl_pct=-1.0)))
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
# Circuit breaker de drawdown ACUMULADO (mismo _risk_monitor_loop)
# ---------------------------------------------------------------------------

def test_risk_monitor_loop_sets_initial_peak_equity_without_halting(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    main_module.state["peak_equity_usd"] = None
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=100_000)))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is False
    assert main_module.state["peak_equity_usd"] == 100_000


def test_risk_monitor_loop_halts_when_cumulative_drawdown_breached(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    main_module.state["peak_equity_usd"] = 100_000
    main_module.rules_engine.reload(RulesConfig(max_drawdown_pct=15))
    # 80_000 = 20% por debajo del maximo de 100_000 -> supera el 15%
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=80_000)))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is True


def test_risk_monitor_loop_keeps_running_within_drawdown_limit(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    main_module.state["peak_equity_usd"] = 100_000
    main_module.rules_engine.reload(RulesConfig(max_drawdown_pct=15))
    # 90_000 = 10% por debajo del maximo -> dentro del 15% permitido
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=90_000)))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["halted"] is False
    assert main_module.state["peak_equity_usd"] == 100_000  # el maximo no baja


def test_risk_monitor_loop_does_not_reset_peak_after_a_new_high_then_a_dip(monkeypatch):
    main_module.state["connected"] = True
    main_module.state["halted"] = False
    main_module.state["peak_equity_usd"] = 100_000
    main_module.rules_engine.reload(RulesConfig(max_drawdown_pct=50))
    # Sube a un nuevo maximo de 120_000 dentro del mismo ciclo de polling.
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=120_000)))
    asyncio.run(_run_one_cycle(monkeypatch))
    assert main_module.state["peak_equity_usd"] == 120_000
    assert main_module.state["halted"] is False


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
# Trabas de asignacion de capital entre fondos (_check_capital_allocation)
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
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=1_000, cash=1_000)))
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 5_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422
    assert "valor neto" in resp.json()["detail"]


def test_create_fund_succeeds_within_available_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
    resp = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 4_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.json()["cash_usd"] == 4_000


def test_create_second_fund_rejected_when_combined_allocation_exceeds_real_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=10_000, cash=10_000)))
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
    assert "valor neto" in second.json()["detail"]


def test_capital_flow_deposit_rejected_when_exceeds_remaining_real_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(net_liq=10_000, cash=10_000)))
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
    assert "valor neto" in resp.json()["detail"]


def test_capital_flow_withdrawal_rejected_when_exceeds_fund_cash(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
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
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
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
# Cierre definitivo de un fondo (/api/funds/{fund_id}/close)
# ---------------------------------------------------------------------------

def test_close_fund_succeeds_when_cash_zero_and_no_positions(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 3_000},
        headers={"X-API-Key": "test-key"},
    ).json()
    client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": -3_000},
        headers={"X-API-Key": "test-key"},
    )

    resp = client.post(f"/api/funds/{created['id']}/close", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["closed"] is True
    assert resp.json()["closed_at"] is not None


def test_close_fund_rejects_when_cash_not_zero(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 3_000},
        headers={"X-API-Key": "test-key"},
    ).json()

    resp = client.post(f"/api/funds/{created['id']}/close", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422


def test_close_fund_unknown_returns_404():
    resp = client.post("/api/funds/no-existe/close", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404


def test_close_fund_waits_for_in_flight_order_holding_the_lock():
    """close_fund debe esperar _funds_order_lock, no solo el lock interno de
    FundsStore: sin esto, podia validar 'sin posiciones abiertas' justo antes
    de que un fill en curso (que no chequea fund.closed) le agregara una
    posicion al fondo ya cerrado. Con el fix, para cuando close_fund corre la
    validacion ya vio el fill que la orden en vuelo dejo, y rechaza el cierre."""
    # Cash justo en 100 para que la compra de 1 accion a $100 deje cash_usd en
    # 0 exacto: la unica razon por la que close() puede rechazar despues es la
    # posicion abierta, no el cash.
    fund = main_module.funds_store.create("Fondo", 1000)
    main_module.funds_store.apply_capital_flow(fund.id, -900, note="Retiro parcial")

    async def scenario():
        order_started = asyncio.Event()

        async def fake_order_in_flight():
            async with main_module._funds_order_lock:
                order_started.set()
                await asyncio.sleep(0.05)
                main_module.funds_store.record_fill(fund.id, "AAPL", Side.BUY, 1, 100)

        task = asyncio.create_task(fake_order_in_flight())
        await order_started.wait()
        with pytest.raises(main_module.HTTPException) as exc_info:
            await main_module.close_fund(fund.id, None)
        await task
        return exc_info.value

    exc = asyncio.run(scenario())
    assert exc.status_code == 422
    assert "abiertas" in exc.detail.lower()


def test_capital_flow_rejected_on_closed_fund(monkeypatch):
    main_module.state["connected"] = True
    monkeypatch.setattr(main_module.broker, "get_account_summary", _async_account_summary(make_account(cash=10_000)))
    created = client.post(
        "/api/funds",
        json={"name": "Fondo", "initial_capital_usd": 3_000},
        headers={"X-API-Key": "test-key"},
    ).json()
    client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": -3_000},
        headers={"X-API-Key": "test-key"},
    )
    client.post(f"/api/funds/{created['id']}/close", headers={"X-API-Key": "test-key"})

    resp = client.post(
        f"/api/funds/{created['id']}/capital-flows",
        json={"amount": 1_000},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422
    assert "cerrado" in resp.json()["detail"].lower()


def test_validate_fund_order_rejects_orders_on_closed_fund():
    fund = main_module.funds_store.create("Fondo", 1000)
    main_module.funds_store.apply_capital_flow(fund.id, -1000, note="Retiro total")
    main_module.funds_store.close(fund.id)
    order = main_module.OrderRequest(symbol="AAPL", side=main_module.Side.BUY, quantity=1, fund_id=fund.id)
    with pytest.raises(main_module.HTTPException) as exc_info:
        main_module._validate_fund_order(order, 100.0)
    assert exc_info.value.status_code == 422


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
        "per_fund": [],
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
        "per_fund": [],
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


# ---------------------------------------------------------------------------
# GET /api/audit
# ---------------------------------------------------------------------------

def test_get_audit_rejects_limit_above_max():
    resp = client.get("/api/audit?limit=1001", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422


def test_get_audit_rejects_non_positive_limit():
    resp = client.get("/api/audit?limit=0", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422


def test_get_audit_accepts_limit_within_bounds():
    resp = client.get("/api/audit?limit=5", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/sectors/refresh
# ---------------------------------------------------------------------------

def test_refresh_sectors_rejects_missing_api_key():
    resp = client.post("/api/sectors/refresh", json={"symbols": ["AAPL"]})
    assert resp.status_code == 401


def test_refresh_sectors_skips_already_classified_symbols(monkeypatch):
    # AAPL ya esta en el mapeo estatico (app/sectors.py): no debe golpear
    # refresh_sector ni aparecer en la respuesta, solo NEWCO (sin clasificar).
    calls = []

    def fake_refresh(symbol):
        calls.append(symbol)
        return "Information Technology"

    monkeypatch.setattr(main_module, "refresh_sector", fake_refresh)
    resp = client.post(
        "/api/sectors/refresh",
        json={"symbols": ["AAPL", "newco"]},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert calls == ["NEWCO"]
    body = resp.json()
    assert body["resolved"] == {"NEWCO": "Information Technology"}
    assert body["unresolved"] == []


def test_refresh_sectors_reports_unresolved_symbols(monkeypatch):
    monkeypatch.setattr(main_module, "refresh_sector", lambda symbol: None)
    resp = client.post(
        "/api/sectors/refresh",
        json={"symbols": ["NOCOVERAGE"]},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] == {"NOCOVERAGE": None}
    assert body["unresolved"] == ["NOCOVERAGE"]


def test_refresh_sectors_defaults_to_screener_universe_when_no_symbols_given(monkeypatch):
    main_module.screener_config.universe = ["NEWCO"]
    monkeypatch.setattr(main_module, "refresh_sector", lambda symbol: "Energy")
    resp = client.post("/api/sectors/refresh", json={}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    assert resp.json()["resolved"] == {"NEWCO": "Energy"}


def test_refresh_sectors_rejects_malformed_symbol():
    resp = client.post(
        "/api/sectors/refresh",
        json={"symbols": ["<img src=x onerror=alert(1)>"]},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


def test_refresh_sectors_rejects_oversized_symbol_list():
    resp = client.post(
        "/api/sectors/refresh",
        json={"symbols": [f"S{i}" for i in range(201)]},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 422


def test_update_screener_config_auto_refreshes_sector_for_new_unclassified_symbol(monkeypatch):
    # Antes de este fix, un simbolo nuevo agregado al universo (ej. via
    # addTickerToUniverse() en el frontend) quedaba sin sector conocido hasta
    # que alguien apretaba manualmente "Refrescar sectores": mientras tanto,
    # max_sector_concentration_pct/max_concurrent_positions_per_sector no lo
    # cubrian (sin dato, esas reglas no bloquean). Corre en un hilo aparte
    # (ver _refresh_missing_sectors_in_background), por eso el Event en vez
    # de asumir que ya termino apenas vuelve la respuesta del PUT.
    done = threading.Event()
    calls = []

    def fake_refresh_sector(symbol):
        calls.append(symbol)
        done.set()
        return "Energy"

    monkeypatch.setattr(main_module, "refresh_sector", fake_refresh_sector)
    original_universe = list(main_module.screener_config.universe)
    try:
        resp = client.put(
            "/api/signals/config",
            json={"config": {"universe": original_universe + ["ZZZNEWCO"]}},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        assert done.wait(timeout=2), "refresh_sector no se llamo a tiempo"
        assert calls == ["ZZZNEWCO"]
    finally:
        main_module.screener_config.universe = original_universe


def test_update_screener_config_does_not_refresh_sector_for_already_classified_symbol(monkeypatch):
    # AAPL ya esta en el mapeo estatico (TICKER_SECTOR): agregarlo al
    # universo no deberia disparar ningun refresh de sector.
    calls = []
    monkeypatch.setattr(main_module, "refresh_sector", lambda symbol: calls.append(symbol))
    original_universe = list(main_module.screener_config.universe)
    try:
        universe_without_aapl = [s for s in original_universe if s != "AAPL"]
        resp = client.put(
            "/api/signals/config",
            json={"config": {"universe": universe_without_aapl + ["AAPL"]}},
            headers={"X-API-Key": "test-key"},
        )
        assert resp.status_code == 200
        import time
        time.sleep(0.2)  # margen para que un thread indebido alcanzara a correr
        assert calls == []
    finally:
        main_module.screener_config.universe = original_universe


# ---------------------------------------------------------------------------
# GET /api/signals/scan y /api/signals/scan/all
# ---------------------------------------------------------------------------

def _make_signal(symbol, score, strategy_id="momentum", sector=None, passes=True) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        as_of=datetime.now(timezone.utc),
        last_price=100.0,
        score=score,
        momentum_3m_pct=10.0,
        momentum_1m_pct=5.0,
        trend_ok=True,
        rsi=55.0,
        avg_volume=1_000_000,
        suggested_stop_loss_price=95.0,
        suggested_stop_loss_pct=5.0,
        passes_filters=passes,
        strategy_id=strategy_id,
        sector=sector,
    )


@pytest.fixture(autouse=True)
def _clear_signal_cache():
    main_module.signal_cache.clear()
    yield
    main_module.signal_cache.clear()


@pytest.fixture(autouse=True)
def _reset_live_radar_state():
    """Mismo motivo que _clear_signal_cache: _hot_symbols/_live_prices son
    estado global de proceso (ver _hot_set_loop/_price_rotation_loop en
    main.py), asi que un test que los puebla puede filtrar al siguiente si no
    se limpian entre tests."""
    main_module._hot_symbols.clear()
    main_module._live_prices.clear()
    main_module._live_prices_as_of.clear()
    main_module._rotation_cursor = 0
    yield
    main_module._hot_symbols.clear()
    main_module._live_prices.clear()
    main_module._live_prices_as_of.clear()
    main_module._rotation_cursor = 0


def test_scan_signals_rejects_missing_api_key():
    resp = client.get("/api/signals/scan")
    assert resp.status_code == 401


def test_scan_signals_rejects_unknown_strategy_id():
    resp = client.get("/api/signals/scan", params={"strategy_id": "no-existe"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 422


def test_scan_signals_uses_active_strategy_by_default(monkeypatch):
    main_module.screener_config.strategy_id = "momentum"
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])
    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is False
    assert [r["symbol"] for r in body["results"]] == ["AAPL"]


def test_scan_signals_serves_from_cache_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: calls.append(1) or [_make_signal("AAPL", 8.0)])
    client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    assert resp.json()["cached"] is True
    assert calls == [1]


def test_scan_signals_force_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: calls.append(1) or [_make_signal("AAPL", 8.0)])
    client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    resp = client.get("/api/signals/scan", params={"force": "true"}, headers={"X-API-Key": "test-key"})
    assert resp.json()["cached"] is False
    assert calls == [1, 1]


def test_scan_signals_all_rejects_missing_api_key():
    resp = client.get("/api/signals/scan/all")
    assert resp.status_code == 401


def test_scan_signals_all_merges_scores_per_symbol_across_strategies(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 9.0, strategy_id="momentum", sector="Information Technology"),
        _make_signal("XOM", 2.0, strategy_id="momentum"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["opportunistic"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 5.0, strategy_id="opportunistic"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["long_term"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 7.0, strategy_id="long_term"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 3.0, strategy_id="dividend", sector="Information Technology"),
    ])

    resp = client.get("/api/signals/scan/all", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    by_symbol = {r["symbol"]: r for r in body["results"]}

    assert by_symbol["AAPL"]["scores"] == {
        "momentum": 9.0, "opportunistic": 5.0, "long_term": 7.0, "dividend": 3.0,
    }
    assert by_symbol["AAPL"]["sector"] == "Information Technology"
    # XOM solo aparece en momentum: las demas estrategias no la trajeron.
    assert by_symbol["XOM"]["scores"] == {"momentum": 2.0}


def test_scan_signals_all_reuses_cache_already_warmed_by_single_strategy_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module.screener, "scan",
        lambda force=False, cache_only=False: calls.append("momentum") or [_make_signal("AAPL", 9.0, strategy_id="momentum")],
    )
    for sid in ("opportunistic", "long_term", "dividend"):
        monkeypatch.setattr(main_module.strategy_registry[sid], "scan", lambda force=False, cache_only=False, sid=sid: [])

    client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})  # calienta el cache de momentum
    client.get("/api/signals/scan/all", headers={"X-API-Key": "test-key"})

    assert calls == ["momentum"]  # no se volvio a escanear momentum, ya estaba fresco


def test_scan_signals_all_propagates_strategy_failure_as_502(monkeypatch):
    def failing_scan(force=False, cache_only=False):
        raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module.screener, "scan", failing_scan)
    resp = client.get("/api/signals/scan/all", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# GET /api/signals/scan?strategy_id=general (vista sintetica del radar en
# vivo, ver _scan_general en main.py)
# ---------------------------------------------------------------------------

def test_scan_signals_general_picks_max_score_strategy_per_symbol(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 9.0, strategy_id="momentum"),
        _make_signal("XOM", 2.0, strategy_id="momentum"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["opportunistic"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 5.0, strategy_id="opportunistic"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["long_term"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 7.0, strategy_id="long_term"),
        _make_signal("XOM", 6.0, strategy_id="long_term"),
    ])
    monkeypatch.setattr(main_module.strategy_registry["dividend"], "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 3.0, strategy_id="dividend"),
    ])

    resp = client.get("/api/signals/scan", params={"strategy_id": "general"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    by_symbol = {r["symbol"]: r for r in body["results"]}

    assert by_symbol["AAPL"]["score"] == 9.0
    assert by_symbol["AAPL"]["winning_strategy_id"] == "momentum"
    assert by_symbol["XOM"]["score"] == 6.0
    assert by_symbol["XOM"]["winning_strategy_id"] == "long_term"
    # ordenado por score MAXIMO descendente, no por orden de aparicion
    assert [r["symbol"] for r in body["results"]] == ["AAPL", "XOM"]


def test_scan_signals_general_reuses_cache_already_warmed_by_single_strategy_scan(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module.screener, "scan",
        lambda force=False, cache_only=False: calls.append("momentum") or [_make_signal("AAPL", 9.0, strategy_id="momentum")],
    )
    for sid in ("opportunistic", "long_term", "dividend"):
        monkeypatch.setattr(main_module.strategy_registry[sid], "scan", lambda force=False, cache_only=False, sid=sid: [])

    client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})  # calienta el cache de momentum
    client.get("/api/signals/scan", params={"strategy_id": "general"}, headers={"X-API-Key": "test-key"})

    assert calls == ["momentum"]  # no se volvio a escanear momentum, ya estaba fresco


def test_scan_signals_general_propagates_strategy_failure_as_502(monkeypatch):
    def failing_scan(force=False, cache_only=False):
        raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module.screener, "scan", failing_scan)
    resp = client.get("/api/signals/scan", params={"strategy_id": "general"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# Overlay del radar en vivo sobre /api/signals/scan (is_hot / last_price,
# ver _overlay_live_data en main.py)
# ---------------------------------------------------------------------------

def test_scan_signals_marks_is_hot_and_uses_live_price_for_hot_symbol(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: 123.45 if symbol == "AAPL" else None)

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert result["is_hot"] is True
    assert result["last_price"] == 123.45


def test_scan_signals_is_hot_false_when_hot_symbol_has_no_live_price_yet(monkeypatch):
    # En el hot-set pero todavia sin un primer precio cacheado (ej. justo
    # despues de un reconnect del broker, antes del proximo ciclo de
    # _hot_set_loop): no debe mostrarse como "en vivo" ni pisar el precio.
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: None)

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert result["is_hot"] is False
    assert result["last_price"] == 100.0  # precio cacheado original, sin pisar


def test_scan_signals_uses_rotation_price_for_cold_symbol(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])
    main_module._live_prices["AAPL"] = 111.11

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert result["is_hot"] is False
    assert result["last_price"] == 111.11


def test_scan_signals_keeps_cached_price_when_no_rotation_price_yet(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert result["is_hot"] is False
    assert result["last_price"] == 100.0


def test_scan_signals_price_as_of_is_now_for_hot_symbol(monkeypatch):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal("AAPL", 8.0)
    signal.as_of = stale
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [signal])
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: 123.45)

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    price_as_of = datetime.fromisoformat(result["price_as_of"])
    assert price_as_of > stale  # un simbolo en vivo se reporta como recien actualizado


def test_scan_signals_price_as_of_uses_rotation_timestamp_for_cold_symbol(monkeypatch):
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    signal = _make_signal("AAPL", 8.0)
    signal.as_of = stale
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [signal])
    rotated_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
    main_module._live_prices["AAPL"] = 111.11
    main_module._live_prices_as_of["AAPL"] = rotated_at

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert datetime.fromisoformat(result["price_as_of"]) == rotated_at


def test_scan_signals_price_as_of_falls_back_to_scan_time_without_overlay(monkeypatch):
    as_of = datetime(2024, 3, 1, tzinfo=timezone.utc)
    signal = _make_signal("AAPL", 8.0)
    signal.as_of = as_of
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [signal])

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert datetime.fromisoformat(result["price_as_of"]) == as_of


def test_scan_signals_reports_hot_set_size_and_cap(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [_make_signal("AAPL", 8.0)])
    main_module._hot_symbols.update({"AAPL", "MSFT"})
    main_module.screener_config.live_hot_symbols_cap = 50

    resp = client.get("/api/signals/scan", headers={"X-API-Key": "test-key"})
    body = resp.json()
    assert body["live_hot_count"] == 2
    assert body["live_hot_cap"] == 50


def test_scan_signals_general_view_includes_live_overlay(monkeypatch):
    monkeypatch.setattr(main_module.screener, "scan", lambda force=False, cache_only=False: [
        _make_signal("AAPL", 9.0, strategy_id="momentum"),
    ])
    for sid in ("opportunistic", "long_term", "dividend"):
        monkeypatch.setattr(main_module.strategy_registry[sid], "scan", lambda force=False, cache_only=False, sid=sid: [])
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: 200.0)

    resp = client.get("/api/signals/scan", params={"strategy_id": "general"}, headers={"X-API-Key": "test-key"})
    result = resp.json()["results"][0]
    assert result["winning_strategy_id"] == "momentum"
    assert result["is_hot"] is True
    assert result["last_price"] == 200.0


# ---------------------------------------------------------------------------
# GET /api/signals/live-prices (refresco liviano de precios para filas ya
# visibles del radar, ver _live_overlay_for_symbol en main.py)
# ---------------------------------------------------------------------------

def test_live_prices_rejects_missing_api_key():
    resp = client.get("/api/signals/live-prices", params={"symbols": "AAPL"})
    assert resp.status_code == 401


def test_live_prices_returns_price_for_hot_symbol(monkeypatch):
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: 123.45 if symbol == "AAPL" else None)

    resp = client.get("/api/signals/live-prices", params={"symbols": "AAPL"}, headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["AAPL"]["is_hot"] is True
    assert body["AAPL"]["last_price"] == 123.45


def test_live_prices_omits_hot_symbol_with_no_live_price_yet(monkeypatch):
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: None)

    resp = client.get("/api/signals/live-prices", params={"symbols": "AAPL"}, headers={"X-API-Key": "test-key"})
    assert resp.json() == {}


def test_live_prices_returns_rotation_price_for_cold_symbol():
    rotated_at = datetime(2025, 6, 1, tzinfo=timezone.utc)
    main_module._live_prices["AAPL"] = 111.11
    main_module._live_prices_as_of["AAPL"] = rotated_at

    resp = client.get("/api/signals/live-prices", params={"symbols": "AAPL"}, headers={"X-API-Key": "test-key"})
    body = resp.json()
    assert body["AAPL"]["is_hot"] is False
    assert body["AAPL"]["last_price"] == 111.11
    assert datetime.fromisoformat(body["AAPL"]["price_as_of"]) == rotated_at


def test_live_prices_omits_cold_symbol_with_no_rotation_price_yet():
    resp = client.get("/api/signals/live-prices", params={"symbols": "AAPL"}, headers={"X-API-Key": "test-key"})
    assert resp.json() == {}


def test_live_prices_only_includes_requested_symbols_that_have_data(monkeypatch):
    main_module._hot_symbols.add("AAPL")
    monkeypatch.setattr(main_module.broker, "get_live_price", lambda symbol: 123.45 if symbol == "AAPL" else None)
    main_module._live_prices["MSFT"] = 222.22

    resp = client.get(
        "/api/signals/live-prices", params={"symbols": "AAPL,MSFT,XOM"}, headers={"X-API-Key": "test-key"}
    )
    body = resp.json()
    assert set(body.keys()) == {"AAPL", "MSFT"}
    assert body["MSFT"]["last_price"] == 222.22


def test_live_prices_rejects_invalid_symbol_format():
    resp = client.get(
        "/api/signals/live-prices", params={"symbols": "AAPL,not-a-valid-symbol!!"}, headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# lifespan: shutdown debe esperar a que las tareas de background terminen
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_awaits_background_tasks_cancellation(monkeypatch):
    """cancel() solo pide la cancelacion, no la espera: el shutdown debia
    hacer gather() de las 7 tareas de background para garantizar que ya
    terminaron de cancelarse antes de desconectar del broker y salir, en vez
    de dejarlas 'pending' (cleanup propio sin correr, CancelledError nunca
    recuperada). Se verifica espiando asyncio.create_task para capturar las
    tareas reales que crea lifespan y comprobando que estan 'done' apenas
    termina el `async with`."""
    async def fake_connect():
        pass

    monkeypatch.setattr(main_module.broker, "connect", fake_connect)
    monkeypatch.setattr(main_module.broker, "disconnect", lambda: None)

    async def fake_restore():
        pass

    monkeypatch.setattr(main_module, "_restore_persisted_mode", fake_restore)

    async def long_running():
        await asyncio.sleep(100)

    for loop_name in (
        "_broadcast_loop", "_risk_monitor_loop", "_score_recompute_loop",
        "_data_refresh_loop", "_auto_exit_monitor_loop", "_trailing_stop_loop",
        "_hot_set_loop", "_price_rotation_loop", "_session_cleanup_loop",
        "_cache_eviction_loop", "_health_alert_loop",
    ):
        monkeypatch.setattr(main_module, loop_name, long_running)

    created_tasks = []
    original_create_task = asyncio.create_task

    def spy_create_task(coro, *args, **kwargs):
        new_task = original_create_task(coro, *args, **kwargs)
        created_tasks.append(new_task)
        return new_task

    monkeypatch.setattr(main_module.asyncio, "create_task", spy_create_task)

    async def scenario():
        async with main_module.lifespan(main_module.app):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(created_tasks) == 14  # 11 previas + reconcile (Fase 1) + watchdog (Fase 3) + audit_prune (Fase 6)
    assert all(t.done() for t in created_tasks)


# ---------------------------------------------------------------------------
# GET /api/funds/{fund_id}/trades/{trade_id}/context
# ---------------------------------------------------------------------------

def test_get_trade_context_returns_signal_for_buy_trade():
    fund = main_module.funds_store.create("F", 10_000.0)
    trade = main_module.funds_store.record_fill(fund.id, "AAPL", Side.BUY, 10, 150.0)
    main_module.audit.record(
        "auto_trade_executed",
        {"symbol": "AAPL", "side": "BUY", "fund_id": fund.id, "quantity": 10},
        {"signal": {"score": 72, "strategy_id": "momentum", "sector": "Information Technology"}},
    )
    resp = client.get(
        f"/api/funds/{fund.id}/trades/{trade.id}/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "auto_trade_executed"
    assert body["signal"]["score"] == 72
    assert body["reason"] is None
    assert isinstance(body["rationale"], str) and len(body["rationale"]) > 0


def test_get_trade_context_rationale_is_none_when_no_signal_found():
    fund = main_module.funds_store.create("F", 10_000.0)
    trade = main_module.funds_store.record_fill(fund.id, "ZZZZ", Side.BUY, 1, 10.0)
    resp = client.get(
        f"/api/funds/{fund.id}/trades/{trade.id}/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    assert resp.json()["rationale"] is None


def test_get_trade_context_returns_reason_for_sell_trade():
    fund = main_module.funds_store.create("F", 10_000.0)
    main_module.funds_store.record_fill(fund.id, "AAPL", Side.BUY, 10, 150.0)
    trade = main_module.funds_store.record_fill(fund.id, "AAPL", Side.SELL, 10, 160.0)
    main_module.audit.record(
        "auto_trade_exit",
        {"symbol": "AAPL", "side": "SELL", "fund_id": fund.id, "quantity": 10},
        {"reason": "trend_break"},
    )
    resp = client.get(
        f"/api/funds/{fund.id}/trades/{trade.id}/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "auto_trade_exit"
    assert body["reason"] == "trend_break"
    assert body["signal"] is None


def test_get_trade_context_falls_back_to_draft_signal_when_execution_lacks_it():
    # order_executed_after_approval no vuelve a adjuntar la señal original
    # (ver docstring de approve_order) -- tiene que ir a buscar el
    # signal_order_drafted que la origino.
    fund = main_module.funds_store.create("F", 10_000.0)
    main_module.audit.record(
        "signal_order_drafted",
        {"symbol": "AAPL", "side": "BUY", "fund_id": fund.id},
        {"signal": {"score": 88, "strategy_id": "momentum"}},
    )
    trade = main_module.funds_store.record_fill(fund.id, "AAPL", Side.BUY, 10, 150.0)
    main_module.audit.record(
        "order_executed_after_approval",
        {"symbol": "AAPL", "side": "BUY", "fund_id": fund.id, "quantity": 10},
        {"filled_qty": 10, "avg_fill_price": 150.0},
    )
    resp = client.get(
        f"/api/funds/{fund.id}/trades/{trade.id}/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    assert resp.json()["signal"]["score"] == 88


def test_get_trade_context_returns_none_fields_when_no_audit_entry_found():
    fund = main_module.funds_store.create("F", 10_000.0)
    trade = main_module.funds_store.record_fill(fund.id, "ZZZZ", Side.BUY, 1, 10.0)
    resp = client.get(
        f"/api/funds/{fund.id}/trades/{trade.id}/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] is None
    assert body["signal"] is None
    assert body["reason"] is None


def test_get_trade_context_unknown_fund_returns_404():
    resp = client.get(
        "/api/funds/nope/trades/nope/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 404


def test_get_trade_context_unknown_trade_returns_404():
    fund = main_module.funds_store.create("F", 10_000.0)
    resp = client.get(
        f"/api/funds/{fund.id}/trades/nope/context", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/sectors
# ---------------------------------------------------------------------------

def test_get_sectors_returns_known_symbols_only():
    resp = client.get(
        "/api/sectors?symbols=AAPL,ZZZZNOPE", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["AAPL"] == "Information Technology"
    assert "ZZZZNOPE" not in body


def test_get_sectors_requires_api_key():
    resp = client.get("/api/sectors?symbols=AAPL")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# _supervised_loop: C2 del NUEVO_INFORME.
# Los loops de background no tenian try/except: una excepcion no atrapada
# mataba la tarea de forma permanente y silenciosa. _supervised_loop la
# captura, loguea y continua; CancelledError se propaga para el shutdown.
# ---------------------------------------------------------------------------

def test_supervised_loop_survives_exception_and_runs_next_cycle():
    """Un ciclo que lanza ValueError no debe matar el loop: el siguiente
    ciclo debe correr. ESTE TEST DEBIA FALLAR antes del fix (sin supervisor,
    la excepcion habria matado el while-True)."""
    calls = []

    async def flaky_cycle():
        calls.append(len(calls))
        if len(calls) == 1:
            raise ValueError("fallo simulado del ciclo")
        # El segundo ciclo levanta CancelledError para terminar el test.
        raise asyncio.CancelledError

    async def scenario():
        await main_module._supervised_loop(flaky_cycle, "test_flaky", lambda: 0)

    try:
        asyncio.run(scenario())
    except asyncio.CancelledError:
        pass

    assert len(calls) == 2, "el loop debe haber corrido 2 ciclos (el primero fallo, el segundo se cancelo)"


def test_supervised_loop_propagates_cancelled_error():
    """CancelledError no debe quedar atrapada: el shutdown necesita poder
    cancelar los loops limpiamente."""
    ran = []

    async def cancel_immediately():
        raise asyncio.CancelledError

    async def scenario():
        await main_module._supervised_loop(cancel_immediately, "test_cancel", lambda: 0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_supervised_loop_backoff_grows_on_consecutive_failures():
    """Ante fallos consecutivos, el sleep debe crecer hasta 300s (tope)."""
    import asyncio as _asyncio

    slept = []
    original_sleep = _asyncio.sleep

    async def counting_cycle():
        raise ValueError("siempre falla")

    async def scenario():
        async def fake_sleep(secs):
            slept.append(secs)
            if len(slept) >= 4:
                raise asyncio.CancelledError

        main_module.asyncio.sleep = fake_sleep
        try:
            await main_module._supervised_loop(counting_cycle, "test_backoff", lambda: 10)
        except asyncio.CancelledError:
            pass
        finally:
            main_module.asyncio.sleep = original_sleep

    asyncio.run(scenario())

    # Primer fallo: 10*2=20; segundo: 10*4=40; tercero: 10*8=80
    assert slept[0] == pytest.approx(20.0)
    assert slept[1] == pytest.approx(40.0)
    assert slept[2] == pytest.approx(80.0)


def test_supervised_loop_backoff_resets_after_success():
    """Un ciclo exitoso debe resetear el contador de fallos (backoff a 1x)."""
    import asyncio as _asyncio

    slept = []
    calls = []
    original_sleep = _asyncio.sleep

    async def sometimes_fail():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("primer fallo")
        # segundo ciclo: exito
        if len(calls) >= 3:
            raise asyncio.CancelledError

    async def scenario():
        async def fake_sleep(secs):
            slept.append(secs)
            # dejar correr hasta que el ciclo cancele

        main_module.asyncio.sleep = fake_sleep
        try:
            await main_module._supervised_loop(sometimes_fail, "test_reset", lambda: 10)
        except asyncio.CancelledError:
            pass
        finally:
            main_module.asyncio.sleep = original_sleep

    asyncio.run(scenario())

    # Primer sleep: backoff (1 fallo -> 20s); segundo: reset (0 fallos -> 10s)
    assert slept[0] == pytest.approx(20.0), "post-fallo debe aplicar backoff"
    assert slept[1] == pytest.approx(10.0), "post-exito debe resetear al intervalo normal"


def test_audit_record_does_not_propagate_on_sqlite_error():
    """Si sqlite falla, audit.record no debe propagar: matar el loop de
    background que llamo record() es peor que perder una entrada de auditoria."""
    import sqlite3
    import tempfile
    from app.audit import AuditLog

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    log = AuditLog(db_path)
    # Cerrar la conexion a mano para simular un error de sqlite irrecuperable.
    log._conn.close()

    # No debe propagar -- debe quedar atrapado internamente.
    log.record("test_action", {}, {})


def test_health_check_reports_background_loop_failing(monkeypatch):
    """/api/health debe incluir 'background_loop_failing' en issues si algun
    loop tiene 3+ fallos consecutivos."""
    main_module._loop_health["test_loop"] = {"fails": 3, "last_error": "boom"}
    try:
        resp = client.get("/api/health")
        # Puede ser 200 o 503 dependiendo del estado del sistema en el test;
        # lo que importa es que el payload contiene el issue y el dato del loop.
        body = resp.json()
        if resp.status_code == 503:
            body = body.get("detail", body)
        assert "background_loop_failing" in body.get("issues", [])
        assert "test_loop" in body.get("loops", {})
        assert body["loops"]["test_loop"]["consecutive_failures"] == 3
    finally:
        del main_module._loop_health["test_loop"]


# ---------------------------------------------------------------------------
# _connection_watchdog_cycle: C3 del NUEVO_INFORME.
# Sin reconexion automatica, la unica forma de volver era el cron de las 8am
# o una intervencion manual. El watchdog reconecta solo cuando detecta que
# state["connected"] es False.
# ---------------------------------------------------------------------------

def test_connection_watchdog_reconnects_when_disconnected(monkeypatch):
    """Si state["connected"] es False, el watchdog debe llamar a broker.reconnect
    y setear connected=True en caso de exito."""
    main_module.state["connected"] = False
    main_module.state["mode"] = "paper"

    reconnect_calls = []

    async def fake_reconnect(host, port, client_id):
        reconnect_calls.append((host, port, client_id))

    monkeypatch.setattr(main_module.broker, "reconnect", fake_reconnect)

    # _reconcile_unfilled_on_startup se invoca tras reconectar; mockearlo.
    async def noop_reconcile():
        pass

    monkeypatch.setattr(main_module, "_reconcile_unfilled_on_startup", noop_reconcile)

    asyncio.run(main_module._connection_watchdog_cycle())

    assert main_module.state["connected"] is True
    assert len(reconnect_calls) == 1


def test_connection_watchdog_does_nothing_when_connected(monkeypatch):
    """Si ya esta conectado, el watchdog no debe llamar a reconnect."""
    main_module.state["connected"] = True

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no debe intentar reconectar si ya esta conectado")

    monkeypatch.setattr(main_module.broker, "reconnect", fail_if_called)
    asyncio.run(main_module._connection_watchdog_cycle())  # no debe explotar


def test_connection_watchdog_does_not_reconnect_in_live_mode_without_confirm(monkeypatch):
    """En modo live sin live_confirm, el watchdog NO debe reconectar (igual que
    /api/mode): una reconexion automatica en live podria generar ordenes reales
    inesperadas."""
    main_module.state["connected"] = False
    main_module.state["mode"] = "live"
    monkeypatch.setattr(main_module.settings, "live_confirm", False)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("no debe reconectar en live sin confirmacion")

    monkeypatch.setattr(main_module.broker, "reconnect", fail_if_called)
    asyncio.run(main_module._connection_watchdog_cycle())  # no debe explotar


def test_connection_watchdog_triggers_reconcile_after_reconnect(monkeypatch):
    """Tras una reconexion exitosa, debe invocar _reconcile_unfilled_on_startup:
    pueden haberse llenado ordenes mientras estuvimos desconectados."""
    main_module.state["connected"] = False
    main_module.state["mode"] = "paper"

    async def fake_reconnect(host, port, client_id):
        pass

    monkeypatch.setattr(main_module.broker, "reconnect", fake_reconnect)

    reconcile_called = []

    async def fake_reconcile():
        reconcile_called.append(True)

    monkeypatch.setattr(main_module, "_reconcile_unfilled_on_startup", fake_reconcile)

    asyncio.run(main_module._connection_watchdog_cycle())

    assert reconcile_called == [True], "reconcile_unfilled_on_startup debe llamarse tras reconectar"
