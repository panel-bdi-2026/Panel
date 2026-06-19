import numpy as np
import pandas as pd
import pytest

from app.market_data import MarketDataError
from app.screener_config import ScreenerConfig
from app.strategies import common as common_module
from app.strategies import long_term as long_term_module
from app.strategies.long_term import LongTermStrategy


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


CLOSE = _series(265, 0.1, 3, 4)
BARS = _bars(CLOSE)
ILLIQUID_BARS = _bars(CLOSE, volume=10)

FUNDAMENTALS = {
    "STRONG": dict(
        trailing_pe=15.0, forward_pe=14.0, earnings_growth=0.20, revenue_growth=0.15,
        return_on_equity=0.25, debt_to_equity=80.0, profit_margins=0.18,
    ),
    "EXPENSIVE": dict(
        trailing_pe=60.0, forward_pe=55.0, earnings_growth=0.20, revenue_growth=0.15,
        return_on_equity=0.25, debt_to_equity=80.0, profit_margins=0.18,
    ),
    "SLOWGROWTH": dict(
        trailing_pe=15.0, forward_pe=14.0, earnings_growth=0.01, revenue_growth=0.01,
        return_on_equity=0.25, debt_to_equity=80.0, profit_margins=0.18,
    ),
    "NODATA": dict(
        trailing_pe=None, forward_pe=None, earnings_growth=None, revenue_growth=None,
        return_on_equity=None, debt_to_equity=None, profit_margins=None,
    ),
    "ILLIQUID": dict(
        trailing_pe=15.0, forward_pe=14.0, earnings_growth=0.20, revenue_growth=0.15,
        return_on_equity=0.25, debt_to_equity=80.0, profit_margins=0.18,
    ),
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

    monkeypatch.setattr(long_term_module, "get_daily_bars", fake_get_daily_bars)
    monkeypatch.setattr(long_term_module, "get_fundamentals", fake_get_fundamentals)
    monkeypatch.setattr(common_module, "get_next_earnings_date", fake_get_next_earnings_date)


@pytest.fixture
def strategy(patched_market_data) -> LongTermStrategy:
    config = ScreenerConfig(universe=list(FUNDAMENTALS.keys()))
    return LongTermStrategy(config)


def test_strong_fundamentals_symbol_passes_filters(strategy):
    result = strategy.evaluate_symbol("STRONG")
    assert result.passes_filters
    assert result.pe_ratio == 15.0
    assert result.strategy_id == "long_term"


def test_expensive_pe_symbol_fails_value_filter(strategy):
    result = strategy.evaluate_symbol("EXPENSIVE")
    assert not result.passes_filters
    assert any("PE" in note for note in result.notes)


def test_slow_earnings_growth_symbol_fails_growth_filter(strategy):
    result = strategy.evaluate_symbol("SLOWGROWTH")
    assert not result.passes_filters
    assert any("crecimiento de ganancias" in note.lower() for note in result.notes)


def test_missing_fundamentals_symbol_fails_filters_with_explanatory_notes(strategy):
    result = strategy.evaluate_symbol("NODATA")
    assert not result.passes_filters
    assert result.pe_ratio is None
    assert any("sin dato de pe" in note.lower() for note in result.notes)
    assert any("crecimiento de ganancias" in note.lower() for note in result.notes)


def test_illiquid_symbol_fails_liquidity_filter_despite_strong_fundamentals(strategy):
    result = strategy.evaluate_symbol("ILLIQUID")
    assert not result.passes_filters
    assert any("volumen" in note.lower() for note in result.notes)


def test_roe_missing_adds_note_but_does_not_block_filter(monkeypatch, patched_market_data):
    fundamentals_no_roe = dict(FUNDAMENTALS["STRONG"], return_on_equity=None)
    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: fundamentals_no_roe)
    config = ScreenerConfig(universe=["STRONG"])
    s = LongTermStrategy(config)
    result = s.evaluate_symbol("STRONG")
    assert result.passes_filters
    assert any("sin dato de roe" in note.lower() for note in result.notes)


def test_high_debt_to_equity_adds_note_but_does_not_block_filter(monkeypatch, patched_market_data):
    fundamentals_high_debt = dict(FUNDAMENTALS["STRONG"], debt_to_equity=300.0)
    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: fundamentals_high_debt)
    config = ScreenerConfig(universe=["STRONG"])
    s = LongTermStrategy(config)
    result = s.evaluate_symbol("STRONG")
    assert result.passes_filters
    assert any("deuda/equity" in note.lower() for note in result.notes)


def test_results_sorted_descending_by_score(strategy):
    results = strategy.scan()
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_unknown_symbol_is_skipped(patched_market_data):
    config = ScreenerConfig(universe=["STRONG", "GHOST"])
    s = LongTermStrategy(config)
    results = s.scan()
    assert [r.symbol for r in results] == ["STRONG"]


def test_scan_raises_when_no_symbol_has_data(monkeypatch):
    def always_fails(symbol, lookback_days, force=False):
        raise MarketDataError("sin red en este entorno")

    monkeypatch.setattr(long_term_module, "get_daily_bars", always_fails)
    config = ScreenerConfig(universe=["STRONG"])
    s = LongTermStrategy(config)
    with pytest.raises(MarketDataError):
        s.scan()


def test_supports_backtest_is_false():
    assert LongTermStrategy.supports_backtest is False


def test_evaluate_symbol_attaches_sector(strategy, monkeypatch):
    monkeypatch.setattr(long_term_module, "get_sector", lambda symbol: "Health Care")
    result = strategy.evaluate_symbol("STRONG")
    assert result.sector == "Health Care"
