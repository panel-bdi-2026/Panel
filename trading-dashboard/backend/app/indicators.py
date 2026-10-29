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


def momentum_12_1(close: pd.Series, lookback: int, skip: int) -> pd.Series:
    """Momentum academico "12 meses menos 1" (Jegadeesh & Titman): retorno
    entre t-lookback y t-skip, saltando el tramo mas reciente (`skip` dias).
    A diferencia de un blend `momentum_3m + momentum_1m`, no carga el tramo
    de 1 mes que en la practica tiene reversion de corto plazo en vez de
    momentum -- ese tramo queda excluido del calculo, no solo sub-ponderado."""
    return (close.shift(skip) / close.shift(lookback) - 1) * 100


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


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD estandar: linea (EMA rapida - EMA lenta), señal (EMA de la linea)
    e histograma (linea - señal). Histograma > 0 indica momentum alcista."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_percent_b(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    """%B: posicion del precio dentro de las bandas de Bollinger (SMA +/- N
    desvios). 0 = banda inferior, 1 = banda superior; valores fuera de
    [0, 1] indican que el precio rompio una banda."""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (close - lower) / (upper - lower)


def market_regime_ok(
    close: pd.Series,
    sma_period: int = 200,
    slope_lookback: int = 20,
    absolute_momentum_lookback: int = 252,
) -> pd.Series:
    """Filtro de regimen sin whipsaw: en vez de un cruce binario y lento
    (precio vs SMA200), exige que se cumplan dos condiciones mas lentas a la
    vez. (a) Pendiente: la SMA de regimen hoy esta por encima de si misma
    `slope_lookback` dias atras (la media esta subiendo, no solo el precio
    cruzandola). (b) Momentum absoluto (dual momentum, Antonacci): el
    benchmark tiene retorno positivo en los ultimos `absolute_momentum_lookback`
    dias. Ambas en AND: alternar de a una sola senal lenta sigue dejando
    pasar el mismo whipsaw que motivo este cambio. Sin suficiente historia
    para alguna de las dos, esa condicion no bloquea (True), igual que
    pct_from_high/near_high_filter: NaN no es evidencia de regimen malo."""
    regime_sma = sma(close, sma_period)
    prior_sma = regime_sma.shift(slope_lookback)
    slope_ok = (regime_sma > prior_sma) | prior_sma.isna()
    absolute_momentum = rate_of_change(close, absolute_momentum_lookback)
    momentum_ok = (absolute_momentum > 0) | absolute_momentum.isna()
    return slope_ok & momentum_ok


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
