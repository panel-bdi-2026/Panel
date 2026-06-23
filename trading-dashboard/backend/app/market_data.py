from __future__ import annotations

import threading
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

# Los fundamentals (PE, ROE, crecimiento, dividend yield, etc.) cambian a lo
# sumo trimestralmente: mismo cache largo que earnings, por la misma razon
# (no vale la pena pagar la cuota de la API en cada scan por un dato que casi
# nunca cambia de un dia a otro).
_FUNDAMENTALS_CACHE_TTL_SECONDS = 24 * 3600
_fundamentals_cache: dict[str, tuple[float, dict]] = {}


class _KeyedLocks:
    """Un threading.Lock por clave, creado on-demand. get_daily_bars y
    compania se llaman concurrentemente desde threads de verdad (via
    asyncio.to_thread en main.py, no solo corutinas intercaladas): sin esto,
    dos threads que piden el mismo simbolo a la vez (ej. dos estrategias
    escaneando el mismo ticker en el mismo ciclo) pueden pasar juntos el
    check de cache-vacio y disparar dos fetches redundantes a la API gratuita
    en vez de que el segundo espere y reuse el resultado del primero. Un lock
    por clave (no uno global) evita serializar fetches de simbolos distintos
    entre si."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict = {}

    def get(self, key) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


_bars_locks = _KeyedLocks()
_earnings_locks = _KeyedLocks()
_fundamentals_locks = _KeyedLocks()


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

    with _bars_locks.get(key):
        # Re-chequea el cache bajo el lock: mientras se esperaba para entrar
        # aca, otro thread puede haber completado el fetch para esta misma
        # clave (mismo symbol+lookback_days).
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


def is_bars_cached(symbol: str, lookback_days: int) -> bool:
    """True si get_daily_bars(symbol, lookback_days) devolveria el cache sin
    pegarle a la red ahora mismo. Le permite a cada estrategia saltear la
    pausa entre simbolos (pensada para no rafagar la API gratuita) cuando el
    dato ya esta cacheado -- tipicamente porque otra estrategia ya escaneo
    este mismo simbolo en este ciclo, ya que las 4 estrategias comparten
    lookback_days y por lo tanto la misma entrada de cache."""
    cached = _cache.get((symbol.upper(), lookback_days))
    return cached is not None and time.time() - cached[0] < _CACHE_TTL_SECONDS


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

    with _earnings_locks.get(key):
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


# Mapeo de las claves crudas de yfinance (Ticker.get_info(), un dict de
# estructura libre y no documentada formalmente) a nombres estables que el
# resto del codigo consume, para que un cambio interno de yfinance quede
# aislado a esta sola linea por campo.
_INFO_FIELD_MAP = {
    "trailing_pe": "trailingPE",
    "forward_pe": "forwardPE",
    "price_to_book": "priceToBook",
    "return_on_equity": "returnOnEquity",
    "revenue_growth": "revenueGrowth",
    "earnings_growth": "earningsGrowth",
    "debt_to_equity": "debtToEquity",
    "profit_margins": "profitMargins",
    "dividend_yield": "dividendYield",
    "payout_ratio": "payoutRatio",
    # peg_ratio_trailing es el respaldo de peg_ratio: yfinance reemplazo
    # "pegRatio" por "trailingPegRatio" en versiones recientes, y no hay forma
    # de saber de antemano cual de las dos va a estar presente (ver el mismo
    # patron de respaldo trailing_pe -> forward_pe en strategies/long_term.py).
    "peg_ratio": "pegRatio",
    "peg_ratio_trailing": "trailingPegRatio",
    "beta": "beta",
    "recommendation_key": "recommendationKey",
    # Fracciones (0-1) en yfinance, no porcentajes: se normalizan a % recien
    # en las estrategias que las consumen, igual que payout_ratio.
    "insider_ownership": "heldPercentInsiders",
    "institutional_ownership": "heldPercentInstitutions",
    "short_pct_of_float": "shortPercentOfFloat",
    "current_ratio": "currentRatio",
    "quick_ratio": "quickRatio",
    "free_cash_flow": "freeCashflow",
}


def get_fundamentals(symbol: str, force: bool = False) -> dict:
    """Datos fundamentales de `symbol` para las estrategias Largo plazo y
    Dividendos (ver app/strategies/). Devuelve un dict con las claves de
    _INFO_FIELD_MAP; un campo ausente en la respuesta de yfinance queda en
    None (dato no disponible, no es un error) en vez de levantar excepcion,
    ya que estas estrategias tratan cada campo faltante de forma puntual
    (algunos bloquean el filtro, otros son solo bonus de score).

    Igual que get_next_earnings_date: si yfinance falla o no devuelve nada
    util, se cachea un dict de Nones en vez de reintentar en cada llamada
    (la causa mas comun es falta de cobertura para ese simbolo, no un fallo
    transitorio, a diferencia de las barras de precio).
    """
    key = symbol.upper()
    now = time.time()
    cached = _fundamentals_cache.get(key)
    if not force and cached and now - cached[0] < _FUNDAMENTALS_CACHE_TTL_SECONDS:
        return cached[1]

    with _fundamentals_locks.get(key):
        now = time.time()
        cached = _fundamentals_cache.get(key)
        if not force and cached and now - cached[0] < _FUNDAMENTALS_CACHE_TTL_SECONDS:
            return cached[1]

        try:
            info = yf.Ticker(symbol).get_info() or {}
        except Exception:
            info = {}

        result = {name: info.get(raw_key) for name, raw_key in _INFO_FIELD_MAP.items()}
        _fundamentals_cache[key] = (now, result)
        return result
