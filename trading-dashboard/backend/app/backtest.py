from __future__ import annotations

import statistics

import pandas as pd

from .indicators import atr, rate_of_change, rsi, sma
from .market_data import MarketDataError, get_daily_bars
from .models import BacktestSummary, BacktestTrade
from .screener_config import ScreenerConfig


class BacktestError(RuntimeError):
    pass


def _simulate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    cfg: ScreenerConfig,
    benchmark_roc: pd.Series,
    benchmark_regime_ok: pd.Series,
) -> list[BacktestTrade]:
    """Simula la misma logica de entrada/salida del screener sobre historia.

    Entra cuando se cumplen los mismos filtros que en `scan()` (tendencia,
    momentum, RSI, fuerza relativa vs benchmark, regimen de mercado). Sale por
    stop-loss (basado en ATR, igual que la sugerencia en vivo), por ruptura de
    tendencia, o por tiempo maximo en la posicion. Una sola posicion por
    simbolo a la vez.

    El stop-loss se chequea contra el minimo intradiario (no el cierre): si el
    precio perfora el stop durante el dia, en la realidad se sale ahi (o peor,
    si abre con un gap por debajo del stop), no se espera al cierre. Tambien
    se restan comision y slippage estimados, para no inflar los retornos
    respecto a la operatoria real.
    """
    close = bars["Close"]
    sma_fast_s = sma(close, cfg.sma_fast)
    sma_slow_s = sma(close, cfg.sma_slow)
    roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
    roc_1m = rate_of_change(close, cfg.momentum_short_days)
    rsi_s = rsi(close, cfg.rsi_period)
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)

    notional_per_trade = cfg.backtest_assumed_capital_usd / cfg.top_n if cfg.top_n else 0.0

    trades: list[BacktestTrade] = []
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_price = 0.0

    start_idx = max(cfg.sma_slow, cfg.momentum_lookback_days, cfg.atr_period) + 1
    for i in range(start_idx, len(bars)):
        date = bars.index[i]
        price = float(close.iloc[i])
        low_price = float(bars["Low"].iloc[i])
        open_price = float(bars["Open"].iloc[i])

        if in_position:
            held_days = i - entry_idx
            hit_stop = low_price <= stop_price
            timed_out = held_days >= cfg.max_holding_days
            trend_broke = price < sma_fast_s.iloc[i]
            if hit_stop or timed_out or trend_broke:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                # Si hubo gap por debajo del stop, el fill realista es el open
                # (peor que el stop); si no, se asume fill al precio del stop.
                raw_exit_price = min(open_price, stop_price) if hit_stop else price

                entry_fill = entry_price * (1 + cfg.slippage_pct / 100)
                exit_fill = raw_exit_price * (1 - cfg.slippage_pct / 100)
                commission_pct = (
                    (2 * cfg.commission_per_trade_usd / notional_per_trade) * 100
                    if notional_per_trade
                    else 0.0
                )
                ret_pct = (exit_fill - entry_fill) / entry_fill * 100 - commission_pct

                trades.append(BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=date,
                    entry_price=round(entry_price, 2),
                    exit_price=round(raw_exit_price, 2),
                    return_pct=round(ret_pct, 2),
                    exit_reason=exit_reason,
                ))
                in_position = False
            continue

        if pd.isna(sma_slow_s.iloc[i]) or pd.isna(atr_s.iloc[i]) or pd.isna(roc_3m.iloc[i]):
            continue

        trend_ok = price > sma_fast_s.iloc[i] > sma_slow_s.iloc[i]
        rsi_ok = cfg.rsi_min <= rsi_s.iloc[i] <= cfg.rsi_max
        bench_roc = benchmark_roc.iloc[i] if i < len(benchmark_roc) and not pd.isna(benchmark_roc.iloc[i]) else None
        rel_strength_ok = bench_roc is None or roc_3m.iloc[i] > bench_roc
        momentum_ok = roc_3m.iloc[i] > 0 and (pd.isna(roc_1m.iloc[i]) or roc_1m.iloc[i] > 0)
        regime_ok = bool(benchmark_regime_ok.iloc[i]) if i < len(benchmark_regime_ok) else True

        if trend_ok and rsi_ok and rel_strength_ok and momentum_ok and regime_ok:
            in_position = True
            entry_price = price
            entry_idx = i
            entry_date = date
            stop_price = price - cfg.stop_loss_atr_multiplier * atr_s.iloc[i]

    return trades


def cap_concurrent_positions(trades: list[BacktestTrade], top_n: int) -> list[BacktestTrade]:
    """Filtra una lista de operaciones a un maximo de top_n posiciones abiertas
    a la vez, recorriendolas por fecha de entrada y descartando las que no
    tendrian cupo libre (como en la operatoria real, donde no se pueden tener
    mas de top_n posiciones simultaneas). Sin top_n valido devuelve la lista
    intacta. Se asume que `trades` ya viene ordenada por fecha de entrada."""
    if not top_n or top_n <= 0:
        return list(trades)
    taken: list[BacktestTrade] = []
    open_exit_dates: list = []
    for t in trades:
        open_exit_dates = [d for d in open_exit_dates if d > t.entry_date]
        if len(open_exit_dates) >= top_n:
            continue  # sin cupo libre: en la realidad no se podria abrir
        taken.append(t)
        open_exit_dates.append(t.exit_date)
    return taken


def run_backtest(cfg: ScreenerConfig) -> BacktestSummary:
    """Backtest simplificado de la estrategia momentum sobre el universo configurado.

    Simplificaciones explicitas que siguen sin modelarse (no es un backtester
    de produccion): la curva de equity asume que cada operacion ocupa 1/top_n
    del capital de forma secuencial (no rastrea solapamiento real de
    posiciones concurrentes), y el sharpe_ratio se aproxima a partir de los
    retornos por operacion (no de una curva de equity diaria), asi que no es
    comparable 1:1 con un Sharpe calculado sobre retornos diarios. Si modela
    comision/slippage estimados y un fill de stop-loss realista (minimo
    intradiario, no el cierre). Sirve para validar la direccion de la idea
    antes de arriesgar capital real, no como promesa de resultados futuros.
    """
    history_days = int(cfg.backtest_years * 365)

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    benchmark_roc = rate_of_change(bench_bars["Close"], cfg.momentum_lookback_days)

    if cfg.regime_filter_enabled:
        bench_regime_sma = sma(bench_bars["Close"], cfg.regime_sma_period)
        benchmark_regime_ok = (bench_bars["Close"] > bench_regime_sma) | bench_regime_sma.isna()
    else:
        benchmark_regime_ok = pd.Series(True, index=bench_bars.index)

    all_trades: list[BacktestTrade] = []
    for symbol in cfg.universe:
        try:
            bars = get_daily_bars(symbol, history_days)
        except MarketDataError:
            continue
        if len(bars) < cfg.sma_slow + cfg.momentum_lookback_days:
            continue
        aligned_bench_roc = benchmark_roc.reindex(bars.index, method="ffill")
        aligned_regime_ok = benchmark_regime_ok.reindex(bars.index, method="ffill").fillna(True)
        all_trades.extend(_simulate_symbol(symbol, bars, cfg, aligned_bench_roc, aligned_regime_ok))

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)

    # Cartera con top_n cupos concurrentes: antes se contaba CADA señal de cada
    # simbolo como una operacion, pero la curva de equity ponderaba cada una
    # como 1/top_n. Con mas de top_n posiciones abiertas a la vez eso
    # subrepresentaba el capital realmente usado e inflaba el retorno. Ahora se
    # descartan las operaciones que no tendrian cupo libre (ver
    # cap_concurrent_positions), igual que en la operatoria real.
    all_trades = cap_concurrent_positions(all_trades, cfg.top_n)

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    returns = [t.return_pct for t in all_trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    win_rate = len(wins) / len(returns) * 100
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else None
    expectancy = sum(returns) / len(returns)

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for t in all_trades:
        weight = 1.0 / cfg.top_n
        equity *= 1 + weight * t.return_pct / 100
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak * 100
        max_drawdown = min(max_drawdown, drawdown)
    cumulative_return = (equity - 1) * 100

    benchmark_cumulative = float(bench_bars["Close"].iloc[-1] / bench_bars["Close"].iloc[0] - 1) * 100

    sharpe_ratio = None
    returns_decimal = [r / 100 for r in returns]
    if len(returns_decimal) >= 2:
        std_r = statistics.pstdev(returns_decimal)
        if std_r > 0:
            span_days = max((all_trades[-1].exit_date - all_trades[0].entry_date).days, 1)
            trades_per_year = len(all_trades) / (span_days / 365.25)
            sharpe_ratio = (statistics.mean(returns_decimal) / std_r) * (trades_per_year ** 0.5)

    return BacktestSummary(
        start_date=all_trades[0].entry_date,
        end_date=all_trades[-1].exit_date,
        total_trades=len(all_trades),
        win_rate_pct=round(win_rate, 1),
        avg_return_pct=round(expectancy, 2),
        avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        expectancy_pct=round(expectancy, 2),
        strategy_cumulative_return_pct=round(cumulative_return, 2),
        benchmark_cumulative_return_pct=round(benchmark_cumulative, 2),
        max_drawdown_pct=round(max_drawdown, 2),
        sharpe_ratio=round(sharpe_ratio, 2) if sharpe_ratio is not None else None,
        trades=all_trades[-50:],
    )
