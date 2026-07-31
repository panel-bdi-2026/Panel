#!/usr/bin/env python3
"""Sweep del REGIMEN DE GATES de la estrategia Oportunista.

Contexto de por que existe este script (2026-07-31):

El backtest tiene dos formas de decidir que simbolos son elegibles cada dia:

  - Absoluto (produccion hoy, backtest_cross_sectional_gates=False): pisos fijos
    -- min_volatility_pct=3.0 de ATR y min_pct_below_52w_high=10.0.
  - Cross-seccional (backtest.py:947, implementado pero apagado): el umbral es
    el percentil 60 de la volatilidad del universo ESE DIA y la mediana del
    %-bajo-maximo. La vara se mueve con la poblacion.

Importa por dos razones concretas:

1. Al probar sumar el S&P MidCap 400 al universo, el piso absoluto de 3.0%
   resulto estar calibrado justo en el borde entre las dos poblaciones: el ATR
   medio del S&P 500 es 2.78% (debajo del piso) y el del S&P 400 es 3.21%
   (encima). O sea que el gate admite midcaps y descarta largecaps de forma
   sistematica, y las midcaps se llevaron el 48.8% de las operaciones siendo el
   40.7% del universo. Un gate cross-seccional no tiene ese problema por
   construccion: si entra una poblacion mas volatil, el percentil 60 sube.

2. Mas grave: la optimizacion v5b (2026-07-07) que fijo el rsi_max=60 que hoy
   corre en produccion se hizo con los gates cross-seccionales ENCENDIDOS
   (optimize_strategy.py:111 los fuerza, y ademas arma ScreenerConfig() desde
   los defaults del codigo en vez de leer screener.yaml). Produccion corre con
   gates absolutos. El parametro vivo se valido en un regimen que no es el que
   se usa.

Este sweep los compara cara a cara sobre la MISMA ventana, el MISMO universo y
los MISMOS folds, leyendo screener.yaml (no los defaults), con DSR corregido por
el tamaño real de la grilla.

Uso:
    cd trading-dashboard/backend
    .venv/bin/python scripts/sweep_gates.py --years 10 --folds 6
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from app.screener_config import ScreenerConfig  # noqa: E402
from sweep_exit_params import _fmt, _run_combo  # noqa: E402

# Eje 1: el piso absoluto, barrido alrededor del 3.0 actual. 2.78 es el ATR
# medio del S&P 500 y 3.21 el del S&P 400 -- el rango cubre ambos lados de esa
# frontera para ver si el valor actual esta cortando la poblacion por la mitad.
# Eje 2: el regimen cross-seccional, que ignora el piso absoluto (solo lo usa de
# fallback si el percentil del dia es NaN), asi que alcanza con probarlo una vez.
COMBOS: list[dict[str, Any]] = [
    {"backtest_cross_sectional_gates": False, "min_volatility_pct": 2.0},
    {"backtest_cross_sectional_gates": False, "min_volatility_pct": 2.5},
    {"backtest_cross_sectional_gates": False, "min_volatility_pct": 3.0},  # produccion
    {"backtest_cross_sectional_gates": False, "min_volatility_pct": 3.5},
    {"backtest_cross_sectional_gates": False, "min_volatility_pct": 4.0},
    {"backtest_cross_sectional_gates": True, "min_volatility_pct": 3.0},   # regimen v5b
]

# Re-validacion de rsi_max en el regimen que produccion USA (absoluto). El valor
# vivo (60) salio de la optimizacion v5b, que corrio con gates cross-seccionales
# y con ScreenerConfig() de defaults en vez de screener.yaml -- nunca se probo en
# el regimen real. Los dos regimenes empatan (ver arriba), asi que lo mas
# probable es que 60 siga siendo correcto, pero no estaba verificado.
# Los pesos score_weight_momentum y score_weight_room_to_grow estan INTERCAMBIADOS
# entre los defaults del codigo y screener.yaml (0.3077 <-> 0.1538). Como
# optimize_strategy.py arma ScreenerConfig() desde los defaults, toda la
# optimizacion v5b corrio con momentum como componente dominante, mientras que
# produccion corre con room_to_grow dominante. Es el peso mas grande del score,
# o sea el criterio principal de ranking -- nunca se comparo cual de los dos
# ordenamientos es mejor. El resto de los pesos no cambia (suman 1.0 igual).
# top_n es la palanca de CONCENTRACION, y nunca se barrio. Es independiente de
# todo lo demas que se probo (universo, gates, señal de entrada): no cambia QUE
# se compra, sino cuanto pesa cada posicion y cuantas señales se descartan por
# falta de cupo (cap_concurrent_positions). Menos posiciones = mas retorno
# esperado y mas drawdown; el punto del barrido es ver donde deja de compensar.
COMBOS_TOPN: list[dict[str, Any]] = [
    {"top_n": 5},
    {"top_n": 8},
    {"top_n": 10},  # produccion
    {"top_n": 15},
    {"top_n": 20},
]

COMBOS_WEIGHTS: list[dict[str, Any]] = [
    # produccion: room_to_grow domina
    {"score_weight_momentum": 0.1538, "score_weight_room_to_grow": 0.3077},
    # defaults del codigo / regimen v5b: momentum domina
    {"score_weight_momentum": 0.3077, "score_weight_room_to_grow": 0.1538},
    # control: ambos iguales, para ver si la asimetria aporta algo
    {"score_weight_momentum": 0.2308, "score_weight_room_to_grow": 0.2308},
]

COMBOS_RSI: list[dict[str, Any]] = [
    {"backtest_cross_sectional_gates": False, "rsi_max": 55.0},
    {"backtest_cross_sectional_gates": False, "rsi_max": 60.0},  # produccion
    {"backtest_cross_sectional_gates": False, "rsi_max": 65.0},
    {"backtest_cross_sectional_gates": False, "rsi_max": 70.0},
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--screener", default="screener.yaml")
    ap.add_argument("--out", default="sweep_gates.json")
    ap.add_argument("--rsi", action="store_true", help="Barre rsi_max en vez del regimen de gates")
    ap.add_argument("--weights", action="store_true", help="Barre los pesos momentum vs room_to_grow")
    ap.add_argument("--topn", action="store_true", help="Barre top_n (concentracion de cartera)")
    args = ap.parse_args()

    path = Path(__file__).resolve().parent.parent / args.screener
    cfg = ScreenerConfig(**yaml.safe_load(path.read_text()))
    combos = (COMBOS_TOPN if args.topn else COMBOS_WEIGHTS if args.weights
              else COMBOS_RSI if args.rsi else COMBOS)
    total = len(combos)

    print(f"Sweep de regimen de gates — {total} combinaciones")
    print(f"Screener: {path}  (universo: {len(cfg.universe)} simbolos)")
    # backtest_years se interpreta como dias de TRADING aguas abajo (~252/año),
    # no calendarios: el factor 365/252 es la conversion real, no un typo.
    print(f"backtest_years={args.years} -> ~{args.years * 365 / 252:.1f} años reales, "
          f"{args.folds} folds walk-forward")
    print(f"DSR calculado con num_trials={total} (corrige el sesgo de seleccion)\n")

    results: list[dict[str, Any]] = []
    for i, ov in enumerate(combos, 1):
        regime = ("cross-seccional" if ov.get("backtest_cross_sectional_gates") else "absoluto")
        label = (f"top_n={ov['top_n']}" if args.topn
                 else f"mom={ov['score_weight_momentum']:.4f} r2g={ov['score_weight_room_to_grow']:.4f}"
                 if args.weights
                 else f"rsi_max={ov['rsi_max']:.0f}" if args.rsi
                 else f"{regime:<15} min_vol={ov['min_volatility_pct']:.1f}")
        print(f"[{i}/{total}] {label} ...", end=" ", flush=True)
        try:
            row = _run_combo(cfg, ov, total, years=args.years, n_folds=args.folds)
            results.append(row)
            print(
                f"wf_shp={row['wf_sharpe']}  (peor fold {row['wf_sharpe_worst']})  "
                f"full_ret={row['full_ret_pct']}%  DSR={row['full_dsr_pct']}%  "
                f"n={row['n_trades']}  hold={row['hold_median_days']}d"
            )
            comp = " | ".join(
                f"{s} {c['pct_de_trades']}% ret{c['avg_ret_pct']:+.2f}"
                for s, c in row["composition"].items()
            )
            print(f"{'':>8} composicion: {comp}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")
            results.append({**ov, "error": str(exc)})

    print("\n" + "=" * 92)
    eje = ("top_n" if args.topn else "mom_w" if args.weights
           else "rsi_max" if args.rsi else "min_vol")
    print(f"{'regimen':<16} {eje:>7} | {'wf_ret':>7} {'wf_shp':>7} {'peor':>6} | "
          f"{'ret':>8} {'dd':>7} {'DSR':>6} | {'n':>5} {'hold':>5}")
    print("-" * 92)
    ok = [r for r in results if "error" not in r]
    best = max(ok, key=lambda r: r["wf_sharpe_worst"] or -99, default=None)
    for r in ok:
        regime = "cross-seccional" if r.get("backtest_cross_sectional_gates") else "absoluto"
        mark = "  <-- mejor peor-fold" if r is best else ""
        print(
            f"{regime:<16} {r.get('top_n', r.get('score_weight_momentum', r.get('rsi_max', r.get('min_volatility_pct')))):>7.4g} | {_fmt(r['wf_ret_pct'])} "
            f"{_fmt(r['wf_sharpe'])} {_fmt(r['wf_sharpe_worst'], 6)} | "
            f"{_fmt(r['full_ret_pct'], 8)} {_fmt(r['full_dd_pct'])} "
            f"{_fmt(r['full_dsr_pct'], 6, 1)} | {r['n_trades']:>5} "
            f"{str(r['hold_median_days']) + 'd':>5}{mark}"
        )
    print("=" * 92)

    if best:
        print(f"\nMejor por PEOR fold: "
              f"{'cross-seccional' if best.get('backtest_cross_sectional_gates') else 'absoluto'}"
              f" {eje}={best.get('top_n', best.get('score_weight_momentum', best.get('rsi_max', best.get('min_volatility_pct'))))}")
        print(f"  Sharpe por fold:  {[f['sharpe'] for f in best['folds']]}")
        print(f"  Retorno por fold: {[f['ret_pct'] for f in best['folds']]}")
        print(f"  Benchmark:        {[f['bench_ret_pct'] for f in best['folds']]}")
        alfa = [round(f["ret_pct"] - f["bench_ret_pct"], 2) for f in best["folds"]]
        print(f"  Alfa por fold:    {alfa}  -> gana en {sum(1 for a in alfa if a > 0)}/{len(alfa)}")
        print(f"  Exits: {best['exits']}")
        print(f"\n  DSR={best['full_dsr_pct']}% — <95% = no demostrado.")

    out = Path(__file__).resolve().parent / "results" / args.out
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResultados guardados en {out}")


if __name__ == "__main__":
    main()
