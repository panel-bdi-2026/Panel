#!/usr/bin/env python3
"""Sweep de parámetros de salida para la estrategia Oportunista.

Corre una grilla de combinaciones y reporta walk-forward + backtest completo
por cada una. Útil para calibrar parámetros de salida antes de subirlos a
screener.yaml.

Uso:
    cd trading-dashboard/backend
    .venv/bin/python scripts/sweep_exit_params.py [ruta_screener.yaml]

Dos detalles de método que importan para no auto-engañarse:

1. `_collect_opportunistic_trades` (simular 503 símbolos) es el 95% del costo
   y es idéntico para el summary y para el walk-forward. Las funciones públicas
   `run_opportunistic_backtest` / `run_opportunistic_backtest_walk_forward` lo
   llaman cada una por su lado, así que usarlas las dos duplicaba el trabajo:
   acá se colecta UNA vez y se alimentan los dos cálculos (~2x más rápido).

2. `deflated_sharpe_num_trials` se fuerza al tamaño real de la grilla. El
   Sharpe crudo del mejor combo de una grilla de N está sesgado al alza por
   selección: cuantas más combinaciones se prueban, más alto es el mejor
   Sharpe esperado aunque ninguna tenga alfa real. El DSR corrige ese sesgo
   (ver _deflated_sharpe_ratio_pct en backtest.py) y es la cifra a mirar para
   decidir, no el Sharpe crudo.
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import (  # noqa: E402
    _build_walk_forward_result,
    _collect_opportunistic_trades,
    _compute_summary_stats,
)
from app.screener_config import ScreenerConfig  # noqa: E402


# take_profit_pct quedó fijo en 0: el sweep anterior (6 valores × 4 de
# score_exit) mostró que CUALQUIER take-profit degrada retorno y Sharpe
# respecto de no tenerlo, en las 20 combinaciones con tp>0. Tiene tesis:
# Oportunista compra giros al alza, y los mejores trades son justo los que
# necesitan semanas para desarrollarse — cortarlos a +7/+10% mata la cola
# derecha. Se deja el eje fuera de la grilla para gastar el cómputo en los
# dos ejes que sí discriminan.
# El sweep de 18 combos sobre ~4.8 años dejó dos conclusiones que acotan la
# grilla, y una que la reorienta entera:
#
#  - max_holding_days se queda en 20. Subirlo a 30 daba Sharpe promedio apenas
#    mayor (1.35 vs 1.32) pero PEOR fold mínimo (0.68 vs 0.80) y peor DSR, y
#    alarga la duración de las posiciones en contra de la tesis de Oportunista
#    (alta rotación, días/semanas). No se vuelve a barrer.
#
#  - score_exit=55 fue el único valor que superó al baseline, pero es un pico
#    angosto (57 y 58 quedan POR DEBAJO del baseline) y su ventaja se concentra
#    en un solo fold de tres. Se lleva a la ventana larga como hipótesis única
#    a validar, con 50 y 60 al lado para ver si el pico sobrevive o se aplana.
#
#  - Lo decisivo: el DSR nunca pasó de 79.8% (hace falta >95%). Con ~4.8 años
#    y 3 folds no hay poder estadístico, y cada combo extra que se prueba sobre
#    la misma ventana EMPEORA el DSR de todos. Por eso esta grilla es chica a
#    propósito y corre sobre ~16 años: el objetivo ya no es buscar el máximo,
#    es ver si lo que encontramos sobrevive a otros regímenes de mercado.
GRID: dict[str, list[float]] = {
    "score_exit_threshold": [0.0, 50.0, 55.0, 60.0],
    # Rango alto pedido explícitamente: la hipótesis es que un take-profit
    # chico corta la cola derecha (confirmado: 7-20% degradó todo), pero uno
    # alto solo actuaría sobre movimientos ya extendidos, donde la tesis de
    # "comprar el giro" está agotada y suele haber reversión. Nota: a medida
    # que sube, el parámetro se vuelve inerte (dispara en menos trades) y el
    # resultado converge por construcción al de tp=0.
    "take_profit_pct": [0.0, 25.0, 30.0],
}
# Fijo, no se barre (ver arriba).
FIXED: dict[str, float] = {"max_holding_days": 20}


def _avg(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def _run_combo(
    cfg: ScreenerConfig, overrides: dict[str, Any], num_trials: int,
    years: int | None = None, n_folds: int = 3,
) -> dict[str, Any]:
    patched_opp = cfg.opportunistic.model_copy(update={**FIXED, **overrides})
    cfg_update: dict[str, Any] = {
        "opportunistic": patched_opp,
        "deflated_sharpe_num_trials": num_trials,
    }
    if years is not None:
        cfg_update["backtest_years"] = years
    patched_cfg = cfg.model_copy(update=cfg_update)

    # Simulación de los 503 símbolos: una sola vez para ambas métricas.
    all_trades, marks, bench_bars = _collect_opportunistic_trades(patched_cfg)

    summary = _compute_summary_stats(
        all_trades, patched_cfg.top_n, bench_bars, marks,
        patched_cfg.invest_idle_cash_in_benchmark,
        patched_cfg.backtest_vol_weighting_enabled,
        patched_cfg.deflated_sharpe_num_trials,
        risk_based_sizing_enabled=patched_cfg.backtest_risk_based_sizing_enabled,
        assumed_capital_usd=patched_cfg.backtest_assumed_capital_usd,
    )
    wf = _build_walk_forward_result(
        all_trades, patched_cfg.top_n, bench_bars, marks, n_folds,
        patched_cfg.invest_idle_cash_in_benchmark,
        patched_cfg.backtest_vol_weighting_enabled,
        risk_based_sizing_enabled=patched_cfg.backtest_risk_based_sizing_enabled,
        assumed_capital_usd=patched_cfg.backtest_assumed_capital_usd,
    )

    # Se guarda el detalle POR FOLD, no solo el promedio: un promedio alto
    # sostenido por un solo fold bueno y dos mediocres es una configuración
    # frágil, y con el promedio solo no se distingue de una pareja.
    folds = [
        {
            "start": f.start_date.date().isoformat(),
            "end": f.end_date.date().isoformat(),
            "n_trades": f.total_trades,
            "ret_pct": f.strategy_cumulative_return_pct,
            "bench_ret_pct": f.benchmark_cumulative_return_pct,
            "dd_pct": f.max_drawdown_pct,
            "sharpe": f.sharpe_ratio,
            "win_pct": f.win_rate_pct,
        }
        for f in wf.folds
    ]
    fold_sharpes = [f["sharpe"] for f in folds]

    # Días efectivamente en posición. Subir max_holding_days sube el TECHO,
    # no necesariamente la duración real: si score_exit ya cierra la mayoría
    # antes del tope, la estrategia sigue siendo de días/semanas aunque el
    # tope diga 40. Esta es la métrica que define si se respeta la identidad
    # de Oportunista (alta rotación, corto plazo), no el valor del parámetro.
    held = sorted((t.exit_date - t.entry_date).days for t in all_trades)
    n = len(held)

    return {
        **overrides,
        "hold_avg_days": round(sum(held) / n, 1) if n else None,
        "hold_median_days": held[n // 2] if n else None,
        "hold_p90_days": held[int(n * 0.9)] if n else None,
        "wf_ret_pct": _avg([f["ret_pct"] for f in folds]),
        "wf_dd_pct": _avg([f["dd_pct"] for f in folds]),
        "wf_sharpe": _avg(fold_sharpes),
        # Peor fold: criterio de robustez. Entre dos configs de igual promedio
        # se prefiere la que no se derrumba en su peor período.
        "wf_sharpe_worst": min((s for s in fold_sharpes if s is not None), default=None),
        "wf_win_pct": _avg([f["win_pct"] for f in folds]),
        "full_ret_pct": summary.strategy_cumulative_return_pct,
        "full_dd_pct": summary.max_drawdown_pct,
        "full_sharpe": summary.sharpe_ratio,
        "full_dsr_pct": summary.deflated_sharpe_ratio_pct,
        "full_win_pct": summary.win_rate_pct,
        "n_trades": summary.total_trades,
        "exits": dict(summary.exit_reason_counts),
        "folds": folds,
    }


def _fmt(v: float | None, width: int = 7, decimals: int = 2) -> str:
    return " " * width if v is None else f"{v:{width}.{decimals}f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screener", nargs="?", default=None)
    # OJO con la unidad: backtest.py hace `int(backtest_years * 365)` y lo pasa
    # a get_daily_bars, que interpreta ese número como días DE TRADING (~252
    # por año), no calendario. O sea que cada "año" acá vale ~1.45 años reales:
    # --years 3 da ~4.8 años y --years 10 da ~16. Se deja el parámetro tal cual
    # para no cambiar el significado de screener.yaml desde un script, pero la
    # equivalencia se imprime al arrancar para que no se lea mal la tabla.
    parser.add_argument("--years", type=int, default=None,
                        help="override de backtest_years (ojo: ~1.45x en años reales)")
    parser.add_argument("--folds", type=int, default=3,
                        help="ventanas del walk-forward; subirlo con historia larga")
    parser.add_argument("--out", default="sweep_exit_params.json")
    args = parser.parse_args()

    backend_dir = Path(__file__).resolve().parent.parent
    screener_path = Path(args.screener) if args.screener else backend_dir / "screener.yaml"
    cfg = ScreenerConfig.load(screener_path)

    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in product(*GRID.values())]
    total = len(combos)
    results: list[dict[str, Any]] = []

    years = args.years if args.years is not None else cfg.backtest_years
    print(f"Sweep: {total} combinaciones — {' × '.join(keys)}")
    print(f"Fijo: {FIXED}")
    print(f"Screener: {screener_path}")
    print(f"backtest_years={years} -> ~{years * 365 / 252:.1f} años reales, "
          f"{args.folds} folds walk-forward")
    print(f"DSR calculado con num_trials={total} (corrige el sesgo de selección)\n")

    for i, overrides in enumerate(combos, 1):
        label = " ".join(f"{k.split('_')[0]}={v:g}" for k, v in overrides.items())
        print(f"[{i:2d}/{total}] {label} ...", end=" ", flush=True)
        try:
            row = _run_combo(cfg, overrides, total, years=args.years, n_folds=args.folds)
            results.append(row)
            print(
                f"wf_shp={row['wf_sharpe']}  (peor fold {row['wf_sharpe_worst']})  "
                f"full_ret={row['full_ret_pct']}%  DSR={row['full_dsr_pct']}%  "
                f"n={row['n_trades']}  hold={row['hold_median_days']}d (p90 {row['hold_p90_days']}d)"
            )
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append({**overrides, "error": str(exc)})
        # Se persiste en cada iteración: un sweep de horas no debe perderse
        # entero si algo falla a mitad de camino.
        out_path = backend_dir / "scripts" / "results" / args.out
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2))

    # ── Tabla resumen ─────────────────────────────────────────────────────────
    gcols = "".join(f"{k.split('_')[0]:>6}" for k in keys)
    hdr = (f"{gcols} | {'wf_ret':>7} {'wf_shp':>7} {'peor':>6} | "
           f"{'ret':>8} {'dd':>7} {'shp':>6} {'DSR':>6} | {'n':>5} {'hold':>5}")
    print("\n" + "=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    ok = [r for r in results if "error" not in r]
    # Criterio de selección: el PEOR fold, no el promedio. Con ventana larga y
    # varios regímenes, la config que menos se derrumba en su peor período es
    # la que tiene más chance de sobrevivir en vivo (ver fold 3 del sweep de
    # 4.8 años: el promedio escondía que toda la ventaja venía de un período).
    best = max(ok, key=lambda r: r["wf_sharpe_worst"] or -99) if ok else None

    for r in results:
        vals = "".join(f"{r.get(k, 0):>6.0f}" for k in keys)
        if "error" in r:
            print(f"{vals} | ERROR: {r['error'][:50]}")
            continue
        marker = "  ◀" if r is best else ""
        print(
            f"{vals} | {_fmt(r['wf_ret_pct'])} {_fmt(r['wf_sharpe'])} {_fmt(r['wf_sharpe_worst'], 6)} | "
            f"{_fmt(r['full_ret_pct'], 8)} {_fmt(r['full_dd_pct'])} {_fmt(r['full_sharpe'], 6)} "
            f"{_fmt(r['full_dsr_pct'], 6, 1)} | {r['n_trades']:>5} {r['hold_median_days']:>4}d{marker}"
        )
    print("=" * len(hdr))

    if best:
        cfg_desc = " ".join(f"{k}={best[k]:g}" for k in keys)
        print(f"\nMejor por PEOR fold: {cfg_desc}")
        print(f"  Sharpe por fold:  {[f['sharpe'] for f in best['folds']]}")
        print(f"  Retorno por fold: {[f['ret_pct'] for f in best['folds']]}")
        print(f"  Benchmark:        {[f['bench_ret_pct'] for f in best['folds']]}")
        alfas = [round(f["ret_pct"] - f["bench_ret_pct"], 2) for f in best["folds"]
                 if f["ret_pct"] is not None and f["bench_ret_pct"] is not None]
        won = sum(1 for a in alfas if a > 0)
        print(f"  Alfa por fold:    {alfas}  -> gana en {won}/{len(alfas)}")
        print(f"  Duración: mediana {best['hold_median_days']}d, p90 {best['hold_p90_days']}d, "
              f"promedio {best['hold_avg_days']}d")
        print(f"  Exits: {dict(sorted(best['exits'].items()))}")
        print(f"\n  DSR={best['full_dsr_pct']}% — probabilidad de que el Sharpe sea real y no")
        print(f"  producto de haber probado {total} combinaciones. <95% = no demostrado.")

    print(f"\nResultados guardados en {out_path}")


if __name__ == "__main__":
    main()
