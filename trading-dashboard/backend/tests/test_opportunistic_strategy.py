from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.market_data import MarketDataError
from app.screener_config import ScreenerConfig
from app.strategies import common as common_module
from app.strategies import opportunistic as opportunistic_module
from app.strategies.opportunistic import OpportunisticStrategy


def _dip_recovery_close(n3=60, drift3=0.0, amp3=6, period3=5, phase=0.5, decline_to=130.0):
    """Sube de 100 a 200 (forma el maximo de 52 semanas), cae hasta
    `decline_to` (el "espacio de crecimiento" que Oportunista exige) y
    termina con una fase de `n3` dias controlada por drift3/amp3/period3/
    phase para variar momentum/RSI recientes sin afectar el resto."""
    n1 = 150
    p1 = list(np.linspace(100, 200, n1))
    n2 = 80
    p2 = list(np.linspace(200, decline_to, n2))
    i3 = np.arange(n3)
    p3 = list(decline_to + drift3 * i3 + amp3 * np.sin(i3 / period3 + phase))
    return p1 + p2[1:] + p3[1:]


def _bars(close_values, volume=5_000_000, spread_pct=0.02):
    idx = pd.date_range("2024-01-01", periods=len(close_values), freq="D")
    close = pd.Series(close_values, index=idx)
    spread = close * spread_pct
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + spread,
            "Low": close - spread,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


# GROW: momentum reciente positivo, RSI en zona de recuperacion (35-60),
# volatilidad (ATR/precio) por encima del minimo, y bien por debajo (>10%)
# del maximo de 52 semanas: pasa todos los filtros de Oportunista.
GROW_CLOSE = _dip_recovery_close()
# OVERBOUGHT: misma forma pero la recuperacion reciente es mas empinada ->
# RSI sale del rango de recuperacion (>60).
OVERBOUGHT_CLOSE = _dip_recovery_close(drift3=0.3, amp3=8, period3=4, phase=0.0)
# NEARHIGH: la caida previa es chica (solo a 190, ~5% del maximo) -> no hay
# suficiente espacio de crecimiento.
NEARHIGH_CLOSE = _dip_recovery_close(decline_to=190.0)
# DOWN: la fase final sigue cayendo -> momentum reciente no es positivo.
DOWN_CLOSE = _dip_recovery_close(drift3=-0.3, amp3=2, period3=8, phase=0.0)

FAKE_BARS = {
    "GROW": _bars(GROW_CLOSE, spread_pct=0.02),
    "OVERBOUGHT": _bars(OVERBOUGHT_CLOSE, spread_pct=0.02),
    "FLATVOL": _bars(GROW_CLOSE, spread_pct=0.002),  # mismo precio que GROW, sin volatilidad
    "NEARHIGH": _bars(NEARHIGH_CLOSE, spread_pct=0.02),
    "DOWN": _bars(DOWN_CLOSE, spread_pct=0.02),
    "ILLIQUID": _bars(GROW_CLOSE, spread_pct=0.02, volume=1_000),  # mismo precio que GROW, sin liquidez
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days, force=False, cache_only=False):
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    def fake_get_next_earnings_date(symbol, force=False, cache_only=False):
        return None

    monkeypatch.setattr(opportunistic_module, "get_daily_bars", fake_get_daily_bars)
    monkeypatch.setattr(common_module, "get_next_earnings_date", fake_get_next_earnings_date)
    return fake_get_daily_bars


@pytest.fixture
def strategy(patched_market_data) -> OpportunisticStrategy:
    config = ScreenerConfig(universe=list(FAKE_BARS.keys()))
    return OpportunisticStrategy(config)


def test_grow_symbol_passes_all_filters(strategy):
    result = strategy.evaluate_symbol("GROW")
    assert result.passes_filters
    assert result.notes == []
    assert result.strategy_id == "opportunistic"


def test_overbought_symbol_fails_rsi_filter(strategy):
    result = strategy.evaluate_symbol("OVERBOUGHT")
    assert not result.passes_filters
    assert any("rsi" in note.lower() for note in result.notes)


def test_low_volatility_symbol_fails_volatility_filter(strategy):
    result = strategy.evaluate_symbol("FLATVOL")
    assert not result.passes_filters
    assert any("volatilidad" in note.lower() for note in result.notes)


def test_near_high_symbol_fails_room_to_grow_filter(strategy):
    result = strategy.evaluate_symbol("NEARHIGH")
    assert not result.passes_filters
    assert any("espacio de crecimiento" in note.lower() for note in result.notes)


def test_declining_symbol_fails_momentum_filter(strategy):
    result = strategy.evaluate_symbol("DOWN")
    assert not result.passes_filters
    assert any("retorno reciente" in note.lower() for note in result.notes)


def test_illiquid_symbol_fails_liquidity_filter_despite_same_price(strategy):
    result = strategy.evaluate_symbol("ILLIQUID")
    assert not result.passes_filters
    assert any("volumen" in note.lower() for note in result.notes)


def test_evaluate_symbol_exposes_score_components(strategy):
    result = strategy.evaluate_symbol("GROW")
    assert set(result.score_components.keys()) == {
        "momentum", "volatility", "rsi_recovery", "room_to_grow",
        "macd_turn", "sector_relative_strength",
    }


def test_results_sorted_descending_by_score(strategy):
    results = strategy.scan()
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_stop_loss_is_below_last_price(strategy):
    results = strategy.scan()
    for r in results:
        assert r.suggested_stop_loss_price < r.last_price
        assert r.suggested_stop_loss_pct > 0


def test_unknown_symbol_is_skipped(patched_market_data):
    config = ScreenerConfig(universe=["GROW", "GHOST"])
    s = OpportunisticStrategy(config)
    results = s.scan()
    assert [r.symbol for r in results] == ["GROW"]


def test_scan_raises_when_no_symbol_has_data(monkeypatch):
    def always_fails(symbol, lookback_days, force=False, cache_only=False):
        raise MarketDataError("sin red en este entorno")

    monkeypatch.setattr(opportunistic_module, "get_daily_bars", always_fails)
    config = ScreenerConfig(universe=["GROW"])
    s = OpportunisticStrategy(config)
    with pytest.raises(MarketDataError):
        s.scan()


def test_earnings_blackout_blocks_symbol_when_earnings_within_window(monkeypatch, patched_market_data):
    soon = date.today() + timedelta(days=2)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False, cache_only=False: soon)
    config = ScreenerConfig(universe=["GROW"], earnings_blackout_days=5)
    s = OpportunisticStrategy(config)
    result = s.evaluate_symbol("GROW")
    assert not result.passes_filters
    assert any("earnings" in note.lower() for note in result.notes)


def test_earnings_blackout_does_not_block_when_earnings_outside_window(monkeypatch, patched_market_data):
    far = date.today() + timedelta(days=30)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False, cache_only=False: far)
    config = ScreenerConfig(universe=["GROW"], earnings_blackout_days=5)
    s = OpportunisticStrategy(config)
    result = s.evaluate_symbol("GROW")
    assert result.passes_filters


def test_earnings_lookup_is_skipped_for_symbols_that_already_fail_other_filters(monkeypatch, patched_market_data):
    calls: list[str] = []

    def tracking_get_next_earnings_date(symbol, force=False, cache_only=False):
        calls.append(symbol)
        return None

    monkeypatch.setattr(common_module, "get_next_earnings_date", tracking_get_next_earnings_date)
    config = ScreenerConfig(universe=["GROW", "DOWN", "NEARHIGH"])
    s = OpportunisticStrategy(config)
    s.scan()
    assert calls == ["GROW"]


def test_evaluate_symbol_attaches_sector(strategy, monkeypatch):
    monkeypatch.setattr(opportunistic_module, "get_sector", lambda symbol: "Information Technology")
    result = strategy.evaluate_symbol("GROW")
    assert result.sector == "Information Technology"


def _bearish_benchmark_bars(n=300):
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    close = pd.Series([200.0 - 0.3 * i for i in range(n)], index=idx)
    return pd.DataFrame({"Close": close})


def test_benchmark_regime_ok_ignores_benchmark_when_filter_disabled(monkeypatch):
    # opportunistic_regime_filter_enabled=False (default desde el re-test del
    # 2026-06-24): a diferencia de Momentum, Oportunista no consulta el
    # regimen del benchmark en absoluto sin importar que tan bajista este.
    bench_bars = _bearish_benchmark_bars()

    def fake_get_daily_bars(symbol, lookback_days, force=False, cache_only=False):
        if symbol == "SPY":
            return bench_bars
        raise MarketDataError("sin datos sinteticos")

    monkeypatch.setattr(opportunistic_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=[], benchmark_symbol="SPY")
    strategy = OpportunisticStrategy(config)
    assert strategy._benchmark_regime_ok() is True


def test_benchmark_regime_ok_false_when_filter_enabled_and_benchmark_bearish(monkeypatch):
    bench_bars = _bearish_benchmark_bars()

    def fake_get_daily_bars(symbol, lookback_days, force=False, cache_only=False):
        if symbol == "SPY":
            return bench_bars
        raise MarketDataError("sin datos sinteticos")

    monkeypatch.setattr(opportunistic_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(
        universe=[], benchmark_symbol="SPY",
        opportunistic_regime_filter_enabled=True, regime_sma_period=50,
    )
    strategy = OpportunisticStrategy(config)
    assert strategy._benchmark_regime_ok() is False


def test_benchmark_regime_ok_is_independent_from_momentum_regime_flag(monkeypatch):
    # regime_filter_enabled (Momentum) en su default True no deberia afectar
    # a Oportunista: solo opportunistic_regime_filter_enabled lo hace.
    bench_bars = _bearish_benchmark_bars()

    def fake_get_daily_bars(symbol, lookback_days, force=False, cache_only=False):
        if symbol == "SPY":
            return bench_bars
        raise MarketDataError("sin datos sinteticos")

    monkeypatch.setattr(opportunistic_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=[], benchmark_symbol="SPY", regime_filter_enabled=True, regime_sma_period=50)
    strategy = OpportunisticStrategy(config)
    assert strategy._benchmark_regime_ok() is True
