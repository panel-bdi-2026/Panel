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
