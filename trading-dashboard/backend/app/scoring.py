from __future__ import annotations

import pandas as pd

from .models import SignalResult


def apply_cross_sectional_normalization(results: list[SignalResult], weights: dict[str, float]) -> None:
    """Recalcula `result.score` (in-place) como una suma ponderada de los
    percentiles (0-100) de cada componente en `score_components`, calculados
    contra el resto de `results`, en vez de los valores crudos.

    El problema que resuelve: cada estrategia computa su score como una suma
    pesada de componentes con escalas muy distintas entre si (ej. RSI
    centrado en un rango chico y acotado vs. un PE invertido sin tope
    superior). Sin normalizar, un simbolo con un valor extremo en un solo
    componente puede dominar su score total aunque sea mediocre en el resto,
    desplazando en el ranking a candidatos mas parejos. Convertir cada
    componente a su percentil dentro del propio batch (0 = el peor de los
    simbolos escaneados en ese componente, 100 = el mejor) antes de ponderar
    hace que los componentes sean comparables entre si ("peras con peras"):
    un percentil de 90 vale lo mismo sin importar de que componente venga.

    No hace nada con menos de 2 resultados (no hay nada contra que rankear:
    el score crudo de evaluate_symbol() queda como esta)."""
    n = len(results)
    if n < 2:
        return
    percentile_by_key: dict[str, pd.Series] = {}
    for key in weights:
        values = pd.Series([r.score_components.get(key, 0.0) for r in results])
        ranks = values.rank(method="average")
        percentile_by_key[key] = (ranks - 1) / (n - 1) * 100
    for i, result in enumerate(results):
        result.score = round(sum(weights[key] * percentile_by_key[key].iloc[i] for key in weights), 2)
