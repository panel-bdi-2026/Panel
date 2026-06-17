from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

# Universo de partida: large/mid-caps liquidos de EEUU, repartidos en varios
# sectores. Pensado para mantenerse corto y no chocar con los limites de la
# API gratuita de datos; editalo en screener.yaml segun tu criterio.
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "AMD", "CRM", "ADBE",
    "NFLX", "COST", "TSLA", "V", "MA", "UNH", "JPM", "XOM", "CAT", "DE",
    "LIN", "HD", "LOW", "NKE", "DIS", "PEP", "KO", "WMT", "ORCL", "QCOM",
]


class ScreenerConfig(BaseModel):
    universe: list[str] = DEFAULT_UNIVERSE
    benchmark_symbol: str = "SPY"
    lookback_days: int = 400

    # Filtro de tendencia: precio > SMA rapida > SMA lenta.
    sma_fast: int = 20
    sma_slow: int = 50

    # Momentum: retorno (%) en cada ventana.
    momentum_lookback_days: int = 63  # ~3 meses
    momentum_short_days: int = 21  # ~1 mes

    rsi_period: int = 14
    rsi_min: float = 40
    rsi_max: float = 75

    # Volumen promedio en DOLARES (precio x acciones), no en cantidad de
    # acciones: una accion barata puede superar un umbral de acciones y
    # seguir siendo poco liquida en terminos de dinero realmente operado.
    min_avg_dollar_volume: float = 1_000_000
    top_n: int = 10

    atr_period: int = 14
    stop_loss_atr_multiplier: float = 1.5
    max_holding_days: int = 20

    backtest_years: int = 3

    @classmethod
    def load(cls, path: Path) -> "ScreenerConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = yaml.safe_load(path.read_text()) or {}
        return cls(**data)

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.model_dump(), sort_keys=False))
