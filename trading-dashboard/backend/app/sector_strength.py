from __future__ import annotations

import pandas as pd

from .indicators import rate_of_change
from .market_data import MarketDataError, get_daily_bars
from .sectors import SECTOR_ETF, get_sector

# Misma ventana que momentum_3m_pct en strategies/common.py (context_technicals):
# garantiza que la fuerza relativa vs. sector sea comparable, sin tener que
# importar ese modulo desde aca (evitaria un ciclo: screener.py -> este modulo
# -> strategies/common.py -> ... -> strategies/__init__.py -> screener.py).
_MOMENTUM_3M_DAYS = 63


def sector_relative_strength(
    symbol: str, own_momentum_3m_pct: float, lookback_days: int, force: bool = False
) -> "float | None":
    """Fuerza relativa de `symbol` contra el ETF de su propio sector GICS
    (distinto del benchmark general usado por Momentum, ej. SPY): retorno a
    _MOMENTUM_3M_DAYS del simbolo menos el del ETF en la misma ventana. None
    si el sector es desconocido, no tiene ETF mapeado (ver SECTOR_ETF) o no
    hay suficiente historia del ETF."""
    sector = get_sector(symbol)
    etf = SECTOR_ETF.get(sector) if sector else None
    if not etf:
        return None
    try:
        bars = get_daily_bars(etf, lookback_days, force=force)
    except MarketDataError:
        return None
    roc = rate_of_change(bars["Close"], _MOMENTUM_3M_DAYS)
    if not len(roc) or pd.isna(roc.iloc[-1]):
        return None
    return own_momentum_3m_pct - float(roc.iloc[-1])
