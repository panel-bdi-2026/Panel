import time

import pandas as pd
import pytest

from app import market_data as market_data_module
from app.market_data import (
    MarketDataError,
    get_bars_failure_stats,
    get_daily_bars,
    get_fundamentals,
    get_next_earnings_date,
    is_bars_cached,
)


def _bars(n=5):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = pd.Series([100.0 + i for i in range(n)], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1_000_000},
        index=idx,
    )


class _FakeTiingo:
    """Simula _tiingo_bars con respuestas predefinidas, en orden."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.call_count = 0

    def __call__(self, symbol, start, end):
        self.call_count += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture(autouse=True)
def _clear_cache():
    market_data_module._cache.clear()
    market_data_module._bars_failure_cache.clear()
    market_data_module._earnings_cache.clear()
    market_data_module._fundamentals_cache.clear()
    yield
    market_data_module._cache.clear()
    market_data_module._bars_failure_cache.clear()
    market_data_module._earnings_cache.clear()
    market_data_module._fundamentals_cache.clear()


def test_get_daily_bars_returns_data_on_first_success(monkeypatch):
    bars = _bars()
    fake = _FakeTiingo([bars])
    sleeps = []
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("AAPL", 100)
    assert len(df) == len(bars)
    assert fake.call_count == 1
    assert sleeps == []  # exito al primer intento: no hay backoff


def test_get_daily_bars_retries_on_exception_then_succeeds(monkeypatch):
    bars = _bars()
    fake = _FakeTiingo([RuntimeError("network blip"), bars])
    sleeps = []
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("MSFT", 100)
    assert len(df) == len(bars)
    assert fake.call_count == 2
    assert sleeps == [0.5]  # backoff antes del segundo intento


def test_get_daily_bars_retries_on_empty_dataframe_then_succeeds(monkeypatch):
    bars = _bars()
    fake = _FakeTiingo([pd.DataFrame(), bars])
    sleeps = []
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("TSLA", 100)
    assert len(df) == len(bars)
    assert sleeps == [0.5]


def test_get_daily_bars_raises_after_exhausting_retries(monkeypatch):
    fake = _FakeTiingo([RuntimeError("fail1"), RuntimeError("fail2"), RuntimeError("fail3")])
    sleeps = []
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(MarketDataError):
        get_daily_bars("BADSTOCK", 100)
    # backoff exponencial entre los 3 intentos, ninguno despues del ultimo
    assert sleeps == [0.5, 1.0]


def test_get_daily_bars_raises_when_always_empty_without_exception(monkeypatch):
    fake = _FakeTiingo([pd.DataFrame(), pd.DataFrame(), pd.DataFrame()])
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: None)

    with pytest.raises(MarketDataError):
        get_daily_bars("DELISTED", 100)


def test_get_daily_bars_uses_cache_on_second_call(monkeypatch):
    bars = _bars()
    fake = _FakeTiingo([bars])
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)

    get_daily_bars("NFLX", 100)
    get_daily_bars("NFLX", 100)
    assert fake.call_count == 1


def test_get_daily_bars_force_bypasses_cache(monkeypatch):
    bars = _bars()
    fake = _FakeTiingo([bars, bars])
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)

    get_daily_bars("NFLX", 100)
    get_daily_bars("NFLX", 100, force=True)
    assert fake.call_count == 2


def test_is_bars_cached_false_before_first_fetch():
    assert is_bars_cached("AAPL", 100) is False


def test_is_bars_cached_true_right_after_fetch(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module, "_tiingo_bars", lambda s, st, en: bars)

    get_daily_bars("AAPL", 100)
    assert is_bars_cached("AAPL", 100) is True


def test_is_bars_cached_keyed_by_symbol_and_lookback(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module, "_tiingo_bars", lambda s, st, en: bars)

    get_daily_bars("AAPL", 100)
    assert is_bars_cached("AAPL", 200) is False  # mismo simbolo, otra ventana
    assert is_bars_cached("MSFT", 100) is False  # otro simbolo, misma ventana


def test_is_bars_cached_false_after_ttl_expires(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module, "_tiingo_bars", lambda s, st, en: bars)

    get_daily_bars("AAPL", 100)
    timestamp, df = market_data_module._cache[("AAPL", 100)]
    expired = timestamp - market_data_module._CACHE_TTL_SECONDS - 1
    market_data_module._cache[("AAPL", 100)] = (expired, df)
    assert is_bars_cached("AAPL", 100) is False


def test_get_bars_failure_stats_no_failures():
    bars = _bars()
    market_data_module._cache[("AAPL", 100)] = (time.time(), bars)
    market_data_module._cache[("MSFT", 100)] = (time.time(), bars)
    failed, total = get_bars_failure_stats(["AAPL", "MSFT"], 100)
    assert (failed, total) == (0, 2)


def test_get_bars_failure_stats_counts_live_failures(monkeypatch):
    fake = _FakeTiingo([RuntimeError("fail1"), RuntimeError("fail2"), RuntimeError("fail3")])
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: None)
    with pytest.raises(MarketDataError):
        get_daily_bars("BADSTOCK", 100)

    failed, total = get_bars_failure_stats(["BADSTOCK", "AAPL"], 100)
    assert (failed, total) == (1, 2)


def test_get_bars_failure_stats_ignores_expired_failure_entries(monkeypatch):
    fake = _FakeTiingo([RuntimeError("fail1"), RuntimeError("fail2"), RuntimeError("fail3")])
    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: None)
    with pytest.raises(MarketDataError):
        get_daily_bars("BADSTOCK", 100)

    ts, msg = market_data_module._bars_failure_cache[("BADSTOCK", 100)]
    expired = ts - market_data_module._CACHE_TTL_SECONDS - 1
    market_data_module._bars_failure_cache[("BADSTOCK", 100)] = (expired, msg)

    failed, total = get_bars_failure_stats(["BADSTOCK"], 100)
    assert (failed, total) == (0, 1)


def test_get_bars_failure_stats_empty_symbol_list():
    assert get_bars_failure_stats([], 100) == (0, 0)


# ---------------------------------------------------------------------------
# _fetch_with_timeout: limite de pared ante una llamada de yfinance que se
# cuelga (no lanza excepcion ni vuelve nunca) en vez de fallar rapido.
# ---------------------------------------------------------------------------

def test_fetch_with_timeout_raises_market_data_error_on_hang(monkeypatch):
    monkeypatch.setattr(market_data_module, "_FETCH_TIMEOUT_SECONDS", 0.05)

    def hangs():
        time.sleep(0.3)
        return "nunca deberia observarse"

    with pytest.raises(MarketDataError):
        market_data_module._fetch_with_timeout(hangs)


def test_get_daily_bars_treats_hang_as_failure_and_retries(monkeypatch):
    """Una llamada colgada en el primer intento no debe trabar el thread para
    siempre: el watchdog la corta y el retry normal de get_daily_bars sigue
    con el segundo intento como si hubiera sido una excepcion cualquiera."""
    monkeypatch.setattr(market_data_module, "_FETCH_TIMEOUT_SECONDS", 0.05)
    bars = _bars()
    call_count = {"n": 0}

    def fake_tiingo(symbol, start, end):
        call_count["n"] += 1
        if call_count["n"] == 1:
            time.sleep(0.3)
        return bars

    monkeypatch.setattr(market_data_module, "_tiingo_bars", fake_tiingo)

    df = get_daily_bars("HUNG", 100)
    assert len(df) == len(bars)
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# get_next_earnings_date / get_fundamentals: TTL corto para fallos
# transitorios vs TTL largo (24hs) para "sin dato" genuino.
# ---------------------------------------------------------------------------

class _FakeEarningsTicker:
    def __init__(self, symbol, dates=None, error=None):
        self._dates = dates
        self._error = error

    def get_earnings_dates(self, limit=12):
        if self._error is not None:
            raise self._error
        return self._dates


class _FakeInfoTicker:
    def __init__(self, symbol, info=None, error=None):
        self._info = info
        self._error = error

    def get_info(self):
        if self._error is not None:
            raise self._error
        return self._info


def test_get_next_earnings_date_caches_genuine_none_for_full_ttl(monkeypatch):
    """Sin earnings futuros (pero el fetch funciono): el None es un dato
    real, amerita el cache largo de 24hs."""
    call_count = {"n": 0}

    def fake_ticker(symbol):
        call_count["n"] += 1
        return _FakeEarningsTicker(symbol, dates=None)

    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)

    assert get_next_earnings_date("AAPL") is None
    cached_at, value, ok = market_data_module._earnings_cache["AAPL"]
    assert ok is True

    # Paso el TTL corto de falla, pero todavia dentro del TTL largo de exito:
    # no debe reintentar porque el resultado anterior fue exitoso (ok=True).
    market_data_module._earnings_cache["AAPL"] = (
        cached_at - market_data_module._EARNINGS_FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )
    get_next_earnings_date("AAPL")
    assert call_count["n"] == 1


def test_get_next_earnings_date_retries_after_short_ttl_on_failure(monkeypatch):
    """Una excepcion transitoria (rate limit, timeout) no debe quedar
    cacheada por las 24hs completas: pasado el TTL corto de falla, una nueva
    llamada debe reintentar en vez de devolver el None cacheado indefinido."""
    monkeypatch.setattr(
        market_data_module.yf, "Ticker",
        lambda symbol: _FakeEarningsTicker(symbol, error=RuntimeError("rate limited")),
    )

    assert get_next_earnings_date("AAPL") is None
    cached_at, value, ok = market_data_module._earnings_cache["AAPL"]
    assert ok is False

    market_data_module._earnings_cache["AAPL"] = (
        cached_at - market_data_module._EARNINGS_FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )

    call_count = {"n": 0}

    def recovered_ticker(symbol):
        call_count["n"] += 1
        return _FakeEarningsTicker(symbol, dates=None)

    monkeypatch.setattr(market_data_module.yf, "Ticker", recovered_ticker)
    get_next_earnings_date("AAPL")
    assert call_count["n"] == 1


def test_get_fundamentals_caches_genuine_empty_info_for_full_ttl(monkeypatch):
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeInfoTicker(symbol, info={}))

    get_fundamentals("AAPL")
    cached_at, value, ok = market_data_module._fundamentals_cache["AAPL"]
    assert ok is True

    market_data_module._fundamentals_cache["AAPL"] = (
        cached_at - market_data_module._FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )

    call_count = {"n": 0}

    def fake_ticker(symbol):
        call_count["n"] += 1
        return _FakeInfoTicker(symbol, info={})

    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)
    get_fundamentals("AAPL")
    assert call_count["n"] == 0


def test_get_fundamentals_retries_after_short_ttl_on_failure(monkeypatch):
    monkeypatch.setattr(
        market_data_module.yf, "Ticker",
        lambda symbol: _FakeInfoTicker(symbol, error=RuntimeError("rate limited")),
    )

    get_fundamentals("AAPL")
    cached_at, value, ok = market_data_module._fundamentals_cache["AAPL"]
    assert ok is False

    market_data_module._fundamentals_cache["AAPL"] = (
        cached_at - market_data_module._FUNDAMENTALS_FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )

    call_count = {"n": 0}

    def recovered_ticker(symbol):
        call_count["n"] += 1
        return _FakeInfoTicker(symbol, info={"trailingPE": 20.0})

    monkeypatch.setattr(market_data_module.yf, "Ticker", recovered_ticker)
    result = get_fundamentals("AAPL")
    assert call_count["n"] == 1
    assert result["trailing_pe"] == 20.0
