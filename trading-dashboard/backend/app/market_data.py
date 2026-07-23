from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from app import tiingo_disk_cache as _disk

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Cache simple en memoria para no golpear el limite de la API gratuita de Yahoo
# Finance en cada refresh del dashboard o cada simbolo de un scan.
_CACHE_TTL_SECONDS = 900  # 15 min
_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}

# Simbolos que fallan de forma persistente (deslistados, ticker invalido) no
# tenian cache de fallo a diferencia de earnings/fundamentals: cada scan
# volvia a gastar los _MAX_FETCH_RETRIES completos en el mismo simbolo
# muerto, ciclo tras ciclo, para siempre. Mismo TTL que el cache de exito:
# si el simbolo vuelve a cotizar dentro de esos 15 min no vale la pena
# reintentar antes.
_bars_failure_cache: dict[tuple[str, int], tuple[float, str]] = {}

# La fecha de earnings no cambia de un minuto a otro: cache mas largo que el
# de las barras de precio para no consumir cuota de la API en cada scan.
_EARNINGS_CACHE_TTL_SECONDS = 24 * 3600
# Si el fetch fallo (excepcion: rate limit, timeout, hiccup de red), el motivo
# mas probable es transitorio, no "este simbolo no tiene earnings" -- cachear
# ese None por las mismas 24hs que un resultado realmente vacio lo deja sin
# reintentar todo un dia por un fallo pasajero. Mismo TTL corto que el cache
# de barras (15 min) para ese caso puntual.
_EARNINGS_FAILURE_CACHE_TTL_SECONDS = _CACHE_TTL_SECONDS
_earnings_cache: dict[str, tuple[float, "date | None", bool]] = {}  # (timestamp, value, ok)

# Los fundamentals (PE, ROE, crecimiento, dividend yield, etc.) cambian a lo
# sumo trimestralmente: mismo cache largo que earnings, por la misma razon
# (no vale la pena pagar la cuota de la API en cada scan por un dato que casi
# nunca cambia de un dia a otro).
_FUNDAMENTALS_CACHE_TTL_SECONDS = 24 * 3600
# Mismo motivo que _EARNINGS_FAILURE_CACHE_TTL_SECONDS: una excepcion en
# get_info() no amerita el cache largo de 24hs.
_FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS = _CACHE_TTL_SECONDS
_fundamentals_cache: dict[str, tuple[float, dict, bool]] = {}  # (timestamp, value, ok)

# El nombre comercial de una empresa no cambia (salvo un rebranding
# excepcional): mismo cache largo que fundamentals/earnings.
_COMPANY_NAME_CACHE_TTL_SECONDS = 24 * 3600
_COMPANY_NAME_FAILURE_CACHE_TTL_SECONDS = _CACHE_TTL_SECONDS
_company_name_cache: dict[str, tuple[float, "str | None", bool]] = {}  # (timestamp, value, ok)


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
_company_name_locks = _KeyedLocks()

# Rate limiter para Tiingo: los 10 workers del executor pueden rafaguear la API
# simultáneamente en el primer scan en frío, provocando 429. Un lock + timestamp
# serializa el momento de inicio de cada request dejando al menos _TIINGO_MIN_GAP_S
# entre ellos (la llamada HTTP en sí se hace fuera del lock, así que el siguiente
# thread puede empezar a prepararse mientras el anterior sigue esperando respuesta).
_tiingo_rate_lock = threading.Lock()
_tiingo_last_ts: list[float] = [0.0]  # lista mutable para compartir estado entre threads
_TIINGO_MIN_GAP_S = 0.5  # ≤2 requests/segundo — plan Power permite 10.000 req/hora (~2.78 req/s max)


class MarketDataError(RuntimeError):
    pass


# La API gratuita de Yahoo Finance a veces responde vacio o con error de forma
# transitoria (rate limiting silencioso, hiccup de red) sin que el simbolo sea
# realmente invalido. Reintentar con backoff evita descartar un simbolo valido
# por un fallo pasajero, a costa de una demora acotada en el peor caso (simbolo
# realmente invalido o limite sostenido).
_MAX_FETCH_RETRIES = 3
_RETRY_BACKOFF_BASE_SECONDS = 0.5

# Limite duro de pared para una sola llamada de red a yfinance. yfinance
# acepta su propio `timeout=` interno en algunos metodos, pero ese timeout es
# "sin datos nuevos por N segundos" (se reinicia con cada byte recibido), no
# un limite total de la operacion -- una conexion de Yahoo que va goteando
# bytes sin terminar la respuesta nunca lo dispara. Peor aun, la negociacion
# de cookie/crumb interna de yfinance esta protegida por un threading.Lock
# compartido por TODOS los simbolos y threads del proceso (yfinance.Ticker
# usa una unica sesion singleton): si esa llamada se cuelga, el lock queda
# tomado para siempre y bloquea cualquier otro fetch, lo que a su vez deja
# _market_scan_lock (ver main.py) tomado para siempre y tira abajo el
# dashboard entero hasta reiniciar el servicio. Forzar un timeout de pared
# con un thread separado evita que un solo simbolo cuelgue toda la app, aun
# si yfinance internamente sigue trabado (ese thread de fetch queda huerfano
# pero el resto de la app sigue funcionando).
_FETCH_TIMEOUT_SECONDS = 45
_fetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="market-data-fetch")


def _fetch_with_timeout(fn):
    future = _fetch_executor.submit(fn)
    try:
        return future.result(timeout=_FETCH_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        raise MarketDataError(f"Sin respuesta del proveedor de datos tras {_FETCH_TIMEOUT_SECONDS}s") from None


def _tiingo_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Descarga barras diarias ajustadas de Tiingo (API oficial, con SLA).

    Devuelve DataFrame con columnas Open/High/Low/Close/Volume (ajustadas por
    splits y dividendos) e índice DatetimeIndex UTC, compatible con el formato
    que espera el resto del stack.

    Aplica rate limiting global (_tiingo_rate_lock) para no rafaguear la API
    con los 10 workers concurrentes del executor durante un scan en frío.
    """
    import httpx
    from app.config import settings

    with _tiingo_rate_lock:
        wait = _TIINGO_MIN_GAP_S - (time.time() - _tiingo_last_ts[0])
        if wait > 0:
            time.sleep(wait)
        _tiingo_last_ts[0] = time.time()

    url = f"https://api.tiingo.com/tiingo/daily/{symbol.upper()}/prices"
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            url,
            params={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "token": settings.tiingo_api_key,
            },
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code == 429:
        time.sleep(10.0)  # backoff duro ante rate limit; el retry loop lo reintenta
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").sort_index()
    df.index.name = "Date"
    return pd.DataFrame(
        {
            "Open": df["adjOpen"],
            "High": df["adjHigh"],
            "Low": df["adjLow"],
            "Close": df["adjClose"],
            "Volume": df["adjVolume"],
        },
        index=df.index,
    )


def get_daily_bars(
    symbol: str, lookback_days: int, force: bool = False, cache_only: bool = False
) -> pd.DataFrame:
    """Barras diarias OHLCV ajustadas para `symbol`, cubriendo ~lookback_days dias de trading.

    Usa Tiingo (API oficial con SLA) para datos historicos, en vez de IBKR, para
    no consumir suscripciones de market data solo para investigacion/backtesting.
    Resultado cacheado por simbolo+ventana.

    `force=True` ignora el cache (usado por el boton "forzar rescan" del
    dashboard): sin esto, forzar un rescan dentro de los 15 minutos del cache
    no traia datos nuevos a pesar de que el usuario lo pidio explicitamente.

    `cache_only=True` (usado por el recalculo de scores, ver _run_score_recompute_cycle
    en main.py) nunca toca la red: devuelve el cache si esta fresco o relanza/lanza
    MarketDataError de inmediato. El refresco de datos real lo hace por separado
    _run_data_refresh_cycle, en lotes chicos -- el recalculo de scores tiene que
    poder correr seguido sin nunca bloquearse esperando a la red. Mutuamente
    excluyente con `force` por construccion del caller.

    Reintenta hasta `_MAX_FETCH_RETRIES` veces con backoff exponencial ante
    excepcion o respuesta vacia, ya que ambas pueden ser un fallo transitorio
    (ver comentario de `_MAX_FETCH_RETRIES`).
    """
    key = (symbol.upper(), lookback_days)
    now = time.time()
    cached = _cache.get(key)
    if not force and cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    failed = _bars_failure_cache.get(key)
    if not force and failed and now - failed[0] < _CACHE_TTL_SECONDS:
        raise MarketDataError(failed[1])
    if cache_only:
        # Antes de rendirse, verificar si el caché en disco tiene datos frescos.
        # cache_only nunca toca la red, pero el disco es legítimo: evita que un
        # reinicio del servicio vacíe el caché en RAM y bloquee el recompute de
        # scores en el primer ciclo posterior al arranque.
        _dc_start = (
            datetime.now(timezone.utc) - timedelta(days=int(lookback_days * 1.6) + 10)
        ).date()
        _dc_sliced = _disk.slice_from(symbol.upper(), _dc_start)
        if _dc_sliced is not None:
            _cache[key] = (now, _dc_sliced)
            return _dc_sliced
        raise MarketDataError(f"cache_only: sin dato cacheado para {symbol}")

    with _bars_locks.get(key):
        # Re-chequea el cache bajo el lock: mientras se esperaba para entrar
        # aca, otro thread puede haber completado el fetch para esta misma
        # clave (mismo symbol+lookback_days).
        now = time.time()
        cached = _cache.get(key)
        if not force and cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        failed = _bars_failure_cache.get(key)
        if not force and failed and now - failed[0] < _CACHE_TTL_SECONDS:
            raise MarketDataError(failed[1])

        end = datetime.now(timezone.utc)
        # *1.6 para convertir dias de trading aproximados a dias calendario (fines de
        # semana/feriados) mas un margen.
        start = end - timedelta(days=int(lookback_days * 1.6) + 10)

        _start = start.date()
        _end = end.date()

        # ── Caché en disco (Parquet por ticker) ───────────────────────────
        # Evita re-descargar historia ya guardada. Flujo:
        #   1. Si el caché tiene datos suficientes Y recientes → devolver directo.
        #   2. Si el caché existe pero está desactualizado → descargar solo el delta.
        #   3. Si le falta historia antigua (ej: servicio live lo llenó con 2 años
        #      y el backtest pide 22) → descargar backfill del tramo faltante.
        #   4. Después de actualizar el caché, servir desde disco.
        #   5. Si el disco sigue sin datos suficientes → fall-through al loop original.
        # force=True omite la lectura del disco pero igual actualiza al final.
        # Todo el bloque está envuelto en try/except: un error de I/O en disco
        # no debe romper el flujo principal (el loop de descarga sigue como fallback).
        if not force:
            try:
                cov = _disk.coverage(symbol.upper())
                if cov is not None:
                    first_disk, last_disk = cov
                    yesterday = _end - timedelta(days=1)
                    if last_disk < yesterday:
                        # Descargar solo el delta desde la última fecha cacheada
                        try:
                            delta_start = last_disk + timedelta(days=1)
                            _delta = _fetch_with_timeout(
                                lambda s=delta_start, e=_end: _tiingo_bars(symbol, s, e)
                            )
                            if _delta is not None and not _delta.empty:
                                _disk.upsert(symbol.upper(), _delta)
                        except Exception as exc:
                            # caché stale pero mejor que nada; sigue abajo
                            logger.warning("delta-update de %s falló: %s", symbol, exc)
                    if first_disk > _start:
                        # Descargar tramo histórico faltante (backfill).
                        # OJO: si esto falla, el caller recibe historia
                        # truncada sin enterarse — un PermissionError
                        # silencioso acá hizo que todos los backtests
                        # "de 22 años" simularan 2003-2024 con solo 4
                        # símbolos (los únicos con parquet completo).
                        # Por eso el warning es obligatorio, no opcional.
                        try:
                            bf_end = first_disk - timedelta(days=1)
                            _bf = _fetch_with_timeout(
                                lambda s=_start, e=bf_end: _tiingo_bars(symbol, s, e)
                            )
                            if _bf is not None and not _bf.empty:
                                _disk.upsert(symbol.upper(), _bf)
                        except Exception as exc:
                            logger.warning(
                                "backfill de %s (%s → %s) falló, se sigue con "
                                "historia truncada desde %s: %s",
                                symbol, _start, first_disk, first_disk, exc,
                            )
                    sliced = _disk.slice_from(symbol.upper(), _start)
                    if sliced is not None:
                        _cache[key] = (now, sliced)
                        _bars_failure_cache.pop(key, None)
                        return sliced
            except Exception:
                pass  # error inesperado en disco → fall-through al loop HTTP
        # ── Fin caché en disco ────────────────────────────────────────────

        df = None
        last_error: Exception | None = None
        for attempt in range(_MAX_FETCH_RETRIES):
            try:
                df = _fetch_with_timeout(lambda: _tiingo_bars(symbol, _start, _end))
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
            message = (
                f"Sin datos para {symbol} tras {_MAX_FETCH_RETRIES} intentos{detail}: "
                f"simbolo invalido o sin cobertura en Tiingo."
            )
            _bars_failure_cache[key] = (now, message)
            raise MarketDataError(message)

        _disk.upsert(symbol.upper(), df)  # guardar descarga completa en disco
        _cache[key] = (now, df)
        _bars_failure_cache.pop(key, None)
        return df


def is_bars_cached(symbol: str, lookback_days: int) -> bool:
    """True si get_daily_bars(symbol, lookback_days) devolveria el cache (o el
    fallo cacheado) sin pegarle a la red ahora mismo. Le permite a cada
    estrategia saltear la pausa entre simbolos (pensada para no rafagar la
    API gratuita) cuando el dato ya esta cacheado -- tipicamente porque otra
    estrategia ya escaneo este mismo simbolo en este ciclo, ya que las 4
    estrategias comparten lookback_days y por lo tanto la misma entrada de
    cache. Un simbolo con fallo cacheado (ver _bars_failure_cache) tampoco
    necesita la pausa: get_daily_bars va a relanzar la excepcion cacheada sin
    tocar la red."""
    key = (symbol.upper(), lookback_days)
    now = time.time()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return True
    failed = _bars_failure_cache.get(key)
    return failed is not None and now - failed[0] < _CACHE_TTL_SECONDS


def get_bars_failure_stats(symbols: list[str], lookback_days: int) -> tuple[int, int]:
    """Devuelve (fallidos, total) de `symbols` mirando el cache de fallos de
    get_daily_bars vigente en este momento (mismo TTL que el cache de exito,
    ver _bars_failure_cache), sin tocar la red.

    Usado por main.py (ver _check_market_data_degradation) para detectar una
    degradacion PARCIAL del feed de datos: a diferencia del caso "0 de N
    simbolos respondieron" (que MomentumScreener.scan y las demas estrategias
    ya rechazan de entrada con MarketDataError), un scan donde una porcion
    grande pero no total del universo fallo silenciosamente devuelve
    resultados "normales" (con menos simbolos evaluados) sin ninguna senal de
    que el feed esta degradado -- justo el caso que este helper hace visible.
    """
    now = time.time()
    failed = 0
    for symbol in symbols:
        key = (symbol.upper(), lookback_days)
        entry = _bars_failure_cache.get(key)
        if entry is not None and now - entry[0] < _CACHE_TTL_SECONDS:
            failed += 1
    return failed, len(symbols)


def is_fundamentals_cached(symbol: str) -> bool:
    """True si get_fundamentals(symbol) devolveria el cache sin tocar la red."""
    cached = _fundamentals_cache.get(symbol.upper())
    if not cached:
        return False
    ttl = _FUNDAMENTALS_CACHE_TTL_SECONDS if cached[2] else _FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS
    return time.time() - cached[0] < ttl


def _earnings_cache_ttl(ok: bool) -> float:
    return _EARNINGS_CACHE_TTL_SECONDS if ok else _EARNINGS_FAILURE_CACHE_TTL_SECONDS


def get_next_earnings_date(symbol: str, force: bool = False, cache_only: bool = False) -> "date | None":
    """Proxima fecha de earnings estimada para `symbol`, o None si no se pudo
    determinar (simbolo sin cobertura, limite de la API gratuita, etc.). El
    filtro de earnings del screener trata None como "sin dato, no bloquea" en
    vez de fallar el scan completo por un dato secundario y best-effort.

    `cache_only=True` nunca toca la red: devuelve el cache (fresco o stale) si
    existe, o None si no hay nada cacheado. Mismo valor neutro que el caller
    ya maneja para "sin dato"."""
    key = symbol.upper()
    now = time.time()
    cached = _earnings_cache.get(key)
    if not force and cached and now - cached[0] < _earnings_cache_ttl(cached[2]):
        return cached[1]
    if cache_only:
        return cached[1] if cached else None

    with _earnings_locks.get(key):
        now = time.time()
        cached = _earnings_cache.get(key)
        if not force and cached and now - cached[0] < _earnings_cache_ttl(cached[2]):
            return cached[1]

        ok = True
        try:
            # get_earnings_dates devuelve fechas pasadas y futuras mezcladas, sin
            # garantia de orden; con limit=1 se podia terminar tomando una fecha
            # YA PASADA y el blackout nunca se activaba (days_to_earnings quedaba
            # negativo). Se pide un lote mas grande y se filtra explicitamente por
            # la mas próxima que sea hoy o futura.
            dates = _fetch_with_timeout(lambda: yf.Ticker(symbol).get_earnings_dates(limit=12))
            next_date = None
            if dates is not None and len(dates):
                today = datetime.now(timezone.utc).date()
                future_dates = [ts.date() for ts in dates.index if ts.date() >= today]
                next_date = min(future_dates) if future_dates else None
        except Exception:
            next_date = None
            ok = False

        _earnings_cache[key] = (now, next_date, ok)
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


def get_fundamentals(symbol: str, force: bool = False, cache_only: bool = False) -> dict:
    """Datos fundamentales de `symbol` para las estrategias Largo plazo y
    Dividendos (ver app/strategies/). Devuelve un dict con las claves de
    _INFO_FIELD_MAP; un campo ausente en la respuesta de yfinance queda en
    None (dato no disponible, no es un error) en vez de levantar excepcion,
    ya que estas estrategias tratan cada campo faltante de forma puntual
    (algunos bloquean el filtro, otros son solo bonus de score).

    Igual que get_next_earnings_date: si yfinance falla o no devuelve nada
    util, se cachea un dict de Nones en vez de reintentar en cada llamada
    (la causa mas comun es falta de cobertura para ese simbolo, no un fallo
    transitorio, a diferencia de las barras de precio). Una excepcion si
    recibe un TTL corto en vez del de 24hs, ya que esa rama si es mas
    probablemente un fallo transitorio (rate limit, timeout) que falta de
    cobertura real.

    `cache_only=True` nunca toca la red: devuelve el cache (fresco o stale)
    si existe, o un dict de Nones (mismo formato que el caller ya maneja para
    "sin cobertura") si no hay nada cacheado.
    """
    key = symbol.upper()
    now = time.time()
    cached = _fundamentals_cache.get(key)
    if not force and cached:
        ttl = _FUNDAMENTALS_CACHE_TTL_SECONDS if cached[2] else _FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS
        if now - cached[0] < ttl:
            return cached[1]
    if cache_only:
        return cached[1] if cached else {name: None for name in _INFO_FIELD_MAP}

    with _fundamentals_locks.get(key):
        now = time.time()
        cached = _fundamentals_cache.get(key)
        if not force and cached:
            ttl = _FUNDAMENTALS_CACHE_TTL_SECONDS if cached[2] else _FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS
            if now - cached[0] < ttl:
                return cached[1]

        ok = True
        try:
            info = _fetch_with_timeout(lambda: yf.Ticker(symbol).get_info()) or {}
        except Exception:
            info = {}
            ok = False

        result = {name: info.get(raw_key) for name, raw_key in _INFO_FIELD_MAP.items()}
        _fundamentals_cache[key] = (now, result, ok)
        return result


def get_company_name(symbol: str) -> "str | None":
    """Nombre comercial de `symbol` (shortName de yfinance, ej. 'Apple Inc.'
    para AAPL), o None si no se pudo resolver. Usado solo para mostrarlo al
    lado del ticker en el frontend -- best-effort, nunca bloquea nada si
    yfinance no tiene el dato o falla."""
    key = symbol.upper()
    now = time.time()
    cached = _company_name_cache.get(key)
    if cached:
        ttl = _COMPANY_NAME_CACHE_TTL_SECONDS if cached[2] else _COMPANY_NAME_FAILURE_CACHE_TTL_SECONDS
        if now - cached[0] < ttl:
            return cached[1]

    with _company_name_locks.get(key):
        now = time.time()
        cached = _company_name_cache.get(key)
        if cached:
            ttl = _COMPANY_NAME_CACHE_TTL_SECONDS if cached[2] else _COMPANY_NAME_FAILURE_CACHE_TTL_SECONDS
            if now - cached[0] < ttl:
                return cached[1]

        ok = True
        try:
            info = _fetch_with_timeout(lambda: yf.Ticker(symbol).get_info()) or {}
            name = info.get("shortName") or info.get("longName") or None
        except Exception:
            name = None
            ok = False

        _company_name_cache[key] = (now, name, ok)
        return name


def evict_stale_cache() -> None:
    """Elimina entradas expiradas de todos los caches en memoria.

    Llamada periódicamente desde el background task de main.py para evitar
    que el proceso acumule RAM indefinidamente en universos grandes (>200
    símbolos × múltiples periodos = miles de DataFrames en _cache).
    """
    now = time.time()
    for key in list(_cache):
        ts, _ = _cache[key]
        if now - ts >= _CACHE_TTL_SECONDS:
            _cache.pop(key, None)
    for key in list(_bars_failure_cache):
        ts, _ = _bars_failure_cache[key]
        if now - ts >= _CACHE_TTL_SECONDS:
            _bars_failure_cache.pop(key, None)
    for key in list(_earnings_cache):
        ts, _, ok = _earnings_cache[key]
        ttl = _EARNINGS_CACHE_TTL_SECONDS if ok else _EARNINGS_FAILURE_CACHE_TTL_SECONDS
        if now - ts >= ttl:
            _earnings_cache.pop(key, None)
    for key in list(_fundamentals_cache):
        ts, _, ok = _fundamentals_cache[key]
        ttl = _FUNDAMENTALS_CACHE_TTL_SECONDS if ok else _FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS
        if now - ts >= ttl:
            _fundamentals_cache.pop(key, None)
    for key in list(_company_name_cache):
        ts, _, ok = _company_name_cache[key]
        ttl = _COMPANY_NAME_CACHE_TTL_SECONDS if ok else _COMPANY_NAME_FAILURE_CACHE_TTL_SECONDS
        if now - ts >= ttl:
            _company_name_cache.pop(key, None)

