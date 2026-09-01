import pytest

import app.sectors as sectors_module
from app.sectors import TICKER_SECTOR, _VALID_SECTORS, get_sector, refresh_sector


@pytest.fixture(autouse=True)
def _clear_runtime_sector_cache():
    # _runtime_sector_cache es estado de modulo compartido por todo el
    # proceso de tests (incluye test_api.py, que ejercita el mismo cache via
    # POST /api/sectors/refresh): sin este fixture, un simbolo de prueba
    # resuelto aca quedaba pegado en el cache y contaminaba esos otros tests.
    sectors_module._runtime_sector_cache.clear()
    yield
    sectors_module._runtime_sector_cache.clear()


def test_get_sector_returns_known_sector():
    assert get_sector("AAPL") == "Information Technology"


def test_get_sector_is_case_insensitive():
    assert get_sector("aapl") == get_sector("AAPL")


def test_get_sector_returns_none_for_unknown_symbol():
    assert get_sector("ZZZZNOTASYMBOL") is None


def test_all_mapped_sectors_are_valid_gics_sectors():
    assert set(TICKER_SECTOR.values()) <= _VALID_SECTORS


class _FakeTicker:
    def __init__(self, info):
        self._info = info

    def get_info(self):
        return self._info


def test_refresh_sector_maps_yfinance_taxonomy_to_gics(monkeypatch):
    monkeypatch.setattr(
        sectors_module.yf, "Ticker", lambda symbol: _FakeTicker({"sector": "Consumer Cyclical"})
    )
    assert refresh_sector("newco") == "Consumer Discretionary"


def test_refresh_sector_populates_runtime_cache_used_by_get_sector(monkeypatch):
    monkeypatch.setattr(sectors_module.yf, "Ticker", lambda symbol: _FakeTicker({"sector": "Technology"}))
    refresh_sector("NEWCO2")
    assert get_sector("NEWCO2") == "Information Technology"


def test_refresh_sector_returns_none_for_unmapped_or_missing_sector(monkeypatch):
    monkeypatch.setattr(sectors_module.yf, "Ticker", lambda symbol: _FakeTicker({}))
    assert refresh_sector("UNKNOWNCO") is None


def test_refresh_sector_returns_none_when_yfinance_raises(monkeypatch):
    class _RaisingTicker:
        def get_info(self):
            raise RuntimeError("rate limited")

    monkeypatch.setattr(sectors_module.yf, "Ticker", lambda symbol: _RaisingTicker())
    assert refresh_sector("BROKENCO") is None


def test_static_mapping_takes_priority_over_runtime_cache():
    sectors_module._runtime_sector_cache["AAPL"] = "Energy"
    assert get_sector("AAPL") == "Information Technology"
