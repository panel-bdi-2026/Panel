#!/usr/bin/env python3
"""Compara dos JSON generados por backtest_snapshot.py (uno por version de
codigo) e imprime una tabla de antes/despues por estrategia y metrica.

Uso:
    python scripts/compare_backtest_snapshots.py /tmp/backtest_antes.json /tmp/backtest_despues.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_METRICS = [
    ("total_trades", "Operaciones totales", "{:.0f}", False),
    ("win_rate_pct", "Win rate (%)", "{:.1f}", True),
    ("avg_return_pct", "Retorno promedio por operacion (%)", "{:.2f}", True),
    ("avg_win_pct", "Ganancia promedio (%)", "{:.2f}", True),
    ("avg_loss_pct", "Perdida promedio (%)", "{:.2f}", True),
    ("profit_factor", "Profit factor", "{:.2f}", True),
    ("expectancy_pct", "Expectancy (%)", "{:.2f}", True),
    ("strategy_cumulative_return_pct", "Retorno acumulado estrategia (%)", "{:.1f}", True),
    ("benchmark_cumulative_return_pct", "Retorno acumulado benchmark (%)", "{:.1f}", False),
    ("max_drawdown_pct", "Max drawdown (%)", "{:.1f}", True),
    ("sharpe_ratio", "Sharpe ratio", "{:.2f}", True),
    ("avg_exposure_pct", "Exposicion promedio (%)", "{:.1f}", False),
]


def _fmt(value, fmt: str) -> str:
    if value is None:
        return "N/A"
    return fmt.format(value)


def _compare_strategy(name: str, before: dict, after: dict) -> None:
    print(f"\n=== {name} ===")
    if "error" in before or "error" in after:
        if "error" in before:
            print(f"  ANTES fallo: {before['error']}")
        if "error" in after:
            print(f"  DESPUES fallo: {after['error']}")
        return

    header = f"{'Metrica':38} {'Antes':>14} {'Despues':>14} {'Delta':>12}"
    print(header)
    print("-" * len(header))
    for key, label, fmt, higher_is_better in _METRICS:
        b_val = before.get(key)
        a_val = after.get(key)
        delta_str = ""
        if isinstance(b_val, (int, float)) and isinstance(a_val, (int, float)):
            delta = a_val - b_val
            sign = "+" if delta >= 0 else ""
            arrow = ""
            if higher_is_better and delta != 0:
                arrow = " mejor" if delta > 0 else " peor"
            delta_str = f"{sign}{fmt.format(delta)}{arrow}"
        print(f"{label:38} {_fmt(b_val, fmt):>14} {_fmt(a_val, fmt):>14} {delta_str:>12}")

    if before.get("profit_factor_is_infinite") or after.get("profit_factor_is_infinite"):
        print("  (profit_factor 'infinito' = no hubo operaciones perdedoras en ese periodo)")

    _compare_exit_reasons(before.get("exit_reason_counts"), after.get("exit_reason_counts"))
    _compare_walk_forward(before.get("walk_forward"), after.get("walk_forward"))


def _compare_exit_reasons(before: dict | None, after: dict | None) -> None:
    """Por que se cerro cada operacion (ver BacktestTrade.exit_reason):
    distingue stop_loss (stop demasiado ajustado/ancho) de max_holding_days
    (el limite de tiempo corta antes de que el score confirme la salida) de
    score_exit (la salida 'normal', dirigida por el umbral de score)."""
    if not before and not after:
        return
    before, after = before or {}, after or {}
    b_total = sum(before.values()) or 1
    a_total = sum(after.values()) or 1
    reasons = sorted(set(before) | set(after))
    if not reasons:
        return
    print("\n  -- Motivo de salida --")
    for reason in reasons:
        b_n, a_n = before.get(reason, 0), after.get(reason, 0)
        print(
            f"  {reason:18} antes {b_n:4d} ({100 * b_n / b_total:4.1f}%)"
            f"   despues {a_n:4d} ({100 * a_n / a_total:4.1f}%)"
        )


def _compare_walk_forward(before: dict | None, after: dict | None) -> None:
    """Folds consecutivos (misma duracion calendario cada uno, ver
    run_backtest_walk_forward): si la mejora del periodo completo no se
    sostiene en folds individuales, es probable que dependa de un tramo
    puntual favorable en vez de ser una mejora real y consistente."""
    if not before or not after:
        return
    b_folds = before.get("folds", [])
    a_folds = after.get("folds", [])
    if not b_folds or not a_folds:
        return
    print(f"\n  -- Walk-forward ({before.get('n_folds')} folds) --")
    for i in range(min(len(b_folds), len(a_folds))):
        bf, af = b_folds[i], a_folds[i]
        b_start, b_end = str(bf.get("start_date", ""))[:10], str(bf.get("end_date", ""))[:10]
        b_ret, a_ret = bf.get("strategy_cumulative_return_pct"), af.get("strategy_cumulative_return_pct")
        b_ret_str = f"{b_ret:.1f}%" if isinstance(b_ret, (int, float)) else "N/A"
        a_ret_str = f"{a_ret:.1f}%" if isinstance(a_ret, (int, float)) else "N/A"
        b_sharpe, a_sharpe = bf.get("sharpe_ratio"), af.get("sharpe_ratio")
        b_sharpe_str = f"{b_sharpe:.2f}" if isinstance(b_sharpe, (int, float)) else "N/A"
        a_sharpe_str = f"{a_sharpe:.2f}" if isinstance(a_sharpe, (int, float)) else "N/A"
        print(
            f"  Fold {i + 1} ({b_start}..{b_end}): "
            f"antes {bf.get('total_trades')} ops / ret {b_ret_str} / sharpe {b_sharpe_str}"
            "   ->   "
            f"despues {af.get('total_trades')} ops / ret {a_ret_str} / sharpe {a_sharpe_str}"
        )


def main() -> None:
    if len(sys.argv) != 3:
        print("Uso: python scripts/compare_backtest_snapshots.py <antes.json> <despues.json>")
        sys.exit(1)

    before_data = json.loads(Path(sys.argv[1]).read_text())
    after_data = json.loads(Path(sys.argv[2]).read_text())

    print(f"ANTES:   commit {before_data.get('commit')}  ({before_data.get('generated_at')})")
    print(f"DESPUES: commit {after_data.get('commit')}  ({after_data.get('generated_at')})")

    strategy_labels = {"momentum": "Momentum", "opportunistic": "Oportunista"}
    for key, label in strategy_labels.items():
        before_strategy = before_data.get("strategies", {}).get(key)
        after_strategy = after_data.get("strategies", {}).get(key)
        if before_strategy is None or after_strategy is None:
            print(f"\n=== {label} ===\n  Sin datos en uno de los dos JSON.")
            continue
        _compare_strategy(label, before_strategy, after_strategy)


if __name__ == "__main__":
    main()
