import numpy as np
import pandas as pd
import pytest

from app.indicators import atr, market_regime_ok, pct_from_high, rate_of_change, rsi, sma


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


def test_pct_from_high_with_insufficient_history_is_nan():
    # Con menos dias que la ventana pedida no hay un maximo real de esa
    # ventana para comparar: debe ser NaN, no un "maximo" calculado sobre
    # los pocos dias disponibles.
    s = pd.Series([100.0, 110.0, 99.0])
    out = pct_from_high(s, 5)
    assert out.isna().all()


def test_market_regime_ok_true_when_sma_rising_and_momentum_positive():
    s = pd.Series(np.linspace(100, 150, 60))
    out = market_regime_ok(s, sma_period=10, slope_lookback=5, absolute_momentum_lookback=20)
    assert bool(out.iloc[-1])


def test_market_regime_ok_false_when_sma_falling_and_momentum_negative():
    s = pd.Series(np.linspace(150, 100, 60))
    out = market_regime_ok(s, sma_period=10, slope_lookback=5, absolute_momentum_lookback=20)
    assert not bool(out.iloc[-1])


def test_market_regime_ok_defaults_true_with_insufficient_history():
    # Sin suficiente historia para calcular la SMA de regimen ni el momentum
    # absoluto, ninguna de las dos condiciones puede bloquear: NaN no es
    # evidencia de regimen malo (mismo criterio que pct_from_high/near_high_
    # filter).
    s = pd.Series([100.0, 101.0, 102.0])
    out = market_regime_ok(s, sma_period=200, slope_lookback=20, absolute_momentum_lookback=252)
    assert bool(out.iloc[-1])


def test_market_regime_ok_blocks_on_negative_absolute_momentum_despite_rising_sma_slope():
    # Antonacci / dual momentum: una caida fuerte (200 -> 60) seguida de una
    # recuperacion reciente (60 -> 80) deja la SMA corta en pendiente
    # positiva (el filtro de pendiente solo, sin momentum absoluto, dejaria
    # pasar esto), pero el retorno de la ventana larga sigue siendo muy
    # negativo: el AND con momentum absoluto bloquea la falsa señal de
    # "regimen ya recuperado".
    falling = np.linspace(200, 60, 40)
    recovering = np.linspace(60, 80, 20)[1:]
    s = pd.Series(np.concatenate([falling, recovering]))
    out = market_regime_ok(s, sma_period=10, slope_lookback=5, absolute_momentum_lookback=50)
    assert not bool(out.iloc[-1])


def test_market_regime_ok_blocks_on_falling_sma_slope_despite_positive_absolute_momentum():
    # Caso inverso: suba sostenida (100 -> 200) seguida de una caida reciente
    # (200 -> 150) deja el momentum absoluto de la ventana larga positivo,
    # pero la SMA corta esta en pendiente negativa (el regimen reciente ya
    # gira a la baja): el AND con la pendiente bloquea igual.
    rising = np.linspace(100, 200, 40)
    pulling_back = np.linspace(200, 150, 20)[1:]
    s = pd.Series(np.concatenate([rising, pulling_back]))
    out = market_regime_ok(s, sma_period=10, slope_lookback=5, absolute_momentum_lookback=50)
    assert not bool(out.iloc[-1])
