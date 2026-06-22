import numpy as np
import pandas as pd
import pytest

from app.market_data import MarketDataError
from app.screener_config import ScreenerConfig
from app.strategies import common as common_module
from app.strategies import dividend as dividend_module
from app.strategies.dividend import DividendStrategy, _dividend_yield_pct


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


CLOSE = _series(265, 0.05, 2, 4)
BARS = _bars(CLOSE)
ILLIQUID_BARS = _bars(CLOSE, volume=10)

FUNDAMENTALS = {
    "GOODDIV": dict(dividend_yield=0.04, payout_ratio=0.5, return_on_equity=0.15, profit_margins=0.1),
    "LOWYIELD": dict(dividend_yield=0.01, payout_ratio=0.5, return_on_equity=0.15, profit_margins=0.1),
    "HIGHPAYOUT": dict(dividend_yield=0.04, payout_ratio=0.95, return_on_equity=0.15, profit_margins=0.1),
    "LOWPAYOUT": dict(dividend_yield=0.04, payout_ratio=0.05, return_on_equity=0.15, profit_margins=0.1),
    "NODATA": dict(dividend_yield=None, payout_ratio=None, return_on_equity=None, profit_margins=None),
    "ILLIQUID": dict(dividend_yield=0.04, payout_ratio=0.5, return_on_equity=0.15, profit_margins=0.1),
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days, force=False):
        if symbol not in FUNDAMENTALS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return ILLIQUID_BARS if symbol == "ILLIQUID" else BARS

    def fake_get_fundamentals(symbol, force=False):
        return FUNDAMENTALS[symbol]

    def fake_get_next_earnings_date(symbol, force=False):
        return None

    monkeypatch.setattr(dividend_module, "get_daily_bars", fake_get_daily_bars)
    monkeypatch.setattr(dividend_module, "get_fundamentals", fake_get_fundamentals)
    monkeypatch.setattr(common_module, "get_next_earnings_date", fake_get_next_earnings_date)


@pytest.fixture
def strategy(patched_market_data) -> DividendStrategy:
    config = ScreenerConfig(universe=list(FUNDAMENTALS.keys()))
    return DividendStrategy(config)


def test_good_dividend_symbol_passes_filters(strategy):
    result = strategy.evaluate_symbol("GOODDIV")
    assert result.passes_filters
    assert result.dividend_yield_pct == 4.0
    assert result.payout_ratio_pct == 50.0
    assert result.strategy_id == "dividend"


def test_low_yield_symbol_fails_yield_filter(strategy):
    result = strategy.evaluate_symbol("LOWYIELD")
    assert not result.passes_filters
    assert any("dividend yield" in note.lower() for note in result.notes)


def test_payout_too_high_symbol_fails_payout_filter(strategy):
    result = strategy.evaluate_symbol("HIGHPAYOUT")
    assert not result.passes_filters
    assert any("payout ratio" in note.lower() for note in result.notes)


def test_payout_too_low_symbol_fails_payout_filter(strategy):
    result = strategy.evaluate_symbol("LOWPAYOUT")
    assert not result.passes_filters
    assert any("payout ratio" in note.lower() for note in result.notes)


def test_missing_fundamentals_symbol_fails_filters_with_explanatory_notes(strategy):
    result = strategy.evaluate_symbol("NODATA")
    assert not result.passes_filters
    assert result.dividend_yield_pct is None
    assert result.payout_ratio_pct is None
    assert any("sin dato de dividend yield" in note.lower() for note in result.notes)
    assert any("sin dato de payout ratio" in note.lower() for note in result.notes)


def test_illiquid_symbol_fails_liquidity_filter_despite_good_dividend(strategy):
    result = strategy.evaluate_symbol("ILLIQUID")
    assert not result.passes_filters
    assert any("volumen" in note.lower() for note in result.notes)


def test_payout_quality_score_is_highest_near_midpoint_of_sustainable_range(strategy):
    # Centro del rango sostenible configurado (20%-75%) ~ 47.5%: deberia
    # puntuar mejor en la componente de payout que un payout en el extremo.
    mid_result = strategy.evaluate_symbol("GOODDIV")  # payout 50%, cerca del centro
    extreme_result = strategy.evaluate_symbol("LOWPAYOUT")  # payout 5%, lejos del centro
    assert mid_result.score > extreme_result.score


def test_dividend_yield_pct_normalizes_fraction_and_percent_formats():
    # yfinance reporto el yield como fraccion historicamente (ej 0.045 = 4.5%)
    # pero respuestas recientes lo devuelven ya en porcentaje (ej 4.5).
    assert _dividend_yield_pct(0.045) == pytest.approx(4.5)
    assert _dividend_yield_pct(4.5) == pytest.approx(4.5)
    assert _dividend_yield_pct(None) is None


def test_evaluate_symbol_exposes_score_components(strategy):
    result = strategy.evaluate_symbol("GOODDIV")
    assert set(result.score_components.keys()) == {"yield", "payout_quality", "quality"}


def test_results_sorted_descending_by_score(strategy):
    results = strategy.scan()
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_unknown_symbol_is_skipped(patched_market_data):
    config = ScreenerConfig(universe=["GOODDIV", "GHOST"])
    s = DividendStrategy(config)
    results = s.scan()
    assert [r.symbol for r in results] == ["GOODDIV"]


def test_scan_raises_when_no_symbol_has_data(monkeypatch):
    def always_fails(symbol, lookback_days, force=False):
        raise MarketDataError("sin red en este entorno")

    monkeypatch.setattr(dividend_module, "get_daily_bars", always_fails)
    config = ScreenerConfig(universe=["GOODDIV"])
    s = DividendStrategy(config)
    with pytest.raises(MarketDataError):
        s.scan()


def test_supports_backtest_is_false():
    assert DividendStrategy.supports_backtest is False


def test_evaluate_symbol_attaches_sector(strategy, monkeypatch):
    monkeypatch.setattr(dividend_module, "get_sector", lambda symbol: "Utilities")
    result = strategy.evaluate_symbol("GOODDIV")
    assert result.sector == "Utilities"


def test_extra_fundamentals_are_exposed_on_signal_result(monkeypatch, patched_market_data):
    fundamentals = dict(
        FUNDAMENTALS["GOODDIV"], peg_ratio=1.2, beta=0.8, recommendation_key="buy",
        insider_ownership=0.02, institutional_ownership=0.55, short_pct_of_float=0.04,
        current_ratio=1.5, quick_ratio=1.1, free_cash_flow=3_000_000.0,
    )
    monkeypatch.setattr(dividend_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["GOODDIV"])
    result = DividendStrategy(config).evaluate_symbol("GOODDIV")
    assert result.peg_ratio == 1.2
    assert result.beta == 0.8
    assert result.analyst_recommendation == "buy"
    assert result.insider_ownership_pct == pytest.approx(2.0)
    assert result.institutional_ownership_pct == pytest.approx(55.0)
    assert result.short_pct_of_float == pytest.approx(4.0)
    assert result.current_ratio == 1.5
    assert result.quick_ratio == 1.1
    assert result.free_cash_flow == 3_000_000.0


def test_high_beta_adds_advisory_note_but_does_not_block_filter(monkeypatch, patched_market_data):
    fundamentals = dict(FUNDAMENTALS["GOODDIV"], beta=3.0)
    monkeypatch.setattr(dividend_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["GOODDIV"])
    result = DividendStrategy(config).evaluate_symbol("GOODDIV")
    assert result.passes_filters
    assert any("beta" in note.lower() for note in result.notes)


def test_weak_liquidity_ratios_add_advisory_notes_but_do_not_block_filter(monkeypatch, patched_market_data):
    fundamentals = dict(FUNDAMENTALS["GOODDIV"], current_ratio=0.6, quick_ratio=0.4)
    monkeypatch.setattr(dividend_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["GOODDIV"])
    result = DividendStrategy(config).evaluate_symbol("GOODDIV")
    assert result.passes_filters
    assert any("current ratio" in note.lower() for note in result.notes)
    assert any("quick ratio" in note.lower() for note in result.notes)
