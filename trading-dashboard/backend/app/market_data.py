from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

# Cache simple en memoria para no golpear el limite de la API gratuita de Yahoo
# Finance en cada refresh del dashboard o cada simbolo de un scan.
_CACHE_TTL_SECONDS = 900  # 15 min
_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}

# La fecha de earnings no cambia de un minuto a otro: cache mas largo que el
# de las barras de precio para no consumir cuota de la API en cada scan.
_EARNINGS_CACHE_TTL_SECONDS = 24 * 3600
_earnings_cache: dict[str, tuple[float, "date | None"]] = {}


class MarketDataError(RuntimeError):
    pass


# La API gratuita de Yahoo Finance a veces responde vacio o con error de forma
# transitoria (rate limiting silencioso, hiccup de red) sin que el simbolo sea
# realmente invalido. Reintentar con backoff evita descartar un simbolo valido
# por un fallo pasajero, a costa de una demora acotada en el peor caso (simbolo
# realmente invalido o limite sostenido).
_MAX_FETCH_RETRIES = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.5


def get_daily_bars(symbol: str, lookback_days: int, force: bool = False) -> pd.DataFrame:
    """Barras diarias OHLCV ajustadas para `symbol`, cubriendo ~lookback_days dias de trading.

    Usa yfinance (datos de Yahoo Finance, no oficiales, gratis y con limites de
    uso) en vez de IBKR para no consumir suscripciones de market data solo para
    investigacion/backtesting. Resultado cacheado por simbolo+ventana.

    `force=True` ignora el cache (usado por el boton "forzar rescan" del
    dashboard): sin esto, forzar un rescan dentro de los 15 minutos del cache
    no traia datos nuevos a pesar de que el usuario lo pidio explicitamente.

    Reintenta hasta `_MAX_FETCH_RETRIES` veces con backoff exponencial ante
    excepcion o respuesta vacia, ya que ambas pueden ser un fallo transitorio
    de la API gratuita (ver comentario de `_MAX_FETCH_RETRIES`).
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

    df = None
    last_error: Exception | None = None
    for attempt in range(_MAX_FETCH_RETRIES):
        try:
            df = yf.Ticker(symbol).history(start=start.date(), end=end.date(), interval="1d", auto_adjust=True)
            last_error = None
            if df is not None and not df.empty:
                break
        except Exception as exc:
            df = None
            last_error = exc
        if attempt < _MAX_FETCH_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt))

    if df is None or df.empty:
        detail = f" ({last_error})" if last_error else ""
        raise MarketDataError(
            f"Sin datos para {symbol} tras {_MAX_FETCH_RETRIES} intentos{detail}: "
            f"simbolo invalido o limite de la API gratuita alcanzado."
        )

    df = df.rename(columns=str.title)
    _cache[key] = (now, df)
    return df


def get_next_earnings_date(symbol: str, force: bool = False) -> "date | None":
    """Proxima fecha de earnings estimada para `symbol`, o None si no se pudo
    determinar (simbolo sin cobertura, limite de la API gratuita, etc.). El
    filtro de earnings del screener trata None como "sin dato, no bloquea" en
    vez de fallar el scan completo por un dato secundario y best-effort."""
    key = symbol.upper()
    now = time.time()
    cached = _earnings_cache.get(key)
    if not force and cached and now - cached[0] < _EARNINGS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        # get_earnings_dates devuelve fechas pasadas y futuras mezcladas, sin
        # garantia de orden; con limit=1 se podia terminar tomando una fecha
        # YA PASADA y el blackout nunca se activaba (days_to_earnings quedaba
        # negativo). Se pide un lote mas grande y se filtra explicitamente por
        # la mas próxima que sea hoy o futura.
        dates = yf.Ticker(symbol).get_earnings_dates(limit=12)
        next_date = None
        if dates is not None and len(dates):
            today = datetime.now(timezone.utc).date()
            future_dates = [ts.date() for ts in dates.index if ts.date() >= today]
            next_date = min(future_dates) if future_dates else None
    except Exception:
        next_date = None

    _earnings_cache[key] = (now, next_date)
    return next_date
