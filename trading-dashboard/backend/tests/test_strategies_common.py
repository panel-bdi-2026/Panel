from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies import common as common_module
from app.strategies.common import avg_dollar_volume, avg_volume, context_technicals, earnings_blackout_ok


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


def test_context_technicals_returns_none_with_insufficient_history():
    bars = _bars(_series(30, 0.1, 1, 4))
    assert context_technicals(bars, atr_period=14) is None


def test_context_technicals_returns_fields_with_enough_history():
    bars = _bars(_series(265, 0.25, 3, 4))
    ctx = context_technicals(bars, atr_period=14)
    assert ctx is not None
    assert ctx["last_price"] == float(bars["Close"].iloc[-1])
    assert isinstance(ctx["trend_ok"], bool)
    assert ctx["pct_from_52w_high"] is not None


def test_context_technicals_pct_from_52w_high_is_none_without_full_window():
    # Con menos de 252 dias no hay ventana completa para un maximo de 52
    # semanas real (ver indicators.pct_from_high): debe quedar en None, no en
    # un maximo calculado sobre una ventana mas corta.
    bars = _bars(_series(100, 0.25, 3, 4))
    ctx = context_technicals(bars, atr_period=14)
    assert ctx is not None
    assert ctx["pct_from_52w_high"] is None


def test_avg_dollar_volume_is_volume_times_price():
    bars = _bars([100.0] * 25, volume=1_000)
    assert avg_dollar_volume(bars) == pytest.approx(100_000.0)


def test_avg_volume_is_rolling_mean_of_last_20_days():
    bars = _bars([100.0] * 25, volume=2_000)
    assert avg_volume(bars) == pytest.approx(2_000.0)


def test_earnings_blackout_ok_when_no_earnings_date_known(monkeypatch):
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: None)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is True
    assert days is None


def test_earnings_blackout_blocks_when_within_window(monkeypatch):
    soon = date.today() + timedelta(days=2)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: soon)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is False
    assert days == 2


def test_earnings_blackout_does_not_block_outside_window(monkeypatch):
    far = date.today() + timedelta(days=30)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: far)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is True
    assert days == 30
