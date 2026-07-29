#!/usr/bin/env python3
"""Sweep de take_profit_pct × score_exit_threshold para la estrategia Oportunista.

Corre run_opportunistic_backtest_walk_forward() con cada combinación de
parámetros y muestra los resultados como tabla. Útil para calibrar los nuevos
parámetros de salida antes de subirlos a screener.yaml.

Uso:
    cd trading-dashboard/backend
    .venv/bin/python scripts/sweep_exit_params.py [ruta_screener.yaml]

Columnas de la tabla:
    tp    — take_profit_pct
    se    — score_exit_threshold
    ret%  — retorno acumulado (strategy_cumulative_return_pct), promedio entre folds
    dd%   — max drawdown (max_drawdown_pct), promedio entre folds
    shp   — Sharpe ratio, promedio entre folds
    win%  — win rate %, promedio entre folds
    n     — número total de trades (suma de folds)
    exits — desglose de exit reasons (stop/tp/maxdays/score_exit/sector)
"""
from __future__ import annotations

import json
import sys
from itertools import product
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import (  # noqa: E402
    run_opportunistic_backtest,
    run_opportunistic_backtest_walk_forward,
)
from app.screener_config import ScreenerConfig  # noqa: E402


TAKE_PROFIT_VALUES = [0.0, 7.0, 10.0, 12.0, 15.0, 20.0]
SCORE_EXIT_VALUES = [0.0, 25.0, 35.0, 45.0]


def _avg(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def _run_combo(cfg: ScreenerConfig, tp: float, se: float) -> dict[str, Any]:
    patched_opp = cfg.opportunistic.model_copy(update={
        "take_profit_pct": tp,
        "score_exit_threshold": se,
    })
    patched_cfg = cfg.model_copy(update={"opportunistic": patched_opp})

    # Walk-forward: métricas por período para detectar overfitting
    wf = run_opportunistic_backtest_walk_forward(patched_cfg)
    rets, dds, shps, wins, ns = [], [], [], [], []
    for fold in wf.folds:
        rets.append(fold.strategy_cumulative_return_pct)
        dds.append(fold.max_drawdown_pct)
        shps.append(fold.sharpe_ratio)
        wins.append(fold.win_rate_pct)
        ns.append(fold.total_trades)

    # Backtest completo: exit_reason_counts (no disponible en folds individuales)
    full = run_opportunistic_backtest(patched_cfg)

    return {
        "tp": tp,
        "se": se,
        "ret_pct": _avg(rets),
        "dd_pct": _avg(dds),
        "sharpe": _avg(shps),
        "win_pct": _avg(wins),
        "n_trades": sum(ns),
        "n_folds": wf.n_folds,
        "exits": dict(full.exit_reason_counts),
        "full_ret_pct": full.strategy_cumulative_return_pct,
        "full_dd_pct": full.max_drawdown_pct,
        "full_sharpe": full.sharpe_ratio,
    }


def _fmt(v: float | None, width: int = 7, decimals: int = 2) -> str:
    if v is None:
        return " " * width
    return f"{v:{width}.{decimals}f}"


def main() -> None:
    backend_dir = Path(__file__).resolve().parent.parent
    screener_path = Path(sys.argv[1]) if len(sys.argv) > 1 else backend_dir / "screener.yaml"
    cfg = ScreenerConfig.load(screener_path)

    combos = list(product(TAKE_PROFIT_VALUES, SCORE_EXIT_VALUES))
    total = len(combos)
    results: list[dict[str, Any]] = []

    print(f"Sweep: {total} combinaciones — take_profit × score_exit")
    print(f"Screener: {screener_path}\n")

    for i, (tp, se) in enumerate(combos, 1):
        label = f"tp={tp:.0f}% se={se:.0f}"
        print(f"[{i:2d}/{total}] {label} ...", end=" ", flush=True)
        try:
            row = _run_combo(cfg, tp, se)
            results.append(row)
            print(
                f"ret={_fmt(row['ret_pct']).strip()}%  "
                f"dd={_fmt(row['dd_pct']).strip()}%  "
                f"shp={_fmt(row['sharpe']).strip()}  "
                f"n={row['n_trades']}"
            )
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append({"tp": tp, "se": se, "error": str(exc)})

    # ── Tabla resumen ─────────────────────────────────────────────────────────
    print("\n" + "=" * 78)
    print(f"{'tp':>5}  {'se':>5}  {'ret%':>7}  {'dd%':>7}  {'shp':>6}  {'win%':>6}  {'n':>5}  exits")
    print("-" * 78)

    ok = [r for r in results if "error" not in r]
    best_sharpe = max((r["sharpe"] or -99) for r in ok) if ok else None

    for r in results:
        if "error" in r:
            print(f"{r['tp']:>5.0f}  {r['se']:>5.0f}  ERROR: {r['error'][:40]}")
            continue
        marker = " ◀ mejor Sharpe" if r["sharpe"] == best_sharpe else ""
        exits_str = " ".join(f"{k}:{v}" for k, v in sorted(r["exits"].items()))
        print(
            f"{r['tp']:>5.0f}  {r['se']:>5.0f}  "
            f"{_fmt(r['ret_pct'])}  "
            f"{_fmt(r['dd_pct'])}  "
            f"{_fmt(r['sharpe'], 6)}  "
            f"{_fmt(r['win_pct'], 6)}  "
            f"{r['n_trades']:>5}  {exits_str}{marker}"
        )

    print("=" * 78)

    out_path = backend_dir / "scripts" / "results" / "sweep_exit_params.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResultados guardados en {out_path}")


if __name__ == "__main__":
    main()
