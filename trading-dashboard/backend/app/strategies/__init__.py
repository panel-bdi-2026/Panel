from __future__ import annotations

from ..screener import MomentumScreener
from .dividend import DividendStrategy
from .long_term import LongTermStrategy
from .opportunistic import OpportunisticStrategy

# Clases en el orden en que se muestran en el selector del dashboard. Solo
# metadata (id/name/supports_backtest): main.py construye las instancias a
# mano para que `screener` (el singleton historico de Momentum, sobre el que
# los tests existentes hacen monkeypatch directo) sea el mismo objeto que
# strategy_registry["momentum"], no una instancia nueva.
STRATEGY_CLASSES = [MomentumScreener, OpportunisticStrategy, LongTermStrategy, DividendStrategy]


def reload_strategy_registry(registry: dict[str, object], config) -> None:
    for strategy in registry.values():
        strategy.reload(config)
