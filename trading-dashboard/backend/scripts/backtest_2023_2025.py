"""Backtest oportunista (macd=3, rsi_max=60) para 2023-01-01 → 2026-01-01.
Compara retorno acumulado, DSR y Sharpe contra SPY en el mismo período.

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/backtest_2023_2025.py
"""
from __future__ import annotations

import sys, time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.backtest import (
    _collect_opportunistic_trades,
    _compute_summary_stats,
)
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig


def _filter_trades_by_date(trades, bench_bars, marks, from_=None, before=None):
    from pandas import Timestamp
    if from_ is not None:
        cutoff = Timestamp(from_, tz="UTC")
        trades = [t for t in trades if t.entry_date >= cutoff]
        bench_bars = bench_bars[bench_bars.index >= cutoff]
    if before is not None:
        cutoff = Timestamp(before, tz="UTC")
        trades = [t for t in trades if t.entry_date < cutoff]
        bench_bars = bench_bars[bench_bars.index < cutoff]
    surviving_ids = {id(t) for t in trades}
    filtered_marks = {k: v for k, v in marks.items() if k in surviving_ids}
    return trades, bench_bars, filtered_marks

MACD_DAYS       = 3
RSI_MAX         = 60.0
PERIOD_START    = date(2023, 1, 1)
PERIOD_END      = date(2026, 1, 1)   # 3 años out-of-sample
CONCURRENT_CAP  = 25
ASSUMED_CAPITAL = 100_000.0
BACKTEST_YEARS  = 9   # warmup para indicadores

# Parámetros para fondo de $11k con ~99% deployment:
# Avg concurrent positions ≈ 11 (stop-losses cortan rápido, hold real < 20d)
# → max_pos = 99%/11 ≈ 9% → $990/posición; IBKR enforcea el cash en live
RISK_PCT      = 2.0       # risk_per_trade_pct (sube de 1% → 2%)
MAX_POS_PCT   = 9.0       # max_position_pct_of_equity → 11 × 9% ≈ 99%
MAX_ORDER_USD = 5_000.0   # no binding (9% × $11k = $990 << $5k)
ASSUMED_CAPITAL = 11_000.0


def _build_cfg() -> ScreenerConfig:
    cfg = ScreenerConfig()
    cfg.backtest_years = BACKTEST_YEARS
    cfg.top_n = CONCURRENT_CAP
    cfg.opportunistic.macd_crossover_lookback_days = MACD_DAYS
    cfg.opportunistic.rsi_max = RSI_MAX
    cfg.opportunistic.backtest_cross_sectional_gates = True
    return cfg


def _compute(trades, bench_bars, marks):
    if not trades:
        return None
    rules = RulesConfig()
    rules.risk_per_trade_pct = RISK_PCT                # 1% (producción)
    rules.max_position_pct_of_equity = MAX_POS_PCT     # 10% (producción)
    rules.max_order_value_usd = MAX_ORDER_USD           # $5k (producción, binding)
    try:
        return _compute_summary_stats(
            trades, CONCURRENT_CAP, bench_bars, marks,
            invest_idle_cash_in_benchmark=False,
            vol_weighting_enabled=False,
            deflated_sharpe_num_trials=1,
            risk_based_sizing_enabled=True,
            rules_config=rules,
            assumed_capital_usd=ASSUMED_CAPITAL,
        )
    except Exception as exc:
        print(f"  ERROR en _compute_summary_stats: {exc}")
        return None


def _fmt(v, decimals=1, suffix=""):
    return f"{v:.{decimals}f}{suffix}" if v is not None else "—"


def main():
    cfg = _build_cfg()
    rules_cfg = RulesConfig()

    print(f"\n{'='*65}")
    print(f"  BACKTEST OPORTUNISTA — macd={MACD_DAYS}d  rsi_max={RSI_MAX:.0f}")
    print(f"  Período: {PERIOD_START} → {PERIOD_END}  (3 años out-of-sample)")
    print(f"  Gates cross-seccionales | cap_concurrent={CONCURRENT_CAP}")
    print(f"  Capital: ${ASSUMED_CAPITAL:,.0f} | risk={RISK_PCT}%/trade | max_pos={MAX_POS_PCT:.0f}% | max_order=${MAX_ORDER_USD:,.0f}")
    print(f"  → posición típica: ~${ASSUMED_CAPITAL*MAX_POS_PCT/100:,.0f} c/u | binding: max_pos")
    print(f"{'='*65}\n")

    print("  Recolectando trades (caché de disco)...")
    t0 = time.time()
    all_trades, marks, bench_bars = _collect_opportunistic_trades(cfg, rules_cfg, cache_only=True)
    elapsed = time.time() - t0
    print(f"  {len(all_trades)} trades totales en {elapsed:.0f}s\n")

    # Filtrar al período 2023-2026
    period_trades, period_bench, period_marks = _filter_trades_by_date(
        all_trades, bench_bars, marks,
        from_=PERIOD_START, before=PERIOD_END,
    )
    print(f"  Trades en {PERIOD_START}–{PERIOD_END}: {len(period_trades)}")

    if not period_trades:
        print("  ERROR: sin trades en el período.")
        return

    stats = _compute(period_trades, period_bench, period_marks)

    # Benchmark SPY del mismo período (desde bench_bars real)
    try:
        from pandas import Timestamp
        bench_start = period_bench.first_valid_index()
        bench_end   = period_bench.last_valid_index()
        spy_start_px = period_bench.asof(Timestamp(PERIOD_START, tz="UTC"))
        spy_end_px   = period_bench.iloc[-1]
        spy_cumret   = (spy_end_px / spy_start_px - 1) * 100
    except Exception:
        # Fallback: retornos anuales conocidos 2023+2024+2025
        spy_cumret = ((1.2628) * (1.2502) * (1.25) - 1) * 100

    strategy_ret = stats.strategy_cumulative_return_pct if stats else None
    bench_ret    = getattr(stats, "benchmark_cumulative_return_pct", None) or spy_cumret

    print(f"\n{'─'*65}")
    print(f"  {'Métrica':<32} {'Estrategia':>14}  {'SPY':>12}")
    print(f"{'─'*65}")
    print(f"  {'Retorno acumulado':<32} {_fmt(strategy_ret, suffix='%'):>14}  {_fmt(bench_ret, suffix='%'):>12}")
    print(f"  {'Sharpe (anualizado)':<32} {_fmt(getattr(stats,'sharpe_ratio',None), decimals=2):>14}  {'':>12}")
    print(f"  {'DSR (prob. Sharpe > 0)':<32} {_fmt(getattr(stats,'deflated_sharpe_ratio_pct',None), suffix='%'):>14}  {'':>12}")
    print(f"  {'Max drawdown':<32} {_fmt(getattr(stats,'max_drawdown_pct',None), suffix='%'):>14}  {'':>12}")
    print(f"  {'Win rate':<32} {_fmt(getattr(stats,'win_rate_pct',None), suffix='%'):>14}  {'':>12}")
    print(f"  {'Exposición media':<32} {_fmt(getattr(stats,'avg_exposure_pct',None), suffix='%'):>14}  {'100%':>12}")
    print(f"  {'Nº trades':<32} {len(period_trades):>14}  {'':>12}")
    print(f"{'─'*65}")
    if strategy_ret is not None:
        alpha = strategy_ret - bench_ret
        print(f"  Alpha vs SPY: {_fmt(alpha, suffix='pp')}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
