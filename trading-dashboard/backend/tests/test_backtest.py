import numpy as np
import pandas as pd
import pytest

from app import backtest as backtest_module
from app.backtest import BacktestError, run_backtest
from app.market_data import MarketDataError
from app.screener_config import ScreenerConfig


def _series(n, drift, amplitude, period, base=100.0):
    i = np.arange(n)
    return list(base + drift * i + amplitude * np.sin(i / period))


def _bars(close_values, volume=5_000_000):
    idx = pd.date_range("2021-01-01", periods=len(close_values), freq="D")
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


# Suficiente historia (>1 anio de trading) para que entren y salgan varias
# operaciones dentro del backtest.
MOM_CLOSE = _series(500, 0.12, 4, 5)
FLAT_CLOSE = _series(500, 0.0, 1, 4)
BENCH_CLOSE = _series(500, 0.02, 1, 6)

FAKE_BARS = {
    "MOM": _bars(MOM_CLOSE),
    "FLAT": _bars(FLAT_CLOSE),
    "SPY": _bars(BENCH_CLOSE),
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days):
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)


def test_backtest_produces_trades_and_metrics(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert summary.total_trades > 0
    assert 0 <= summary.win_rate_pct <= 100
    assert summary.start_date < summary.end_date
    assert all(t.symbol in ("MOM", "FLAT") for t in summary.trades)


def test_backtest_raises_when_benchmark_unavailable(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days):
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["MOM"], benchmark_symbol="SPY")
    with pytest.raises(BacktestError):
        run_backtest(config)


def test_backtest_raises_when_no_trades_generated(monkeypatch):
    # Universo vacio de simbolos validos -> nunca hay senal de entrada.
    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return FAKE_BARS["SPY"]
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["MISSING"], benchmark_symbol="SPY")
    with pytest.raises(BacktestError):
        run_backtest(config)
