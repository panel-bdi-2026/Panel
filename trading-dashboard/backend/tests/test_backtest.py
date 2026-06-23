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


def test_backtest_sharpe_ratio_is_none_with_fewer_than_two_trades():
    # El gate de "minimo 2 operaciones" para el sharpe vive en
    # _compute_summary_stats (ver ese docstring): se testea ahi directamente,
    # con una operacion sintetica, en vez de tratar de forzar exactamente una
    # sola operacion a traves de todo el pipeline de score cross-sectional
    # (fragil de armar a mano: con un universo de 2 simbolos el percentil de
    # cada componente es binario 0/100 dia por dia, asi que un empate casi
    # exacto se puede romper por un simple corrimiento de un dia en alguna
    # ventana movil).
    from app.backtest import _compute_summary_stats

    trades = [_trade("A", 1, 2, return_pct=3.0)]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats())
    assert summary.total_trades == 1
    assert summary.sharpe_ratio is None


def test_costs_reduce_returns_vs_zero_cost_baseline():
    from app.backtest import _simulate_symbol

    bars = FAKE_BARS["MOM"]
    base_kwargs = dict(universe=["MOM"], benchmark_symbol="SPY", backtest_years=1, regime_filter_enabled=False)
    cfg_no_cost = ScreenerConfig(**base_kwargs, commission_per_trade_usd=0.0, slippage_pct=0.0)
    cfg_with_cost = ScreenerConfig(**base_kwargs, commission_per_trade_usd=1.0, slippage_pct=0.05)

    # Score siempre por encima de cualquier umbral razonable: aisla el efecto
    # de la comision/slippage del mecanismo de entrada/salida por score (no es
    # lo que testea este caso), asi que las salidas quedan determinadas solo
    # por stop-loss o tiempo maximo.
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades_no_cost = _simulate_symbol("MOM", bars, cfg_no_cost, score_series, regime_ok)
    trades_with_cost = _simulate_symbol("MOM", bars, cfg_with_cost, score_series, regime_ok)

    assert len(trades_no_cost) > 0
    assert [t.entry_date for t in trades_no_cost] == [t.entry_date for t in trades_with_cost]
    for t_no_cost, t_cost in zip(trades_no_cost, trades_with_cost):
        assert t_cost.return_pct < t_no_cost.return_pct


def test_trade_daily_marks_uses_real_close_path_and_corrects_last_day():
    from app.backtest import _trade_daily_marks

    closes = [100.0, 105.0, 95.0, 90.0, 108.0, 112.0]
    bars = _bars(closes)
    entry_idx, exit_idx = 1, 5
    entry_fill = 105.0
    ret_pct = 8.0  # retorno final ya neto de comision/slippage, distinto del +6.67% bruto (112/105-1)

    marks = _trade_daily_marks(bars, entry_idx, exit_idx, entry_fill, ret_pct)

    assert marks[bars.index[1]] == pytest.approx(1.0)
    assert marks[bars.index[2]] == pytest.approx(95 / 105)  # camino real, no interpolado
    assert marks[bars.index[3]] == pytest.approx(90 / 105)
    assert marks[bars.index[4]] == pytest.approx(108 / 105)
    assert marks[bars.index[5]] == pytest.approx(1.08)  # corregido al retorno final neto, no al cierre crudo


def test_simulate_symbol_populates_marks_when_dict_provided():
    from app.backtest import _simulate_symbol

    bars = FAKE_BARS["MOM"]
    cfg = ScreenerConfig(universe=["MOM"], benchmark_symbol="SPY", backtest_years=1, regime_filter_enabled=False)
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    marks_by_trade_id: dict = {}
    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok, marks_by_trade_id)

    assert len(trades) > 0
    for t in trades:
        marks = marks_by_trade_id[id(t)]
        assert marks
        # El ultimo dia coincide (salvo el redondeo a 2 decimales de
        # return_pct) con el retorno final ya neto de comision/slippage, no
        # con el cierre crudo de ese dia.
        assert marks[t.exit_date] == pytest.approx(1 + t.return_pct / 100, abs=1e-3)


def test_daily_equity_curve_uses_real_close_path_for_open_positions():
    from datetime import datetime

    from app.backtest import _compute_summary_stats

    # A queda abierta los dias 1-11 (retorno final +10%). Con interpolacion
    # lineal el dia 6 (a mitad de camino) mostraria +5% no realizado, pero el
    # camino real de A tuvo una caida fuerte ese dia (marks_by_trade_id la
    # fija en -20%): la curva debe reflejar la caida real, no el promedio
    # lineal entre entrada y el resultado final.
    idx = pd.date_range("2024-01-01", periods=15, freq="D")
    bench_close = pd.Series([100.0 + i for i in range(15)], index=idx)
    bench_bars = pd.DataFrame(
        {"Open": bench_close, "High": bench_close + 1, "Low": bench_close - 1, "Close": bench_close, "Volume": 5_000_000},
        index=idx,
    )

    trade = _trade("A", 1, 11, return_pct=10.0)
    marks_by_trade_id = {id(trade): {datetime(2024, 1, 6): 0.80}}

    summary = _compute_summary_stats([trade], top_n=1, bench_bars=bench_bars, marks_by_trade_id=marks_by_trade_id)
    points = {p.date: p.equity_pct for p in summary.equity_curve}

    assert points[datetime(2024, 1, 6)] == -20.0
    # Dia sin marca explicita para esta operacion (ej. desajuste de
    # calendario): se cae de vuelta a la interpolacion lineal de siempre.
    assert points[datetime(2024, 1, 3)] == 2.0
    # El resultado final no depende del camino recorrido.
    assert points[datetime(2024, 1, 11)] == 10.0


def test_stop_loss_triggers_on_intraday_low_not_close():
    from app.backtest import _simulate_symbol

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    bars.loc[bars.index[9], "Low"] = 50.0  # mecha intradiaria que perfora el stop sin que cierre por debajo

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

    # Score siempre por encima del umbral de entrada (default 80.0): el ATR ya
    # es valido desde el inicio del loop (atr_period=3), asi que la senal de
    # entrada se confirma en la primera iteracion (index start_idx=6) y el
    # fill ocurre a la apertura del dia siguiente (index 7), igual que con el
    # viejo AND de filtros booleanos para esta misma configuracion.
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_date == bars.index[9]
    assert bars["Close"].iloc[9] > trades[0].exit_price  # el cierre nunca perforo el stop, solo el minimo intradiario


def test_entry_fills_at_next_day_open_not_signal_day_close():
    from app.backtest import _simulate_symbol

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
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades[0].entry_date == bars.index[entry_day_idx]
    assert trades[0].entry_price == round(float(bars["Open"].iloc[entry_day_idx]), 2)
    signal_day_close = float(bars["Close"].iloc[entry_day_idx - 1])
    assert trades[0].entry_price != round(signal_day_close, 2)


def test_regime_filter_blocks_entries_when_benchmark_below_regime_sma(monkeypatch):
    # Universo de 2 simbolos (no 1): el score cross-sectional de Momentum es
    # un percentil contra el resto del universo valido ese mismo dia (ver
    # _cross_sectional_score_panel) e indefinido (NaN, nunca dispara entrada)
    # con un solo simbolo, sin importar el regimen -- eso no es lo que testea
    # este caso, que necesita que el score SI pudiera ser valido para poder
    # verificar que el gate de regimen lo bloquea de todos modos.
    n = 300
    closes = [100.0 + 0.12 * i + 4 * np.sin(i / 5) for i in range(n)]
    bars = _bars(closes)
    closes2 = [120.0 + 0.08 * i + 3 * np.sin(i / 7) for i in range(n)]
    bars2 = _bars(closes2)
    # Benchmark en clara tendencia bajista y por debajo de su SMA de regimen
    # durante todo el periodo: ninguna entrada larga deberia activarse, sin
    # importar el universo.
    bench_closes = [200.0 - 0.3 * i for i in range(n)]
    bench_bars = _bars(bench_closes)

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "MOM":
            return bars
        if symbol == "MOM2":
            return bars2
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = ScreenerConfig(
        universe=["MOM", "MOM2"], benchmark_symbol="SPY", backtest_years=1,
        regime_filter_enabled=True, regime_sma_period=50,
    )
    with pytest.raises(BacktestError):
        run_backtest(config)


def _trade(symbol, entry_day, exit_day, return_pct=1.0, exit_reason="max_holding_days"):
    from datetime import datetime
    from app.models import BacktestTrade
    return BacktestTrade(
        symbol=symbol,
        entry_date=datetime(2024, 1, entry_day),
        exit_date=datetime(2024, 1, exit_day),
        entry_price=100.0,
        exit_price=100.0 * (1 + return_pct / 100),
        return_pct=return_pct,
        exit_reason=exit_reason,
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


def test_exit_reason_counts_cover_all_trades_not_just_truncated_sample():
    from app.backtest import _compute_summary_stats
    # summary.trades se trunca a las ultimas 50 (ver _compute_summary_stats),
    # pero exit_reason_counts debe reflejar TODAS las operaciones: 60 en
    # total, repartidas en 3 motivos de salida, no solo las ultimas 50.
    trades = (
        [_trade(f"A{i}", 1, 2, exit_reason="stop_loss") for i in range(20)]
        + [_trade(f"B{i}", 1, 2, exit_reason="max_holding_days") for i in range(20)]
        + [_trade(f"C{i}", 1, 2, exit_reason="score_exit") for i in range(20)]
    )
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats(2))
    assert summary.total_trades == 60
    assert len(summary.trades) == 50
    assert summary.exit_reason_counts == {"stop_loss": 20, "max_holding_days": 20, "score_exit": 20}
    assert sum(summary.exit_reason_counts.values()) == summary.total_trades


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

    # Segundo simbolo del universo (no 1): el score cross-sectional necesita
    # >=2 simbolos validos por fecha para que el percentil este definido (ver
    # _cross_sectional_score_panel). Mismo patron ramp+tail con parametros
    # distintos (no un simple desfasaje de fase) para que FARHI no empate
    # sistematicamente con su companero en cada componente del score.
    ramp_target2, tail_mean2, tail_amp2, tail_period2 = 260, 180, 12, 13
    vals2 = [100 + i * (ramp_target2 - 100) / (n_ramp - 1) for i in range(n_ramp)]
    vals2 += [tail_mean2 + tail_amp2 * math.sin(i / tail_period2) for i in range(n_tail)]
    close2 = pd.Series(vals2, index=idx)

    def _mk(c):
        return pd.DataFrame(
            {"Open": c, "High": c * 1.004, "Low": c * 0.996, "Close": c, "Volume": 5_000_000},
            index=c.index,
        )

    bars = _mk(close)
    bars2 = _mk(close2)
    bench = _mk(pd.Series([100.0] * (n_ramp + n_tail), index=idx))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench
        if symbol == "FARHI":
            return bars
        if symbol == "FARHI2":
            return bars2
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)

    base = dict(
        universe=["FARHI", "FARHI2"], benchmark_symbol="SPY", backtest_years=1,
        sma_fast=3, sma_slow=5, atr_period=3, momentum_lookback_days=5,
        momentum_short_days=3, rsi_period=3, rsi_min=0, rsi_max=100,
        regime_filter_enabled=False, top_n=5,
    )
    without = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=False))
    with_filter = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=True, max_pct_below_52w_high=15.0))
    assert with_filter.total_trades < without.total_trades


# ---------------------------------------------------------------------------
# Oportunista (_simulate_symbol_opportunistic / run_opportunistic_backtest):
# misma estructura de backtest que Momentum pero con la logica de entrada y
# salida score-driven de strategies/opportunistic.py (ver _opportunistic_raw_
# components/_cross_sectional_score_panel en backtest.py). La funcion fija
# "252" para la ventana de maximo de 52 semanas y para start_idx (ver
# backtest.py), por lo que toda serie de prueba necesita 253+ dias sin
# importar que tan chicos sean los demas periodos configurados.
# ---------------------------------------------------------------------------

def _opportunistic_oscillating_bars(n=320, amplitude=2.0, period=4.0):
    """Oscilacion pura (sin tendencia neta), usada como insumo de precio en
    los tests unitarios de _simulate_symbol_opportunistic de mas abajo (que
    le pasan un score_series sintetico armado a mano, no derivado de este
    precio): solo necesitan suficiente historia (253+ dias) y suficiente
    volatilidad para que el ATR del stop-loss no sea cero."""
    i = np.arange(n)
    close_vals = 100.0 + amplitude * np.sin(i / period)
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    close = pd.Series(close_vals, index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 5_000_000},
        index=idx,
    )


def _opportunistic_buddy_bars(n=320):
    """Segundo simbolo del universo para los tests de integracion de
    Oportunista (run_opportunistic_backtest/_walk_forward): el score
    cross-sectional necesita >=2 simbolos validos por fecha para que el
    percentil este definido (ver _cross_sectional_score_panel), igual que en
    los tests de integracion de Momentum. Tendencia bajista lenta y pareja
    (sin pico propio): no le disputa el ranking a OPP de forma sistematica."""
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    close = pd.Series([100.0 - 0.05 * i for i in range(n)], index=idx)
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
    return ScreenerConfig(
        universe=["OPP", "OPP2"], atr_period=14, opportunistic=OpportunisticConfig(**defaults)
    )


def test_opportunistic_score_exit_when_score_drops_below_threshold():
    # _simulate_symbol_opportunistic ya no deriva el score de un solo
    # indicador (antes RSI sobrecomprado): score_series es ahora un insumo
    # externo (el percentil cross-sectional, ver _cross_sectional_score_panel),
    # asi que este test unitario lo arma a mano para verificar la mecanica de
    # entrada/salida (no la formula de ningun componente, eso lo cubren los
    # tests de integracion mas abajo). Senal de entrada confirmada al cierre
    # del dia 262 (score alto), fill al abrir el dia 263; score se mantiene
    # alto hasta el dia 277 y cae por debajo del umbral de salida el dia 278.
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    cfg = _opportunistic_cfg(max_holding_days=30)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_exit_threshold - 1, index=bars.index)
    score_series.iloc[262:278] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series)

    assert trades[0].exit_reason == "score_exit"
    assert trades[0].entry_date == bars.index[263]
    assert trades[0].exit_date == bars.index[278]


def test_opportunistic_max_holding_days_exit_takes_priority_over_score_exit():
    # Mismo escenario que el test anterior: el score cae por debajo del
    # umbral de salida justo el dia en que tambien se cumplen los
    # max_holding_days configurados aqui (15 dias desde la entrada en el dia
    # 263). Verifica que el timeout tiene prioridad sobre la caida de score
    # cuando ambas condiciones de salida coinciden (ver el orden del ternario
    # en _simulate_symbol_opportunistic).
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    cfg = _opportunistic_cfg(max_holding_days=15)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_exit_threshold - 1, index=bars.index)
    score_series.iloc[262:278] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series)

    assert trades[0].exit_reason == "max_holding_days"
    assert trades[0].exit_date == bars.index[278]


def test_opportunistic_stop_loss_triggers_on_intraday_low_not_close():
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_oscillating_bars()
    bars.loc[bars.index[264], "Low"] = 50.0  # mecha intradiaria el dia siguiente al fill, perfora el stop sin que el cierre lo refleje
    cfg = _opportunistic_cfg(max_holding_days=30)
    opp = cfg.opportunistic

    # Score alto desde el dia de la senal (262) en adelante, sin caer nunca
    # por debajo del umbral de salida: la unica salida posible es el stop-loss.
    score_series = pd.Series(opp.backtest_score_exit_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series)

    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_date == bars.index[264]
    assert bars["Close"].iloc[264] > trades[0].exit_price


def test_opportunistic_backtest_produces_trades_and_metrics(monkeypatch):
    bars = _opportunistic_oscillating_bars()
    bench_bars = _bars([100.0] * len(bars))
    buddy_bars = _opportunistic_buddy_bars(len(bars))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "OPP":
            return bars
        if symbol == "OPP2":
            return buddy_bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg()
    config = config.model_copy(update={"benchmark_symbol": "SPY", "backtest_years": 1})

    summary = backtest_module.run_opportunistic_backtest(config)

    assert summary.total_trades > 0
    assert all(t.symbol in ("OPP", "OPP2") for t in summary.trades)
    assert 0 <= summary.win_rate_pct <= 100
    assert summary.start_date < summary.end_date


def test_opportunistic_backtest_raises_when_benchmark_unavailable(monkeypatch):
    bars = _opportunistic_oscillating_bars()
    buddy_bars = _opportunistic_buddy_bars(len(bars))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "OPP":
            return bars
        if symbol == "OPP2":
            return buddy_bars
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


def _bench_bars_2024(n_days=11):
    idx = pd.date_range("2024-01-01", periods=n_days, freq="D")
    close = pd.Series([100.0 + i for i in range(n_days)], index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 5_000_000},
        index=idx,
    )


def test_build_walk_forward_result_boundary_trade_goes_to_next_fold_not_previous():
    from app.backtest import _build_walk_forward_result
    # 11 dias (01-01..01-11): con n_folds=2 el limite cae exacto en 01-06 (mitad
    # de los 10 dias entre el primer y el ultimo). Una operacion que entra
    # justo ahi debe quedar en el segundo fold (limite inferior inclusivo), no
    # en el primero (limite superior exclusivo, salvo en el ultimo fold).
    bench_bars = _bench_bars_2024()
    trades = [
        _trade("A", 6, 7, return_pct=1.0),
        _trade("B", 2, 3, return_pct=2.0),
        _trade("C", 9, 10, return_pct=-1.0),
    ]
    result = _build_walk_forward_result(trades, top_n=10, bench_bars=bench_bars, marks_by_trade_id={}, n_folds=2)
    assert result.n_folds == 2
    assert result.folds[0].total_trades == 1  # solo B
    assert result.folds[1].total_trades == 2  # A (en el limite) y C


def test_build_walk_forward_result_handles_fold_with_no_trades():
    from app.backtest import _build_walk_forward_result
    bench_bars = _bench_bars_2024()
    trades = [_trade("A", 1, 2, return_pct=5.0)]  # solo cae en el primer fold
    result = _build_walk_forward_result(trades, top_n=10, bench_bars=bench_bars, marks_by_trade_id={}, n_folds=2)
    assert result.folds[0].total_trades == 1
    assert result.folds[0].win_rate_pct is not None
    assert result.folds[1].total_trades == 0
    assert result.folds[1].win_rate_pct is None
    assert result.folds[1].sharpe_ratio is None


def test_run_backtest_walk_forward_partitions_all_trades_without_loss(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    full_summary = run_backtest(config)
    result = backtest_module.run_backtest_walk_forward(config, n_folds=3)
    assert result.n_folds == 3
    assert len(result.folds) == 3
    # cada operacion del backtest completo cae en exactamente un fold.
    assert sum(f.total_trades for f in result.folds) == full_summary.total_trades
    for i in range(len(result.folds) - 1):
        assert result.folds[i].end_date == result.folds[i + 1].start_date


def test_opportunistic_backtest_walk_forward_partitions_all_trades_without_loss(monkeypatch):
    bars = _opportunistic_oscillating_bars()
    bench_bars = _bars([100.0] * len(bars))
    buddy_bars = _opportunistic_buddy_bars(len(bars))

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "OPP":
            return bars
        if symbol == "OPP2":
            return buddy_bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg()
    config = config.model_copy(update={"benchmark_symbol": "SPY", "backtest_years": 1})

    full_summary = backtest_module.run_opportunistic_backtest(config)
    result = backtest_module.run_opportunistic_backtest_walk_forward(config, n_folds=3)

    assert result.n_folds == 3
    assert len(result.folds) == 3
    assert sum(f.total_trades for f in result.folds) == full_summary.total_trades
