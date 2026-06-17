import numpy as np
import pandas as pd
import pytest

from app import backtest as backtest_module
from app.backtest import BacktestError, run_backtest
from app.market_data import MarketDataError
from app.screener_config import ScreenerConfig


def _series(n, drift, amplitude, period, base=100.0):
    i = np.arange(n)
    return list(base + drift * i + amplitude * np.sin(i / period))


def _bars(close_values, volume=5_000_000):
    idx = pd.date_range("2021-01-01", periods=len(close_values), freq="D")
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


# Suficiente historia (>1 anio de trading) para que entren y salgan varias
# operaciones dentro del backtest.
MOM_CLOSE = _series(500, 0.12, 4, 5)
FLAT_CLOSE = _series(500, 0.0, 1, 4)
BENCH_CLOSE = _series(500, 0.02, 1, 6)

FAKE_BARS = {
    "MOM": _bars(MOM_CLOSE),
    "FLAT": _bars(FLAT_CLOSE),
    "SPY": _bars(BENCH_CLOSE),
}


@pytest.fixture
def patched_market_data(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days):
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)


def test_backtest_produces_trades_and_metrics(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert summary.total_trades > 0
    assert 0 <= summary.win_rate_pct <= 100
    assert summary.start_date < summary.end_date
    assert all(t.symbol in ("MOM", "FLAT") for t in summary.trades)


def test_backtest_raises_when_benchmark_unavailable(monkeypatch):
    def fake_get_daily_bars(symbol, lookback_days):
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["MOM"], benchmark_symbol="SPY")
    with pytest.raises(BacktestError):
        run_backtest(config)


def test_backtest_raises_when_no_trades_generated(monkeypatch):
    # Universo vacio de simbolos validos -> nunca hay senal de entrada.
    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return FAKE_BARS["SPY"]
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["MISSING"], benchmark_symbol="SPY")
    with pytest.raises(BacktestError):
        run_backtest(config)


def test_backtest_includes_sharpe_ratio_when_enough_trades(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert summary.total_trades >= 2
    assert summary.sharpe_ratio is not None


def test_backtest_sharpe_ratio_is_none_with_fewer_than_two_trades(monkeypatch):
    # Un solo pico de tendencia y despues una caida plana que nunca vuelve a
    # disparar una entrada: una sola operacion en todo el periodo.
    n_spike, n_flat = 60, 400
    spike = [100.0 + i for i in range(n_spike)]
    flat = [spike[-1] - 5 - 0.001 * i for i in range(n_flat)]
    bars = _bars(spike + flat)
    bench_bars = _bars([100.0] * (n_spike + n_flat))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "ONE":
            return bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["ONE"], benchmark_symbol="SPY", backtest_years=1, regime_filter_enabled=False)
    summary = run_backtest(config)
    assert summary.total_trades == 1
    assert summary.sharpe_ratio is None


def test_costs_reduce_returns_vs_zero_cost_baseline():
    from app.backtest import _simulate_symbol
    from app.indicators import rate_of_change

    bars = FAKE_BARS["MOM"]
    bench_bars = FAKE_BARS["SPY"]
    base_kwargs = dict(universe=["MOM"], benchmark_symbol="SPY", backtest_years=1, regime_filter_enabled=False)
    cfg_no_cost = ScreenerConfig(**base_kwargs, commission_per_trade_usd=0.0, slippage_pct=0.0)
    cfg_with_cost = ScreenerConfig(**base_kwargs, commission_per_trade_usd=1.0, slippage_pct=0.05)

    benchmark_roc = rate_of_change(bench_bars["Close"], cfg_no_cost.momentum_lookback_days)
    regime_ok = pd.Series(True, index=bars.index)

    trades_no_cost = _simulate_symbol("MOM", bars, cfg_no_cost, benchmark_roc, regime_ok)
    trades_with_cost = _simulate_symbol("MOM", bars, cfg_with_cost, benchmark_roc, regime_ok)

    assert len(trades_no_cost) > 0
    assert [t.entry_date for t in trades_no_cost] == [t.entry_date for t in trades_with_cost]
    for t_no_cost, t_cost in zip(trades_no_cost, trades_with_cost):
        assert t_cost.return_pct < t_no_cost.return_pct


def test_stop_loss_triggers_on_intraday_low_not_close():
    from app.backtest import _simulate_symbol
    from app.indicators import rate_of_change

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    bars.loc[bars.index[9], "Low"] = 50.0  # mecha intradiaria que perfora el stop sin que cierre por debajo

    bench_bars = _bars([100.0] * 30)

    cfg = ScreenerConfig(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.0,
        max_holding_days=50,
        regime_filter_enabled=False,
        top_n=10,
    )

    benchmark_roc = rate_of_change(bench_bars["Close"], cfg.momentum_lookback_days)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, benchmark_roc, regime_ok)

    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_date == bars.index[9]
    assert bars["Close"].iloc[9] > trades[0].exit_price  # el cierre nunca perforo el stop, solo el minimo intradiario


def test_regime_filter_blocks_entries_when_benchmark_below_regime_sma(monkeypatch):
    n = 300
    closes = [100.0 + 0.12 * i + 4 * np.sin(i / 5) for i in range(n)]
    bars = _bars(closes)
    # Benchmark en clara tendencia bajista y por debajo de su SMA de regimen
    # durante todo el periodo: ninguna entrada larga deberia activarse.
    bench_closes = [200.0 - 0.3 * i for i in range(n)]
    bench_bars = _bars(bench_closes)

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "MOM":
            return bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(
        universe=["MOM"], benchmark_symbol="SPY", backtest_years=1,
        regime_filter_enabled=True, regime_sma_period=50,
    )
    with pytest.raises(BacktestError):
        run_backtest(config)


def _trade(symbol, entry_day, exit_day, return_pct=1.0):
    from datetime import datetime, timezone
    from app.models import BacktestTrade
    return BacktestTrade(
        symbol=symbol,
        entry_date=datetime(2024, 1, entry_day, tzinfo=timezone.utc),
        exit_date=datetime(2024, 1, exit_day, tzinfo=timezone.utc),
        entry_price=100.0,
        exit_price=100.0 * (1 + return_pct / 100),
        return_pct=return_pct,
        exit_reason="max_holding_days",
    )


def test_cap_concurrent_positions_limits_simultaneous_trades():
    from app.backtest import cap_concurrent_positions
    # 3 operaciones que se solapan completamente (todas abiertas dia 1-10) con
    # top_n=2: solo entran las dos primeras, la tercera no tiene cupo.
    trades = [_trade("A", 1, 10), _trade("B", 1, 10), _trade("C", 1, 10)]
    taken = cap_concurrent_positions(trades, top_n=2)
    assert [t.symbol for t in taken] == ["A", "B"]


def test_cap_concurrent_positions_reuses_freed_slot():
    from app.backtest import cap_concurrent_positions
    # A ocupa el unico cupo dias 1-5; cuando cierra, D (que entra dia 6) puede
    # tomarlo. B y C se solapan con A y quedan afuera con top_n=1.
    trades = [_trade("A", 1, 5), _trade("B", 2, 4), _trade("C", 3, 4), _trade("D", 6, 9)]
    taken = cap_concurrent_positions(trades, top_n=1)
    assert [t.symbol for t in taken] == ["A", "D"]


def test_cap_concurrent_positions_no_cap_when_top_n_invalid():
    from app.backtest import cap_concurrent_positions
    trades = [_trade("A", 1, 10), _trade("B", 1, 10)]
    assert len(cap_concurrent_positions(trades, top_n=0)) == 2


def test_near_high_filter_reduces_entries_far_from_52w_high(monkeypatch):
    # Pico temprano a ~196 (fija el maximo de 52s), caida, y luego un uptrend
    # limpio pero siempre >15% por debajo de ese pico. Con el filtro activo,
    # esas entradas "lejos del maximo" se descartan.
    n = 120
    vals = []
    for i in range(n):
        if i <= 12:
            vals.append(100 + i * 8)
        elif i <= 24:
            vals.append(196 - (i - 12) * 7)
        else:
            vals.append(112 + (i - 24) * 0.5)
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    close = pd.Series(vals, index=idx)

    def _mk(c):
        return pd.DataFrame(
            {"Open": c, "High": c * 1.004, "Low": c * 0.996, "Close": c, "Volume": 5_000_000},
            index=c.index,
        )

    bars = _mk(close)
    bench = _mk(pd.Series([100.0] * n, index=idx))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench
        if symbol == "FARHI":
            return bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)

    base = dict(
        universe=["FARHI"], benchmark_symbol="SPY", backtest_years=1,
        sma_fast=3, sma_slow=5, atr_period=3, momentum_lookback_days=5,
        momentum_short_days=3, rsi_period=3, rsi_min=0, rsi_max=100,
        regime_filter_enabled=False, top_n=5,
    )
    without = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=False))
    with_filter = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=True, max_pct_below_52w_high=15.0))
    assert with_filter.total_trades < without.total_trades
