"""Tests del radar en vivo (hot-set dinamico + rotacion de snapshots sobre
IBKR, ver _run_hot_set_cycle / _run_price_rotation_cycle en main.py).

_scan_general y _overlay_live_data (las piezas que conectan esto con
/api/signals/scan) ya tienen cobertura en test_api.py via TestClient; este
archivo cubre los dos ciclos de background en si: que diffean correctamente
contra el estado anterior, que respetan los limites de lineas de market
data de IBKR, y que no rompen el estado existente si IBKR falla a mitad de
camino.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Mismo patron que el resto de los tests: apuntar las rutas de config a un
# directorio temporal ANTES de importar app.main.
_tmp_dir = tempfile.mkdtemp(prefix="live_radar_test_")
os.environ.setdefault("API_KEY", "test-key")
os.environ["RULES_PATH"] = str(Path(_tmp_dir) / "rules.yaml")
os.environ["SCREENER_PATH"] = str(Path(_tmp_dir) / "screener.yaml")
os.environ["AUDIT_DB_PATH"] = str(Path(_tmp_dir) / "audit.db")
os.environ["STATE_PATH"] = str(Path(_tmp_dir) / "state.json")

import pytest

from app import main as main_module
from app.screener_config import ScreenerConfig


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    main_module.state["connected"] = True
    # Instancia nueva por test (swapeada con monkeypatch, se revierte sola):
    # varios tests de aca mutan screener_config.universe/live_* directamente.
    monkeypatch.setattr(main_module, "screener_config", ScreenerConfig())
    main_module._hot_symbols.clear()
    main_module._live_prices.clear()
    main_module._live_prices_as_of.clear()
    main_module._rotation_cursor = 0
    yield
    main_module._hot_symbols.clear()
    main_module._live_prices.clear()
    main_module._live_prices_as_of.clear()
    main_module._rotation_cursor = 0


def _ranked(*symbols_and_scores):
    return [{"symbol": s, "score": score} for s, score in symbols_and_scores]


def _fake_scan_general(ranked):
    async def fake(force=False):
        return datetime.now(timezone.utc), False, ranked
    return fake


# ---------------------------------------------------------------------------
# _run_hot_set_cycle
# ---------------------------------------------------------------------------

def test_hot_set_cycle_noop_when_radar_disabled(monkeypatch):
    main_module.screener_config.live_radar_enabled = False

    async def fail_if_called(force=False):
        raise AssertionError("no deberia recalcular el hot-set con el radar deshabilitado")

    monkeypatch.setattr(main_module, "_scan_general", fail_if_called)
    asyncio.run(main_module._run_hot_set_cycle())
    assert main_module._hot_symbols == set()


def test_hot_set_cycle_noop_when_not_connected(monkeypatch):
    main_module.state["connected"] = False

    async def fail_if_called(force=False):
        raise AssertionError("no deberia recalcular el hot-set sin conexion a IBKR")

    monkeypatch.setattr(main_module, "_scan_general", fail_if_called)
    asyncio.run(main_module._run_hot_set_cycle())
    assert main_module._hot_symbols == set()


def test_hot_set_cycle_subscribes_top_n_by_max_score_up_to_cap(monkeypatch):
    main_module.screener_config.live_hot_symbols_cap = 2
    monkeypatch.setattr(
        main_module, "_scan_general",
        _fake_scan_general(_ranked(("AAPL", 9.0), ("MSFT", 8.0), ("XOM", 1.0))),
    )

    subscribed = []

    async def fake_subscribe(symbols):
        subscribed.append(set(symbols))

    monkeypatch.setattr(main_module.broker, "stream_subscribe", fake_subscribe)
    monkeypatch.setattr(main_module.broker, "stream_unsubscribe", lambda symbols: None)

    asyncio.run(main_module._run_hot_set_cycle())

    assert subscribed == [{"AAPL", "MSFT"}]  # XOM se quedo afuera, no entra en el top-2
    assert main_module._hot_symbols == {"AAPL", "MSFT"}


def test_hot_set_cycle_diffs_against_previous_hot_set(monkeypatch):
    main_module._hot_symbols.update({"AAPL", "MSFT"})
    main_module.screener_config.live_hot_symbols_cap = 2
    monkeypatch.setattr(
        main_module, "_scan_general",
        _fake_scan_general(_ranked(("MSFT", 9.0), ("XOM", 8.0))),
    )

    subscribe_calls, unsubscribe_calls = [], []

    async def fake_subscribe(symbols):
        subscribe_calls.append(set(symbols))

    monkeypatch.setattr(main_module.broker, "stream_subscribe", fake_subscribe)
    monkeypatch.setattr(main_module.broker, "stream_unsubscribe", lambda symbols: unsubscribe_calls.append(set(symbols)))

    asyncio.run(main_module._run_hot_set_cycle())

    assert subscribe_calls == [{"XOM"}]  # solo lo nuevo
    assert unsubscribe_calls == [{"AAPL"}]  # solo lo que salio del top
    assert main_module._hot_symbols == {"MSFT", "XOM"}


def test_hot_set_cycle_keeps_previous_set_intact_if_subscribe_fails(monkeypatch):
    main_module._hot_symbols.update({"AAPL"})
    monkeypatch.setattr(
        main_module, "_scan_general",
        _fake_scan_general(_ranked(("MSFT", 9.0))),
    )

    async def failing_subscribe(symbols):
        raise RuntimeError("error de IBKR")

    unsubscribe_calls = []
    monkeypatch.setattr(main_module.broker, "stream_subscribe", failing_subscribe)
    monkeypatch.setattr(main_module.broker, "stream_unsubscribe", lambda symbols: unsubscribe_calls.append(set(symbols)))

    asyncio.run(main_module._run_hot_set_cycle())

    # Si suscribir lo nuevo falla, no se desuscribe nada de lo viejo: el
    # hot-set anterior queda intacto en vez de quedar sin streaming.
    assert unsubscribe_calls == []
    assert main_module._hot_symbols == {"AAPL"}


def test_hot_set_cycle_handles_scan_failure_gracefully(monkeypatch):
    main_module._hot_symbols.update({"AAPL"})

    async def failing_scan(force=False):
        raise RuntimeError("fallo de datos de mercado")

    monkeypatch.setattr(main_module, "_scan_general", failing_scan)
    asyncio.run(main_module._run_hot_set_cycle())  # no debe propagar la excepcion
    assert main_module._hot_symbols == {"AAPL"}  # sin cambios


# ---------------------------------------------------------------------------
# _run_price_rotation_cycle
# ---------------------------------------------------------------------------

def test_price_rotation_noop_when_radar_disabled(monkeypatch):
    main_module.screener_config.live_radar_enabled = False
    main_module.screener_config.universe = ["AAPL", "MSFT"]

    async def fail_if_called(symbols):
        raise AssertionError("no deberia rotar precios con el radar deshabilitado")

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fail_if_called)
    asyncio.run(main_module._run_price_rotation_cycle())
    assert main_module._live_prices == {}


def test_price_rotation_noop_when_not_connected(monkeypatch):
    main_module.state["connected"] = False
    main_module.screener_config.universe = ["AAPL", "MSFT"]

    async def fail_if_called(symbols):
        raise AssertionError("no deberia rotar precios sin conexion a IBKR")

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fail_if_called)
    asyncio.run(main_module._run_price_rotation_cycle())
    assert main_module._live_prices == {}


def test_price_rotation_noop_when_every_symbol_is_hot(monkeypatch):
    main_module.screener_config.universe = ["AAPL", "MSFT"]
    main_module._hot_symbols.update({"AAPL", "MSFT"})

    async def fail_if_called(symbols):
        raise AssertionError("no deberia rotar si no quedan simbolos frios")

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fail_if_called)
    asyncio.run(main_module._run_price_rotation_cycle())
    assert main_module._live_prices == {}


def test_price_rotation_excludes_hot_symbols_from_batch(monkeypatch):
    main_module.screener_config.universe = ["AAPL", "MSFT", "XOM"]
    main_module.screener_config.live_rotation_batch_size = 10
    main_module._hot_symbols.update({"AAPL"})

    requested = []

    async def fake_snapshot(symbols):
        requested.append(list(symbols))
        return {}

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fake_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())

    assert requested == [["MSFT", "XOM"]]  # AAPL esta caliente, no se pide por snapshot


def test_price_rotation_batch_size_respects_free_lines_left_by_hot_set(monkeypatch):
    main_module.screener_config.universe = [f"S{i}" for i in range(10)]
    main_module.screener_config.live_rotation_batch_size = 10
    # 97 simbolos calientes (simulado por la longitud del set, no por
    # streaming real) dejan solo 3 lineas libres para la rotacion.
    main_module._hot_symbols.update({f"H{i}" for i in range(97)})

    requested = []

    async def fake_snapshot(symbols):
        requested.append(list(symbols))
        return {}

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fake_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())

    assert len(requested[0]) == 3


def test_price_rotation_batch_size_respects_configured_cap(monkeypatch):
    main_module.screener_config.universe = [f"S{i}" for i in range(10)]
    main_module.screener_config.live_rotation_batch_size = 2

    requested = []

    async def fake_snapshot(symbols):
        requested.append(list(symbols))
        return {}

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fake_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())

    assert len(requested[0]) == 2


def test_price_rotation_advances_cursor_round_robin_across_cycles(monkeypatch):
    main_module.screener_config.universe = ["A", "B", "C", "D"]
    main_module.screener_config.live_rotation_batch_size = 2

    requested = []

    async def fake_snapshot(symbols):
        requested.append(list(symbols))
        return {}

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fake_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())
    asyncio.run(main_module._run_price_rotation_cycle())
    asyncio.run(main_module._run_price_rotation_cycle())  # da la vuelta completa

    assert requested == [["A", "B"], ["C", "D"], ["A", "B"]]


def test_price_rotation_updates_live_prices_with_snapshot_results(monkeypatch):
    main_module.screener_config.universe = ["AAPL", "MSFT"]

    async def fake_snapshot(symbols):
        return {"AAPL": 155.0, "MSFT": 290.0}

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", fake_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())

    assert main_module._live_prices == {"AAPL": 155.0, "MSFT": 290.0}
    assert main_module._live_prices_as_of.keys() == {"AAPL", "MSFT"}


def test_price_rotation_handles_snapshot_failure_gracefully(monkeypatch):
    main_module.screener_config.universe = ["AAPL", "MSFT"]
    main_module._live_prices["AAPL"] = 100.0

    async def failing_snapshot(symbols):
        raise RuntimeError("pacing violation")

    monkeypatch.setattr(main_module.broker, "get_snapshot_prices", failing_snapshot)
    asyncio.run(main_module._run_price_rotation_cycle())  # no debe propagar la excepcion

    assert main_module._live_prices == {"AAPL": 100.0}  # sin cambios
