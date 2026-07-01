from datetime import datetime, timezone

import pytest

from app import news_sentiment as news_sentiment_module
from app.models import SignalResult
from app.news_sentiment import (
    NewsSentiment,
    apply_news_sentiment_adjustment,
    get_news_sentiment,
)


def make_result(symbol: str, score: float = 50.0) -> SignalResult:
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
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    news_sentiment_module._cache.clear()
    yield
    news_sentiment_module._cache.clear()


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(news_sentiment_module.settings, "anthropic_api_key", "fake-key-for-tests")


# --- _fetch_headlines: extraccion defensiva ---------------------------------


class _FakeTicker:
    def __init__(self, news):
        self.news = news


def test_fetch_headlines_extracts_nested_content_title(monkeypatch):
    # El titulo debe mencionar el ticker o la empresa para pasar el filtro de
    # relevancia (_is_relevant, ver news_sentiment.py): sin eso, Yahoo mezcla
    # "editors picks" genericos en el feed de cualquier accion y quedarian
    # filtrados por diseno, no por un bug de este test.
    items = [{"content": {"title": "AAPL anuncia resultados record"}}]
    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _FakeTicker(items))
    assert news_sentiment_module._fetch_headlines("AAPL") == ["AAPL anuncia resultados record"]


def test_fetch_headlines_falls_back_to_top_level_title(monkeypatch):
    items = [{"title": "Titulo plano sin anidar sobre AAPL"}]
    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _FakeTicker(items))
    assert news_sentiment_module._fetch_headlines("AAPL") == ["Titulo plano sin anidar sobre AAPL"]


def test_fetch_headlines_skips_items_without_title(monkeypatch):
    items = [{"content": {}}, {"title": "Si tiene titulo de AAPL"}]
    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _FakeTicker(items))
    assert news_sentiment_module._fetch_headlines("AAPL") == ["Si tiene titulo de AAPL"]


def test_fetch_headlines_caps_at_max_headlines(monkeypatch):
    items = [{"title": f"Titular {i} sobre AAPL"} for i in range(20)]
    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _FakeTicker(items))
    headlines = news_sentiment_module._fetch_headlines("AAPL")
    assert len(headlines) == news_sentiment_module._MAX_HEADLINES


def test_fetch_headlines_returns_empty_list_on_exception(monkeypatch):
    class _BrokenTicker:
        @property
        def news(self):
            raise RuntimeError("rate limited")

    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _BrokenTicker())
    assert news_sentiment_module._fetch_headlines("AAPL") == []


def test_fetch_headlines_returns_empty_list_when_news_is_none(monkeypatch):
    monkeypatch.setattr(news_sentiment_module.yf, "Ticker", lambda symbol: _FakeTicker(None))
    assert news_sentiment_module._fetch_headlines("AAPL") == []


# --- _call_claude: timeout explicito al cliente de Anthropic ---------------


def test_call_claude_passes_explicit_timeout_to_anthropic_client(with_api_key, monkeypatch):
    """Sin un timeout explicito, el SDK de Anthropic usa un default de 600s
    (10 min) de read timeout -- esta llamada es sincronica DENTRO del scan de
    señales, asi que un colgazo de Claude frenaria el scan completo varios
    minutos por un solo simbolo del shortlist."""
    captured_kwargs = {}

    class FakeMessages:
        def parse(self, **kwargs):
            return type("Response", (), {"parsed_output": NewsSentiment(sentiment="neutral", summary="ok")})()

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.messages = FakeMessages()

    monkeypatch.setattr(news_sentiment_module.anthropic, "Anthropic", FakeAnthropicClient)

    news_sentiment_module._call_claude("AAPL", ["un titular"])

    assert captured_kwargs.get("timeout") == news_sentiment_module._CLAUDE_TIMEOUT_SECONDS
    assert 0 < captured_kwargs["timeout"] < 600  # bien por debajo del default del SDK (10 min)


# --- get_news_sentiment: fail-safe, cache --------------------------------


def test_get_news_sentiment_none_without_api_key(monkeypatch):
    monkeypatch.setattr(news_sentiment_module.settings, "anthropic_api_key", "")
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])
    assert get_news_sentiment("AAPL") is None


def test_get_news_sentiment_none_without_headlines(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: [])
    assert get_news_sentiment("AAPL") is None


def test_get_news_sentiment_none_when_call_claude_raises(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])

    def boom(symbol, headlines):
        raise RuntimeError("API down")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", boom)
    assert get_news_sentiment("AAPL") is None


def test_get_news_sentiment_returns_classification(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["buenas noticias"])
    expected = NewsSentiment(sentiment="positive", summary="Resultados mejores a lo esperado.")
    monkeypatch.setattr(news_sentiment_module, "_call_claude", lambda symbol, headlines: expected)

    result = get_news_sentiment("AAPL")
    assert result == expected


def test_get_news_sentiment_caches_result(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])
    call_count = {"n": 0}

    def fake_call(symbol, headlines):
        call_count["n"] += 1
        return NewsSentiment(sentiment="neutral", summary="Sin novedades relevantes.")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", fake_call)

    get_news_sentiment("AAPL")
    get_news_sentiment("AAPL")
    assert call_count["n"] == 1


def test_get_news_sentiment_force_bypasses_cache(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])
    call_count = {"n": 0}

    def fake_call(symbol, headlines):
        call_count["n"] += 1
        return NewsSentiment(sentiment="neutral", summary="Sin novedades relevantes.")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", fake_call)

    get_news_sentiment("AAPL")
    get_news_sentiment("AAPL", force=True)
    assert call_count["n"] == 2


def test_get_news_sentiment_ttl_expired_refetches(with_api_key, monkeypatch):
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])
    monkeypatch.setattr(
        news_sentiment_module, "_call_claude",
        lambda symbol, headlines: NewsSentiment(sentiment="neutral", summary="Sin novedades."),
    )

    get_news_sentiment("AAPL")
    timestamp, cached, ok = news_sentiment_module._cache["AAPL"]
    expired = timestamp - news_sentiment_module._CACHE_TTL_SECONDS - 1
    news_sentiment_module._cache["AAPL"] = (expired, cached, ok)

    call_count = {"n": 0}

    def fake_call(symbol, headlines):
        call_count["n"] += 1
        return NewsSentiment(sentiment="neutral", summary="Sin novedades.")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", fake_call)
    get_news_sentiment("AAPL")
    assert call_count["n"] == 1


def test_get_news_sentiment_caches_none_result_too(with_api_key, monkeypatch):
    # Sin titulares: el resultado (None) tambien debe quedar cacheado, igual
    # que get_fundamentals/get_next_earnings_date en market_data.py, para no
    # re-pegarle a yfinance en cada llamada por un simbolo sin cobertura.
    calls = {"n": 0}

    def fake_fetch(symbol):
        calls["n"] += 1
        return []

    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", fake_fetch)
    get_news_sentiment("GHOST")
    get_news_sentiment("GHOST")
    assert calls["n"] == 1


def test_get_news_sentiment_genuine_none_uses_full_ttl(with_api_key, monkeypatch):
    """Sin titulares (sin excepcion, dato real): el None cacheado debe seguir
    valiendo por las 6hs completas, no por el TTL corto de falla."""
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: [])
    get_news_sentiment("GHOST")
    cached_at, value, ok = news_sentiment_module._cache["GHOST"]
    assert ok is True

    news_sentiment_module._cache["GHOST"] = (
        cached_at - news_sentiment_module._FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )

    calls = {"n": 0}
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: calls.update(n=calls["n"] + 1) or [])
    get_news_sentiment("GHOST")
    assert calls["n"] == 0


def test_get_news_sentiment_failure_uses_short_ttl_for_retry(with_api_key, monkeypatch):
    """Una excepcion transitoria de la API de Claude (rate limit, timeout) no
    debe quedar pegada en cache por las 6hs completas: pasado el TTL corto de
    falla (pero todavia dentro del TTL largo de exito), una nueva llamada
    debe reintentar en vez de devolver el None cacheado."""
    monkeypatch.setattr(news_sentiment_module, "_fetch_headlines", lambda symbol: ["algo"])

    def boom(symbol, headlines):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", boom)
    assert get_news_sentiment("AAPL") is None

    cached_at, value, ok = news_sentiment_module._cache["AAPL"]
    assert ok is False

    news_sentiment_module._cache["AAPL"] = (
        cached_at - news_sentiment_module._FAILURE_CACHE_TTL_SECONDS - 1, value, ok,
    )

    call_count = {"n": 0}

    def fake_call(symbol, headlines):
        call_count["n"] += 1
        return NewsSentiment(sentiment="neutral", summary="Recupero tras la falla.")

    monkeypatch.setattr(news_sentiment_module, "_call_claude", fake_call)
    result = get_news_sentiment("AAPL")
    assert call_count["n"] == 1
    assert result.sentiment == "neutral"


# --- apply_news_sentiment_adjustment ----------------------------------------


def test_adjustment_only_applies_to_shortlist(monkeypatch):
    results = [make_result(f"S{i}", score=100 - i) for i in range(5)]
    calls = []

    def fake_get(symbol, force=False):
        calls.append(symbol)
        return NewsSentiment(sentiment="positive", summary="x")

    monkeypatch.setattr(news_sentiment_module, "get_news_sentiment", fake_get)
    apply_news_sentiment_adjustment(results, top_n=2, shortlist_multiplier=1.0, max_adjustment=10.0)
    assert calls == ["S0", "S1"]


def test_positive_sentiment_increases_score(monkeypatch):
    results = [make_result("AAPL", score=50.0)]
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: NewsSentiment(sentiment="positive", summary="Buenas noticias."),
    )
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 60.0
    assert results[0].news_sentiment == "positive"
    assert results[0].news_summary == "Buenas noticias."


def test_negative_sentiment_decreases_score(monkeypatch):
    # score=70 (bien dentro de la banda [50,100]) para aislar el efecto de
    # "el sentimiento negativo resta" del clamp de banda (ver tests aparte
    # mas abajo): un score=50 con -10 caeria exactamente en el limite del
    # clamp y no distinguiria un bug del comportamiento correcto.
    results = [make_result("AAPL", score=70.0)]
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: NewsSentiment(sentiment="negative", summary="Malas noticias."),
    )
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 60.0


def test_neutral_sentiment_does_not_change_score(monkeypatch):
    results = [make_result("AAPL", score=50.0)]
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: NewsSentiment(sentiment="neutral", summary="Sin novedades."),
    )
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 50.0
    assert results[0].news_sentiment == "neutral"


def test_missing_sentiment_data_does_not_change_score_or_fields(monkeypatch):
    results = [make_result("AAPL", score=50.0)]
    monkeypatch.setattr(news_sentiment_module, "get_news_sentiment", lambda symbol, force=False: None)
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 50.0
    assert results[0].news_sentiment is None
    assert results[0].news_summary is None


def test_passing_candidate_score_never_drops_below_50(monkeypatch):
    # score=52 (pasa filtros) con -10 daria 42 sin el clamp -- por debajo de
    # la banda [50,100] que garantiza que un candidato que SI pasa los
    # filtros de calidad de la estrategia siempre rankea por encima de uno
    # que no los pasa (ver docstring de apply_news_sentiment_adjustment).
    results = [make_result("AAPL", score=52.0)]
    results[0].passes_filters = True
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: NewsSentiment(sentiment="negative", summary="Malas noticias."),
    )
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 50.0


def test_non_passing_candidate_score_never_exceeds_49(monkeypatch):
    # score=45 (NO pasa filtros -- ej. rompe RSI o tendencia) con +10 daria
    # 55 sin el clamp, cruzando a la banda de los que si pasan -- y si eso
    # ademas superara el umbral de auto-trading, dispararia una compra en un
    # simbolo que falla el filtro de calidad de la estrategia solo por una
    # noticia favorable (el bug que este clamp cierra).
    results = [make_result("AAPL", score=45.0)]
    results[0].passes_filters = False
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: NewsSentiment(sentiment="positive", summary="Buenas noticias."),
    )
    apply_news_sentiment_adjustment(results, top_n=10, shortlist_multiplier=3.0, max_adjustment=10.0)
    assert results[0].score == 49.0


def test_shortlist_size_rounds_and_has_floor_of_one(monkeypatch):
    results = [make_result(f"S{i}", score=100 - i) for i in range(3)]
    calls = []
    monkeypatch.setattr(
        news_sentiment_module, "get_news_sentiment",
        lambda symbol, force=False: calls.append(symbol) or None,
    )
    apply_news_sentiment_adjustment(results, top_n=1, shortlist_multiplier=0.1, max_adjustment=10.0)
    assert calls == ["S0"]
