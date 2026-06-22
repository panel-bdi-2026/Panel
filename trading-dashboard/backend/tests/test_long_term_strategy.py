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
    "LOSSMAKING": dict(
        # eps negativo muy cercano a 0 -> trailing_pe negativo y enorme en
        # valor absoluto (simula una empresa con perdidas, no una de valor).
        trailing_pe=-5000.0, forward_pe=14.0, earnings_growth=0.20, revenue_growth=0.15,
        return_on_equity=0.25, debt_to_equity=80.0, profit_margins=0.18,
    ),
    "FORWARDONLY": dict(
        trailing_pe=None, forward_pe=14.0, earnings_growth=0.20, revenue_growth=0.15,
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


def test_evaluate_symbol_exposes_score_components(strategy):
    result = strategy.evaluate_symbol("STRONG")
    assert set(result.score_components.keys()) == {"value", "growth", "quality", "margin", "peg"}


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


def test_negative_pe_does_not_inflate_value_score(strategy):
    # trailing_pe=-5000 (empresa con perdidas, eps casi 0 por debajo): sin el
    # resguardo, "max_pe_ratio - pe" daria un value_score absurdamente alto
    # (25 - (-5000) = 5025), premiando a la empresa con perdidas como si
    # fuera la mejor oportunidad de valor. Debe quedar en 0, no pasar el
    # filtro de valoracion, y el score total debe quedar muy por debajo del
    # de una empresa con fundamentales igualmente fuertes pero PE sano.
    lossmaking = strategy.evaluate_symbol("LOSSMAKING")
    strong = strategy.evaluate_symbol("STRONG")
    assert not lossmaking.passes_filters
    assert lossmaking.pe_ratio == -5000.0
    assert lossmaking.score < strong.score


def test_forward_pe_used_as_fallback_with_disclosure_note(strategy):
    result = strategy.evaluate_symbol("FORWARDONLY")
    assert result.pe_ratio == 14.0
    assert any("forward pe" in note.lower() for note in result.notes)


def test_trailing_pe_preferred_over_forward_pe_when_both_present(strategy):
    result = strategy.evaluate_symbol("STRONG")
    assert result.pe_ratio == 15.0  # trailing_pe, no forward_pe (14.0)
    assert not any("forward pe" in note.lower() for note in result.notes)


def test_low_peg_ratio_scores_higher_than_high_peg_ratio(monkeypatch, patched_market_data):
    config = ScreenerConfig(universe=["STRONG"])
    s = LongTermStrategy(config)

    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: dict(FUNDAMENTALS["STRONG"], peg_ratio=0.5))
    low_result = s.evaluate_symbol("STRONG")

    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: dict(FUNDAMENTALS["STRONG"], peg_ratio=4.0))
    high_result = s.evaluate_symbol("STRONG")

    assert low_result.peg_ratio == 0.5
    assert high_result.peg_ratio == 4.0
    assert low_result.score_components["peg"] > high_result.score_components["peg"]


def test_extra_fundamentals_are_exposed_on_signal_result(monkeypatch, patched_market_data):
    fundamentals = dict(
        FUNDAMENTALS["STRONG"], peg_ratio=1.2, beta=1.1, recommendation_key="buy",
        insider_ownership=0.05, institutional_ownership=0.65, short_pct_of_float=0.03,
        current_ratio=1.8, quick_ratio=1.3, free_cash_flow=2_000_000.0,
    )
    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["STRONG"])
    result = LongTermStrategy(config).evaluate_symbol("STRONG")
    assert result.peg_ratio == 1.2
    assert result.beta == 1.1
    assert result.analyst_recommendation == "buy"
    assert result.insider_ownership_pct == pytest.approx(5.0)
    assert result.institutional_ownership_pct == pytest.approx(65.0)
    assert result.short_pct_of_float == pytest.approx(3.0)
    assert result.current_ratio == 1.8
    assert result.quick_ratio == 1.3
    assert result.free_cash_flow == 2_000_000.0


def test_high_beta_adds_advisory_note_but_does_not_block_filter(monkeypatch, patched_market_data):
    fundamentals = dict(FUNDAMENTALS["STRONG"], beta=3.0)
    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["STRONG"])
    result = LongTermStrategy(config).evaluate_symbol("STRONG")
    assert result.passes_filters
    assert any("beta" in note.lower() for note in result.notes)


def test_negative_free_cash_flow_adds_advisory_note_but_does_not_block_filter(monkeypatch, patched_market_data):
    fundamentals = dict(FUNDAMENTALS["STRONG"], free_cash_flow=-500_000.0)
    monkeypatch.setattr(long_term_module, "get_fundamentals", lambda symbol, force=False: fundamentals)
    config = ScreenerConfig(universe=["STRONG"])
    result = LongTermStrategy(config).evaluate_symbol("STRONG")
    assert result.passes_filters
    assert any("free cash flow negativo" in note.lower() for note in result.notes)
