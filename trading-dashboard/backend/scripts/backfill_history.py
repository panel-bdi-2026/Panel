"""Backfill de historia completa (2002→hoy) del caché Parquet de Tiingo.

Contexto: los parquets creados por el servicio live solo cubren ~2 años
(desde sept 2024), y el backfill automático de get_daily_bars fallaba en
silencio con PermissionError (archivos 644 de `trading`, proceso corriendo
como `claude-rc`) dentro de su `except: pass`. Resultado: todos los backtests
"históricos" simularon 2003-2024 con solo 4 acciones (AAPL/MSFT/NFLX/TSLA).

Este script baja el tramo faltante por símbolo y escribe vía archivo temporal
+ os.replace (solo requiere permiso de escritura en el DIRECTORIO, que el
grupo `trading` sí tiene), con chmod 664 para que el servicio siga pudiendo
leer/escribir. Reanudable: saltea símbolos cuya historia ya arranca ≤ 2003.

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/backfill_history.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import pandas as pd

from app.market_data import _tiingo_bars
from app.screener_config import ScreenerConfig
from app.sectors import SECTOR_ETF
from app import tiingo_disk_cache as disk

START = date(2002, 7, 1)
# Historia "completa" = arranca antes de 2003-06-30 (o es todo lo que Tiingo
# tiene para símbolos con IPO posterior — se detecta por respuesta corta).
COMPLETE_BEFORE = date(2003, 6, 30)
PAUSE_S = 0.3
MAX_RETRIES = 3


def _write_merged(symbol: str, new_df: pd.DataFrame) -> None:
    """Merge con parquet existente y escritura atómica vía tmp + os.replace.

    A diferencia de disk.upsert, no necesita permiso de escritura sobre el
    archivo destino (solo sobre el directorio), y deja el resultado con 664
    para que el usuario de servicio `trading` pueda seguir escribiéndolo.
    """
    p = disk._path(symbol)
    existing = disk.load(symbol)
    new_df = disk._to_utc(new_df)
    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = new_df.sort_index()
    tmp = p.with_suffix(".parquet.tmp")
    combined.to_parquet(tmp, compression="snappy")
    os.chmod(tmp, 0o664)
    os.replace(tmp, p)


def main() -> None:
    cfg = ScreenerConfig()
    symbols = list(dict.fromkeys(
        cfg.universe
        + [cfg.benchmark_symbol]
        + sorted(set(SECTOR_ETF.values()))
    ))
    print(f"Backfill de {len(symbols)} símbolos desde {START}...")

    ok = skipped = failed = no_history = 0
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        cov = disk.coverage(sym)
        if cov is not None and cov[0] <= COMPLETE_BEFORE:
            skipped += 1
            continue

        # Tramo faltante: desde START hasta el día anterior al primer dato
        # existente (o hasta hoy si no hay parquet).
        end = cov[0] if cov is not None else date.today()

        df = None
        for attempt in range(MAX_RETRIES):
            try:
                df = _tiingo_bars(sym, START, end)
                break
            except Exception as exc:
                if attempt == MAX_RETRIES - 1:
                    print(f"  [{i}/{len(symbols)}] {sym}: FALLÓ tras {MAX_RETRIES} intentos: {exc}")
                    failed += 1
                else:
                    time.sleep(5 * (attempt + 1))
        if df is None:
            continue
        if df.empty:
            # Símbolo sin historia previa al parquet actual (IPO reciente).
            no_history += 1
        else:
            try:
                _write_merged(sym, df)
                ok += 1
            except Exception as exc:
                print(f"  [{i}/{len(symbols)}] {sym}: ERROR escribiendo: {exc}")
                failed += 1

        if i % 50 == 0:
            el = time.time() - t0
            print(f"  [{i}/{len(symbols)}] ok={ok} skip={skipped} sin_hist={no_history}"
                  f" fail={failed} ({el:.0f}s)", flush=True)
        time.sleep(PAUSE_S)

    print(f"\nListo en {time.time()-t0:.0f}s: backfilleados={ok}, ya completos={skipped},"
          f" sin historia previa (IPO reciente)={no_history}, fallidos={failed}")

    # Verificación final de cobertura
    from collections import Counter
    firsts = Counter()
    for sym in symbols:
        cov = disk.coverage(sym)
        if cov is None:
            firsts["sin_parquet"] += 1
        else:
            firsts[str(cov[0].year)] += 1
    print("\nCobertura final (año de la primera barra → símbolos):")
    for k in sorted(firsts):
        print(f"  {k}: {firsts[k]}")


if __name__ == "__main__":
    main()
