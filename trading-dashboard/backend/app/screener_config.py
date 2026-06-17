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

    # Filtro de regimen de mercado: no se sugieren entradas largas si el
    # benchmark esta por debajo de su propia SMA de regimen (mercado en
    # tendencia bajista de fondo). Una estrategia long-only de momentum tiende
    # a funcionar mal en ese contexto aunque el papel individual pase los
    # demas filtros.
    regime_filter_enabled: bool = True
    regime_sma_period: int = 200

    # Dias antes de la fecha estimada de earnings en los que NO se sugieren
    # nuevas entradas: un gap por sorpresa de resultados puede saltarse un
    # stop-loss basado en ATR sin ejecutarse al precio sugerido.
    earnings_blackout_days: int = 5

    backtest_years: int = 3
    # Costos de transaccion asumidos en el backtest (antes no se modelaban, lo
    # que infla artificialmente los retornos reportados respecto a la
    # operatoria real). backtest_assumed_capital_usd es solo una referencia
    # para convertir commission_per_trade_usd en % por operacion: el backtest
    # no rastrea dolares reales, solo retornos %.
    backtest_assumed_capital_usd: float = 100_000
    commission_per_trade_usd: float = 1.0
    slippage_pct: float = 0.05

    # Escaneo proactivo en background: si esta habilitado, el backend corre el
    # screener solo (sin que el usuario abra el dashboard) cada
    # auto_scan_interval_minutes y arma ordenes de compra en borrador para
    # simbolos que recien empiezan a pasar los filtros (transicion no-pasa ->
    # pasa). Esas ordenes quedan en la cola de aprobacion manual igual que
    # cualquier otra: el motor nunca ejecuta nada por si solo.
    auto_scan_enabled: bool = False
    auto_scan_interval_minutes: int = 30

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
