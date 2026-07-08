"""Compara el impacto de 3 features vs el baseline (rsi_max=60, macd=3):
  1. Trailing stop (trailing_stop_enabled=True)
  2. Cooldown post-stop-loss (stop_loss_cooldown_days=5)
  3. Filtro de volumen de entrada (entry_volume_multiplier=1.5)
  4. Combinación de los 3

Período: 2023-01-01 → 2026-01-01 (out-of-sample).
Usa caché de barras en disco (cache_only=True) → rápido.

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/backtest_feature_comparison.py
"""
from __future__ import annotations
import sys, time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.backtest import _collect_opportunistic_trades, _compute_summary_stats
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig

MACD_DAYS       = 3
RSI_MAX         = 60.0
PERIOD_START    = date(2023, 1, 1)
PERIOD_END      = date(2026, 1, 1)
CONCURRENT_CAP  = 25
ASSUMED_CAPITAL = 11_000.0
BACKTEST_YEARS  = 9
RISK_PCT        = 2.0
MAX_POS_PCT     = 9.0
MAX_ORDER_USD   = 5_000.0


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


def _build_cfg(
    trailing_stop: bool = False,
    trailing_activation_pct: float = 0.0,
    cooldown_days: int = 0,
    volume_mult: float = 0.0,
) -> ScreenerConfig:
    cfg = ScreenerConfig()
    cfg.backtest_years = BACKTEST_YEARS
    cfg.top_n = CONCURRENT_CAP
    cfg.opportunistic.macd_crossover_lookback_days = MACD_DAYS
    cfg.opportunistic.rsi_max = RSI_MAX
    cfg.opportunistic.backtest_cross_sectional_gates = True
    cfg.trailing_stop_enabled = trailing_stop
    cfg.trailing_stop_activation_pct = trailing_activation_pct
    cfg.stop_loss_cooldown_days = cooldown_days
    cfg.entry_volume_multiplier = volume_mult
    return cfg


def _rules() -> RulesConfig:
    r = RulesConfig()
    r.risk_per_trade_pct = RISK_PCT
    r.max_position_pct_of_equity = MAX_POS_PCT
    r.max_order_value_usd = MAX_ORDER_USD
    return r


def _compute(trades, bench_bars, marks):
    if not trades:
        return None
    try:
        return _compute_summary_stats(
            trades, CONCURRENT_CAP, bench_bars, marks,
            invest_idle_cash_in_benchmark=False,
            vol_weighting_enabled=False,
            deflated_sharpe_num_trials=1,
            risk_based_sizing_enabled=True,
            rules_config=_rules(),
            assumed_capital_usd=ASSUMED_CAPITAL,
        )
    except Exception as exc:
        print(f"    ERROR: {exc}")
        return None


def _f(v, dec=1, suffix=""):
    return f"{v:.{dec}f}{suffix}" if v is not None else "—"


def _run(label: str, cfg: ScreenerConfig, all_trades, marks, bench_bars):
    t, b, m = _filter_trades_by_date(all_trades, bench_bars, marks, from_=PERIOD_START, before=PERIOD_END)
    stats = _compute(t, b, m)
    ret   = getattr(stats, "strategy_cumulative_return_pct", None)
    sh    = getattr(stats, "sharpe_ratio", None)
    dsr   = getattr(stats, "deflated_sharpe_ratio_pct", None)
    dd    = getattr(stats, "max_drawdown_pct", None)
    wr    = getattr(stats, "win_rate_pct", None)
    exp   = getattr(stats, "avg_exposure_pct", None)
    n     = len(t)
    return label, ret, sh, dsr, dd, wr, exp, n


SCENARIOS = [
    ("Baseline (sin cambios)",           dict()),
    ("+ Trailing stop",                  dict(trailing_stop=True)),
    ("+ Trailing stop (activ.3%)",       dict(trailing_stop=True, trailing_activation_pct=3.0)),
    ("+ Cooldown 5d post-stop",          dict(cooldown_days=5)),
    ("+ Vol. confirmación ×1.5",         dict(volume_mult=1.5)),
    ("+ Vol. confirmación ×2.0",         dict(volume_mult=2.0)),
    ("Trailing+Cooldown+Vol×1.5",        dict(trailing_stop=True, trailing_activation_pct=3.0,
                                              cooldown_days=5, volume_mult=1.5)),
]


def main():
    print(f"\n{'='*75}")
    print(f"  COMPARACIÓN DE FEATURES — macd={MACD_DAYS}d  rsi_max={RSI_MAX:.0f}")
    print(f"  Período: {PERIOD_START} → {PERIOD_END}  | Capital: ${ASSUMED_CAPITAL:,.0f}")
    print(f"{'='*75}\n")

    # Primer escenario: recolecta trades (puede tardar si hay datos nuevos)
    print("  Recolectando trades (caché de disco)...")
    t0 = time.time()
    baseline_cfg = _build_cfg(**SCENARIOS[0][1])
    rules_cfg = _rules()
    all_trades_base, marks_base, bench_base = _collect_opportunistic_trades(
        baseline_cfg, rules_cfg, cache_only=True
    )
    print(f"  {len(all_trades_base)} trades totales en {time.time()-t0:.0f}s\n")

    # Benchmark SPY
    from pandas import Timestamp
    period_bench = bench_base[bench_base.index >= Timestamp(PERIOD_START, tz="UTC")]
    period_bench = period_bench[period_bench.index < Timestamp(PERIOD_END, tz="UTC")]
    spy_ret = (period_bench.iloc[-1] / period_bench.iloc[0] - 1) * 100 if len(period_bench) > 1 else None

    rows = []
    for label, kwargs in SCENARIOS:
        cfg = _build_cfg(**kwargs)
        # Para baseline ya tenemos los trades; para otros re-simulamos (rápido, barras en caché)
        if kwargs:
            t0 = time.time()
            all_trades, marks, bench_bars = _collect_opportunistic_trades(cfg, rules_cfg, cache_only=True)
            row = _run(label, cfg, all_trades, marks, bench_bars)
        else:
            row = _run(label, cfg, all_trades_base, marks_base, bench_base)
        rows.append(row)

    # Header
    hdr = f"  {'Escenario':<36} {'Retorno':>8} {'Sharpe':>7} {'DSR':>6} {'MaxDD':>7} {'WR':>6} {'Exp':>6} {'N':>5}"
    sep = "  " + "-"*73
    print(hdr)
    print(sep)
    for label, ret, sh, dsr, dd, wr, exp, n in rows:
        is_base = label == rows[0][0]
        marker = " ◄" if is_base else ""
        print(f"  {label:<36} {_f(ret,1,'%'):>8} {_f(sh,2):>7} {_f(dsr,1,'%'):>6} "
              f"{_f(dd,1,'%'):>7} {_f(wr,1,'%'):>6} {_f(exp,1,'%'):>6} {n:>5}{marker}")
    print(sep)
    print(f"  {'SPY (benchmark)':<36} {_f(spy_ret,1,'%'):>8}")
    print(f"\n{'='*75}\n")


if __name__ == "__main__":
    main()
