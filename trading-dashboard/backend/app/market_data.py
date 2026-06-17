from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

# Cache simple en memoria para no golpear el limite de la API gratuita de Yahoo
# Finance en cada refresh del dashboard o cada simbolo de un scan.
_CACHE_TTL_SECONDS = 900  # 15 min
_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}


class MarketDataError(RuntimeError):
    pass


def get_daily_bars(symbol: str, lookback_days: int, force: bool = False) -> pd.DataFrame:
    """Barras diarias OHLCV ajustadas para `symbol`, cubriendo ~lookback_days dias de trading.

    Usa yfinance (datos de Yahoo Finance, no oficiales, gratis y con limites de
    uso) en vez de IBKR para no consumir suscripciones de market data solo para
    investigacion/backtesting. Resultado cacheado por simbolo+ventana.

    `force=True` ignora el cache (usado por el boton "forzar rescan" del
    dashboard): sin esto, forzar un rescan dentro de los 15 minutos del cache
    no traia datos nuevos a pesar de que el usuario lo pidio explicitamente.
    """
    key = (symbol.upper(), lookback_days)
    now = time.time()
    cached = _cache.get(key)
    if not force and cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    end = datetime.now(timezone.utc)
    # *1.6 para convertir dias de trading aproximados a dias calendario (fines de
    # semana/feriados) mas un margen.
    start = end - timedelta(days=int(lookback_days * 1.6) + 10)
    try:
        df = yf.Ticker(symbol).history(start=start.date(), end=end.date(), interval="1d", auto_adjust=True)
    except Exception as exc:
        raise MarketDataError(f"No se pudo obtener datos de {symbol}: {exc}") from exc

    if df is None or df.empty:
        raise MarketDataError(
            f"Sin datos para {symbol}: simbolo invalido o limite de la API gratuita alcanzado."
        )

    df = df.rename(columns=str.title)
    _cache[key] = (now, df)
    return df
