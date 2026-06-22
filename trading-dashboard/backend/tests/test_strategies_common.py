from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategies import common as common_module
from app.strategies.common import (
    avg_dollar_volume,
    avg_volume,
    context_technicals,
    earnings_blackout_ok,
    extra_fundamentals_context,
)


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


def test_context_technicals_returns_none_with_insufficient_history():
    bars = _bars(_series(30, 0.1, 1, 4))
    assert context_technicals(bars, atr_period=14) is None


def test_context_technicals_returns_fields_with_enough_history():
    bars = _bars(_series(265, 0.25, 3, 4))
    ctx = context_technicals(bars, atr_period=14)
    assert ctx is not None
    assert ctx["last_price"] == float(bars["Close"].iloc[-1])
    assert isinstance(ctx["trend_ok"], bool)
    assert ctx["pct_from_52w_high"] is not None


def test_context_technicals_pct_from_52w_high_is_none_without_full_window():
    # Con menos de 252 dias no hay ventana completa para un maximo de 52
    # semanas real (ver indicators.pct_from_high): debe quedar en None, no en
    # un maximo calculado sobre una ventana mas corta.
    bars = _bars(_series(100, 0.25, 3, 4))
    ctx = context_technicals(bars, atr_period=14)
    assert ctx is not None
    assert ctx["pct_from_52w_high"] is None


def test_avg_dollar_volume_is_volume_times_price():
    bars = _bars([100.0] * 25, volume=1_000)
    assert avg_dollar_volume(bars) == pytest.approx(100_000.0)


def test_avg_volume_is_rolling_mean_of_last_20_days():
    bars = _bars([100.0] * 25, volume=2_000)
    assert avg_volume(bars) == pytest.approx(2_000.0)


def test_earnings_blackout_ok_when_no_earnings_date_known(monkeypatch):
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: None)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is True
    assert days is None


def test_earnings_blackout_blocks_when_within_window(monkeypatch):
    soon = date.today() + timedelta(days=2)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: soon)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is False
    assert days == 2


def test_earnings_blackout_does_not_block_outside_window(monkeypatch):
    far = date.today() + timedelta(days=30)
    monkeypatch.setattr(common_module, "get_next_earnings_date", lambda symbol, force=False: far)
    ok, days = earnings_blackout_ok("AAPL", blackout_days=5)
    assert ok is True
    assert days == 30


def test_extra_fundamentals_context_normalizes_fractions_to_percent():
    fundamentals = dict(
        peg_ratio=1.5, beta=1.0, recommendation_key="buy",
        insider_ownership=0.05, institutional_ownership=0.70,
        short_pct_of_float=0.02, current_ratio=1.8, quick_ratio=1.2,
        free_cash_flow=5_000_000.0,
    )
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert ctx["peg_ratio"] == 1.5
    assert ctx["insider_ownership_pct"] == pytest.approx(5.0)
    assert ctx["institutional_ownership_pct"] == pytest.approx(70.0)
    assert ctx["short_pct_of_float"] == pytest.approx(2.0)
    assert notes == []


def test_extra_fundamentals_context_falls_back_to_trailing_peg_with_note():
    fundamentals = dict(peg_ratio=None, peg_ratio_trailing=2.0)
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert ctx["peg_ratio"] == 2.0
    assert any("trailing peg" in note.lower() for note in notes)


def test_extra_fundamentals_context_flags_high_beta_without_blocking():
    fundamentals = dict(beta=2.5)
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert ctx["beta"] == 2.5
    assert any("beta" in note.lower() for note in notes)


def test_extra_fundamentals_context_flags_high_short_interest():
    fundamentals = dict(short_pct_of_float=0.30)
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert any("interes en corto" in note.lower() for note in notes)


def test_extra_fundamentals_context_flags_weak_liquidity_ratios():
    fundamentals = dict(current_ratio=0.8, quick_ratio=0.5)
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert any("current ratio" in note.lower() for note in notes)
    assert any("quick ratio" in note.lower() for note in notes)


def test_extra_fundamentals_context_flags_negative_free_cash_flow():
    fundamentals = dict(free_cash_flow=-1_000_000.0)
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert any("free cash flow negativo" in note.lower() for note in notes)


def test_extra_fundamentals_context_flags_bearish_analyst_consensus():
    fundamentals = dict(recommendation_key="sell")
    ctx, notes = extra_fundamentals_context(fundamentals, max_beta=1.5, max_short_interest_pct=20.0)
    assert any("consenso de analistas" in note.lower() for note in notes)


def test_extra_fundamentals_context_returns_none_fields_when_no_data():
    ctx, notes = extra_fundamentals_context({}, max_beta=1.5, max_short_interest_pct=20.0)
    assert all(v is None for v in ctx.values())
    assert notes == []
