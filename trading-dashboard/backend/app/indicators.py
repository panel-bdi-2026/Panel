from __future__ import annotations

import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder. Sin perdidas en la ventana -> 100 (o 50 si tampoco hay
    ganancias, ej. precio plano). Sin suficiente historia -> 50 (neutral)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    out = 100 - (100 / (1 + rs))
    no_losses = avg_loss == 0
    out = out.where(~no_losses, (avg_gain > 0).astype(float) * 100.0 + (avg_gain == 0).astype(float) * 50.0)
    return out.fillna(50.0)


def rate_of_change(close: pd.Series, period: int) -> pd.Series:
    return (close / close.shift(period) - 1) * 100


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def pct_from_high(close: pd.Series, window: int) -> pd.Series:
    """Pct. de distancia (<=0) del cierre actual respecto del maximo de los
    ultimos `window` dias. Exige la ventana completa (min_periods=window):
    con min_periods=1, un simbolo con poca historia (recien listado, o con
    menos de `window` dias en cache) reportaba un "maximo" calculado sobre
    una ventana mucho mas corta como si fuera un dato real -- ej. confundir
    el maximo de unas pocas semanas con el de 52 semanas. Ahora esos casos
    devuelven NaN; los llamadores (screener.py, backtest.py) ya tratan NaN
    como "sin dato, el filtro de proximidad al maximo no bloquea"."""
    rolling_high = close.rolling(window, min_periods=window).max()
    return (close / rolling_high - 1) * 100
