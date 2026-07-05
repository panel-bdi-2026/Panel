import math

import numpy as np
import pandas as pd
import pytest

from app import backtest as backtest_module
from app.backtest import BacktestError, run_backtest
from app.market_data import MarketDataError
from app.screener_config import GROWTH_TICKERS, ScreenerConfig


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


def test_backtest_universe_helper_filters_growth_tickers_by_membership():
    # Filtra por membership contra GROWTH_TICKERS, no por como se construyo
    # el universe: tambien excluye un growth ticker que el usuario agrego a
    # mano a un universe custom, no solo los que vienen de DEFAULT_UNIVERSE.
    from app.backtest import _backtest_universe

    growth_symbol = GROWTH_TICKERS[-1]
    config = ScreenerConfig(universe=["AAPL", growth_symbol, "MSFT"], benchmark_symbol="SPY")

    assert _backtest_universe(config) == ["AAPL", "MSFT"]


def test_backtest_never_requests_market_data_for_growth_tickers(monkeypatch):
    # GROWTH_TICKERS (screener_config.py) se armo buscando hoy nombres que ya
    # se sabe que tuvieron una corrida fuerte reciente: dejarlos en el
    # universo de un backtest historico seria sesgo de look-ahead de
    # inclusion. El backtest nunca deberia pedirles datos de mercado, ni
    # siquiera si estan presentes en cfg.universe.
    requested = []

    def fake_get_daily_bars(symbol, lookback_days):
        requested.append(symbol)
        if symbol not in FAKE_BARS:
            raise MarketDataError(f"sin datos sinteticos para {symbol}")
        return FAKE_BARS[symbol]

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    growth_symbol = GROWTH_TICKERS[0]
    config = ScreenerConfig(
        universe=["MOM", "FLAT", growth_symbol], benchmark_symbol="SPY", backtest_years=1
    )
    summary = run_backtest(config)

    assert growth_symbol not in requested
    assert summary.total_trades > 0


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


def test_backtest_includes_exposure_adjusted_benchmark_and_alpha(patched_market_data):
    # Smoke test end-to-end (no con operaciones sinteticas a mano): confirma
    # que las dos metricas nuevas quedan conectadas a traves de todo el
    # pipeline real (_collect_momentum_trades -> _compute_summary_stats), no
    # solo en los tests unitarios de mas abajo que llaman _compute_summary_
    # stats directamente.
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert isinstance(summary.exposure_adjusted_benchmark_return_pct, float)
    assert any(t.alpha_pct is not None for t in summary.trades)
    assert summary.avg_alpha_pct is not None


def test_backtest_throttles_between_symbols_to_avoid_rate_limiting(monkeypatch, patched_market_data):
    """Mismo motivo que el throttle del scan en vivo (scan_request_delay_seconds):
    con un universo grande (S&P 500 completo) el backtest pega cientos de
    pedidos seguidos a la API gratuita de datos sin esta pausa."""
    sleep_calls: list[float] = []
    monkeypatch.setattr(backtest_module.time, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(backtest_module, "is_bars_cached", lambda symbol, lookback_days: False)
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


def test_backtest_skips_throttle_for_already_cached_symbol(monkeypatch, patched_market_data):
    # Tipico cuando Momentum y Oportunista corren seguidos con el mismo
    # backtest_years (misma history_days, misma clave de cache en
    # market_data.get_daily_bars): la segunda pasada no deberia pagar la
    # pausa anti-rate-limit por un simbolo que ya esta en cache.
    sleep_calls: list[float] = []
    monkeypatch.setattr(backtest_module.time, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(backtest_module, "is_bars_cached", lambda symbol, lookback_days: symbol == "FLAT")
    config = ScreenerConfig(
        universe=["MOM", "FLAT"],
        benchmark_symbol="SPY",
        backtest_years=1,
        scan_request_delay_seconds=0.25,
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


def test_effective_slippage_pct_returns_base_when_volume_at_or_above_threshold():
    from app.backtest import _effective_slippage_pct

    assert _effective_slippage_pct(0.05, 5_000_000, 5_000_000, 3.0) == pytest.approx(0.05)
    assert _effective_slippage_pct(0.05, 9_000_000, 5_000_000, 3.0) == pytest.approx(0.05)


def test_effective_slippage_pct_multiplies_when_volume_below_threshold():
    from app.backtest import _effective_slippage_pct

    assert _effective_slippage_pct(0.05, 1_000_000, 5_000_000, 3.0) == pytest.approx(0.15)


def test_effective_slippage_pct_disabled_when_threshold_not_positive():
    # threshold <= 0 = sin umbral configurado: ningun simbolo recibe el
    # multiplicador, sin importar cuan bajo sea su volumen.
    from app.backtest import _effective_slippage_pct

    assert _effective_slippage_pct(0.05, 1.0, 0, 3.0) == pytest.approx(0.05)
    assert _effective_slippage_pct(0.05, 1.0, -10, 3.0) == pytest.approx(0.05)


def test_effective_slippage_pct_ignores_nan_volume():
    # Sin suficiente historia todavia para el volumen promedio: no penaliza,
    # mismo criterio "sin dato no bloquea/no penaliza" que el resto de los
    # gates del backtest.
    from app.backtest import _effective_slippage_pct

    assert _effective_slippage_pct(0.05, float("nan"), 5_000_000, 3.0) == pytest.approx(0.05)


def test_low_liquidity_symbol_pays_more_slippage_cost_than_liquid_symbol():
    # Mismas barras de precio (MOM_CLOSE) y misma config de costos, solo
    # difiere el volumen: con un volumen bajo, el volumen promedio en dolares
    # (dollar_volume_s) queda por debajo de low_liquidity_dollar_volume_threshold
    # (5_000_000 por default) pero todavia por encima de min_avg_dollar_volume
    # (1_000_000 por default, asi que la entrada no se bloquea), asi que el
    # slippage de cada fill se multiplica por low_liquidity_slippage_multiplier.
    from app.backtest import _simulate_symbol

    base_kwargs = dict(universe=["MOM"], benchmark_symbol="SPY", backtest_years=1, regime_filter_enabled=False)
    cfg = ScreenerConfig(**base_kwargs, commission_per_trade_usd=0.0, slippage_pct=0.05)

    bars_liquid = _bars(MOM_CLOSE, volume=5_000_000)
    bars_low_liquidity = _bars(MOM_CLOSE, volume=30_000)
    score_series = pd.Series(1000.0, index=bars_liquid.index)
    regime_ok = pd.Series(True, index=bars_liquid.index)

    trades_liquid = _simulate_symbol("MOM", bars_liquid, cfg, score_series, regime_ok)
    trades_low_liquidity = _simulate_symbol("MOM", bars_low_liquidity, cfg, score_series, regime_ok)

    assert len(trades_liquid) > 0
    assert [t.entry_date for t in trades_liquid] == [t.entry_date for t in trades_low_liquidity]
    for t_liquid, t_low in zip(trades_liquid, trades_low_liquidity):
        assert t_low.return_pct < t_liquid.return_pct


def test_low_liquidity_multiplier_disabled_when_threshold_is_zero():
    # Con el umbral en 0 (deshabilitado), el volumen bajo ya no importa: el
    # mismo escenario del test anterior debe dar resultados identicos.
    from app.backtest import _simulate_symbol

    base_kwargs = dict(
        universe=["MOM"],
        benchmark_symbol="SPY",
        backtest_years=1,
        regime_filter_enabled=False,
        commission_per_trade_usd=0.0,
        slippage_pct=0.05,
        low_liquidity_dollar_volume_threshold=0,
    )
    cfg = ScreenerConfig(**base_kwargs)

    bars_liquid = _bars(MOM_CLOSE, volume=5_000_000)
    bars_low_liquidity = _bars(MOM_CLOSE, volume=30_000)
    score_series = pd.Series(1000.0, index=bars_liquid.index)
    regime_ok = pd.Series(True, index=bars_liquid.index)

    trades_liquid = _simulate_symbol("MOM", bars_liquid, cfg, score_series, regime_ok)
    trades_low_liquidity = _simulate_symbol("MOM", bars_low_liquidity, cfg, score_series, regime_ok)

    assert len(trades_liquid) > 0
    for t_liquid, t_low in zip(trades_liquid, trades_low_liquidity):
        assert t_low.return_pct == pytest.approx(t_liquid.return_pct)


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
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.0,
        max_holding_days=50,
        regime_filter_enabled=False,
        # Filtro de cercania al maximo de 52 semanas apagado: con 30 dias de
        # historia no alcanza para una ventana real de 252 (ver pct_from_high
        # en indicators.py), y este test no esta probando ese filtro.
        near_high_filter_enabled=False,
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


def test_trailing_stop_price_never_lowers_existing_stop():
    from app.backtest import _trailing_stop_price

    # candidate = 95 (100 - 1*5), por debajo del stop ya colocado en 98: se
    # mantiene 98, el trailing nunca retrocede.
    result = _trailing_stop_price(
        current_stop=98.0, price_today=100.0, atr_today=5.0,
        trail_multiplier=1.0, entry_price=90.0, activation_pct=0.0,
    )
    assert result == 98.0


def test_trailing_stop_price_raises_when_candidate_higher():
    from app.backtest import _trailing_stop_price

    # candidate = 108 (110 - 1*2), por encima del stop vigente en 98: sube.
    result = _trailing_stop_price(
        current_stop=98.0, price_today=110.0, atr_today=2.0,
        trail_multiplier=1.0, entry_price=90.0, activation_pct=0.0,
    )
    assert result == 108.0


def test_trailing_stop_price_activation_pct_not_yet_reached():
    from app.backtest import _trailing_stop_price

    # entry=100, activation_pct=5% → umbral=105. price_today=103 < 105: no mueve.
    result = _trailing_stop_price(
        current_stop=98.0, price_today=103.0, atr_today=1.0,
        trail_multiplier=1.0, entry_price=100.0, activation_pct=5.0,
    )
    assert result == 98.0


def test_trailing_stop_price_activation_pct_reached():
    from app.backtest import _trailing_stop_price

    # entry=100, activation_pct=5% → umbral=105. price_today=110 > 105: mueve.
    result = _trailing_stop_price(
        current_stop=98.0, price_today=110.0, atr_today=1.0,
        trail_multiplier=1.0, entry_price=100.0, activation_pct=5.0,
    )
    assert result == 109.0


def test_trailing_stop_enabled_exits_earlier_than_static_stop():
    # Misma serie de precios y mismo stop inicial (ver test_stop_loss_triggers_
    # on_intraday_low_not_close): con stop_loss_atr_multiplier=1.0 y ATR
    # constante en 2.0 (true range fijo en esta serie con paso +1/dia), el
    # stop ESTATICO (entry_price=107 - 2 = 105) nunca lo alcanza el minimo
    # intradiario organico (Low[i] = Close[i]-1 crece monotonicamente por
    # encima de 105), asi que sin trailing la posicion nunca cierra en estos
    # 30 dias. Con trailing_stop_enabled=True el stop ratchetea dia a dia
    # hasta 112 en el indice 14 (Close[i]-2 desde el dia siguiente a la
    # entrada): una mecha forzada en el indice 15 a 110 -- por debajo del
    # stop trailing pero todavia por encima del estatico -- alcanza al
    # trailing y no al estatico, demostrando que el trailing efectivamente
    # adelanta la salida frente al mismo camino de precios.
    from app.backtest import _simulate_symbol

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)
    bars.loc[bars.index[15], "Low"] = 110.0

    base_kwargs = dict(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.0,
        max_holding_days=50,
        regime_filter_enabled=False,
        near_high_filter_enabled=False,
        top_n=10,
    )
    cfg_static = ScreenerConfig(**base_kwargs, trailing_stop_enabled=False)
    cfg_trailing = ScreenerConfig(**base_kwargs, trailing_stop_enabled=True)

    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades_static = _simulate_symbol("MOM", bars, cfg_static, score_series, regime_ok)
    trades_trailing = _simulate_symbol("MOM", bars, cfg_trailing, score_series, regime_ok)

    assert trades_static == []  # el stop fijo en 105 nunca lo perfora esta serie
    assert len(trades_trailing) == 1
    assert trades_trailing[0].exit_reason == "stop_loss"
    assert trades_trailing[0].entry_date == bars.index[7]
    assert trades_trailing[0].exit_date == bars.index[15]
    assert trades_trailing[0].exit_price == pytest.approx(112.0)


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
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.0,
        max_holding_days=50,
        regime_filter_enabled=False,
        # Ver comentario equivalente en test_stop_loss_triggers_on_intraday_
        # low_not_close: 30 dias de historia no alcanza para una ventana real
        # de 252, y este test no esta probando el filtro de cercania al maximo.
        near_high_filter_enabled=False,
        top_n=10,
    )
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades[0].entry_date == bars.index[entry_day_idx]
    assert trades[0].entry_price == round(float(bars["Open"].iloc[entry_day_idx]), 2)
    signal_day_close = float(bars["Close"].iloc[entry_day_idx - 1])
    assert trades[0].entry_price != round(signal_day_close, 2)


def test_liquidity_filter_blocks_entry_for_low_volume_symbol():
    # Mismo escenario que test_entry_fills_at_next_day_open_not_signal_day_
    # close (la senal de entrada se confirmaria en index 6), pero con un
    # volumen de 1_000 acciones en vez de los 5_000_000 por default de _bars:
    # con closes ~100-129, el volumen promedio en dolares de 20 dias (ver
    # dollar_volume_s en _simulate_symbol) queda muy por debajo de
    # min_avg_dollar_volume (1_000_000 por default), igual que el chequeo de
    # liquidity_ok del scan en vivo (ver screener.py). Sin el gate de liquidez
    # esta misma configuracion si genera una operacion (ver el test anterior).
    from app.backtest import _simulate_symbol

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes, volume=1_000)

    cfg = ScreenerConfig(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.0,
        max_holding_days=50,
        regime_filter_enabled=False,
        near_high_filter_enabled=False,
        top_n=10,
    )
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades == []


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


def test_opportunistic_regime_filter_blocks_entries_when_enabled_and_benchmark_below_regime_sma(monkeypatch):
    # Mismo caso que test_regime_filter_blocks_entries_when_benchmark_below_
    # regime_sma pero para Oportunista con su propio flag
    # (opportunistic_regime_filter_enabled), que esta apagado por defecto.
    n = 320
    bars = _opportunistic_oscillating_bars(n=n)
    buddy_bars = _opportunistic_buddy_bars(n=n)
    bench_bars = _bars([200.0 - 0.3 * i for i in range(n)])

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol == "SPY":
            return bench_bars
        if symbol == "OPP":
            return bars
        if symbol == "OPP2":
            return buddy_bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg().model_copy(
        update={
            "benchmark_symbol": "SPY",
            "backtest_years": 1,
            "opportunistic_regime_filter_enabled": True,
            "regime_sma_period": 50,
        }
    )
    with pytest.raises(BacktestError):
        backtest_module.run_opportunistic_backtest(config)


def test_regime_filter_is_independent_per_strategy(monkeypatch):
    # Re-test del 2026-06-24 (experimento P2, ya con el universo corregido
    # por sesgo de look-ahead): el filtro de regimen ayuda a Momentum pero
    # empeora a Oportunista, asi que cada estrategia tiene su propio flag
    # (regime_filter_enabled / opportunistic_regime_filter_enabled). Este
    # test verifica el desacople: el mismo benchmark bajista bloquea TODAS
    # las entradas de Momentum (filtro activo por defecto) pero ninguna de
    # Oportunista (filtro desactivado por defecto).
    n = 320
    mom_closes = [100.0 + 0.12 * i + 4 * np.sin(i / 5) for i in range(n)]
    mom2_closes = [120.0 + 0.08 * i + 3 * np.sin(i / 7) for i in range(n)]
    bars_by_symbol = {
        "SPY": _bars([200.0 - 0.3 * i for i in range(n)]),
        "MOM": _bars(mom_closes),
        "MOM2": _bars(mom2_closes),
        "OPP": _opportunistic_oscillating_bars(n=n),
        "OPP2": _opportunistic_buddy_bars(n=n),
    }

    def fake_get_daily_bars(symbol, lookback_days):
        if symbol in bars_by_symbol:
            return bars_by_symbol[symbol]
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)

    momentum_config = ScreenerConfig(
        universe=["MOM", "MOM2"], benchmark_symbol="SPY", backtest_years=1, regime_sma_period=50,
    )
    with pytest.raises(BacktestError):
        run_backtest(momentum_config)

    opportunistic_config = _opportunistic_cfg().model_copy(
        update={"benchmark_symbol": "SPY", "backtest_years": 1, "regime_sma_period": 50}
    )
    summary = backtest_module.run_opportunistic_backtest(opportunistic_config)
    assert summary.total_trades > 0


def _trade(
    symbol, entry_day, exit_day, return_pct=1.0, exit_reason="max_holding_days",
    entry_atr_pct=None, stop_loss_pct=None,
):
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
        entry_atr_pct=entry_atr_pct,
        stop_loss_pct=stop_loss_pct,
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


def test_deflated_sharpe_ratio_pct_matches_hand_computed_value_with_one_trial():
    from app.backtest import _deflated_sharpe_ratio_pct
    import statistics as _stats

    daily_returns = [0.01, -0.005, 0.02, -0.01, 0.015, 0.0, 0.008, -0.003]
    n = len(daily_returns)
    mean_r = _stats.mean(daily_returns)
    variance_pop = sum((r - mean_r) ** 2 for r in daily_returns) / n
    std_pop = variance_pop**0.5
    sr_hat = mean_r / std_pop
    skew = (sum((r - mean_r) ** 3 for r in daily_returns) / n) / std_pop**3
    kurtosis = (sum((r - mean_r) ** 4 for r in daily_returns) / n) / std_pop**4
    sr_variance = (1 - skew * sr_hat + (kurtosis - 1) / 4 * sr_hat**2) / (n - 1)
    sr_std = sr_variance**0.5
    # num_trials=1 deja el benchmark en 0: Probabilistic Sharpe Ratio puro
    # contra un Sharpe nulo, sin ajuste por multiples pruebas.
    expected = _stats.NormalDist().cdf(sr_hat / sr_std) * 100

    result = _deflated_sharpe_ratio_pct(daily_returns, num_trials=1)
    assert result == pytest.approx(expected)


def test_deflated_sharpe_ratio_pct_decreases_as_num_trials_increases():
    # Mas variantes probadas -> el liston de comparacion (benchmark_sr, el
    # maximo esperado entre num_trials sharpes con skill verdadero cero) sube,
    # asi que la misma performance observada se vuelve menos creible.
    from app.backtest import _deflated_sharpe_ratio_pct

    daily_returns = [0.01, -0.005, 0.02, -0.01, 0.015, 0.0, 0.008, -0.003]
    dsr_one_trial = _deflated_sharpe_ratio_pct(daily_returns, num_trials=1)
    dsr_many_trials = _deflated_sharpe_ratio_pct(daily_returns, num_trials=50)
    assert dsr_many_trials < dsr_one_trial


def test_deflated_sharpe_ratio_pct_is_none_when_returns_have_no_variation():
    # Desvio poblacional 0 (todos los retornos diarios iguales): el cociente
    # sr_hat = mean/std no esta definido, mismo criterio de "no representable"
    # que sharpe_ratio.
    from app.backtest import _deflated_sharpe_ratio_pct

    result = _deflated_sharpe_ratio_pct([0.01, 0.01, 0.01, 0.01], num_trials=1)
    assert result is None


def test_backtest_summary_includes_deflated_sharpe_ratio_pct_when_enough_trades(patched_market_data):
    config = ScreenerConfig(universe=["MOM", "FLAT"], benchmark_symbol="SPY", backtest_years=1)
    summary = run_backtest(config)
    assert summary.total_trades >= 2
    assert summary.deflated_sharpe_ratio_pct is not None


def test_deflated_sharpe_ratio_pct_responds_to_deflated_sharpe_num_trials_config():
    # Confirma que _compute_summary_stats efectivamente usa el parametro (y no
    # solo lo recibe sin pasarlo a _deflated_sharpe_ratio_pct).
    from app.backtest import _compute_summary_stats

    returns_pct = [2.0, -1.0, 3.0, -0.5, 1.5, 2.5, -1.5, 1.0]
    trades = [_trade(f"S{i}", i + 1, i + 2, return_pct=r) for i, r in enumerate(returns_pct)]

    summary_one_trial = _compute_summary_stats(
        trades, top_n=10, bench_bars=_bench_bars_for_stats(), deflated_sharpe_num_trials=1
    )
    summary_many_trials = _compute_summary_stats(
        trades, top_n=10, bench_bars=_bench_bars_for_stats(), deflated_sharpe_num_trials=50
    )
    assert summary_one_trial.deflated_sharpe_ratio_pct is not None
    assert summary_many_trials.deflated_sharpe_ratio_pct is not None
    assert summary_many_trials.deflated_sharpe_ratio_pct < summary_one_trial.deflated_sharpe_ratio_pct


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


def test_trade_weights_uniform_when_disabled():
    from app.backtest import _trade_weights
    trades = [_trade("A", 1, 2, entry_atr_pct=2.0), _trade("B", 1, 2, entry_atr_pct=8.0)]
    weights = _trade_weights(trades, top_n=2, vol_weighting_enabled=False)
    assert weights == {id(trades[0]): 0.5, id(trades[1]): 0.5}


def test_trade_weights_uniform_when_atr_missing():
    from app.backtest import _trade_weights
    # Vol-weighting activo pero falta el ATR de UNA operacion (ej. trade
    # sintetico de test, o dato historico de antes de este campo): se cae a
    # equiponderar TODO el conjunto en vez de mezclar criterios.
    trades = [_trade("A", 1, 2, entry_atr_pct=2.0), _trade("B", 1, 2, entry_atr_pct=None)]
    weights = _trade_weights(trades, top_n=2, vol_weighting_enabled=True)
    assert weights == {id(trades[0]): 0.5, id(trades[1]): 0.5}


def test_trade_weights_inversely_proportional_to_atr_when_enabled():
    from app.backtest import _trade_weights
    # Razon de ATR 1:2 (2.0 vs 4.0) da multiplicadores 4/3 y 2/3, ambos dentro
    # de [0.5, 2.0] (sin clipping): el de menor ATR pesa el doble que el de
    # mayor ATR, la misma relacion inversa que sus ATR.
    trades = [_trade("A", 1, 2, entry_atr_pct=2.0), _trade("B", 1, 2, entry_atr_pct=4.0)]
    weights = _trade_weights(trades, top_n=2, vol_weighting_enabled=True)
    assert weights[id(trades[0])] == pytest.approx(2 / 3)
    assert weights[id(trades[1])] == pytest.approx(1 / 3)
    assert weights[id(trades[0])] == pytest.approx(2 * weights[id(trades[1])])


def test_trade_weights_clips_extreme_atr_ratio():
    from app.backtest import _trade_weights
    # Razon de ATR 1:4 (2.0 vs 8.0) implicaria un multiplicador 1.6/0.4 sin
    # acotar; 0.4 esta fuera de [0.5, 2.0] y se acota a 0.5 (ver
    # _trade_weights) para que el simbolo de menor ATR no domine la cartera.
    trades = [_trade("A", 1, 2, entry_atr_pct=2.0), _trade("B", 1, 2, entry_atr_pct=8.0)]
    weights = _trade_weights(trades, top_n=2, vol_weighting_enabled=True)
    assert weights[id(trades[0])] == pytest.approx(0.8)
    assert weights[id(trades[1])] == pytest.approx(0.25)


def test_vol_weighting_overweights_lower_volatility_winner_in_summary_stats():
    from app.backtest import _compute_summary_stats
    # A (ATR bajo, ganadora +10%) y B (ATR alto, perdedora -10%) cierran el
    # mismo dia: equiponderado el resultado neto es levemente negativo (los
    # pesos iguales no compensan exactamente por el efecto multiplicativo del
    # compounding), pero con vol-weighting activo A pesa mas que B (0.8 vs
    # 0.25, ver test_trade_weights_clips_extreme_atr_ratio) y el resultado
    # neto pasa a ser claramente positivo -- la misma mecanica de
    # _trade_weights, ahora a traves de la curva de equity completa.
    trades = [
        _trade("A", 1, 2, return_pct=10.0, entry_atr_pct=2.0),
        _trade("B", 1, 2, return_pct=-10.0, entry_atr_pct=8.0),
    ]
    bench_bars = _bench_bars_for_stats()
    equal = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars)
    weighted = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars, vol_weighting_enabled=True)
    assert equal.strategy_cumulative_return_pct == pytest.approx(-0.25)
    assert weighted.strategy_cumulative_return_pct == pytest.approx(5.3)


def test_simulate_symbol_populates_stop_loss_pct_from_entry_atr():
    # Mismo escenario que test_stop_loss_triggers_on_intraday_low_not_close:
    # stop_loss_pct debe quedar en stop_loss_atr_multiplier * entry_atr_pct,
    # calculado una sola vez al momento de la entrada.
    from app.backtest import _simulate_symbol

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)

    cfg = ScreenerConfig(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.5,
        max_holding_days=10,
        regime_filter_enabled=False,
        near_high_filter_enabled=False,
        top_n=10,
    )
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades[0].exit_reason == "max_holding_days"
    assert trades[0].entry_atr_pct is not None
    # rel=1e-3 (no la tolerancia absoluta por default de pytest.approx, mas
    # estricta): stop_loss_pct se deriva directo de la distancia real
    # entrada->stop (ver _simulate_symbol), sin pasar por el redondeo
    # intermedio a 4 decimales de entry_atr_pct que esta formula si tiene --
    # sin tensado de por medio ambas son equivalentes salvo ese redondeo.
    assert trades[0].stop_loss_pct == pytest.approx(
        cfg.stop_loss_atr_multiplier * trades[0].entry_atr_pct, rel=1e-3
    )


def test_simulate_symbol_tightens_stop_when_rules_config_caps_max_stop_loss_pct():
    # Mismo escenario que el test anterior (stop implicado por el ATR ~2.8%),
    # pero con un RulesConfig cuyo max_stop_loss_pct (1.0%) es mas estricto:
    # replica el mismo ajuste que _try_auto_trade_entry hace en vivo -- el
    # stop se tensa al tope en vez de simular la entrada con el stop ancho
    # original (ver fix de paridad backtest/vivo para el auto-ajuste de
    # stop-loss).
    from app.backtest import _simulate_symbol
    from app.rules import RulesConfig

    closes = [100.0 + i for i in range(30)]
    bars = _bars(closes)

    cfg = ScreenerConfig(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,
        atr_period=3,
        stop_loss_atr_multiplier=1.5,
        max_holding_days=10,
        regime_filter_enabled=False,
        near_high_filter_enabled=False,
        top_n=10,
    )
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)
    rules_config = RulesConfig(max_stop_loss_pct=1.0)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok, rules_config=rules_config)

    assert trades[0].stop_loss_pct == pytest.approx(1.0, rel=1e-3)


def test_simulate_symbol_blocks_entry_when_trend_fails_despite_high_score():
    # Serie estrictamente decreciente: la SMA rapida queda por debajo de la
    # lenta y el precio por debajo de ambas, asi que trend_ok es SIEMPRE
    # falso -- aunque score_series este muy por encima del umbral de entrada
    # todos los dias (simulando un simbolo que rankea excelente en el resto
    # de los componentes del score), no debe generarse ninguna operacion. Sin
    # este gate, el backtest simularia una entrada que el motor de auto-
    # trading en vivo jamas tomaria (ver docstring de _simulate_symbol).
    from app.backtest import _simulate_symbol

    closes = [200.0 - i for i in range(60)]
    bars = _bars(closes)

    cfg = ScreenerConfig(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
        rsi_period=3,
        rsi_min=0,
        rsi_max=100,  # RSI bien abierto: aislar el gate de tendencia, no el de RSI
        atr_period=3,
        stop_loss_atr_multiplier=1.5,
        max_holding_days=10,
        regime_filter_enabled=False,
        near_high_filter_enabled=False,
        top_n=10,
    )
    score_series = pd.Series(1000.0, index=bars.index)
    regime_ok = pd.Series(True, index=bars.index)

    trades = _simulate_symbol("MOM", bars, cfg, score_series, regime_ok)

    assert trades == []


def test_simulate_symbol_opportunistic_populates_stop_loss_pct_from_entry_atr():
    # Mismo escenario que test_opportunistic_stop_loss_triggers_on_intraday_
    # low_not_close: stop_loss_pct debe usar opp.stop_loss_atr_multiplier (no
    # cfg.stop_loss_atr_multiplier, que es el de Momentum).
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_uptrend_bars()
    bars.loc[bars.index[264], "Low"] = 50.0
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_entry_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series, pd.Series(True, index=bars.index))

    assert trades[0].entry_atr_pct is not None
    # rel=1e-3: ver el mismo comentario en test_simulate_symbol_populates_
    # stop_loss_pct_from_entry_atr (Momentum) sobre el redondeo intermedio.
    assert trades[0].stop_loss_pct == pytest.approx(
        opp.stop_loss_atr_multiplier * trades[0].entry_atr_pct, rel=1e-3
    )


def test_simulate_symbol_opportunistic_tightens_stop_when_rules_config_caps_max_stop_loss_pct():
    # Mismo escenario que el test anterior (stop implicado por el ATR
    # ~3.5%), pero con un RulesConfig cuyo max_stop_loss_pct (1.0%) es mas
    # estricto: replica el mismo ajuste que _try_auto_trade_entry hace en
    # vivo para CUALQUIER estrategia, Oportunista incluida.
    from app.backtest import _simulate_symbol_opportunistic
    from app.rules import RulesConfig

    bars = _opportunistic_uptrend_bars()
    bars.loc[bars.index[264], "Low"] = 50.0
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_entry_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1
    rules_config = RulesConfig(max_stop_loss_pct=1.0)

    trades = _simulate_symbol_opportunistic(
        "OPP", bars, cfg, score_series, pd.Series(True, index=bars.index), rules_config=rules_config
    )

    assert trades[0].stop_loss_pct == pytest.approx(1.0, rel=1e-3)


def test_risk_based_trade_weight_dominated_by_risk_formula():
    from app.backtest import _risk_based_trade_weight
    from app.rules import RulesConfig

    # weight_by_risk = 1/5 = 0.2, bien por debajo de los otros dos clamps
    # (0.5 y 10): gana el formula de riesgo.
    rules_config = RulesConfig(risk_per_trade_pct=1, max_position_pct_of_equity=50, max_order_value_usd=1_000_000)
    trade = _trade("A", 1, 2, stop_loss_pct=5.0)
    weight = _risk_based_trade_weight(trade, prior_equity_factor=1.0, assumed_capital_usd=100_000, rules_config=rules_config)
    assert weight == pytest.approx(0.2)


def test_risk_based_trade_weight_dominated_by_max_position_pct():
    from app.backtest import _risk_based_trade_weight
    from app.rules import RulesConfig

    # weight_by_risk = 10/2 = 5.0 y weight_by_order_value = 10, ambos muy por
    # encima de max_position_pct_of_equity/100 = 0.08: gana ese clamp.
    rules_config = RulesConfig(risk_per_trade_pct=10, max_position_pct_of_equity=8, max_order_value_usd=1_000_000)
    trade = _trade("A", 1, 2, stop_loss_pct=2.0)
    weight = _risk_based_trade_weight(trade, prior_equity_factor=1.0, assumed_capital_usd=100_000, rules_config=rules_config)
    assert weight == pytest.approx(0.08)


def test_risk_based_trade_weight_dominated_by_max_order_value():
    from app.backtest import _risk_based_trade_weight
    from app.rules import RulesConfig

    # weight_by_risk = 5.0 y weight_by_position_pct = 0.5 quedan muy por
    # encima de max_order_value_usd/equity_usd = 5000/100_000 = 0.05: gana
    # ese clamp.
    rules_config = RulesConfig(risk_per_trade_pct=10, max_position_pct_of_equity=50, max_order_value_usd=5_000)
    trade = _trade("A", 1, 2, stop_loss_pct=2.0)
    weight = _risk_based_trade_weight(trade, prior_equity_factor=1.0, assumed_capital_usd=100_000, rules_config=rules_config)
    assert weight == pytest.approx(0.05)


def test_risk_based_trade_weight_zero_when_equity_non_positive():
    from app.backtest import _risk_based_trade_weight
    from app.rules import RulesConfig

    rules_config = RulesConfig()
    trade = _trade("A", 1, 2, stop_loss_pct=5.0)
    weight = _risk_based_trade_weight(trade, prior_equity_factor=0.0, assumed_capital_usd=100_000, rules_config=rules_config)
    assert weight == 0.0


def test_risk_based_trade_weight_zero_when_stop_loss_pct_missing():
    from app.backtest import _risk_based_trade_weight
    from app.rules import RulesConfig

    rules_config = RulesConfig()
    trade = _trade("A", 1, 2, stop_loss_pct=None)
    weight = _risk_based_trade_weight(trade, prior_equity_factor=1.0, assumed_capital_usd=100_000, rules_config=rules_config)
    assert weight == 0.0


def test_risk_based_sizing_falls_back_to_equal_weight_when_stop_loss_pct_missing():
    from app.backtest import _compute_summary_stats
    from app.rules import RulesConfig

    # Una de las dos operaciones no tiene stop_loss_pct (ej. trade sintetico
    # o dato historico previo a este campo): todo el conjunto cae a
    # equiponderado, misma filosofia "todo o nada" que vol_weighting_enabled
    # con entry_atr_pct faltante.
    trades = [
        _trade("A", 1, 2, return_pct=10.0, stop_loss_pct=5.0),
        _trade("B", 1, 2, return_pct=-10.0, stop_loss_pct=None),
    ]
    bench_bars = _bench_bars_for_stats()
    rules_config = RulesConfig()
    equal = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars)
    fallback = _compute_summary_stats(
        trades, top_n=2, bench_bars=bench_bars,
        risk_based_sizing_enabled=True, rules_config=rules_config,
    )
    assert fallback.strategy_cumulative_return_pct == pytest.approx(equal.strategy_cumulative_return_pct)


def test_risk_based_sizing_changes_equity_curve_vs_equal_weight():
    from app.backtest import _compute_summary_stats
    from app.rules import RulesConfig

    # A tiene un stop angosto (5%) y B uno 4 veces mas ancho (20%): con
    # sizing por riesgo A queda acotado por max_position_pct_of_equity (10%
    # de equity) y B por la formula de riesgo (1%/20% = 5% de equity) -- ya
    # no equiponderan 50/50 como con el sizing por defecto.
    trades = [
        _trade("A", 1, 2, return_pct=8.0, stop_loss_pct=5.0),
        _trade("B", 1, 2, return_pct=8.0, stop_loss_pct=20.0),
    ]
    bench_bars = _bench_bars_for_stats()
    rules_config = RulesConfig(risk_per_trade_pct=1, max_position_pct_of_equity=10, max_order_value_usd=1_000_000)
    equal = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars)
    risk_based = _compute_summary_stats(
        trades, top_n=2, bench_bars=bench_bars,
        risk_based_sizing_enabled=True, rules_config=rules_config, assumed_capital_usd=100_000,
    )
    assert equal.strategy_cumulative_return_pct == pytest.approx(8.16)
    assert risk_based.strategy_cumulative_return_pct == pytest.approx(1.2)
    assert risk_based.strategy_cumulative_return_pct != equal.strategy_cumulative_return_pct


def test_trade_alpha_pct_subtracts_benchmark_return_over_same_window():
    from app.backtest import _trade_alpha_pct
    # Benchmark Jan1->Jan11 (ver _bench_bars_2024): 100->110, +10%. La
    # operacion cubre exactamente esa misma ventana, asi que el alpha es
    # simplemente su propio retorno menos ese +10%.
    bench_bars = _bench_bars_2024(n_days=11)
    trade = _trade("A", 1, 11, return_pct=15.0)
    assert _trade_alpha_pct(trade, bench_bars["Close"]) == 5.0


def test_trade_alpha_pct_returns_none_when_benchmark_predates_entry():
    from app.backtest import _trade_alpha_pct
    # El benchmark recien arranca Jan5: una operacion que entro y salio antes
    # de eso (Jan1/Jan3) no tiene ningun precio de benchmark conocido en o
    # antes de su entry_date (asof devuelve NaN), asi que el alpha de esa
    # operacion queda indefinido en vez de un 0% o un error.
    bench_bars = _bench_bars_2024(n_days=7, start="2024-01-05")
    trade = _trade("A", 1, 3, return_pct=8.0)
    assert _trade_alpha_pct(trade, bench_bars["Close"]) is None


def test_trade_alpha_pct_returns_none_when_benchmark_entry_price_is_zero():
    from app.backtest import _trade_alpha_pct
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    bench_close = pd.Series([0.0, 1.0, 2.0], index=idx)
    trade = _trade("A", 1, 3, return_pct=5.0)
    assert _trade_alpha_pct(trade, bench_close) is None


def test_avg_alpha_pct_averages_only_trades_with_defined_alpha():
    from app.backtest import _compute_summary_stats
    # B opera Jan1-Jan3, antes de que arranque la historia del benchmark
    # (Jan5): su alpha queda indefinido (ver test de _trade_alpha_pct de
    # arriba) y no debe entrar al promedio. A opera Jan5-Jan11, dentro de la
    # ventana del benchmark (100->106, +6%): su alpha es 20 - 6 = 14.0, y debe
    # ser el unico valor que compone avg_alpha_pct.
    bench_bars = _bench_bars_2024(n_days=7, start="2024-01-05")
    trade_without_alpha = _trade("B", 1, 3, return_pct=8.0)
    trade_with_alpha = _trade("A", 5, 11, return_pct=20.0)
    summary = _compute_summary_stats(
        [trade_without_alpha, trade_with_alpha], top_n=10, bench_bars=bench_bars
    )
    assert summary.trades[0].alpha_pct is None
    assert summary.trades[1].alpha_pct == 14.0
    assert summary.avg_alpha_pct == 14.0


def test_avg_alpha_pct_is_none_when_no_trade_has_defined_alpha():
    from app.backtest import _compute_summary_stats
    # Benchmark arranca bien despues (Feb) de que cualquiera de las dos
    # operaciones (Jan1-Jan3) haya cerrado: ninguna tiene alpha definido, asi
    # que avg_alpha_pct debe ser None en vez de promediar sobre una lista
    # vacia (ZeroDivisionError) o devolver 0.0 (que significaria "empato
    # exacto con el benchmark", un dato distinto de "no se pudo calcular").
    bench_bars = _bench_bars_2024(n_days=7, start="2024-02-01")
    trades = [_trade("A", 1, 2, return_pct=5.0), _trade("B", 2, 3, return_pct=-1.0)]
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=bench_bars)
    assert all(t.alpha_pct is None for t in summary.trades)
    assert summary.avg_alpha_pct is None


def test_exposure_adjusted_benchmark_return_pct_only_counts_invested_days():
    from app.backtest import _compute_summary_stats
    # Benchmark sube +1 cada dia (100->110) en los 11 dias del calendario
    # (Jan1..Jan11). La unica operacion (top_n=1, 100% de exposicion) esta
    # abierta esos mismos 11 dias pero ya cerrada el dia de salida (Jan11): ese
    # dia no cuenta exposicion (mismo mecanismo que avg_exposure_pct, ver
    # test_avg_exposure_pct_reflects_capital_utilization), asi que el
    # benchmark ajustado por exposicion no debe capturar la ultima suba del
    # benchmark (109->110): 109/100-1 = 9.0%, no el 110/100-1 = 10.0% del
    # benchmark crudo.
    bench_bars = _bench_bars_2024(n_days=11)
    trades = [_trade("A", 1, 11, return_pct=10.0)]
    summary = _compute_summary_stats(trades, top_n=1, bench_bars=bench_bars)
    assert summary.benchmark_cumulative_return_pct == 10.0
    assert summary.exposure_adjusted_benchmark_return_pct == 9.0


def test_exposure_adjusted_benchmark_return_pct_scales_down_with_lower_exposure():
    from app.backtest import _compute_summary_stats
    # Misma ventana y benchmark que el test anterior, pero top_n=2 en vez de 1
    # (50% de exposicion mientras la operacion esta abierta, no 100%): el
    # benchmark ajustado por exposicion debe capturar menos que con 100% de
    # exposicion (9.0%, ver test anterior) pero mas que cero.
    bench_bars = _bench_bars_2024(n_days=11)
    trades = [_trade("A", 1, 11, return_pct=10.0)]
    summary = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars)
    assert 0 < summary.exposure_adjusted_benchmark_return_pct < 9.0


def test_invest_idle_cash_in_benchmark_disabled_by_default_leaves_idle_cash_flat():
    from app.backtest import _compute_summary_stats
    # Operacion sin ganancia ni perdida (return_pct=0), abierta solo el dia 1
    # (top_n=2: 50% de exposicion ese dia) y ya cerrada el dia 2 (0% de
    # exposicion ese dia, mismo mecanismo que
    # test_exposure_adjusted_benchmark_return_pct_only_counts_invested_days).
    # Con return_pct=0, cualquier movimiento en la curva de equity solo puede
    # venir del cash ocioso: por default (invest_idle_cash_in_benchmark=False,
    # ScreenerConfig.invest_idle_cash_in_benchmark) ese cash queda quieto a
    # 0%, sin importar que el benchmark suba.
    bench_bars = _bench_bars_2024(n_days=2)  # Jan1=100, Jan2=101 (+1%)
    trades = [_trade("A", 1, 2, return_pct=0.0)]
    summary = _compute_summary_stats(trades, top_n=2, bench_bars=bench_bars)
    points = {p.date: p.equity_pct for p in summary.equity_curve}
    assert points[trades[0].entry_date] == 0.0
    assert points[trades[0].exit_date] == 0.0
    assert summary.strategy_cumulative_return_pct == 0.0


def test_invest_idle_cash_in_benchmark_grows_uninvested_capital_with_benchmark():
    from app.backtest import _compute_summary_stats
    # Mismo escenario que el test anterior, pero con invest_idle_cash_in_benchmark
    # activo: el dia 2 la operacion ya cerro (0% de exposicion, 100% ocioso) y
    # ese cash ocioso debe captar el +1% que hizo el benchmark entre Jan1 y
    # Jan2 (100->101), aunque la operacion en si no haya generado nada.
    bench_bars = _bench_bars_2024(n_days=2)
    trades = [_trade("A", 1, 2, return_pct=0.0)]
    summary = _compute_summary_stats(
        trades, top_n=2, bench_bars=bench_bars, invest_idle_cash_in_benchmark=True
    )
    points = {p.date: p.equity_pct for p in summary.equity_curve}
    assert points[trades[0].entry_date] == 0.0
    assert points[trades[0].exit_date] == 1.0
    assert summary.strategy_cumulative_return_pct == 1.0


def test_exit_reason_counts_cover_all_trades_not_just_truncated_sample():
    from app.backtest import _compute_summary_stats
    # summary.trades se trunca a las ultimas 50 (ver _compute_summary_stats),
    # pero exit_reason_counts debe reflejar TODAS las operaciones: 60 en
    # total, repartidas en 3 motivos de salida, no solo las ultimas 50.
    trades = (
        [_trade(f"A{i}", 1, 2, exit_reason="stop_loss") for i in range(20)]
        + [_trade(f"B{i}", 1, 2, exit_reason="max_holding_days") for i in range(20)]
        + [_trade(f"C{i}", 1, 2, exit_reason="trend_break") for i in range(20)]
    )
    summary = _compute_summary_stats(trades, top_n=10, bench_bars=_bench_bars_for_stats(2))
    assert summary.total_trades == 60
    assert len(summary.trades) == 50
    assert summary.exit_reason_counts == {"stop_loss": 20, "max_holding_days": 20, "trend_break": 20}
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


def test_cap_concurrent_positions_sector_cap_blocks_concentrated_sector():
    from app.backtest import cap_concurrent_positions
    # AMD, NVDA e INTC son las 3 "Information Technology" (ver sectors.py):
    # se solapan completamente y top_n=3 no bloquea por cantidad total, pero
    # max_per_sector=2 si bloquea la tercera del mismo sector (el caso "10
    # semis" del audit, aca con 3).
    trades = [_trade("AMD", 1, 10), _trade("NVDA", 1, 10), _trade("INTC", 1, 10)]
    taken = cap_concurrent_positions(trades, top_n=3, max_per_sector=2)
    assert [t.symbol for t in taken] == ["AMD", "NVDA"]


def test_cap_concurrent_positions_sector_cap_allows_diversified_sectors():
    from app.backtest import cap_concurrent_positions
    # Mismo solapamiento total, pero sectores distintos (IT, Financials,
    # Health Care): con max_per_sector=1 ninguno compite por el mismo cupo
    # sectorial, asi que las 3 entran igual (el tope de sector no penaliza
    # una cartera ya diversificada).
    trades = [_trade("AMD", 1, 10), _trade("JPM", 1, 10), _trade("UNH", 1, 10)]
    taken = cap_concurrent_positions(trades, top_n=3, max_per_sector=1)
    assert [t.symbol for t in taken] == ["AMD", "JPM", "UNH"]


def test_cap_concurrent_positions_sector_cap_reuses_freed_slot():
    from app.backtest import cap_concurrent_positions
    # AMD ocupa el unico cupo IT dias 1-5; cuando cierra, INTC (que entra dia
    # 6) puede tomarlo. NVDA se solapa con AMD y queda afuera con
    # max_per_sector=1, aunque top_n=3 le sobre cupo global.
    trades = [_trade("AMD", 1, 5), _trade("NVDA", 2, 4), _trade("INTC", 6, 9)]
    taken = cap_concurrent_positions(trades, top_n=3, max_per_sector=1)
    assert [t.symbol for t in taken] == ["AMD", "INTC"]


def test_cap_concurrent_positions_no_sector_cap_when_max_per_sector_falsy():
    from app.backtest import cap_concurrent_positions
    # Sin max_per_sector (default None), el comportamiento es exactamente el
    # mismo de siempre: 3 simbolos del mismo sector, sin tope sectorial, solo
    # limitados por top_n.
    trades = [_trade("AMD", 1, 10), _trade("NVDA", 1, 10), _trade("INTC", 1, 10)]
    taken = cap_concurrent_positions(trades, top_n=3)
    assert [t.symbol for t in taken] == ["AMD", "NVDA", "INTC"]


def test_near_high_filter_reduces_entries_far_from_52w_high(monkeypatch):
    # Rampa larga (252 dias, hasta 300) que fija un maximo de 52s real (recien
    # con 252 dias previos pct_from_high deja de ser NaN, ver indicators.py),
    # seguida de una cola oscilante (150 dias) que cruza el umbral del 15%
    # por debajo de ese pico en cada ciclo: unos dias queda dentro (near_high_ok
    # verdadero) y otros mas de 15% por debajo (near_high_ok falso) -- a
    # diferencia de una cola que se quedara SIEMPRE por debajo del umbral, que
    # con el fix de la ventana de 252 dias (la simulacion ya no arranca hasta
    # tener historia real) bloquearia el 100% de las entradas con el filtro
    # prendido y la comparacion "menos entradas" nunca podria observarse (el
    # backtest no genera ninguna operacion).
    n_ramp, n_tail = 252, 150
    ramp_target, tail_mean, tail_amp, tail_period = 300, 260, 30, 10
    vals = [100 + i * (ramp_target - 100) / (n_ramp - 1) for i in range(n_ramp)]
    vals += [tail_mean + tail_amp * math.sin(i / tail_period) for i in range(n_tail)]
    idx = pd.date_range("2023-01-01", periods=n_ramp + n_tail, freq="D")
    close = pd.Series(vals, index=idx)

    # Segundo simbolo del universo (no 1): el score cross-sectional necesita
    # >=2 simbolos validos por fecha para que el percentil este definido (ver
    # _cross_sectional_score_panel). Mismo patron ramp+tail con parametros
    # distintos (no un simple desfasaje de fase) para que FARHI no empate
    # sistematicamente con su companero en cada componente del score.
    ramp_target2, tail_mean2, tail_amp2, tail_period2 = 260, 200, 25, 13
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
        momentum_short_days=3, momentum_12_1_lookback_days=5, momentum_12_1_skip_days=3,
        rsi_period=3, rsi_min=0, rsi_max=100,
        regime_filter_enabled=False, top_n=5,
    )
    without = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=False))
    with_filter = run_backtest(ScreenerConfig(**base, near_high_filter_enabled=True, max_pct_below_52w_high=15.0))
    assert with_filter.total_trades < without.total_trades


def test_near_high_filter_enabled_requires_full_252_day_window_before_simulating():
    # Regresion: pct_from_high(close, 252) es NaN hasta tener 252 dias previos
    # (ver indicators.py), y un NaN se trataba como "sin dato, no bloquea" en
    # near_high_ok -- con menos de 253 dias de historia, el filtro quedaba
    # vacuamente pasante durante TODO el backtest, dejando entrar operaciones
    # que nunca llegaron a evaluarse de verdad contra el maximo de 52 semanas.
    # Con solo 100 dias de bars (muy por debajo de los 253 que start_idx exige
    # cuando near_high_filter_enabled=True), _simulate_symbol ya no debe
    # generar ninguna operacion. Apagando el filtro (que no depende de
    # pct_from_high) la misma serie si entra, confirmando que la diferencia es
    # el gate de cercania al maximo y no algun otro filtro.
    from app.backtest import _simulate_symbol

    closes = [100.0 + i for i in range(100)]
    bars = _bars(closes)

    cfg_kwargs = dict(
        universe=["MOM"],
        benchmark_symbol="SPY",
        sma_fast=3,
        sma_slow=5,
        momentum_lookback_days=5,
        momentum_short_days=2,
        momentum_12_1_lookback_days=5,
        momentum_12_1_skip_days=2,
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

    cfg_with_filter = ScreenerConfig(**cfg_kwargs, near_high_filter_enabled=True)
    trades_with_filter = _simulate_symbol("MOM", bars, cfg_with_filter, score_series, regime_ok)
    assert trades_with_filter == []

    cfg_without_filter = ScreenerConfig(**cfg_kwargs, near_high_filter_enabled=False)
    trades_without_filter = _simulate_symbol("MOM", bars, cfg_without_filter, score_series, regime_ok)
    assert trades_without_filter != []


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


def _opportunistic_uptrend_bars(n=320, slope=0.05, volume=5_000_000):
    """Tendencia alcista suave y estrictamente monotona, pensada para tests
    unitarios de MECANICA de entrada/salida (fill, stop-loss, max_holding_days)
    donde la entrada se fuerza a mano via score_series, no por la calidad real
    de la serie. Con el incremento diario constante (sin ningun dia de
    perdida), el RSI de Wilder se satura en 100 pasado el periodo de
    calentamiento (avg_loss queda en 0 para siempre) -- los tests que usan
    esta fixture junto con los gates de calidad de _simulate_symbol_
    opportunistic (momentum_ok/rsi_ok/volatility_ok/room_to_grow_ok) necesitan
    ensanchar rsi_min/rsi_max (ver _opportunistic_cfg) para que ese gate no
    interfiera con lo que realmente estan probando."""
    i = np.arange(n)
    close_vals = 100.0 + slope * i
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    close = pd.Series(close_vals, index=idx)
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": volume},
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
    # Los nuevos gates de calidad (MACD crossover, RSI rising, SMA) también se
    # deshabilitan aquí via sentinels (0 = desactivado): la data sintética
    # lineal no tiene varianza real, y estos tests comprueban MECÁNICA de
    # entrada/salida (fill, stop, holding), no calidad de la señal.
    defaults = dict(
        momentum_lookback_days=10,
        rsi_period=14,
        rsi_min=35,
        rsi_max=60,
        min_volatility_pct=0,
        min_pct_below_52w_high=0,
        stop_loss_atr_multiplier=2.0,
        max_holding_days=30,
        macd_crossover_lookback_days=0,   # gate desactivado para data sintética
        rsi_rising_min_days=0,             # gate desactivado para data sintética
        max_volatility_pct_gate=100.0,     # siempre pasa
        max_5d_run_pct=1000.0,             # siempre pasa
        require_above_sma_period=0,        # gate desactivado para data sintética
    )
    defaults.update(overrides)
    return ScreenerConfig(
        universe=["OPP", "OPP2"], atr_period=14, opportunistic=OpportunisticConfig(**defaults)
    )


def test_opportunistic_ignores_trend_break_and_exits_by_max_holding_days():
    # _simulate_symbol_opportunistic ya no sale por score (ese umbral de
    # salida no existe en vivo, ver _check_fund_exit en main.py): score_series
    # es un insumo externo (el percentil cross-sectional, ver
    # _cross_sectional_score_panel) que solo gatilla la ENTRADA; este test
    # unitario lo arma a mano para verificar la mecanica de entrada/salida, no
    # la formula de ningun componente (eso lo cubren los tests de integracion
    # mas abajo). Senal de entrada confirmada al cierre del dia 262 (score
    # alto, se mantiene alto el resto de la serie ya que ahora es irrelevante
    # para la salida), fill al abrir el dia 263.
    #
    # Oportunista ya NO sale por ruptura de tendencia (su entrada tampoco
    # exige ninguna condicion de tendencia, ver _strategy_exit_params en
    # main.py): la caida puntual del dia 278 (por debajo de la SMA rapida de
    # ese dia, pero por encima del stop-loss) no debe cerrar la posicion. La
    # unica salida disponible en esta serie (que nunca toca el stop) es
    # max_holding_days, a los 30 dias desde la entrada (dia 263 + 30 = 293).
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_uptrend_bars()
    bars.loc[bars.index[278], ["Open", "High", "Low", "Close"]] = [111.0, 112.0, 110.0, 111.0]
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_entry_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series, pd.Series(True, index=bars.index))

    assert trades[0].entry_date == bars.index[263]
    assert trades[0].exit_reason == "max_holding_days"
    assert trades[0].exit_date == bars.index[263 + 30]


def test_opportunistic_stop_loss_triggers_on_intraday_low_not_close():
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_uptrend_bars()
    bars.loc[bars.index[264], "Low"] = 50.0  # mecha intradiaria el dia siguiente al fill, perfora el stop sin que el cierre lo refleje
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    # Score alto desde el dia de la senal (262) en adelante: la tendencia de
    # bars nunca rompe (sube monotonamente, sin la caida puntual de los dos
    # tests anteriores), asi que la unica salida posible es el stop-loss.
    score_series = pd.Series(opp.backtest_score_entry_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series, pd.Series(True, index=bars.index))

    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].exit_date == bars.index[264]
    assert bars["Close"].iloc[264] > trades[0].exit_price


def test_opportunistic_liquidity_filter_blocks_entry_for_low_volume_symbol():
    # Mismo score que los tests anteriores (entra desde el dia 262 en
    # adelante), pero con un volumen de 1_000 acciones en vez de los
    # 5_000_000 por default: el volumen promedio en dolares de 20 dias queda
    # muy por debajo de min_avg_dollar_volume (1_000_000 por default), igual
    # que el gate de liquidez de Momentum (ver dollar_volume_s en
    # _simulate_symbol_opportunistic). Sin este gate la misma configuracion
    # si entra (ver los tests anteriores, que usan el volumen por default).
    from app.backtest import _simulate_symbol_opportunistic

    bars = _opportunistic_uptrend_bars(volume=1_000)
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_entry_threshold - 1, index=bars.index)
    score_series.iloc[262:] = opp.backtest_score_entry_threshold + 1

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series, pd.Series(True, index=bars.index))

    assert trades == []


def test_opportunistic_blocks_entry_when_momentum_fails_despite_high_score():
    # Serie estrictamente decreciente: el retorno reciente (momentum_ok) es
    # siempre negativo, asi que aunque score_series este muy por encima del
    # umbral de entrada (simulando un simbolo que rankea excelente en el
    # resto de los componentes del score), no debe generarse ninguna
    # operacion -- Oportunista exige giro al alza (momentum corto positivo),
    # no una caida sostenida (ver docstring de _simulate_symbol_opportunistic).
    from app.backtest import _simulate_symbol_opportunistic

    idx = pd.date_range("2021-01-01", periods=320, freq="D")
    close = pd.Series([200.0 - 0.1 * i for i in range(320)], index=idx)
    bars = pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 5_000_000}, index=idx
    )
    cfg = _opportunistic_cfg(max_holding_days=30, rsi_min=0, rsi_max=100)
    opp = cfg.opportunistic

    score_series = pd.Series(opp.backtest_score_entry_threshold + 1, index=bars.index)

    trades = _simulate_symbol_opportunistic("OPP", bars, cfg, score_series, pd.Series(True, index=bars.index))

    assert trades == []


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
    # opportunistic_regime_filter_enabled ya es False por defecto, asi que no
    # haria falta pasarlo, pero se deja explicito: el benchmark sintetico de
    # este test es plano (SMA de regimen sin pendiente y momentum absoluto en
    # 0%), lo que bloquearia TODA entrada si este flag estuviera en True (ver
    # indicators.market_regime_ok) -- irrelevante para lo que este test
    # verifica (metricas del backtest de Oportunista).
    config = config.model_copy(
        update={"benchmark_symbol": "SPY", "backtest_years": 1, "opportunistic_regime_filter_enabled": False}
    )

    summary = backtest_module.run_opportunistic_backtest(config)

    assert summary.total_trades > 0
    assert all(t.symbol in ("OPP", "OPP2") for t in summary.trades)
    assert 0 <= summary.win_rate_pct <= 100
    assert summary.start_date < summary.end_date


def test_opportunistic_backtest_skips_throttle_for_already_cached_symbol(monkeypatch):
    # Mismo fix que test_backtest_skips_throttle_for_already_cached_symbol,
    # aplicado al loop de fetch de Oportunista (_collect_opportunistic_trades
    # tiene su propio loop, separado del de Momentum).
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
    sleep_calls: list[float] = []
    monkeypatch.setattr(backtest_module.time, "sleep", lambda secs: sleep_calls.append(secs))
    monkeypatch.setattr(backtest_module, "is_bars_cached", lambda symbol, lookback_days: symbol == "OPP2")
    config = _opportunistic_cfg().model_copy(
        update={
            "benchmark_symbol": "SPY",
            "backtest_years": 1,
            "opportunistic_regime_filter_enabled": False,
            "scan_request_delay_seconds": 0.25,
        }
    )

    backtest_module.run_opportunistic_backtest(config)

    assert sleep_calls == []


def test_opportunistic_backtest_never_requests_market_data_for_growth_tickers(monkeypatch):
    # Mismo sesgo de look-ahead de inclusion que test_backtest_never_requests_
    # market_data_for_growth_tickers (ver ese comentario), aplicado al
    # backtest de Oportunista.
    bars = _opportunistic_oscillating_bars()
    bench_bars = _bars([100.0] * len(bars))
    buddy_bars = _opportunistic_buddy_bars(len(bars))
    growth_symbol = GROWTH_TICKERS[0]

    requested = []

    def fake_get_daily_bars(symbol, lookback_days):
        requested.append(symbol)
        if symbol == "SPY":
            return bench_bars
        if symbol == "OPP":
            return bars
        if symbol == "OPP2":
            return buddy_bars
        raise MarketDataError("no data")

    monkeypatch.setattr(backtest_module, "get_daily_bars", fake_get_daily_bars)
    config = _opportunistic_cfg()
    # opportunistic_regime_filter_enabled=False: ver comentario equivalente en
    # test_opportunistic_backtest_produces_trades_and_metrics (benchmark
    # sintetico plano, irrelevante para lo que este test verifica).
    config = config.model_copy(
        update={
            "benchmark_symbol": "SPY",
            "backtest_years": 1,
            "universe": ["OPP", "OPP2", growth_symbol],
            "opportunistic_regime_filter_enabled": False,
        }
    )

    summary = backtest_module.run_opportunistic_backtest(config)

    assert growth_symbol not in requested
    assert summary.total_trades > 0


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


def _bench_bars_2024(n_days=11, start="2024-01-01"):
    idx = pd.date_range(start, periods=n_days, freq="D")
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
    # opportunistic_regime_filter_enabled=False: ver comentario equivalente en
    # test_opportunistic_backtest_produces_trades_and_metrics (benchmark
    # sintetico plano, irrelevante para lo que este test verifica).
    config = config.model_copy(
        update={"benchmark_symbol": "SPY", "backtest_years": 1, "opportunistic_regime_filter_enabled": False}
    )

    full_summary = backtest_module.run_opportunistic_backtest(config)
    result = backtest_module.run_opportunistic_backtest_walk_forward(config, n_folds=3)

    assert result.n_folds == 3
    assert len(result.folds) == 3
    assert sum(f.total_trades for f in result.folds) == full_summary.total_trades
