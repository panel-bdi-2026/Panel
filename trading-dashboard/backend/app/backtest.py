from __future__ import annotations

import pandas as pd

from .indicators import atr, rate_of_change, rsi, sma
from .market_data import MarketDataError, get_daily_bars
from .models import BacktestSummary, BacktestTrade
from .screener_config import ScreenerConfig


class BacktestError(RuntimeError):
    pass


def _simulate_symbol(
    symbol: str, bars: pd.DataFrame, cfg: ScreenerConfig, benchmark_roc: pd.Series
) -> list[BacktestTrade]:
    """Simula la misma logica de entrada/salida del screener sobre historia.

    Entra cuando se cumplen los mismos filtros que en `scan()` (tendencia,
    momentum, RSI, fuerza relativa vs benchmark). Sale por stop-loss (basado en
    ATR, igual que la sugerencia en vivo), por ruptura de tendencia, o por
    tiempo maximo en la posicion. Una sola posicion por simbolo a la vez.
    """
    close = bars["Close"]
    sma_fast_s = sma(close, cfg.sma_fast)
    sma_slow_s = sma(close, cfg.sma_slow)
    roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
    roc_1m = rate_of_change(close, cfg.momentum_short_days)
    rsi_s = rsi(close, cfg.rsi_period)
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)

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

        if in_position:
            held_days = i - entry_idx
            hit_stop = price <= stop_price
            timed_out = held_days >= cfg.max_holding_days
            trend_broke = price < sma_fast_s.iloc[i]
            if hit_stop or timed_out or trend_broke:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                ret_pct = (price - entry_price) / entry_price * 100
                trades.append(BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=date,
                    entry_price=round(entry_price, 2),
                    exit_price=round(price, 2),
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

        if trend_ok and rsi_ok and rel_strength_ok and momentum_ok:
            in_position = True
            entry_price = price
            entry_idx = i
            entry_date = date
            stop_price = price - cfg.stop_loss_atr_multiplier * atr_s.iloc[i]

    return trades


def run_backtest(cfg: ScreenerConfig) -> BacktestSummary:
    """Backtest simplificado de la estrategia momentum sobre el universo configurado.

    Simplificaciones explicitas (no es un backtester de produccion): no modela
    comisiones ni slippage, y la curva de equity asume que cada operacion ocupa
    1/top_n del capital de forma secuencial (no rastrea solapamiento real de
    posiciones concurrentes). Sirve para validar la direccion de la idea antes
    de arriesgar capital real, no como promesa de resultados futuros.
    """
    history_days = int(cfg.backtest_years * 365)

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    benchmark_roc = rate_of_change(bench_bars["Close"], cfg.momentum_lookback_days)

    all_trades: list[BacktestTrade] = []
    for symbol in cfg.universe:
        try:
            bars = get_daily_bars(symbol, history_days)
        except MarketDataError:
            continue
        if len(bars) < cfg.sma_slow + cfg.momentum_lookback_days:
            continue
        aligned_bench_roc = benchmark_roc.reindex(bars.index, method="ffill")
        all_trades.extend(_simulate_symbol(symbol, bars, cfg, aligned_bench_roc))

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)

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
        trades=all_trades[-50:],
    )
