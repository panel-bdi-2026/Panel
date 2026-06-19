import pandas as pd
import pytest

from app import market_data as market_data_module
from app.market_data import MarketDataError, get_daily_bars, is_bars_cached


def _bars(n=5):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = pd.Series([100.0 + i for i in range(n)], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1_000_000},
        index=idx,
    )


class _FakeTicker:
    def __init__(self, symbol, responses):
        self._responses = responses

    def history(self, **kwargs):
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture(autouse=True)
def _clear_cache():
    market_data_module._cache.clear()
    yield
    market_data_module._cache.clear()


def test_get_daily_bars_returns_data_on_first_success(monkeypatch):
    bars = _bars()
    calls = []

    def fake_ticker(symbol):
        calls.append(symbol)
        return _FakeTicker(symbol, [bars])

    sleeps = []
    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("AAPL", 100)
    assert len(df) == len(bars)
    assert calls == ["AAPL"]
    assert sleeps == []  # exito al primer intento: no hay backoff


def test_get_daily_bars_retries_on_exception_then_succeeds(monkeypatch):
    bars = _bars()
    responses = [RuntimeError("network blip"), bars]
    call_count = {"n": 0}

    def fake_ticker(symbol):
        call_count["n"] += 1
        return _FakeTicker(symbol, responses)

    sleeps = []
    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("MSFT", 100)
    assert len(df) == len(bars)
    assert call_count["n"] == 2
    assert sleeps == [0.5]  # backoff antes del segundo intento


def test_get_daily_bars_retries_on_empty_dataframe_then_succeeds(monkeypatch):
    bars = _bars()
    responses = [pd.DataFrame(), bars]

    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, responses))
    sleeps = []
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    df = get_daily_bars("TSLA", 100)
    assert len(df) == len(bars)
    assert sleeps == [0.5]


def test_get_daily_bars_raises_after_exhausting_retries(monkeypatch):
    responses = [RuntimeError("fail1"), RuntimeError("fail2"), RuntimeError("fail3")]
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, responses))
    sleeps = []
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(MarketDataError):
        get_daily_bars("BADSTOCK", 100)
    # backoff exponencial entre los 3 intentos, ninguno despues del ultimo
    assert sleeps == [0.5, 1.0]


def test_get_daily_bars_raises_when_always_empty_without_exception(monkeypatch):
    responses = [pd.DataFrame(), pd.DataFrame(), pd.DataFrame()]
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, responses))
    monkeypatch.setattr(market_data_module.time, "sleep", lambda s: None)

    with pytest.raises(MarketDataError):
        get_daily_bars("DELISTED", 100)


def test_get_daily_bars_uses_cache_on_second_call(monkeypatch):
    bars = _bars()
    call_count = {"n": 0}

    def fake_ticker(symbol):
        call_count["n"] += 1
        return _FakeTicker(symbol, [bars])

    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)

    get_daily_bars("NFLX", 100)
    get_daily_bars("NFLX", 100)
    assert call_count["n"] == 1


def test_get_daily_bars_force_bypasses_cache(monkeypatch):
    bars = _bars()
    call_count = {"n": 0}

    def fake_ticker(symbol):
        call_count["n"] += 1
        return _FakeTicker(symbol, [bars])

    monkeypatch.setattr(market_data_module.yf, "Ticker", fake_ticker)

    get_daily_bars("NFLX", 100)
    get_daily_bars("NFLX", 100, force=True)
    assert call_count["n"] == 2


def test_is_bars_cached_false_before_first_fetch():
    assert is_bars_cached("AAPL", 100) is False


def test_is_bars_cached_true_right_after_fetch(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, [bars]))

    get_daily_bars("AAPL", 100)
    assert is_bars_cached("AAPL", 100) is True


def test_is_bars_cached_keyed_by_symbol_and_lookback(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, [bars]))

    get_daily_bars("AAPL", 100)
    assert is_bars_cached("AAPL", 200) is False  # mismo simbolo, otra ventana
    assert is_bars_cached("MSFT", 100) is False  # otro simbolo, misma ventana


def test_is_bars_cached_false_after_ttl_expires(monkeypatch):
    bars = _bars()
    monkeypatch.setattr(market_data_module.yf, "Ticker", lambda symbol: _FakeTicker(symbol, [bars]))

    get_daily_bars("AAPL", 100)
    timestamp, df = market_data_module._cache[("AAPL", 100)]
    expired = timestamp - market_data_module._CACHE_TTL_SECONDS - 1
    market_data_module._cache[("AAPL", 100)] = (expired, df)
    assert is_bars_cached("AAPL", 100) is False
