#!/usr/bin/env python3
"""Corre los backtests de Momentum y Oportunista con el codigo de ESTA copia
del repo (commit que este checked out aca) y guarda un resumen a JSON.

Pensado para comparar el rediseno de scoring antes/despues: se corre una vez
parado en el commit viejo (en otro worktree, sin tocar el deploy en vivo) y
otra vez parado en el commit nuevo, ambas veces contra el mismo screener.yaml,
y despues se comparan los dos JSON con compare_backtest_snapshots.py.

No importa app.config ni nada que dependa de .env (API_KEY, TRADING_MODE,
etc.): solo necesita app.backtest/app.screener_config y sus dependencias, asi
que corre en un worktree limpio sin variables de entorno configuradas.

Uso:
    python scripts/backtest_snapshot.py <etiqueta> [ruta_screener.yaml]

Ejemplo:
    python scripts/backtest_snapshot.py antes
    python scripts/backtest_snapshot.py despues
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import (  # noqa: E402
    BacktestError,
    run_backtest,
    run_backtest_walk_forward,
    run_opportunistic_backtest,
    run_opportunistic_backtest_walk_forward,
)
from app.screener_config import ScreenerConfig  # noqa: E402


def _git_commit(repo_dir: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _summary_to_dict(summary) -> dict:
    return {
        "total_trades": summary.total_trades,
        "win_rate_pct": summary.win_rate_pct,
        "avg_return_pct": summary.avg_return_pct,
        "avg_win_pct": summary.avg_win_pct,
        "avg_loss_pct": summary.avg_loss_pct,
        "profit_factor": summary.profit_factor,
        "profit_factor_is_infinite": summary.profit_factor_is_infinite,
        "expectancy_pct": summary.expectancy_pct,
        "strategy_cumulative_return_pct": summary.strategy_cumulative_return_pct,
        "benchmark_cumulative_return_pct": summary.benchmark_cumulative_return_pct,
        "max_drawdown_pct": summary.max_drawdown_pct,
        "sharpe_ratio": summary.sharpe_ratio,
        "avg_exposure_pct": summary.avg_exposure_pct,
        # Desglose por que cerro cada operacion (stop_loss/max_holding_days/
        # trend_break, ver BacktestTrade.exit_reason): diagnostico para saber
        # que tocar a continuacion (ATR del stop, max_holding_days, o la SMA
        # rapida de la ruptura de tendencia) en vez de ajustar a ciegas. Viene de
        # summary.exit_reason_counts (calculado en backtest.py sobre TODAS las
        # operaciones), no de summary.trades (que se trunca a las ultimas 50).
        "exit_reason_counts": dict(summary.exit_reason_counts),
    }


def _walk_forward_to_dict(result) -> dict:
    return {
        "n_folds": result.n_folds,
        "folds": [fold.model_dump(mode="json") for fold in result.folds],
    }


def main() -> None:
    if len(sys.argv) < 2:
        print("Uso: python scripts/backtest_snapshot.py <etiqueta> [ruta_screener.yaml]")
        sys.exit(1)
    label = sys.argv[1]
    backend_dir = Path(__file__).resolve().parent.parent
    screener_path = Path(sys.argv[2]) if len(sys.argv) > 2 else backend_dir / "screener.yaml"

    cfg = ScreenerConfig.load(screener_path)

    results: dict = {
        "label": label,
        "commit": _git_commit(backend_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "screener_path": str(screener_path),
        "strategies": {},
    }

    print(f"[{label}] commit {results['commit']} -- corriendo backtest de Momentum...")
    try:
        momentum_summary = run_backtest(cfg)
        results["strategies"]["momentum"] = _summary_to_dict(momentum_summary)
        print(f"[{label}] Momentum: {momentum_summary.total_trades} operaciones.")
        # Walk-forward reusa los datos de mercado ya cacheados por run_backtest
        # (mismo cfg.lookback_days): no pega de nuevo a la red, solo resimula.
        try:
            momentum_wf = run_backtest_walk_forward(cfg)
            results["strategies"]["momentum"]["walk_forward"] = _walk_forward_to_dict(momentum_wf)
        except BacktestError as exc:
            print(f"[{label}] Momentum walk-forward fallo: {exc}")
    except BacktestError as exc:
        results["strategies"]["momentum"] = {"error": str(exc)}
        print(f"[{label}] Momentum fallo: {exc}")

    print(f"[{label}] corriendo backtest de Oportunista...")
    try:
        opportunistic_summary = run_opportunistic_backtest(cfg)
        results["strategies"]["opportunistic"] = _summary_to_dict(opportunistic_summary)
        print(f"[{label}] Oportunista: {opportunistic_summary.total_trades} operaciones.")
        try:
            opportunistic_wf = run_opportunistic_backtest_walk_forward(cfg)
            results["strategies"]["opportunistic"]["walk_forward"] = _walk_forward_to_dict(opportunistic_wf)
        except BacktestError as exc:
            print(f"[{label}] Oportunista walk-forward fallo: {exc}")
    except BacktestError as exc:
        results["strategies"]["opportunistic"] = {"error": str(exc)}
        print(f"[{label}] Oportunista fallo: {exc}")

    out_path = Path(f"/tmp/backtest_{label}.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"[{label}] resultados guardados en {out_path}")


if __name__ == "__main__":
    main()
