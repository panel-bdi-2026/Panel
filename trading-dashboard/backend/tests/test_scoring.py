from datetime import datetime, timezone

from app.models import SignalResult
from app.scoring import apply_cross_sectional_normalization


def make_result(symbol: str, components: dict[str, float], score: float = 0.0) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        as_of=datetime.now(timezone.utc),
        last_price=100.0,
        score=score,
        momentum_3m_pct=0.0,
        momentum_1m_pct=0.0,
        trend_ok=True,
        rsi=50.0,
        avg_volume=1_000_000,
        suggested_stop_loss_price=95.0,
        suggested_stop_loss_pct=5.0,
        passes_filters=True,
        score_components=components,
    )


def test_noop_with_fewer_than_two_results():
    result = make_result("A", {"comp1": 5.0}, score=42.0)
    apply_cross_sectional_normalization([result], {"comp1": 1.0})
    assert result.score == 42.0


def test_percentile_rank_spans_0_to_100():
    results = [
        make_result("LOW", {"comp1": 1.0}),
        make_result("MID", {"comp1": 2.0}),
        make_result("HIGH", {"comp1": 3.0}),
    ]
    apply_cross_sectional_normalization(results, {"comp1": 1.0})
    by_symbol = {r.symbol: r.score for r in results}
    assert by_symbol["LOW"] == 0.0
    assert by_symbol["MID"] == 50.0
    assert by_symbol["HIGH"] == 100.0


def test_outlier_in_one_component_no_longer_dominates_ranking():
    # A tiene un valor extremo en comp1 (1000 vs ~10 del resto) pero el peor
    # valor en comp2: con una suma cruda ponderada, ese unico componente
    # extremo lo pondria muy por delante de todos (0.5*1000+0.5*1=500.5,
    # contra ~9-10 del resto). C es el candidato mas parejo (bueno en ambos
    # componentes). Tras la normalizacion cross-sectional, C debe rankear
    # primero, no A.
    weights = {"comp1": 0.5, "comp2": 0.5}
    results = [
        make_result("A", {"comp1": 1000.0, "comp2": 1.0}),
        make_result("B", {"comp1": 10.0, "comp2": 8.0}),
        make_result("C", {"comp1": 11.0, "comp2": 9.0}),
        make_result("D", {"comp1": 9.0, "comp2": 10.0}),
    ]
    apply_cross_sectional_normalization(results, weights)
    by_symbol = {r.symbol: r.score for r in results}
    assert by_symbol["C"] == max(by_symbol.values())
    assert by_symbol["C"] > by_symbol["A"]


def test_ignores_weight_keys_missing_from_components():
    results = [
        make_result("X", {"comp1": 1.0}),
        make_result("Y", {"comp1": 2.0}),
    ]
    apply_cross_sectional_normalization(results, {"comp1": 1.0, "missing": 1.0})
    by_symbol = {r.symbol: r.score for r in results}
    # "missing" no esta en score_components de ningun resultado: ambos
    # resultados quedan empatados en 0.0 para ese componente, asi que su
    # percentil (50, el rank promedio de un empate) se suma igual a los dos y
    # no afecta la diferencia entre ellos (la sigue dando solo "comp1").
    assert by_symbol["Y"] - by_symbol["X"] == 100.0
