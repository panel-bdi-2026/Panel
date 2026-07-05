"""Walk-forward optimization de estrategia Oportunista (y opcionalmente Momentum).

Split temporal fijo:
  Train  2003-2016 (13 años) — selección de parámetros
  Test   2016-2026 (10 años) — validación out-of-sample

El primer run descarga datos de Tiingo y los guarda en el caché de disco
(backend/data/tiingo_eod/). Los runs siguientes usan el caché y son rápidos
(segundos de carga vs ~40 min de descarga).

Proceso:
  1. Para cada combinación (stop_atr, top_n, rsi_max, holding) → recolectar
     trades una vez sobre el período completo 2003-2026.
  2. Para cada variante de invest_idle (True/False) → compute_summary_stats
     sobre las ventanas Train y Test sin re-recolectar.
  3. Clasificar por DSR en la ventana Train, reportar Test out-of-sample.
  4. Guardar resultados en scripts/results/optimize_{strategy}_{timestamp}.json

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/optimize_strategy.py [--strategy opportunistic|momentum]
    .venv/bin/python scripts/optimize_strategy.py --strategy opportunistic --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.backtest import (
    _collect_opportunistic_trades,
    _collect_momentum_trades,
    _compute_summary_stats,
)
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig

RESULTS_DIR = Path(__file__).resolve().parent / "results"
BACKTEST_YEARS = 15  # 15 × 365 ≈ 22 años de datos de trading

# Split temporal: todo antes de TRAIN_END es train, desde TEST_START en adelante es test
TRAIN_END = date(2016, 1, 1)
TEST_START = date(2016, 1, 1)

# ── Grid de parámetros ───────────────────────────────────────────────────────
# Parámetros que requieren re-recolectar trades (afectan la simulación de la
# estrategia: cuándo se entra, cuándo se sale, cuántas posiciones simultáneas).
#
# Grid recortado a los parámetros más informativos para la primera iteración:
# - stop_atr: valor actual (1.5) vs el optimizado para 3 años (2.5)
# - rsi_max: más restrictivo (65) vs default actual (75)
# - top_n: 10 (actual) vs 15 (más exposición)
# - holding: fijo en 20 (menos dimensiones = menos corridas, resultado más claro)
# Total recolecciones: 2×2×2×1 = 8  → ~2-3 horas con caché en disco
# Para un grid más completo, agregar más valores en fases posteriores.
OUTER_GRID_OPP = {
    "stop_atr":  [1.5, 2.5],
    "rsi_max":   [65.0, 75.0],
    "top_n":     [10, 15],
    "holding":   [20],
}
# Parámetros que solo afectan compute_summary_stats (no requieren re-recolectar).
INNER_GRID = {
    "invest_idle": [False, True],
}
# Total corridas de recolección: 2×2×2×1 = 8
# Total de evaluaciones: 8 × 2 = 16

OUTER_GRID_MOM = {
    "stop_atr":  [1.0, 1.5, 2.0],
    "rsi_max":   [50.0, 60.0, 70.0],
    "top_n":     [5, 10],
    "holding":   [20, 25],
}
# ── Fin grid ─────────────────────────────────────────────────────────────────


def _build_cfg(stop_atr: float, rsi_max: float, top_n: int, holding: int) -> ScreenerConfig:
    cfg = ScreenerConfig()
    cfg.backtest_years = BACKTEST_YEARS
    cfg.backtest_risk_based_sizing_enabled = True
    cfg.stop_loss_atr_multiplier = stop_atr
    cfg.rsi_max = rsi_max
    cfg.top_n = top_n
    cfg.max_holding_days = holding
    return cfg


def _filter_trades_by_date(trades, bench_bars, marks, before: date | None, from_: date | None):
    """Filtra trades, bench_bars y marks a una ventana de fechas."""
    from pandas import Timestamp
    import pandas as pd

    if before is not None:
        cutoff = Timestamp(before, tz="UTC")
        trades = [t for t in trades if t.entry_date < cutoff]
        bench_bars = bench_bars[bench_bars.index < cutoff]
        marks = [m for m in marks if m.ts < cutoff]

    if from_ is not None:
        cutoff = Timestamp(from_, tz="UTC")
        trades = [t for t in trades if t.entry_date >= cutoff]
        bench_bars = bench_bars[bench_bars.index >= cutoff]
        marks = [m for m in marks if m.ts >= cutoff]

    return trades, bench_bars, marks


def _compute(trades, top_n, bench_bars, marks, invest_idle: bool):
    """Wrapper de _compute_summary_stats con manejo de errores."""
    if not trades:
        return None
    try:
        return _compute_summary_stats(
            trades, top_n, bench_bars, marks,
            invest_idle_cash_in_benchmark=invest_idle,
            vol_weighting_enabled=False,
            deflated_sharpe_num_trials=100,
            risk_based_sizing_enabled=True,
            rules_config=RulesConfig(),
            assumed_capital_usd=None,
        )
    except Exception as exc:
        print(f"      [warn] compute_summary_stats falló: {exc}")
        return None


def _stat(s, key, default=None):
    if s is None:
        return default
    v = getattr(s, key, None)
    return round(v, 3) if v is not None else default


def _run_outer(strategy: str, outer_keys, outer_vals, rules_cfg, n_total, n_done):
    """Recolecta trades una vez con los parámetros outer, luego evalúa inner."""
    stop_atr, rsi_max, top_n, holding = outer_vals
    tag = f"stop={stop_atr} rsi={rsi_max} top={top_n} hold={holding}"
    print(f"\n  [{n_done+1}/{n_total}] {tag} — recolectando trades...", end=" ", flush=True)
    t0 = time.time()

    cfg = _build_cfg(stop_atr, rsi_max, top_n, holding)
    try:
        if strategy == "opportunistic":
            all_trades, marks, bench_bars = _collect_opportunistic_trades(cfg, rules_cfg)
        else:
            all_trades, marks, bench_bars = _collect_momentum_trades(cfg, rules_cfg)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return []

    elapsed = time.time() - t0
    print(f"{len(all_trades)} trades en {elapsed:.0f}s")

    results = []
    for (invest_idle,) in product(*[INNER_GRID[k] for k in INNER_GRID]):
        # Ventana train
        tr_trades, tr_bench, tr_marks = _filter_trades_by_date(
            all_trades, bench_bars, marks, before=TRAIN_END, from_=None
        )
        train = _compute(tr_trades, top_n, tr_bench, tr_marks, invest_idle)

        # Ventana test
        te_trades, te_bench, te_marks = _filter_trades_by_date(
            all_trades, bench_bars, marks, before=None, from_=TEST_START
        )
        test = _compute(te_trades, top_n, te_bench, te_marks, invest_idle)

        row = {
            "strategy": strategy,
            "stop_atr": stop_atr,
            "rsi_max": rsi_max,
            "top_n": top_n,
            "holding": holding,
            "invest_idle": invest_idle,
            "train": {
                "n_trades": len(tr_trades),
                "cumret": _stat(train, "strategy_cumulative_return_pct"),
                "sharpe": _stat(train, "sharpe_ratio"),
                "dsr": _stat(train, "deflated_sharpe_ratio_pct"),
                "max_dd": _stat(train, "max_drawdown_pct"),
                "win_rate": _stat(train, "win_rate_pct"),
                "exposure": _stat(train, "avg_exposure_pct"),
                "expectancy": _stat(train, "expectancy_pct"),
                "bench_cumret": _stat(train, "benchmark_cumulative_return_pct"),
            },
            "test": {
                "n_trades": len(te_trades),
                "cumret": _stat(test, "strategy_cumulative_return_pct"),
                "sharpe": _stat(test, "sharpe_ratio"),
                "dsr": _stat(test, "deflated_sharpe_ratio_pct"),
                "max_dd": _stat(test, "max_drawdown_pct"),
                "win_rate": _stat(test, "win_rate_pct"),
                "exposure": _stat(test, "avg_exposure_pct"),
                "expectancy": _stat(test, "expectancy_pct"),
                "bench_cumret": _stat(test, "benchmark_cumulative_return_pct"),
            },
        }
        results.append(row)

        # Print rápido de las métricas clave
        t_dsr = _stat(train, "deflated_sharpe_ratio_pct", 0)
        t_ret = _stat(train, "strategy_cumulative_return_pct", 0)
        v_dsr = _stat(test, "deflated_sharpe_ratio_pct", 0)
        v_ret = _stat(test, "strategy_cumulative_return_pct", 0)
        idle_str = "idle→bench" if invest_idle else "idle→cash"
        print(f"      {idle_str}  train: DSR={t_dsr:.1f}% ret={t_ret:.1f}%  "
              f"test: DSR={v_dsr:.1f}% ret={v_ret:.1f}%")

    return results


def _print_ranking(results: list[dict], n=10) -> None:
    """Imprime los top-N por DSR de train, con sus métricas de test."""
    ranked = sorted(results, key=lambda r: r["train"]["dsr"] or 0, reverse=True)[:n]
    print(f"\n{'='*90}")
    print(f"{'RANKING — Top {0} por DSR en TRAIN (2003-2016)'.format(n)}")
    print(f"{'='*90}")
    hdr = (f"  {'stop':>5} {'rsi':>5} {'top':>4} {'hold':>5} {'idle':>5}"
           f" | {'tDSR':>7} {'tRet%':>8} {'tShp':>7}"
           f" | {'vDSR':>7} {'vRet%':>8} {'vShp':>7}")
    print(hdr)
    print("  " + "-" * 85)
    for r in ranked:
        tr, te = r["train"], r["test"]
        idle = "Y" if r["invest_idle"] else "N"
        print(
            f"  {r['stop_atr']:>5.1f} {r['rsi_max']:>5.0f} {r['top_n']:>4}"
            f" {r['holding']:>5} {idle:>5}"
            f" | {tr['dsr'] or 0:>7.1f} {tr['cumret'] or 0:>8.1f} {tr['sharpe'] or 0:>7.2f}"
            f" | {te['dsr'] or 0:>7.1f} {te['cumret'] or 0:>8.1f} {te['sharpe'] or 0:>7.2f}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="Walk-forward optimization de estrategia")
    parser.add_argument("--strategy", choices=["opportunistic", "momentum", "both"],
                        default="opportunistic")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra el grid sin ejecutar")
    args = parser.parse_args()

    strategies = ["opportunistic", "momentum"] if args.strategy == "both" else [args.strategy]
    rules_cfg = RulesConfig()

    for strategy in strategies:
        outer_grid = OUTER_GRID_OPP if strategy == "opportunistic" else OUTER_GRID_MOM
        outer_combinations = list(product(
            outer_grid["stop_atr"], outer_grid["rsi_max"],
            outer_grid["top_n"],    outer_grid["holding"],
        ))
        n_inner = len(list(product(*[INNER_GRID[k] for k in INNER_GRID])))
        n_total_evals = len(outer_combinations) * n_inner

        print(f"\n{'='*70}")
        print(f"  OPTIMIZACIÓN: {strategy.upper()}")
        print(f"  Grid: {len(outer_combinations)} recolecciones × {n_inner} variantes"
              f" = {n_total_evals} evaluaciones")
        print(f"  Train: 2003-2016 | Test: 2016-2026")
        print(f"{'='*70}")

        if args.dry_run:
            for i, combo in enumerate(outer_combinations, 1):
                stop_atr, rsi_max, top_n, holding = combo
                print(f"  [{i}/{len(outer_combinations)}] stop={stop_atr} rsi={rsi_max}"
                      f" top={top_n} hold={holding}")
            continue

        all_results = []
        for i, combo in enumerate(outer_combinations):
            rows = _run_outer(strategy, list(outer_grid.keys()), combo,
                              rules_cfg, len(outer_combinations), i)
            all_results.extend(rows)

        if all_results:
            _print_ranking(all_results, n=15)

            # Guardar JSON
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            out = RESULTS_DIR / f"optimize_{strategy}_{ts}.json"
            with open(out, "w") as f:
                json.dump(all_results, f, indent=2, default=str)
            print(f"Resultados guardados en: {out}\n")


if __name__ == "__main__":
    main()
