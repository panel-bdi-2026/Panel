import numpy as np
import pandas as pd
import pytest

from app.indicators import atr, pct_from_high, rate_of_change, rsi, sma


def _series(values, periods=None):
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.Series(values, index=idx)


def test_sma_basic():
    s = _series([1, 2, 3, 4, 5])
    out = sma(s, 2)
    assert out.iloc[-1] == pytest.approx(4.5)
    assert pd.isna(out.iloc[0])


def test_rsi_pure_uptrend_is_100():
    s = pd.Series(np.linspace(100, 130, 60))
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_pure_downtrend_is_0():
    s = pd.Series(np.linspace(130, 100, 60))
    assert rsi(s, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_neutral():
    s = pd.Series([100.0] * 60)
    assert rsi(s, 14).iloc[-1] == pytest.approx(50.0)


def test_rsi_insufficient_history_is_neutral():
    s = pd.Series([100.0, 101.0, 99.0])
    assert rsi(s, 14).iloc[-1] == pytest.approx(50.0)


def test_rate_of_change():
    s = pd.Series([100.0, 110.0, 121.0])
    roc = rate_of_change(s, 2)
    assert roc.iloc[-1] == pytest.approx(21.0)


def test_atr_constant_range():
    close = pd.Series(np.linspace(100, 110, 30))
    high = close + 2
    low = close - 2
    out = atr(high, low, close, 14)
    assert out.iloc[-1] == pytest.approx(4.0, rel=0.2)


def test_pct_from_high():
    s = pd.Series([100.0, 110.0, 99.0])
    out = pct_from_high(s, 3)
    assert out.iloc[-1] == pytest.approx((99.0 / 110.0 - 1) * 100)
