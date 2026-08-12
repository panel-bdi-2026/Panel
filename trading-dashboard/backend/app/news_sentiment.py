from __future__ import annotations

import concurrent.futures
import re
import time
from typing import Literal, Optional

import anthropic
import yfinance as yf
from pydantic import BaseModel

from .config import settings
from .market_data import _fetch_with_timeout
from .models import SignalResult

_CACHE_TTL_SECONDS = 6 * 3600
# TTL corto solo para fallos de la API de Claude (ver _classify).
_FAILURE_CACHE_TTL_SECONDS = 15 * 60
_cache: dict[str, tuple[float, "NewsSentiment | None", bool]] = {}  # (timestamp, value, ok)
# Singleton: reutiliza el connection pool HTTP entre llamadas al SDK de Anthropic.
_anthropic_client: "anthropic.Anthropic | None" = None

# Pool dedicado para paralelizar get_news_sentiment sobre el shortlist (ver
# apply_news_sentiment_adjustment): cada llamada es IO-bound (un fetch de
# noticias a yfinance + una clasificacion via la API de Claude), asi que
# corrian secuenciales dejaba un scan con sentimiento activo tardar la SUMA
# de las latencias de red de cada simbolo del shortlist (hasta 30 por
# default) en vez del maximo. max_workers moderado (no el tamaño del
# shortlist completo) para no saturar de golpe el rate limit de la cuenta de
# Anthropic ni el de Yahoo Finance.
_SENTIMENT_MAX_WORKERS = 6
_sentiment_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=_SENTIMENT_MAX_WORKERS, thread_name_prefix="news-sentiment"
)


def shutdown_sentiment_executor() -> None:
    """Cierra el ThreadPoolExecutor de sentiment. Llamar desde el lifespan
    shutdown de main.py para evitar threads huérfanos tras el shutdown."""
    _sentiment_executor.shutdown(wait=False)

# Cache de nombre de empresa para filtrado de titulares. Se llena la primera
# vez que se pide sentimiento de un símbolo y se reutiliza dentro de la sesión.
_company_name_cache: dict[str, str] = {}

_MODEL = "claude-haiku-4-5"
_MAX_HEADLINES = 8
# Cuántos items brutos revisar antes de llegar a _MAX_HEADLINES relevantes.
_MAX_RAW_ITEMS = 30
_CLAUDE_TIMEOUT_SECONDS = 20.0

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class NewsSentiment(BaseModel):
    sentiment: Literal["positive", "negative", "neutral"]
    summary: str


def _get_company_filter_word(symbol: str) -> str:
    """Primera palabra significativa del nombre de la empresa (ej. 'Uber' para
    UBER, 'Apple' para AAPL). Se usa para filtrar noticias genéricas que Yahoo
    Finance mezcla en el feed de cualquier acción (ej. 'editors picks' de tech
    que no mencionan la empresa). Se cachea en memoria por sesión: el nombre
    comercial de una empresa no cambia entre scans."""
    if symbol in _company_name_cache:
        return _company_name_cache[symbol]
    try:
        info = _fetch_with_timeout(lambda: yf.Ticker(symbol).info) or {}
        raw = info.get("shortName") or info.get("longName") or ""
        # Toma la primera palabra de más de 2 caracteres que no sea un sufijo
        # corporativo genérico (Inc., Corp., etc.).
        _CORP_SUFFIXES = {"inc", "corp", "corporation", "company", "group",
                         "holdings", "technologies", "technology", "ltd", "llc"}
        words = [w.strip(".,") for w in raw.split() if len(w.strip(".,")) > 2]
        word = next((w.lower() for w in words if w.lower() not in _CORP_SUFFIXES), symbol.lower())
    except Exception:
        word = symbol.lower()
    _company_name_cache[symbol] = word
    return word


def _is_relevant(item: dict, sym_lower: str, company_word: str) -> bool:
    """Devuelve True si el item de noticias de yfinance es relevante para el
    símbolo dado. Filtra 'editors picks' y artículos genéricos que Yahoo
    incluye en el feed de cualquier acción sin relación directa."""
    content = item.get("content") if isinstance(item, dict) else None
    title       = (content.get("title")       if isinstance(content, dict) else None) or item.get("title", "")
    summary     = (content.get("summary")     if isinstance(content, dict) else None) or ""
    description = (content.get("description") if isinstance(content, dict) else None) or ""
    # Elimina tags HTML de la descripción antes de buscar texto
    clean_desc = _HTML_TAG_RE.sub(" ", description)
    combined = (title + " " + summary + " " + clean_desc).lower()
    return sym_lower in combined or company_word in combined


def _fetch_headlines(symbol: str) -> list[str]:
    """Titulares recientes de Yahoo Finance para `symbol`, filtrados para
    incluir solo artículos que mencionan la empresa o el ticker. Yahoo mezcla
    'editors picks' genéricos en el feed de cualquier acción; sin filtrar,
    esos artículos contaminan la clasificación de sentimiento."""
    try:
        items = _fetch_with_timeout(lambda: yf.Ticker(symbol).news) or []
    except Exception:
        return []

    sym_lower    = symbol.lower()
    company_word = _get_company_filter_word(symbol)

    headlines: list[str] = []
    for item in items[:_MAX_RAW_ITEMS]:
        if not isinstance(item, dict):
            continue
        if not _is_relevant(item, sym_lower, company_word):
            continue
        content = item.get("content")
        title = (content.get("title") if isinstance(content, dict) else None) or item.get("title")
        if title:
            headlines.append(str(title))
        if len(headlines) >= _MAX_HEADLINES:
            break
    return headlines


def _call_claude(symbol: str, headlines: list[str]) -> NewsSentiment:
    """Aislado en su propia funcion (en vez de inline en _classify) para que
    los tests puedan reemplazarla sin tocar el SDK de Anthropic ni la red."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_CLAUDE_TIMEOUT_SECONDS)
    client = _anthropic_client
    headlines_text = "\n".join(f"- {h}" for h in headlines)
    response = client.messages.parse(
        model=_MODEL,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Sos un analista financiero. A partir de estos titulares "
                f"recientes sobre la accion {symbol}, clasifica el "
                f"sentimiento de mercado predominante (positive, negative o "
                f"neutral) y resumi en 1-2 oraciones en español por que.\n\n"
                f"Titulares:\n{headlines_text}"
            ),
        }],
        output_format=NewsSentiment,
    )
    return response.parsed_output


def _classify(symbol: str, headlines: list[str]) -> tuple[Optional[NewsSentiment], bool]:
    """Devuelve (resultado, ok). ok=False marca que _call_claude exploto (API
    key invalida, rate limit, timeout, respuesta que no matchea el schema,
    etc.), para que get_news_sentiment lo cachee con el TTL corto de falla en
    vez del largo: ninguno de esos motivos amerita que un scan completo falle
    por una feature opcional de costo extra, pero tampoco que un fallo
    pasajero quede pegado en cache por 6hs."""
    if not headlines:
        return None, True
    try:
        return _call_claude(symbol, headlines), True
    except Exception:
        return None, False


def get_news_sentiment(symbol: str, force: bool = False) -> Optional[NewsSentiment]:
    """Sentimiento de noticias recientes para `symbol`, o None si no hay
    ANTHROPIC_API_KEY configurada, no hay titulares disponibles, o la
    clasificacion fallo. None significa "sin dato", nunca "neutral": el
    llamador (apply_news_sentiment_adjustment) no ajusta el score en ese
    caso, en vez de asumir neutralidad que el modelo nunca evaluo.
    """
    # Corta antes de pedir titulares (yfinance, gratis pero no instantaneo) si
    # de todas formas no hay key para clasificarlos: evita un round-trip de
    # red que se va a descartar siempre que la feature este sin configurar.
    if not settings.anthropic_api_key:
        return None

    key = symbol.upper()
    now = time.time()
    cached = _cache.get(key)
    if not force and cached:
        ttl = _CACHE_TTL_SECONDS if cached[2] else _FAILURE_CACHE_TTL_SECONDS
        if now - cached[0] < ttl:
            return cached[1]

    headlines = _fetch_headlines(key)
    result, ok = _classify(key, headlines)
    _cache[key] = (now, result, ok)
    return result


def apply_news_sentiment_adjustment(
    results: list[SignalResult],
    top_n: int,
    shortlist_multiplier: float,
    max_adjustment: float,
) -> None:
    """Segunda etapa del ranking, posterior a apply_cross_sectional_normalization
    + el primer sort por score: ajusta result.score (in-place) segun el
    sentimiento de noticias recientes, pero solo para el shortlist de los
    `top_n * shortlist_multiplier` mejores candidatos (no todo el universo
    escaneado), ya que cada simbolo sin cache le pega a la API de Claude, que
    a diferencia del resto de este pipeline tiene costo real por llamada.

    Positivo suma max_adjustment al score, negativo lo resta, neutral no lo
    mueve. Sin dato (None) tampoco mueve el score: ver docstring de
    get_news_sentiment. No reordena `results`: el llamador debe volver a
    ordenar despues de este ajuste, igual que despues de
    apply_cross_sectional_normalization.

    Clampeado DESPUES del ajuste a [50, 100] si result.passes_filters, o a
    [0, 49] si no: preserva la garantia de diseno del llamador (que separa
    "pasa filtros de calidad" de "no pasa" en dos bandas disjuntas antes de
    llamar aca, ver screener.py/strategies/*.py) de que un candidato que NO
    pasa los filtros tecnicos de la estrategia (tendencia, RSI, etc.) nunca
    puede rankear por encima de uno que si los pasa. Sin este clamp, un
    ajuste positivo grande sobre un candidato de la banda baja (ej. 49 + 10)
    podia cruzar a la banda alta y, si superaba ademas el umbral de
    auto-trading, disparar una compra en un simbolo que fallaba el filtro de
    calidad de la estrategia solo por una noticia favorable.

    Las llamadas a get_news_sentiment del shortlist se paralelizan en
    _sentiment_executor (IO-bound: fetch de noticias + clasificacion via
    Claude), en vez de una por una: secuencial, un shortlist de 30 simbolos
    sin cache tardaba la SUMA de las latencias de red de cada uno (podia
    ser varios minutos en un scan forzado), en vez del maximo entre todas.
    Los resultados se recolectan primero (sin tocar `results` todavia) y
    recien despues se aplican los ajustes en orden, para que el resultado
    final sea identico al del loop secuencial de antes -- solo cambia
    CUANTO tarda, no que hace.
    """
    shortlist_size = max(1, round(top_n * shortlist_multiplier))
    shortlist = results[:shortlist_size]
    if not shortlist:
        return
    symbols = [r.symbol for r in shortlist]
    sentiments = list(_sentiment_executor.map(get_news_sentiment, symbols))
    for result, sentiment_data in zip(shortlist, sentiments):
        if sentiment_data is None:
            continue
        result.news_sentiment = sentiment_data.sentiment
        result.news_summary = sentiment_data.summary
        if sentiment_data.sentiment == "positive":
            result.score = result.score + max_adjustment
        elif sentiment_data.sentiment == "negative":
            result.score = result.score - max_adjustment
        if result.passes_filters:
            result.score = round(max(50.0, min(100.0, result.score)), 2)
        else:
            result.score = round(max(0.0, min(49.0, result.score)), 2)
