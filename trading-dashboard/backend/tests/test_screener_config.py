import pytest
from pydantic import ValidationError

from app.screener_config import ScreenerConfig


def test_universe_is_normalized_to_uppercase():
    config = ScreenerConfig(universe=["aapl", "brk.b"])
    assert config.universe == ["AAPL", "BRK.B"]


def test_universe_rejects_malicious_or_malformed_symbol():
    # _sync_whitelist_with_universe() (main.py) copia este universo directo a
    # rules_config.symbol_whitelist, que RulesEngine.evaluate() usa para
    # aprobar ordenes: un simbolo sin la misma sanitizacion que
    # OrderRequest.symbol podria persistirse en la whitelist sin que ninguna
    # orden real (siempre normalizada) lo matchee nunca.
    with pytest.raises(ValidationError):
        ScreenerConfig(universe=["<script>alert(1)</script>"])


def test_universe_rejects_oversized_list():
    # Tope de DoS: una lista descomunal haria que un scan tarde horas o agote
    # la cuota de la API de datos (ver Field(max_length=1000) en ScreenerConfig).
    with pytest.raises(ValidationError):
        ScreenerConfig(universe=[f"S{i}" for i in range(1001)])


def test_benchmark_symbol_is_normalized_to_uppercase():
    config = ScreenerConfig(benchmark_symbol="spy")
    assert config.benchmark_symbol == "SPY"


def test_benchmark_symbol_rejects_invalid_value():
    with pytest.raises(ValidationError):
        ScreenerConfig(benchmark_symbol="; DROP TABLE")
