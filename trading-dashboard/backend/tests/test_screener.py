import numpy as np
import pandas as pd
import pytest

from app import screener as screener_module
from app.market_data import MarketDataError
from app.screener import MomentumScreener
from app.screener_config import ScreenerConfig


def _series(n, drift, amplitude, period, base=100.0):
    i = np.arange(n)
    return list(base + drift * i + amplitude * np.sin(i / period))


def _bars(close_values, volume=5_000_000):
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="D")
    close = pd.Series(close_values, index=idx)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


# Series sinteticas (140 dias, sin red): MOM sube con fuerza relativa clara vs
# el benchmark; WEAK avanza apenas por encima del benchmark; BROKEN esta en
# clara tendencia bajista; ILLIQUID tiene el mismo precio que MOM pero volumen
# por debajo del minimo de liquidez configurado.
MOM_CLOSE = _series(140, 0.25, 3, 4)
WEAK_CLOSE = _series(140, 0.02, 1, 3)
BROKEN_CLOSE = _series(140, -0.25, -2, 4, base=160.0)
BENCH_CLOSE = _series(140, 0.05, 1, 6)

FAKE_BARS = {
    "MOM": _bars(MOM_CLOSE),
    "WEAK": _bars(WEAK_CLOSE),
    "BROKEN": _bars(BROKEN_CLOSE),
    "ILLIQUID": _bars(MOM_CLOSE, volume=10_000),
    "SPY": _bars(BENCH_CLOSE),
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days):
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    monkeypatch.setattr(screener_module, "get_daily_bars", fake_get_daily_bars)
    return fake_get_daily_bars


@pytest.fixture
def screener(patched_market_data) -> MomentumScreener:
    config = ScreenerConfig(universe=["MOM", "WEAK", "BROKEN", "ILLIQUID"], benchmark_symbol="SPY")
    return MomentumScreener(config)


def test_strong_momentum_symbol_passes_filters_and_scores_highest(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["MOM"].trend_ok
    assert by_symbol["MOM"].passes_filters
    assert results[0].symbol == "MOM"


def test_broken_trend_symbol_fails_filters(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert not by_symbol["BROKEN"].trend_ok
    assert not by_symbol["BROKEN"].passes_filters


def test_illiquid_symbol_fails_liquidity_filter_despite_same_price(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["ILLIQUID"].trend_ok  # mismo precio que MOM
    assert not by_symbol["ILLIQUID"].passes_filters
    assert any("volumen" in note.lower() for note in by_symbol["ILLIQUID"].notes)


def test_results_sorted_descending_by_score(screener):
    results = screener.scan()
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_unknown_symbol_is_skipped(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "GHOST"], benchmark_symbol="SPY")
    s = MomentumScreener(config)
    results = s.scan()
    assert [r.symbol for r in results] == ["MOM"]


def test_stop_loss_is_below_last_price(screener):
    results = screener.scan()
    for r in results:
        assert r.suggested_stop_loss_price < r.last_price
        assert r.suggested_stop_loss_pct > 0


def test_scan_raises_when_no_symbol_has_data(monkeypatch):
    def always_fails(symbol, lookback_days):
        raise MarketDataError("sin red en este entorno")

    monkeypatch.setattr(screener_module, "get_daily_bars", always_fails)
    config = ScreenerConfig(universe=["MOM", "WEAK"], benchmark_symbol="SPY")
    s = MomentumScreener(config)
    with pytest.raises(MarketDataError):
        s.scan()
