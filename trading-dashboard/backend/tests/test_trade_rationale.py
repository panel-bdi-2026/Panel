from app.trade_rationale import build_buy_rationale


def test_momentum_rationale_mentions_momentum_and_rsi():
    signal = {
        "strategy_id": "momentum",
        "momentum_3m_pct": 18.3,
        "momentum_1m_pct": 4.2,
        "trend_ok": True,
        "rsi": 62.0,
        "macd_histogram_pct": 0.5,
        "sector": "Information Technology",
        "sector_relative_strength_pct": 12.0,
    }
    text = build_buy_rationale(signal)
    assert "18.3%" in text
    assert "RSI en 62" in text
    assert "Information Technology" in text
    assert "por encima" in text


def test_momentum_rationale_notes_when_trend_not_confirmed():
    signal = {"strategy_id": "momentum", "momentum_3m_pct": 5.0, "trend_ok": False}
    text = build_buy_rationale(signal)
    assert "no la confirma del todo" in text


def test_opportunistic_rationale_describes_rebound():
    signal = {
        "strategy_id": "opportunistic",
        "momentum_3m_pct": -16.6,
        "momentum_1m_pct": -3.3,
        "rsi": 45.1,
        "macd_histogram_pct": 0.11,
        "pct_from_52w_high": -21.6,
    }
    text = build_buy_rationale(signal)
    assert "caída" in text
    assert "-16.6%" in text
    assert "recuperándose desde sobreventa" in text
    assert "22%" in text


def test_long_term_rationale_mentions_pe_and_analyst_recommendation():
    signal = {
        "strategy_id": "long_term",
        "pe_ratio": 18.4,
        "peg_ratio": 1.2,
        "analyst_recommendation": "buy",
        "price_to_book": 3.1,
    }
    text = build_buy_rationale(signal)
    assert "PE de 18.4" in text
    assert "PEG 1.20" in text
    assert "buy" in text


def test_long_term_rationale_handles_missing_pe():
    signal = {"strategy_id": "long_term"}
    text = build_buy_rationale(signal)
    assert "sin dato de PE" in text


def test_dividend_rationale_mentions_yield_and_payout():
    signal = {"strategy_id": "dividend", "dividend_yield_pct": 3.8, "payout_ratio_pct": 55.0}
    text = build_buy_rationale(signal)
    assert "3.80%" in text
    assert "55%" in text


def test_dividend_rationale_handles_missing_yield():
    signal = {"strategy_id": "dividend"}
    text = build_buy_rationale(signal)
    assert "sin dato de yield" in text


def test_unknown_strategy_falls_back_to_generic_sentence():
    signal = {"strategy_id": "some_future_strategy"}
    text = build_buy_rationale(signal)
    assert "some_future_strategy" in text
    assert "Pasó los filtros" in text


def test_missing_strategy_id_defaults_to_momentum():
    signal = {"momentum_3m_pct": 10.0, "trend_ok": True}
    text = build_buy_rationale(signal)
    assert "momentum" in text.lower() or "10.0%" in text


def test_never_raises_on_empty_signal():
    text = build_buy_rationale({})
    assert isinstance(text, str)
    assert len(text) > 0
