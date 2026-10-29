import os

# Mismo patron que test_auto_trading.py: asegurar API_KEY antes de importar
# app.config, para que Settings() no falle su propia validacion si este
# archivo se corre solo (sin que otro test module ya lo haya seteado).
os.environ.setdefault("API_KEY", "test-key")

import pytest

from app.config import Settings, _float_env, _int_env


def test_int_env_parses_valid_value():
    assert _int_env("IB_PORT", "7497") == 7497


def test_int_env_raises_friendly_runtime_error_on_invalid_value():
    # Antes de este fix, un IB_PORT mal escrito tiraba un ValueError crudo de
    # int() al importar app.config, sin decir cual variable de entorno fue.
    with pytest.raises(RuntimeError, match="IB_PORT"):
        _int_env("IB_PORT", "not-a-number")


def test_float_env_parses_valid_value():
    assert _float_env("POLL_INTERVAL_SECONDS", "2.5") == 2.5


def test_float_env_raises_friendly_runtime_error_on_invalid_value():
    with pytest.raises(RuntimeError, match="POLL_INTERVAL_SECONDS"):
        _float_env("POLL_INTERVAL_SECONDS", "not-a-number")


def test_settings_raises_friendly_error_for_invalid_ib_port(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IB_PORT", "not-a-number")
    with pytest.raises(RuntimeError, match="IB_PORT"):
        Settings()


def test_settings_raises_friendly_error_for_invalid_poll_interval(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "not-a-number")
    with pytest.raises(RuntimeError, match="POLL_INTERVAL_SECONDS"):
        Settings()


def test_ib_market_data_type_defaults_to_delayed(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("IB_MARKET_DATA_TYPE", raising=False)
    assert Settings().ib_market_data_type == 3


def test_ib_market_data_type_configurable_to_real_time(monkeypatch):
    # Sin tocar codigo: una cuenta con suscripcion de datos en tiempo real
    # (incluida la paper, que suele heredar los entitlements de la cuenta
    # real vinculada) puede pasar a real-time (1) via .env.
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IB_MARKET_DATA_TYPE", "1")
    assert Settings().ib_market_data_type == 1
