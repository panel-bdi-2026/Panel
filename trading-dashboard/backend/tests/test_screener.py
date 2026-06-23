import numpy as np
import pandas as pd
import pytest

from app import screener as screener_module
from app.market_data import MarketDataError
from app.screener import MomentumScreener
from app.screener_config import ScreenerConfig


def _series(n, drift, amplitude, period, base=100.0):
    i = np.arange(n)
    return list(base + drift * i + amplitude * np.sin(i / period))


def _bars(close_values, volume=5_000_000):
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="D")
    close = pd.Series(close_values, index=idx)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


# Series sinteticas (265 dias, sin red: mas de 252 para que pct_from_high
# tenga la ventana completa de un "maximo de 52 semanas" real, ver
# indicators.py). MOM sube con fuerza relativa clara vs el benchmark; WEAK
# avanza apenas por encima del benchmark; BROKEN esta en clara tendencia
# bajista; ILLIQUID tiene el mismo precio que MOM pero volumen en dolares
# (precio x volumen) por debajo del minimo de liquidez configurado.
MOM_CLOSE = _series(265, 0.25, 3, 4)
WEAK_CLOSE = _series(265, 0.02, 1, 3)
BROKEN_CLOSE = _series(265, -0.25, -2, 4, base=160.0)
BENCH_CLOSE = _series(265, 0.05, 1, 6)

FAKE_BARS = {
    "MOM": _bars(MOM_CLOSE),
    "WEAK": _bars(WEAK_CLOSE),
    "BROKEN": _bars(BROKEN_CLOSE),
    "ILLIQUID": _bars(MOM_CLOSE, volume=1_000),
    "SPY": _bars(BENCH_CLOSE),
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days, force=False):
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    def fake_get_next_earnings_date(symbol, force=False):
        return None

    monkeypatch.setattr(screener_module, "get_daily_bars", fake_get_daily_bars)
    monkeypatch.setattr(screener_module, "get_next_earnings_date", fake_get_next_earnings_date)
    return fake_get_daily_bars


@pytest.fixture
def screener(patched_market_data) -> MomentumScreener:
    config = ScreenerConfig(universe=["MOM", "WEAK", "BROKEN", "ILLIQUID"], benchmark_symbol="SPY")
    return MomentumScreener(config)


def test_strong_momentum_symbol_passes_filters_and_scores_highest(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["MOM"].trend_ok
    assert by_symbol["MOM"].passes_filters
    assert results[0].symbol == "MOM"


def test_broken_trend_symbol_fails_filters(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert not by_symbol["BROKEN"].trend_ok
    assert not by_symbol["BROKEN"].passes_filters


def test_illiquid_symbol_fails_liquidity_filter_despite_same_price(screener):
    results = screener.scan()
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["ILLIQUID"].trend_ok  # mismo precio que MOM
    assert not by_symbol["ILLIQUID"].passes_filters
    assert any("volumen" in note.lower() for note in by_symbol["ILLIQUID"].notes)


def test_results_sorted_descending_by_score(screener):
    results = screener.scan()
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_unknown_symbol_is_skipped(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "GHOST"], benchmark_symbol="SPY")
    s = MomentumScreener(config)
    results = s.scan()
    assert [r.symbol for r in results] == ["MOM"]


def test_scan_skips_delay_for_already_cached_symbol(monkeypatch, patched_market_data):
    # MOM (i=0) nunca duerme por ser el primero del loop, sin importar cache.
    # WEAK e ILLIQUID no estan "cacheados" (segun el fake): deben dormir.
    # BROKEN si esta "cacheado" (ej. otra estrategia ya lo escaneo en este
    # mismo ciclo, ver is_bars_cached): no debe dormir.
    sleeps = []
    monkeypatch.setattr(screener_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(screener_module, "is_bars_cached", lambda symbol, lookback_days: symbol == "BROKEN")
    config = ScreenerConfig(
        universe=["MOM", "WEAK", "BROKEN", "ILLIQUID"], benchmark_symbol="SPY", scan_request_delay_seconds=0.01,
    )
    s = MomentumScreener(config)
    s.scan()
    assert sleeps == [0.01, 0.01]


def test_scan_does_not_skip_delay_when_force_even_if_cached(monkeypatch, patched_market_data):
    # force=True siempre pega a la red (bypassa el cache en get_daily_bars),
    # asi que el delay debe aplicarse igual aunque is_bars_cached diga que
    # esta cacheado.
    sleeps = []
    monkeypatch.setattr(screener_module.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(screener_module, "is_bars_cached", lambda symbol, lookback_days: True)
    config = ScreenerConfig(
        universe=["MOM", "WEAK", "BROKEN"], benchmark_symbol="SPY", scan_request_delay_seconds=0.01,
    )
    s = MomentumScreener(config)
    s.scan(force=True)
    assert sleeps == [0.01, 0.01]


def test_stop_loss_is_below_last_price(screener):
    results = screener.scan()
    for r in results:
        assert r.suggested_stop_loss_price < r.last_price
        assert r.suggested_stop_loss_pct > 0


def test_scan_raises_when_no_symbol_has_data(monkeypatch):
    def always_fails(symbol, lookback_days, force=False):
        raise MarketDataError("sin red en este entorno")

    monkeypatch.setattr(screener_module, "get_daily_bars", always_fails)
    config = ScreenerConfig(universe=["MOM", "WEAK"], benchmark_symbol="SPY")
    s = MomentumScreener(config)
    with pytest.raises(MarketDataError):
        s.scan()


def test_regime_filter_blocks_symbol_when_benchmark_below_regime_sma(screener):
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=5.0, regime_ok=False)
    assert result.trend_ok
    assert not result.passes_filters
    assert any("regimen" in note.lower() for note in result.notes)


def test_regime_filter_does_not_block_when_benchmark_above_regime_sma(screener):
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=5.0, regime_ok=True)
    assert result.passes_filters


def test_evaluate_symbol_exposes_score_components(screener):
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=5.0, regime_ok=True)
    assert set(result.score_components.keys()) == {
        "relative_strength", "momentum_12_1", "trend", "rsi",
        "macd", "bollinger", "sector_relative_strength",
    }


def test_earnings_blackout_blocks_symbol_when_earnings_within_window(monkeypatch, patched_market_data):
    from datetime import date, timedelta

    soon = date.today() + timedelta(days=2)
    monkeypatch.setattr(screener_module, "get_next_earnings_date", lambda symbol, force=False: soon)
    config = ScreenerConfig(universe=["MOM"], benchmark_symbol="SPY", earnings_blackout_days=5)
    s = MomentumScreener(config)
    results = s.scan()
    by_symbol = {r.symbol: r for r in results}
    assert not by_symbol["MOM"].passes_filters
    assert any("earnings" in note.lower() for note in by_symbol["MOM"].notes)


def test_earnings_blackout_does_not_block_when_earnings_outside_window(monkeypatch, patched_market_data):
    from datetime import date, timedelta

    far = date.today() + timedelta(days=30)
    monkeypatch.setattr(screener_module, "get_next_earnings_date", lambda symbol, force=False: far)
    config = ScreenerConfig(universe=["MOM"], benchmark_symbol="SPY", earnings_blackout_days=5)
    s = MomentumScreener(config)
    results = s.scan()
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["MOM"].passes_filters


def test_earnings_blackout_does_not_block_when_no_earnings_date_known(screener):
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=5.0, regime_ok=True)
    assert result.passes_filters


def test_earnings_lookup_is_skipped_for_symbols_that_already_fail_other_filters(monkeypatch, patched_market_data):
    """get_next_earnings_date es un pedido de red aparte (mas lento que las
    barras, que ya estan cacheadas): solo vale la pena pagarlo si el simbolo
    ya pasaria el resto de los filtros. Sin este chequeo, un universo grande
    (ej. S&P 500 completo) dispara un pedido de earnings por cada simbolo, sin
    importar cuantos vayan a descartarse de todas formas por tendencia/RSI/
    liquidez/regimen."""
    calls: list[str] = []

    def tracking_get_next_earnings_date(symbol, force=False):
        calls.append(symbol)
        return None

    monkeypatch.setattr(screener_module, "get_next_earnings_date", tracking_get_next_earnings_date)
    config = ScreenerConfig(universe=["MOM", "BROKEN", "ILLIQUID"], benchmark_symbol="SPY")
    s = MomentumScreener(config)
    s.scan()
    assert calls == ["MOM"]


def test_near_high_filter_blocks_symbol_far_from_52w_high(screener):
    # MOM cotiza ~1.0% por debajo de su maximo de 52s; con un umbral muy
    # estricto (0.5%) queda fuera por proximidad, aunque pase el resto.
    screener.reload(ScreenerConfig(
        universe=["MOM"], benchmark_symbol="SPY",
        near_high_filter_enabled=True, max_pct_below_52w_high=0.5,
    ))
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=0.0, regime_ok=True)
    assert result.trend_ok
    assert not result.passes_filters
    assert any("52 semanas" in note for note in result.notes)


def test_near_high_filter_does_not_block_when_within_threshold(screener):
    # Con el umbral por defecto (15%), MOM a ~1.0% del maximo pasa el filtro.
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=0.0, regime_ok=True)
    assert result.passes_filters


def test_near_high_filter_returns_none_with_insufficient_history(monkeypatch):
    # Con menos de 252 dias de historia no hay un maximo de 52 semanas real
    # para comparar: pct_from_52w_high debe quedar en None y el filtro de
    # proximidad no debe bloquear por esto (ver indicators.pct_from_high).
    short_bars = FAKE_BARS["MOM"].iloc[:100]

    def fake_get_daily_bars(symbol, lookback_days, force=False):
        if symbol != "MOM":
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return short_bars

    monkeypatch.setattr(screener_module, "get_daily_bars", fake_get_daily_bars)
    monkeypatch.setattr(screener_module, "get_next_earnings_date", lambda symbol, force=False: None)

    config = ScreenerConfig(
        universe=["MOM"], benchmark_symbol="SPY",
        momentum_12_1_lookback_days=20, momentum_12_1_skip_days=5,
    )
    s = MomentumScreener(config)
    result = s.evaluate_symbol("MOM", benchmark_roc_3m=0.0, regime_ok=True)

    assert result is not None
    assert result.pct_from_52w_high is None
    assert not any("52 semanas" in note for note in result.notes)


def test_near_high_filter_disabled_does_not_block(screener):
    screener.reload(ScreenerConfig(
        universe=["MOM"], benchmark_symbol="SPY",
        near_high_filter_enabled=False, max_pct_below_52w_high=0.5,
    ))
    result = screener.evaluate_symbol("MOM", benchmark_roc_3m=0.0, regime_ok=True)
    assert result.passes_filters
