"""Caché persistente en disco de barras EOD de Tiingo (Parquet por ticker).

Persiste entre reinicios del proceso y entre corridas del script de backtest.
Sin este caché, el backtest re-descarga 22 años × 589 tickers de Tiingo en
cada corrida (~40 min); con él, la primera corrida los guarda y las siguientes
los leen del disco en segundos.

Directorio: backend/data/tiingo_eod/{SYMBOL}.parquet
Formato: igual que _tiingo_bars() en market_data.py — DatetimeIndex UTC con
nombre "Date", columnas Open/High/Low/Close/Volume (ajustadas por splits y
dividendos). Compresión Snappy (~5-15 KB por símbolo con 20 años de historia).

Thread safety: un threading.Lock por símbolo protege las escrituras. Las
lecturas son lock-free (Parquet es inmutable entre escrituras; en el peor
caso se lee una versión ligeramente desactualizada, sin corrupción).
"""
from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "tiingo_eod"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()


def _write_lock(symbol: str) -> threading.Lock:
    with _write_locks_guard:
        if symbol not in _write_locks:
            _write_locks[symbol] = threading.Lock()
        return _write_locks[symbol]


def _path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.parquet"


def load(symbol: str) -> pd.DataFrame | None:
    """DataFrame completo del símbolo desde disco, o None si no existe.

    Garantiza que el índice sea siempre DatetimeIndex UTC-aware,
    independientemente de cómo pyarrow serialice la zona horaria.
    """
    p = _path(symbol)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        elif str(df.index.tz) != "UTC":
            df.index = df.index.tz_convert("UTC")
        return df
    except Exception:
        return None


def coverage(symbol: str) -> tuple[date, date] | None:
    """(primera_fecha, última_fecha) del caché en disco, o None si vacío."""
    df = load(symbol)
    if df is None or df.empty:
        return None
    return df.index.min().date(), df.index.max().date()


def is_sufficient(symbol: str, needed_start: date) -> bool:
    """True si el caché cubre desde needed_start hasta ayer (incluido)."""
    cov = coverage(symbol)
    if cov is None:
        return False
    first, last = cov
    yesterday = date.today() - timedelta(days=1)
    return first <= needed_start and last >= yesterday


def slice_from(symbol: str, needed_start: date) -> pd.DataFrame | None:
    """Carga el caché y devuelve solo las filas desde needed_start en adelante."""
    df = load(symbol)
    if df is None or df.empty:
        return None
    cutoff = pd.Timestamp(needed_start, tz="UTC")
    sliced = df[df.index >= cutoff]
    return sliced if not sliced.empty else None


def _to_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza el índice a UTC-aware in-place (copia si hace falta)."""
    if df.index.tz is None:
        df = df.copy()
        df.index = df.index.tz_localize("UTC")
    elif str(df.index.tz) != "UTC":
        df = df.copy()
        df.index = df.index.tz_convert("UTC")
    return df


def upsert(symbol: str, new_df: pd.DataFrame) -> None:
    """Escribe new_df al caché, mergeando con datos existentes.

    Nunca borra datos existentes: si ya hay barras para una fecha en el
    archivo, la versión nueva de new_df tiene precedencia (keep='last').
    Normaliza el índice a UTC-aware antes de guardar.
    """
    if new_df is None or new_df.empty:
        return
    new_df = _to_utc(new_df)
    p = _path(symbol)
    with _write_lock(symbol):
        if p.exists():
            try:
                existing = pd.read_parquet(p)
                existing = _to_utc(existing)
                combined = pd.concat([existing, new_df])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            except Exception:
                combined = new_df.sort_index()
        else:
            combined = new_df.sort_index()
        combined.to_parquet(p, compression="snappy")


def cache_stats() -> dict:
    """Estadísticas del caché en disco: cantidad de archivos y rango de fechas."""
    files = list(CACHE_DIR.glob("*.parquet"))
    if not files:
        return {"symbols": 0, "total_mb": 0.0}
    total_bytes = sum(f.stat().st_size for f in files)
    return {
        "symbols": len(files),
        "total_mb": round(total_bytes / 1024 / 1024, 1),
    }
