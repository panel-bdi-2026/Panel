"""Backtest de 15 años + walk-forward + Monte Carlo + análisis por sub-período.

Corre las dos estrategias (Momentum y Oportunista) sobre el período 2010-2025
usando datos de Tiingo. Tarda ~10-20 minutos la primera vez (fetch de datos);
en runs subsiguientes usa el caché en RAM si el servicio está corriendo, o el
caché de disco si se implementó persistencia.

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/backtest_15yr.py [--n-sims N] [--strategy momentum|opportunistic|both]

Salida:
    - Reporte en consola (Markdown)
    - JSON con todos los resultados en scripts/results/backtest_15yr_{timestamp}.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Asegurar que el package app sea importable desde el directorio del backend
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
# Buscar .env en el directorio del backend (padre de scripts/)
_backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(_backend_dir / ".env")

from app.backtest import (
    ANALYSIS_SUBPERIODS,
    _collect_momentum_trades,
    _collect_opportunistic_trades,
    _compute_summary_stats,
    analyze_subperiods,
    run_monte_carlo,
    _build_walk_forward_result,
)
from app.rules import RulesConfig
from app.screener_config import ScreenerConfig

BACKTEST_YEARS = 15
N_WALK_FORWARD_FOLDS = 5
DEFAULT_N_SIMS = 10_000
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _fmt(v, fmt=".1f", suffix="") -> str:
    if v is None:
        return "—"
    return f"{v:{fmt}}{suffix}"


def _pct_bar(v: float | None, width: int = 20) -> str:
    """Barra ASCII proporcional a v% (rango ±100%)."""
    if v is None:
        return ""
    clamp = max(-100.0, min(100.0, v))
    if clamp >= 0:
        filled = int(clamp / 100 * width)
        return "█" * filled + "░" * (width - filled)
    else:
        empty = int(-clamp / 100 * width)
        return "░" * (width - empty) + "█" * empty


def _build_cfg(years: int) -> ScreenerConfig:
    """Construye un ScreenerConfig con backtest_years sobreescrito."""
    cfg = ScreenerConfig()
    cfg.backtest_years = years
    cfg.backtest_risk_based_sizing_enabled = True
    return cfg


def _run_strategy(name: str, cfg: ScreenerConfig, rules_cfg: RulesConfig, n_sims: int) -> dict:
    """Corre un análisis completo para una estrategia y devuelve todos los resultados."""
    print(f"\n{'='*70}")
    print(f"  {name.upper()} — backtest {cfg.backtest_years} años")
    print(f"{'='*70}")

    t0 = time.time()
    print("  [1/5] Simulando operaciones y descargando datos...")
    if name == "momentum":
        all_trades, marks, bench_bars = _collect_momentum_trades(cfg, rules_cfg)
    else:
        all_trades, marks, bench_bars = _collect_opportunistic_trades(cfg, rules_cfg)
    print(f"        {len(all_trades)} operaciones en {time.time()-t0:.0f}s")

    print("  [2/5] Calculando métricas del período completo...")
    summary = _compute_summary_stats(
        all_trades, cfg.top_n, bench_bars, marks,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
        cfg.deflated_sharpe_num_trials,
        risk_based_sizing_enabled=cfg.backtest_risk_based_sizing_enabled,
        rules_config=rules_cfg,
        assumed_capital_usd=cfg.backtest_assumed_capital_usd,
    )

    print(f"  [3/5] Walk-forward ({N_WALK_FORWARD_FOLDS} folds)...")
    wf = _build_walk_forward_result(
        all_trades, cfg.top_n, bench_bars, marks, N_WALK_FORWARD_FOLDS,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
        risk_based_sizing_enabled=cfg.backtest_risk_based_sizing_enabled,
        rules_config=rules_cfg,
        assumed_capital_usd=cfg.backtest_assumed_capital_usd,
    )

    print(f"  [4/5] Monte Carlo ({n_sims:,} simulaciones)...")
    mc = run_monte_carlo(
        all_trades, cfg.top_n, bench_bars, marks, n_sims,
        vol_weighting_enabled=cfg.backtest_vol_weighting_enabled,
        risk_based_sizing_enabled=cfg.backtest_risk_based_sizing_enabled,
        rules_config=rules_cfg,
        assumed_capital_usd=cfg.backtest_assumed_capital_usd,
    )

    print("  [5/5] Análisis por sub-período...")
    subperiods = analyze_subperiods(
        all_trades, cfg.top_n, bench_bars, marks,
        vol_weighting_enabled=cfg.backtest_vol_weighting_enabled,
        risk_based_sizing_enabled=cfg.backtest_risk_based_sizing_enabled,
        rules_config=rules_cfg,
        assumed_capital_usd=cfg.backtest_assumed_capital_usd,
    )

    total_time = time.time() - t0
    print(f"  ✓ Completado en {total_time:.0f}s")

    return {
        "strategy": name,
        "years": cfg.backtest_years,
        "summary": summary.model_dump(exclude={"trades", "equity_curve"}),
        "walk_forward": wf.model_dump(),
        "monte_carlo": mc,
        "subperiods": subperiods,
        "elapsed_seconds": total_time,
    }


def _print_report(result: dict) -> None:
    name = result["strategy"].title()
    s = result["summary"]
    mc = result["monte_carlo"]
    wf_folds = result["walk_forward"]["folds"]
    subperiods = result["subperiods"]

    print(f"\n{'#'*70}")
    print(f"# {name} — {result['years']} años (2010-2025)")
    print(f"{'#'*70}")

    # Resumen global
    print(f"\n## Resultado global ({s['start_date'][:10]} → {s['end_date'][:10]})\n")
    bench_ret = s['benchmark_cumulative_return_pct']
    strat_ret = s['strategy_cumulative_return_pct']
    alpha     = strat_ret - bench_ret if (strat_ret is not None and bench_ret is not None) else None
    print(f"  Retorno acumulado estrategia : {_fmt(strat_ret)}%")
    print(f"  Retorno acumulado benchmark  : {_fmt(bench_ret)}%")
    print(f"  Alpha vs benchmark           : {_fmt(alpha, '+.1f')}%")
    print(f"  Max drawdown                 : {_fmt(s['max_drawdown_pct'])}%")
    print(f"  Sharpe ratio (anualizado)    : {_fmt(s['sharpe_ratio'], '.2f')}")
    print(f"  DSR (prob. Sharpe real > 0)  : {_fmt(s['deflated_sharpe_ratio_pct'])}%")
    print(f"  Win rate                     : {_fmt(s['win_rate_pct'])}%")
    print(f"  Total operaciones            : {s['total_trades']}")
    print(f"  Expectancy                   : {_fmt(s['expectancy_pct'])}%")
    print(f"  Exposición promedio          : {_fmt(s['avg_exposure_pct'])}%")

    # Monte Carlo
    if mc:
        cr = mc["cumulative_return_pct"]
        dd = mc["max_drawdown_pct"]
        sh = mc["sharpe"]
        actual = strat_ret
        print(f"\n## Monte Carlo ({mc['n_simulations']:,} simulaciones, {mc['n_trading_days']} días)\n")
        print(f"  Retorno acumulado real    : {_fmt(actual)}%")
        print(f"  Rango P5–P95 (retorno)    : {_fmt(cr['p5'])}% → {_fmt(cr['p95'])}%")
        print(f"  Mediana simulada          : {_fmt(cr['p50'])}%")
        print(f"  Max drawdown P50/P95      : {_fmt(dd['p50'])}% / {_fmt(dd['p95'])}%")
        print(f"  Sharpe mediano simulado   : {_fmt(sh['p50'], '.2f')}")
        print(f"  Prob. retorno positivo    : {_fmt(mc['prob_positive_pct'])}%")
        if actual is not None:
            pct_above_actual = sum(
                1 for _ in range(mc["n_simulations"])
            )  # placeholder — el JSON no tiene la lista completa
            note = ("✅ El resultado real está por encima de la mediana simulada"
                    if actual >= cr["p50"] else
                    "⚠️  El resultado real está por debajo de la mediana simulada")
            print(f"  {note}")

    # Walk-forward
    print(f"\n## Walk-forward ({len(wf_folds)} folds)\n")
    header = f"  {'Período':<28} {'Trades':>7} {'Retorno':>9} {'Bench':>9} {'MaxDD':>8} {'Sharpe':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for fold in wf_folds:
        period = f"{fold['start_date'][:7]} → {fold['end_date'][:7]}"
        ret  = _fmt(fold.get("strategy_cumulative_return_pct"), ".1f", "%")
        bch  = _fmt(fold.get("benchmark_cumulative_return_pct"), ".1f", "%")
        dd   = _fmt(fold.get("max_drawdown_pct"), ".1f", "%")
        shr  = _fmt(fold.get("sharpe_ratio"), ".2f")
        n    = fold.get("total_trades", 0)
        print(f"  {period:<28} {n:>7} {ret:>9} {bch:>9} {dd:>8} {shr:>7}")

    # Sub-períodos
    print(f"\n## Análisis por régimen de mercado\n")
    header2 = f"  {'Período':<35} {'Trades':>7} {'Retorno':>9} {'Bench':>9} {'MaxDD':>8} {'WR%':>6}"
    print(header2)
    print("  " + "-" * (len(header2) - 2))
    for sp in subperiods:
        n     = sp.get("n_trades", 0)
        ret   = _fmt(sp.get("cumulative_return_pct"), ".1f", "%") if n else "—"
        bch   = _fmt(sp.get("benchmark_return_pct"), ".1f", "%") if n else "—"
        dd    = _fmt(sp.get("max_drawdown_pct"), ".1f", "%") if n else "—"
        wr    = _fmt(sp.get("win_rate_pct"), ".0f", "%") if n else "—"
        print(f"  {sp['period']:<35} {n:>7} {ret:>9} {bch:>9} {dd:>8} {wr:>6}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest 15 años con Monte Carlo y walk-forward")
    parser.add_argument("--n-sims", type=int, default=DEFAULT_N_SIMS,
                        help=f"Simulaciones Monte Carlo (default: {DEFAULT_N_SIMS})")
    parser.add_argument("--strategy", choices=["momentum", "opportunistic", "both"],
                        default="both", help="Estrategia a correr (default: both)")
    parser.add_argument("--years", type=int, default=BACKTEST_YEARS,
                        help=f"Años de historia (default: {BACKTEST_YEARS})")
    args = parser.parse_args()

    if not os.getenv("TIINGO_API_KEY"):
        print("ERROR: TIINGO_API_KEY no encontrada en .env")
        sys.exit(1)

    cfg = _build_cfg(args.years)
    rules_cfg = RulesConfig()

    print(f"\nBacktest {args.years} años | estrategia: {args.strategy} | Monte Carlo: {args.n_sims:,} sims")
    print(f"Universo: {len(cfg.universe)} símbolos | top_n={cfg.top_n}")
    print(f"Nota: la primera corrida descarga datos de Tiingo (~{len(cfg.universe) * 0.5:.0f}s).\n")

    strategies = (
        ["momentum", "opportunistic"] if args.strategy == "both" else [args.strategy]
    )

    all_results = []
    for strat in strategies:
        try:
            result = _run_strategy(strat, cfg, rules_cfg, args.n_sims)
            all_results.append(result)
            _print_report(result)
        except Exception as exc:
            print(f"\n  ERROR en {strat}: {exc}")
            import traceback
            traceback.print_exc()

    # Guardar JSON
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"backtest_{args.years}yr_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResultados guardados en: {out_path}")


if __name__ == "__main__":
    main()
