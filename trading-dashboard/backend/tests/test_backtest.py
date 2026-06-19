import math

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


def test_backtest_includes_equity_curve_anchored_at_zero(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    # La curva es diaria (un punto por dia de trading entre la primera entrada
    # y la ultima salida), no un punto por operacion: con mas de un dia de
    # historia entre operaciones tiene que haber mas puntos que operaciones.
    assert len(summary.equity_curve) > summary.total_trades
    assert summary.equity_curve[0].equity_pct == 0.0
    assert summary.equity_curve[0].date == summary.start_date
    assert summary.equity_curve[-1].date == summary.end_date
    assert summary.equity_curve[-1].equity_pct == summary.strategy_cumulative_return_pct


def test_backtest_includes_sharpe_ratio_when_enough_trades(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert summary.total_trades >= 2
    assert summary.sharpe_ratio is not None


def test_backtest_throttles_between_symbols_to_avoid_rate_limiting(monkeypatch, patched_market_data):
    """Mismo motivo que el throttle del scan en vivo (scan_request_delay_seconds):
    con un universo grande (S&P 500 completo) el backtest pega cientos de
    pedidos seguidos a la API gratuita de datos sin esta pausa."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(backtest_module.time, "sleep", lambda secs: sleep_calls.append(secs))
    config = ScreenerConfig(
        universe=["MOM", "FLAT"],
        benchmark_symbol="SPY",
        backtest_years=1,
        scan_request_delay_seconds=0.25,
    )
    run_backtest(config)
    assert sleep_calls == [0.25]


def test_backtest_skips_throttle_when_delay_is_zero(monkeypatch, patched_market_data):
    sleep_calls: list[float] = []
    monkeypatch.setattr(backtest_module.time, "sleep", lambda secs: sleep_calls.append(secs))
    config = ScreenerConfig(
        universe=["MOM", "FLAT"],
        benchmark_symbol="SPY",
        backtest_years=1,
        scan_request_delay_seconds=0.0,
    )
    run_backtest(config)
    assert sleep_calls == []


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


def test_entry_fills_at_next_day_open_not_signal_day_close():
    from app.backtest import _simulate_symbol
    from app.indicators import rate_of_change

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    # Con esta configuracion (igual a test_stop_loss_triggers_on_intraday_low_
    # not_close) el filtro de entrada se confirma con el cierre del dia index
    # 6; el fill realista es la apertura del dia siguiente (index 7), no el
    # cierre del dia de la senal (eso seria mirar al futuro, ya que los
    # indicadores recien se conocen al cierre). Se fuerza un gap de apertura
    # bien marcado en ese dia para distinguir numericamente ambos precios.
    entry_day_idx = 7
    bars.loc[bars.index[entry_day_idx], "Open"] = bars["Close"].iloc[entry_day_idx - 1] + 50.0

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

    assert trades[0].entry_date == bars.index[entry_day_idx]
    assert trades[0].entry_price == round(float(bars["Open"].iloc[entry_day_idx]), 2)
    signal_day_close = float(bars["Close"].iloc[entry_day_idx - 1])
    assert trades[0].entry_price != round(signal_day_close, 2)


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
    from datetime import datetime
    from app.models import BacktestTrade
    return BacktestTrade(
        symbol=symbol,
        entry_date=datetime(2024, 1, entry_day),
        exit_date=datetime(2024, 1, exit_day),
        entry_price=100.0,
        exit_price=100.0 * (1 + return_pct / 100),
        return_pct=return_pct,
        exit_reason="max_holding_days",
    )


def _bench_bars_for_stats(n_days=10):
    return _bars([100.0 + i for i in range(n_days)])


def test_breakeven_trades_excluded_from_losses_and_win_rate():
    from app.backtest import _compute_summary_stats
    # Dos ganadoras, una empate exacto (0%): el empate no debe contar ni como
    # ganadora ni como perdedora, y no debe distorsionar avg_loss.
    trades = [
        _trade("A", 1, 2, return_pct=2.0),
        _trade("B", 2, 3, return_pct=0.0),
        _trade("C", 3, 4, return_pct=4.0),
    ]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats())
    assert summary.total_trades == 3
    # win_rate solo cuenta retornos > 0 sobre el total: 2/3.
    assert summary.win_rate_pct == round(2 / 3 * 100, 1)
    assert summary.avg_loss_pct == 0.0  # losses list vacia -> default 0.0, no contaminada por el 0% literal


def test_profit_factor_is_infinite_when_no_losing_trades():
    from app.backtest import _compute_summary_stats
    trades = [_trade("A", 1, 2, return_pct=3.0), _trade("B", 2, 3, return_pct=5.0)]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats())
    assert summary.profit_factor is None
    assert summary.profit_factor_is_infinite is True


def test_profit_factor_is_none_and_finite_when_all_trades_breakeven():
    from app.backtest import _compute_summary_stats
    trades = [_trade("A", 1, 2, return_pct=0.0), _trade("B", 2, 3, return_pct=0.0)]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats())
    assert summary.profit_factor is None
    assert summary.profit_factor_is_infinite is False


def test_profit_factor_is_finite_ratio_when_mixed_wins_and_losses():
    from app.backtest import _compute_summary_stats
    trades = [_trade("A", 1, 2, return_pct=4.0), _trade("B", 2, 3, return_pct=-2.0)]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats())
    assert summary.profit_factor == 2.0
    assert summary.profit_factor_is_infinite is False


def test_sharpe_uses_sample_stdev_not_population_stdev():
    from app.backtest import _compute_summary_stats
    import statistics as _stats
    returns_pct = [2.0, -1.0, 3.0, -0.5, 1.5]
    # Operaciones consecutivas de 1 dia cada una (la salida de una coincide
    # con la entrada de la siguiente): con esa cadencia cada operacion aporta
    # un unico retorno diario igual a weight*return_pct, sin interpolacion
    # parcial de por medio, asi que los retornos diarios de la curva
    # coinciden exactamente con los retornos por operacion ponderados.
    trades = [_trade(f"S{i}", i + 1, i + 2, return_pct=r) for i, r in enumerate(returns_pct)]
    top_n = 10
    summary = _compute_summary_stats(trades, top_n=top_n, bench_bars=_bench_bars_for_stats())

    daily_returns = [(1.0 / top_n) * r / 100 for r in returns_pct]
    expected_sharpe_sample = round(
        (_stats.mean(daily_returns) / _stats.stdev(daily_returns)) * (252 ** 0.5), 2
    )
    expected_sharpe_population = round(
        (_stats.mean(daily_returns) / _stats.pstdev(daily_returns)) * (252 ** 0.5), 2
    )
    assert summary.sharpe_ratio == expected_sharpe_sample
    assert summary.sharpe_ratio != expected_sharpe_population


def test_daily_equity_curve_reflects_overlapping_open_positions():
    from app.backtest import _compute_summary_stats
    # A queda abierta los dias 1-11 (ganadora, +10%) mientras B esta abierta
    # en paralelo los dias 1-6 (perdedora, -20%). El modelo anterior solo
    # actualizaba la curva al cerrar cada operacion y nunca acreditaba la
    # ganancia no realizada de A mientras seguia abierta: el cierre de B se
    # veia como una caida al -10% (1*(1+0.5*-20/100)), mas profunda de lo que
    # realmente era con A ya compensando en paralelo (+2.5% no realizado a
    # esa fecha).
    trades = [_trade("A", 1, 11, return_pct=10.0), _trade("B", 1, 6, return_pct=-20.0)]
    summary = _compute_summary_stats(trades, top_n=2, bench_bars=_bench_bars_for_stats())

    points = {p.date: p.equity_pct for p in summary.equity_curve}
    assert points[trades[0].entry_date] == 0.0
    assert points[trades[1].exit_date] == -7.75
    assert points[trades[0].exit_date] == -5.5  # resultado final, no depende del camino
    assert summary.max_drawdown_pct == -7.75  # mas leve que el -10.0% que mostraria el modelo anterior
    assert summary.strategy_cumulative_return_pct == -5.5


def test_avg_exposure_pct_reflects_capital_utilization():
    from app.backtest import _compute_summary_stats
    # Mismo escenario que el test anterior: dia 1, las 2 posiciones de
    # top_n=2 estan ocupadas (100% de exposicion); dia 6, B ya cerro y solo
    # queda A (50%); dia 11, A tambien cerro (0%). Promedio: (100+50+0)/3.
    trades = [_trade("A", 1, 11, return_pct=10.0), _trade("B", 1, 6, return_pct=-20.0)]
    summary = _compute_summary_stats(trades, top_n=2, bench_bars=_bench_bars_for_stats())
    assert summary.avg_exposure_pct == 50.0


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
    # Rampa larga (252 dias, hasta 300) que fija un maximo de 52s alto y
    # permanece "a la vista" de la ventana movil durante toda la segunda
    # mitad de la serie, seguida de una cola oscilante (150 dias) que se
    # queda siempre >15% por debajo de ese pico. Se necesitan al menos 252
    # dias para que pct_from_high tenga la ventana completa (ver
    # indicators.py): con menos, el "maximo" no es un dato real y el filtro
    # nunca bloquea nada. Un pico corto seguido de una sola caida no alcanza:
    # el maximo "sale" de la ventana movil en pocos dias y solo retrasa una
    # entrada en vez de bloquearla.
    n_ramp, n_tail = 252, 150
    ramp_target, tail_mean, tail_amp, tail_period = 300, 210, 15, 10
    vals = [100 + i * (ramp_target - 100) / (n_ramp - 1) for i in range(n_ramp)]
    vals += [tail_mean + tail_amp * math.sin(i / tail_period) for i in range(n_tail)]
    idx = pd.date_range("2023-01-01", periods=n_ramp + n_tail, freq="D")
    close = pd.Series(vals, index=idx)

    def _mk(c):
        return pd.DataFrame(
            {"Open": c, "High": c * 1.004, "Low": c * 0.996, "Close": c, "Volume": 5_000_000},
            index=c.index,
        )

    bars = _mk(close)
    bench = _mk(pd.Series([100.0] * (n_ramp + n_tail), index=idx))

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


# ---------------------------------------------------------------------------
# Oportunista (_simulate_symbol_opportunistic / run_opportunistic_backtest):
# misma estructura de backtest que Momentum pero con la logica de entrada
# (momentum de corto plazo + RSI en zona de recuperacion + espacio de
# crecimiento respecto al maximo de 52 semanas) y salida (stop-loss, tiempo
# maximo, o RSI sobrecomprado) de strategies/opportunistic.py. La funcion
# fija "252" para la ventana de maximo de 52 semanas y para start_idx (ver
# backtest.py), por lo que toda serie de prueba necesita 253+ dias sin
# importar que tan chicos sean los demas periodos configurados.
# ---------------------------------------------------------------------------

def _opportunistic_oscillating_bars(n=320, amplitude=2.0, period=4.0):
    """Oscilacion pura (sin tendencia neta). Combinada con el suavizado
    Wilder de rsi_period=14, el RSI recorre naturalmente tanto la zona de
    recuperacion (35-60, dispara la entrada) como la de sobrecompra (>60,
    dispara la salida trend_break) varias veces a lo largo de la serie."""
    i = np.arange(n)
    close_vals = 100.0 + amplitude * np.sin(i / period)
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    close = pd.Series(close_vals, index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 5_000_000},
        index=idx,
    )


def _opportunistic_cfg(**overrides):
    from app.screener_config import OpportunisticConfig

    # min_volatility_pct y min_pct_below_52w_high en 0: la serie sintetica de
    # oscilacion pura tiene muy poca amplitud y no aleja el precio de su
    # maximo de 52 semanas, asi que esos dos filtros (irrelevantes para lo
    # que testean estos casos) se dejan siempre pasantes.
    defaults = dict(
        momentum_lookback_days=10,
        rsi_period=14,
        rsi_min=35,
        rsi_max=60,
        min_volatility_pct=0,
        min_pct_below_52w_high=0,
        stop_loss_atr_multiplier=2.0,
        max_holding_days=30,
    )
    defaults.update(overrides)
    return ScreenerConfig(universe=["OPP"], atr_period=14, opportunistic=OpportunisticConfig(**defaults))


def test_opportunistic_trend_break_exit_on_rsi_exhaustion():
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    cfg = _opportunistic_cfg(max_holding_days=30)

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg)

    assert trades[0].exit_reason == "trend_break"
    assert trades[0].entry_date == bars.index[263]
    assert trades[0].exit_date == bars.index[278]


def test_opportunistic_max_holding_days_exit_takes_priority_over_trend_break():
    # Mismo escenario que el test de trend_break: el RSI cruza rsi_max justo
    # el dia en que tambien se cumplen los max_holding_days configurados aqui
    # (15). Verifica que el timeout tiene prioridad sobre el RSI sobrecomprado
    # cuando ambas condiciones de salida coinciden (ver el orden del ternario
    # en _simulate_symbol_opportunistic).
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    cfg = _opportunistic_cfg(max_holding_days=15)

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg)

    assert trades[0].exit_reason == "max_holding_days"
    assert trades[0].exit_date == bars.index[278]


def test_opportunistic_stop_loss_triggers_on_intraday_low_not_close():
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    bars.loc[bars.index[264], "Low"] = 50.0  # mecha intradiaria el dia siguiente al fill, perfora el stop sin que el cierre lo refleje
    cfg = _opportunistic_cfg(max_holding_days=30)

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg)

    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_date == bars.index[264]
    assert bars["Close"].iloc[264] > trades[0].exit_price


def test_opportunistic_backtest_produces_trades_and_metrics(monkeypatch):
    bars = _opportunistic_oscillating_bars()
    bench_bars = _bars([100.0] * len(bars))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "OPP":
            return bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg()
    config = config.model_copy(update={"benchmark_symbol": "SPY", "backtest_years": 1})

    summary = backtest_module.run_opportunistic_backtest(config)

    assert summary.total_trades > 0
    assert all(t.symbol == "OPP" for t in summary.trades)
    assert 0 <= summary.win_rate_pct <= 100
    assert summary.start_date < summary.end_date


def test_opportunistic_backtest_raises_when_benchmark_unavailable(monkeypatch):
    bars = _opportunistic_oscillating_bars()

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "OPP":
            return bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg()
    config = config.model_copy(update={"benchmark_symbol": "SPY", "backtest_years": 1})

    with pytest.raises(BacktestError):
        backtest_module.run_opportunistic_backtest(config)


def test_opportunistic_backtest_raises_when_no_trades_generated(monkeypatch):
    # Serie sin tendencia ni oscilacion (precio plano): roc nunca es positivo,
    # asi que el filtro de momentum jamas deja pasar una entrada.
    flat_bars = _bars([100.0] * 320)
    bench_bars = _bars([100.0] * 320)

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "FLAT":
            return flat_bars
        if symbol == "SPY":
            return bench_bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(universe=["FLAT"], benchmark_symbol="SPY", backtest_years=1)

    with pytest.raises(BacktestError):
        backtest_module.run_opportunistic_backtest(config)
