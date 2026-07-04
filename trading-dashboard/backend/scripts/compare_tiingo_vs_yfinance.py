"""Compara precios de cierre ajustados de Tiingo vs yfinance en 50 tickers.

Objetivo: verificar que los datos de Tiingo son correctos antes de fiarles
un backtest de 15 años. Busca discrepancias en el precio de cierre ajustado
de los últimos 30 días hábiles.

Uso:
    cd /opt/panel/trading-dashboard/backend
    .venv/bin/python scripts/compare_tiingo_vs_yfinance.py
"""
from __future__ import annotations

import sys
import os
import time
from datetime import date, timedelta

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

TIINGO_API_KEY = os.getenv("TIINGO_API_KEY", "")
LOOKBACK_DAYS = 45  # pedir 45 días calendario para asegurarnos ~30 hábiles

# 50 tickers representativos del universo:
# - Large caps S&P 500 (múltiples sectores)
# - Algunos con dividendos (para verificar ajuste de dividendos)
# - Algunos con split reciente (NVDA tuvo 10:1 en jun-2024)
# - Algunos de crecimiento / mid caps
TICKERS = [
    # Mega caps tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    # Financieros
    "JPM", "BAC", "GS", "V", "MA",
    # Salud
    "JNJ", "UNH", "LLY", "ABBV", "MRK",
    # Industriales
    "CAT", "HON", "RTX", "GE",
    # Energía
    "XOM", "CVX", "COP",
    # Consumo
    "WMT", "COST", "PG", "KO", "PEP",
    # Con dividendos altos (prueba de ajuste)
    "T", "VZ", "MO", "IBM",
    # Growth / mid caps del universo Oportunista
    "CRWD", "DDOG", "PLTR", "AXON", "TTD", "HOOD", "COIN",
    # Con split reciente (NVDA 10:1 jun-2024) — ya está arriba
    # Algunos S&P 500 menos conocidos
    "NUE", "MLM", "DECK", "CASY", "HWM",
    # BRK-B: símbolo con guión (caso especial de conversión IBKR)
    "BRK-B",
]

TIINGO_RATE_LIMIT_S = 0.5  # mismo que en market_data.py
_last_call = [0.0]


def _tiingo_close(symbol: str, start: date, end: date) -> pd.Series:
    """Serie de closes ajustados de Tiingo, indexada por fecha."""
    wait = TIINGO_RATE_LIMIT_S - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()

    url = f"https://api.tiingo.com/tiingo/daily/{symbol.upper()}/prices"
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            url,
            params={
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "token": TIINGO_API_KEY,
            },
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code == 429:
        print(f"  429 en {symbol}, durmiendo 15s...")
        time.sleep(15)
        return pd.Series(dtype=float)
    if not resp.is_success:
        return pd.Series(dtype=float)
    data = resp.json()
    if not data:
        return pd.Series(dtype=float)
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df = df.set_index("date").sort_index()
    return df["adjClose"].rename(symbol)


def _yfinance_close(symbol: str, start: date, end: date) -> pd.Series:
    """Serie de closes ajustados de yfinance, indexada por fecha."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
        )
    except Exception:
        return pd.Series(dtype=float)
    if df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    close.index = pd.to_datetime(close.index).normalize().tz_localize(None)
    return close.rename(symbol)


def main() -> None:
    if not TIINGO_API_KEY:
        print("ERROR: TIINGO_API_KEY no encontrada en .env")
        sys.exit(1)

    end = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)

    print(f"\nComparando Tiingo vs yfinance | {start} → {end} | {len(TICKERS)} tickers\n")
    print(f"{'Ticker':<8} {'Días':<6} {'Dif media %':<14} {'Dif máx %':<12} {'Estado'}")
    print("-" * 60)

    results = []

    for sym in TICKERS:
        t_series = _tiingo_close(sym, start, end)
        y_series = _yfinance_close(sym, start, end)

        if t_series.empty:
            print(f"{sym:<8} {'—':<6} {'—':<14} {'—':<12} ❌ sin datos Tiingo")
            results.append({"symbol": sym, "status": "no_tiingo"})
            continue
        if y_series.empty:
            print(f"{sym:<8} {'—':<6} {'—':<14} {'—':<12} ⚠️  sin datos yfinance")
            results.append({"symbol": sym, "status": "no_yfinance"})
            continue

        # Normalizar índices: quitar timezone si lo tienen
        t_series.index = pd.to_datetime(t_series.index).tz_localize(None) if t_series.index.tz else pd.to_datetime(t_series.index)
        y_series.index = pd.to_datetime(y_series.index).tz_localize(None) if y_series.index.tz else pd.to_datetime(y_series.index)

        common = t_series.index.intersection(y_series.index)
        if len(common) == 0:
            print(f"{sym:<8} {'0':<6} {'—':<14} {'—':<12} ⚠️  sin fechas en común")
            results.append({"symbol": sym, "status": "no_overlap"})
            continue

        t_vals = t_series.loc[common]
        y_vals = y_series.loc[common]

        # Diferencia porcentual absoluta día a día
        diff_pct = ((t_vals - y_vals) / y_vals * 100).abs()
        mean_diff = diff_pct.mean()
        max_diff = diff_pct.max()
        n_days = len(common)

        flag = ""
        if max_diff > 2.0:
            flag = "🔴 REVISAR"
        elif max_diff > 0.5:
            flag = "🟡 leve"
        else:
            flag = "✅"

        print(f"{sym:<8} {n_days:<6} {mean_diff:<14.4f} {max_diff:<12.4f} {flag}")
        results.append({
            "symbol": sym,
            "status": "ok",
            "n_days": n_days,
            "mean_diff_pct": mean_diff,
            "max_diff_pct": max_diff,
        })

    # Resumen
    ok = [r for r in results if r["status"] == "ok"]
    no_data = [r for r in results if r["status"] == "no_tiingo"]
    warning = [r for r in results if r.get("max_diff_pct", 0) > 0.5]
    alert = [r for r in results if r.get("max_diff_pct", 0) > 2.0]

    print("\n" + "=" * 60)
    print(f"RESUMEN")
    print(f"  Tickers comparados exitosamente: {len(ok)}/{len(TICKERS)}")
    if no_data:
        print(f"  Sin datos en Tiingo: {[r['symbol'] for r in no_data]}")
    if ok:
        avg_mean = sum(r["mean_diff_pct"] for r in ok) / len(ok)
        avg_max  = sum(r["max_diff_pct"]  for r in ok) / len(ok)
        print(f"  Diferencia media promedio: {avg_mean:.4f}%")
        print(f"  Diferencia máxima promedio: {avg_max:.4f}%")
    if alert:
        print(f"\n  🔴 Discrepancias >2% (requieren revisión manual):")
        for r in alert:
            print(f"     {r['symbol']}: max={r['max_diff_pct']:.2f}%")
    if not alert and not no_data:
        print("\n  ✅ Datos de Tiingo consistentes con yfinance. OK para backtest.")


if __name__ == "__main__":
    main()
