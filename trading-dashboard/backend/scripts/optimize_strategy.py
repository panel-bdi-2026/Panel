"""Walk-forward optimization de estrategia Oportunista (y opcionalmente Momentum).

Split temporal — Oportunista v4:
  Train  2018-2022 (5 años de mercado moderno, no tocados al calibrar)
  Test   2023-2024 (2 años out-of-sample honestos — excluye 2025-2026 donde
         se calibraron los parámetros originales)

Cambios vs v1-v3:
  - top_n=25 en cap_concurrent_positions: refleja que el sistema live puede
    tener 16+ posiciones simultáneas (el cash es el límite real, no top_n=10).
  - Parámetros oportunista correctos: rsi_min=35, rsi_max=65, stop_atr=2.5
    (igual que live — v1-v3 usaban por error los params de Momentum).
  - Grid v4 varía los gates de entrada (outer) que controlan frecuencia de
    señales: macd_crossover_days, opp_rsi_max, min_below_52w.
  - Inner grid simplificado: invest_idle=False, risk=2.0, max_pos=30
    (confirmado en v3 como la mejor combinación de sizing).

El primer run descarga datos de Tiingo y los guarda en el caché de disco
(backend/data/tiingo_eod/). Los runs siguientes usan el caché y son rápidos.

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
BACKTEST_YEARS = 9  # cubre desde ~2017 — suficiente para el split 2018-2024 con 1 año de warmup

# ── Split temporal ────────────────────────────────────────────────────────────
TRAIN_START = date(2018, 1, 1)   # inicio del período de entrenamiento
TRAIN_END   = date(2023, 1, 1)   # fin train / inicio test
TEST_START  = date(2023, 1, 1)   # inicio test out-of-sample
TEST_END    = date(2025, 1, 1)   # fin test — excluye 2025-2026 (calibración)

# ── Capacidad de posiciones concurrentes ─────────────────────────────────────
# El live puede sostener 16+ posiciones simultáneas (el límite real es cash).
# top_n=25 es un cap generoso para que nunca sea el constraint en backtest.
CONCURRENT_CAP = 25

# ── Grid de parámetros ────────────────────────────────────────────────────────
# OUTER: requieren re-recolectar trades. Varían los gates de entrada de
# Oportunista que controlan la frecuencia de señales.
#
#   macd_days:   ventana para el gate MACD crossover (días). El gate exige
#                que el histograma MACD haya cruzado de negativo a positivo
#                en los últimos N días. 0 = desactivado.
#   opp_rsi_max: techo de RSI para entrar. La estrategia busca acciones en
#                "zona de recuperación" (rsi_min=35 fijo, rsi_max varía).
#
# v5: min_below_52w desaparece (reemplazado por mediana dinámica cross-seccional).
# Gates ahora son relativos al universo del día, no umbrales absolutos.
OUTER_GRID_OPP = {
    "macd_days":   [3, 5, 7],   # 3=actual; 5,7=ventana más amplia
    "opp_rsi_max": [60, 65, 70, 75],
}

# Momentum: grid original (no modificado por esta sesión)
OUTER_GRID_MOM = {
    "stop_atr":  [1.0, 1.5, 2.0],
    "rsi_max":   [50.0, 60.0, 70.0],
    "top_n":     [5, 10],
    "holding":   [20, 25],
}

# INNER: solo afectan _compute_summary_stats, no requieren re-recolectar.
# Fijamos los mejores valores de v3 para no multiplicar innecesariamente:
#   invest_idle=False (no correlacionar con SPY), risk=2% (indiferente),
#   max_pos=30% (mayor exposición con DSR positivo en v3).
INNER_GRID = {
    "invest_idle":        [False],
    "risk_per_trade_pct": [2.0],
    "max_position_pct":   [30],
}
# Total recolecciones Oportunista v5: 3 × 4 = 12
# Total evaluaciones:                 12 × 1 = 12

ASSUMED_CAPITAL = 100_000.0


def _build_cfg_opp(macd_days: int, opp_rsi_max: float) -> ScreenerConfig:
    cfg = ScreenerConfig()
    cfg.backtest_years = BACKTEST_YEARS
    cfg.top_n = CONCURRENT_CAP
    # Parámetros específicos de Oportunista — deben coincidir con live
    cfg.opportunistic.macd_crossover_lookback_days = macd_days
    cfg.opportunistic.rsi_max = opp_rsi_max
    # v5: gates cross-seccionales (percentil de universo, no umbral absoluto)
    cfg.opportunistic.backtest_cross_sectional_gates = True
    # stop_loss_atr_multiplier, rsi_min, holding usan defaults del live (2.5, 35, 20)
    return cfg


def _build_cfg_mom(stop_atr: float, rsi_max: float, top_n: int, holding: int) -> ScreenerConfig:
    cfg = ScreenerConfig()
    cfg.backtest_years = BACKTEST_YEARS
    cfg.backtest_risk_based_sizing_enabled = True
    cfg.stop_loss_atr_multiplier = stop_atr
    cfg.rsi_max = rsi_max
    cfg.top_n = top_n
    cfg.max_holding_days = holding
    return cfg


def _filter_trades_by_date(trades, bench_bars, marks_by_trade_id,
                            before: date | None, from_: date | None):
    """Filtra trades y bench_bars a una ventana de fechas [from_, before).

    marks_by_trade_id es {id(trade): {date: factor}} — se filtra por las
    claves que correspondan a los trades que sobreviven el corte de fecha.
    """
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
    filtered_marks = {k: v for k, v in marks_by_trade_id.items() if k in surviving_ids}

    return trades, bench_bars, filtered_marks


def _compute(trades, top_n, bench_bars, marks,
             invest_idle: bool,
             risk_per_trade_pct: float = 2.0,
             max_position_pct: float = 30.0):
    """Wrapper de _compute_summary_stats con manejo de errores.

    max_order_value_usd se fija como max_position_pct × capital para que nunca
    sea el constraint activo.
    """
    if not trades:
        return None
    try:
        rules = RulesConfig()
        rules.risk_per_trade_pct = risk_per_trade_pct
        rules.max_position_pct_of_equity = max_position_pct
        rules.max_order_value_usd = ASSUMED_CAPITAL * max_position_pct / 100
        return _compute_summary_stats(
            trades, top_n, bench_bars, marks,
            invest_idle_cash_in_benchmark=invest_idle,
            vol_weighting_enabled=False,
            deflated_sharpe_num_trials=100,
            risk_based_sizing_enabled=True,
            rules_config=rules,
            assumed_capital_usd=ASSUMED_CAPITAL,
        )
    except Exception as exc:
        print(f"      [warn] compute_summary_stats falló: {exc}")
        return None


def _stat(s, key, default=None):
    if s is None:
        return default
    v = getattr(s, key, None)
    return round(v, 3) if v is not None else default


def _run_outer_opp(outer_vals, rules_cfg, n_total, n_done):
    """Recolecta trades de Oportunista una vez con parámetros outer, evalúa inner."""
    macd_days, opp_rsi_max = outer_vals
    tag = f"macd={macd_days}d rsi_max={opp_rsi_max:.0f} [cross-seccional]"
    print(f"\n  [{n_done+1}/{n_total}] {tag} — recolectando trades...", end=" ", flush=True)
    t0 = time.time()

    cfg = _build_cfg_opp(macd_days, opp_rsi_max)
    try:
        all_trades, marks, bench_bars = _collect_opportunistic_trades(cfg, rules_cfg, cache_only=True)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return []

    elapsed = time.time() - t0
    # Mostrar distribución por período para entender señal real
    from collections import Counter
    yr = Counter(t.entry_date.year for t in all_trades)
    train_n = sum(c for y, c in yr.items() if TRAIN_START.year <= y < TRAIN_END.year)
    test_n  = sum(c for y, c in yr.items() if TEST_START.year <= y < TEST_END.year)
    print(f"{len(all_trades)} trades totales en {elapsed:.0f}s "
          f"(train {TRAIN_START.year}-{TRAIN_END.year-1}: {train_n} | "
          f"test {TEST_START.year}-{TEST_END.year-1}: {test_n})")

    # Pre-filtrar ventanas
    tr_trades, tr_bench, tr_marks = _filter_trades_by_date(
        all_trades, bench_bars, marks, from_=TRAIN_START, before=TRAIN_END
    )
    te_trades, te_bench, te_marks = _filter_trades_by_date(
        all_trades, bench_bars, marks, from_=TEST_START, before=TEST_END
    )

    inner_keys = list(INNER_GRID.keys())
    results = []
    for combo in product(*[INNER_GRID[k] for k in inner_keys]):
        inner = dict(zip(inner_keys, combo))
        invest_idle    = inner.get("invest_idle", False)
        risk_per_trade = inner.get("risk_per_trade_pct", 2.0)
        max_pos_pct    = inner.get("max_position_pct", 30.0)

        train = _compute(tr_trades, CONCURRENT_CAP, tr_bench, tr_marks,
                         invest_idle, risk_per_trade, max_pos_pct)
        test  = _compute(te_trades, CONCURRENT_CAP, te_bench, te_marks,
                         invest_idle, risk_per_trade, max_pos_pct)

        row = {
            "strategy": "opportunistic",
            "macd_days": macd_days,
            "opp_rsi_max": opp_rsi_max,
            "cross_sectional_gates": True,
            "invest_idle": invest_idle,
            "risk_per_trade_pct": risk_per_trade,
            "max_position_pct": max_pos_pct,
            "train": {
                "n_trades": len(tr_trades),
                "cumret":   _stat(train, "strategy_cumulative_return_pct"),
                "sharpe":   _stat(train, "sharpe_ratio"),
                "dsr":      _stat(train, "deflated_sharpe_ratio_pct"),
                "max_dd":   _stat(train, "max_drawdown_pct"),
                "win_rate": _stat(train, "win_rate_pct"),
                "exposure": _stat(train, "avg_exposure_pct"),
                "expectancy": _stat(train, "expectancy_pct"),
                "bench_cumret": _stat(train, "benchmark_cumulative_return_pct"),
            },
            "test": {
                "n_trades": len(te_trades),
                "cumret":   _stat(test, "strategy_cumulative_return_pct"),
                "sharpe":   _stat(test, "sharpe_ratio"),
                "dsr":      _stat(test, "deflated_sharpe_ratio_pct"),
                "max_dd":   _stat(test, "max_drawdown_pct"),
                "win_rate": _stat(test, "win_rate_pct"),
                "exposure": _stat(test, "avg_exposure_pct"),
                "expectancy": _stat(test, "expectancy_pct"),
                "bench_cumret": _stat(test, "benchmark_cumulative_return_pct"),
            },
        }
        results.append(row)

        t_dsr = _stat(train, "deflated_sharpe_ratio_pct", 0)
        t_ret = _stat(train, "strategy_cumulative_return_pct", 0)
        t_exp = _stat(train, "avg_exposure_pct", 0)
        v_dsr = _stat(test, "deflated_sharpe_ratio_pct", 0)
        v_ret = _stat(test, "strategy_cumulative_return_pct", 0)
        v_exp = _stat(test, "avg_exposure_pct", 0)
        print(f"      train({TRAIN_START.year}-{TRAIN_END.year-1}): "
              f"DSR={t_dsr:.1f}% ret={t_ret:.1f}% exp={t_exp:.1f}% n={len(tr_trades)}  "
              f"test({TEST_START.year}-{TEST_END.year-1}): "
              f"DSR={v_dsr:.1f}% ret={v_ret:.1f}% exp={v_exp:.1f}% n={len(te_trades)}")

    return results


def _run_outer_mom(outer_vals, rules_cfg, n_total, n_done):
    """Recolecta trades de Momentum una vez con parámetros outer, evalúa inner."""
    stop_atr, rsi_max, top_n, holding = outer_vals
    tag = f"stop={stop_atr} rsi={rsi_max} top={top_n} hold={holding}"
    print(f"\n  [{n_done+1}/{n_total}] {tag} — recolectando trades...", end=" ", flush=True)
    t0 = time.time()

    cfg = _build_cfg_mom(stop_atr, rsi_max, top_n, holding)
    try:
        all_trades, marks, bench_bars = _collect_momentum_trades(cfg, rules_cfg)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return []

    elapsed = time.time() - t0
    print(f"{len(all_trades)} trades en {elapsed:.0f}s")

    tr_trades, tr_bench, tr_marks = _filter_trades_by_date(
        all_trades, bench_bars, marks, from_=None, before=TRAIN_END
    )
    te_trades, te_bench, te_marks = _filter_trades_by_date(
        all_trades, bench_bars, marks, from_=TEST_START, before=None
    )

    inner_keys = list(INNER_GRID.keys())
    results = []
    for combo in product(*[INNER_GRID[k] for k in inner_keys]):
        inner = dict(zip(inner_keys, combo))
        invest_idle    = inner.get("invest_idle", False)
        risk_per_trade = inner.get("risk_per_trade_pct", 2.0)
        max_pos_pct    = inner.get("max_position_pct", 30.0)

        train = _compute(tr_trades, top_n, tr_bench, tr_marks,
                         invest_idle, risk_per_trade, max_pos_pct)
        test  = _compute(te_trades, top_n, te_bench, te_marks,
                         invest_idle, risk_per_trade, max_pos_pct)

        row = {
            "strategy": "momentum",
            "stop_atr": stop_atr,
            "rsi_max": rsi_max,
            "top_n": top_n,
            "holding": holding,
            "invest_idle": invest_idle,
            "risk_per_trade_pct": risk_per_trade,
            "max_position_pct": max_pos_pct,
            "train": {
                "n_trades": len(tr_trades),
                "cumret":   _stat(train, "strategy_cumulative_return_pct"),
                "sharpe":   _stat(train, "sharpe_ratio"),
                "dsr":      _stat(train, "deflated_sharpe_ratio_pct"),
                "max_dd":   _stat(train, "max_drawdown_pct"),
                "win_rate": _stat(train, "win_rate_pct"),
                "exposure": _stat(train, "avg_exposure_pct"),
                "expectancy": _stat(train, "expectancy_pct"),
                "bench_cumret": _stat(train, "benchmark_cumulative_return_pct"),
            },
            "test": {
                "n_trades": len(te_trades),
                "cumret":   _stat(test, "strategy_cumulative_return_pct"),
                "sharpe":   _stat(test, "sharpe_ratio"),
                "dsr":      _stat(test, "deflated_sharpe_ratio_pct"),
                "max_dd":   _stat(test, "max_drawdown_pct"),
                "win_rate": _stat(test, "win_rate_pct"),
                "exposure": _stat(test, "avg_exposure_pct"),
                "expectancy": _stat(test, "expectancy_pct"),
                "bench_cumret": _stat(test, "benchmark_cumulative_return_pct"),
            },
        }
        results.append(row)

        t_dsr = _stat(train, "deflated_sharpe_ratio_pct", 0)
        t_ret = _stat(train, "strategy_cumulative_return_pct", 0)
        v_dsr = _stat(test, "deflated_sharpe_ratio_pct", 0)
        v_ret = _stat(test, "strategy_cumulative_return_pct", 0)
        idle_str = "idle→bench" if invest_idle else "idle→cash"
        print(f"      {idle_str}  train: DSR={t_dsr:.1f}% ret={t_ret:.1f}%  "
              f"test: DSR={v_dsr:.1f}% ret={v_ret:.1f}%")

    return results


def _print_ranking_opp(results: list[dict], n=15) -> None:
    ranked = sorted(results, key=lambda r: r["train"]["dsr"] or 0, reverse=True)[:n]
    print(f"\n{'='*115}")
    print(f"  RANKING OPP — Top {n} por DSR en TRAIN ({TRAIN_START.year}-{TRAIN_END.year-1})")
    print(f"  (Test out-of-sample: {TEST_START.year}-{TEST_END.year-1}, excluye 2025-2026)")
    print(f"{'='*115}")
    hdr = (f"  {'macd':>5} {'rsiMx':>6} {'52wMn':>6}"
           f" | {'tN':>4} {'tDSR':>7} {'tRet%':>8} {'tExp%':>7} {'tShp':>6} {'tDD%':>7}"
           f" | {'vN':>4} {'vDSR':>7} {'vRet%':>8} {'vExp%':>7} {'vShp':>6}")
    print(hdr)
    print("  " + "-" * 110)
    for r in ranked:
        tr, te = r["train"], r["test"]
        print(
            f"  {r['macd_days']:>5}d {r['opp_rsi_max']:>5.0f} {r['min_below_52w']:>5.0f}%"
            f" | {tr['n_trades'] or 0:>4} {tr['dsr'] or 0:>7.1f} {tr['cumret'] or 0:>8.1f}"
            f" {tr['exposure'] or 0:>7.1f} {tr['sharpe'] or 0:>6.2f} {tr['max_dd'] or 0:>7.1f}"
            f" | {te['n_trades'] or 0:>4} {te['dsr'] or 0:>7.1f} {te['cumret'] or 0:>8.1f}"
            f" {te['exposure'] or 0:>7.1f} {te['sharpe'] or 0:>6.2f}"
        )
    print()


def _print_ranking_mom(results: list[dict], n=10) -> None:
    ranked = sorted(results, key=lambda r: r["train"]["dsr"] or 0, reverse=True)[:n]
    print(f"\n{'='*90}")
    print(f"  RANKING MOM — Top {n} por DSR en TRAIN (hasta {TRAIN_END})")
    print(f"{'='*90}")
    hdr = (f"  {'stop':>5} {'rsi':>5} {'top':>4} {'hold':>5}"
           f" | {'tDSR':>7} {'tRet%':>8} {'tShp':>7}"
           f" | {'vDSR':>7} {'vRet%':>8} {'vShp':>7}")
    print(hdr)
    print("  " + "-" * 85)
    for r in ranked:
        tr, te = r["train"], r["test"]
        print(
            f"  {r['stop_atr']:>5.1f} {r['rsi_max']:>5.0f} {r['top_n']:>4} {r['holding']:>5}"
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
        if strategy == "opportunistic":
            outer_grid = OUTER_GRID_OPP
            outer_combinations = list(product(*[outer_grid[k] for k in outer_grid]))
            n_inner = len(list(product(*[INNER_GRID[k] for k in INNER_GRID])))
            n_total = len(outer_combinations) * n_inner

            print(f"\n{'='*70}")
            print(f"  OPTIMIZACIÓN: OPPORTUNISTIC v5 (gates cross-seccionales)")
            print(f"  Grid: {len(outer_combinations)} recolecciones × {n_inner} variantes"
                  f" = {n_total} evaluaciones")
            print(f"  Train: {TRAIN_START} → {TRAIN_END} | Test: {TEST_START} → {TEST_END}")
            print(f"  cap_concurrent={CONCURRENT_CAP} (refleja live 16+ posiciones)")
            print(f"{'='*70}")

            if args.dry_run:
                for i, combo in enumerate(outer_combinations, 1):
                    macd_d, rsi_mx = combo
                    print(f"  [{i}/{len(outer_combinations)}] "
                          f"macd={macd_d}d rsi_max={rsi_mx} [cross-seccional]")
                continue

            all_results = []
            for i, combo in enumerate(outer_combinations):
                rows = _run_outer_opp(combo, rules_cfg, len(outer_combinations), i)
                all_results.extend(rows)

            if all_results:
                _print_ranking_opp(all_results, n=15)
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                out = RESULTS_DIR / f"optimize_opportunistic_{ts}.json"
                with open(out, "w") as f:
                    json.dump(all_results, f, indent=2, default=str)
                print(f"Resultados guardados en: {out}\n")

        else:  # momentum
            outer_grid = OUTER_GRID_MOM
            outer_combinations = list(product(
                outer_grid["stop_atr"], outer_grid["rsi_max"],
                outer_grid["top_n"],    outer_grid["holding"],
            ))
            n_inner = len(list(product(*[INNER_GRID[k] for k in INNER_GRID])))
            n_total = len(outer_combinations) * n_inner

            print(f"\n{'='*70}")
            print(f"  OPTIMIZACIÓN: MOMENTUM")
            print(f"  Grid: {len(outer_combinations)} recolecciones × {n_inner} variantes"
                  f" = {n_total} evaluaciones")
            print(f"  Train: hasta {TRAIN_END} | Test: desde {TEST_START}")
            print(f"{'='*70}")

            if args.dry_run:
                for i, combo in enumerate(outer_combinations, 1):
                    stop_atr, rsi_max, top_n, holding = combo
                    print(f"  [{i}/{len(outer_combinations)}] stop={stop_atr} rsi={rsi_max}"
                          f" top={top_n} hold={holding}")
                continue

            all_results = []
            for i, combo in enumerate(outer_combinations):
                rows = _run_outer_mom(combo, rules_cfg, len(outer_combinations), i)
                all_results.extend(rows)

            if all_results:
                _print_ranking_mom(all_results, n=10)
                RESULTS_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                out = RESULTS_DIR / f"optimize_momentum_{ts}.json"
                with open(out, "w") as f:
                    json.dump(all_results, f, indent=2, default=str)
                print(f"Resultados guardados en: {out}\n")


if __name__ == "__main__":
    main()
