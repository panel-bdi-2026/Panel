from __future__ import annotations

import pandas as pd

from .models import SignalResult
from .news_sentiment import apply_news_sentiment_adjustment


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


def finalize_scan_results(
    results: list[SignalResult],
    weights: dict[str, float],
    top_n: int,
    news_sentiment_enabled: bool,
    news_sentiment_shortlist_multiplier: float,
    news_sentiment_max_adjustment: float,
) -> list[SignalResult]:
    """Segunda mitad de scan() en las 4 estrategias (screener.py y
    strategies/*.py): separa `results` en dos bandas segun passes_filters,
    normaliza cada banda por separado (ver apply_cross_sectional_normalization),
    remapea a rangos disjuntos (50-100 la banda "pasa", 0-49 la que no) para
    garantizar que un candidato que falla los filtros de calidad de la
    estrategia nunca rankea por encima de uno que si los pasa, y por ultimo
    aplica el ajuste de sentimiento de noticias si esta activo.

    Estaba duplicado identico en las 4 estrategias (una sola vez por cada una,
    con solo `weights` distinto): cualquier fix a esta logica -- ver el clamp
    de apply_news_sentiment_adjustment, que evita que el ajuste de sentimiento
    cruce la banda 50-100/0-49 -- tenia que aplicarse 4 veces por separado, con
    riesgo real de arreglarlo en una y olvidarlo en otra.

    `top_n`/`news_sentiment_*` se pasan explicitos (no un ScreenerConfig
    completo) porque cada estrategia los lee de un lugar distinto (ej.
    top_n es global, pero news_sentiment_* tambien es global aunque los pesos
    del score sean por-estrategia): mantiene esta funcion sin acoplarse a la
    forma de ScreenerConfig."""
    passing = [r for r in results if r.passes_filters]
    non_passing = [r for r in results if not r.passes_filters]
    apply_cross_sectional_normalization(passing, weights)
    apply_cross_sectional_normalization(non_passing, weights)
    for r in passing:
        r.score = round(50.0 + r.score * 0.5, 2)
    for r in non_passing:
        r.score = round(r.score * 0.49, 2)
    combined = sorted(passing + non_passing, key=lambda r: r.score, reverse=True)
    if news_sentiment_enabled:
        apply_news_sentiment_adjustment(
            combined, top_n, news_sentiment_shortlist_multiplier, news_sentiment_max_adjustment,
        )
        combined.sort(key=lambda r: r.score, reverse=True)
    return combined
