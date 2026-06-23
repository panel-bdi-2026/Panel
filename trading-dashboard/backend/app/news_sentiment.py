from __future__ import annotations

import time
from typing import Literal, Optional

import anthropic
import yfinance as yf
from pydantic import BaseModel

from .config import settings
from .models import SignalResult

# El sentimiento de un titular no cambia de un minuto a otro, y a diferencia
# del resto de market_data.py esta llamada tiene costo real (API de Claude):
# cache mas agresivo que el de fundamentals (24hs) no haria falta, pero uno
# mas corto que ese tampoco se justifica solo para "noticias mas frescas".
_CACHE_TTL_SECONDS = 6 * 3600
# Si _call_claude fallo (rate limit, timeout, API key invalida transitoriamente),
# el motivo mas probable es pasajero, no "sin sentimiento": cachear ese None
# por las 6hs completas lo deja sin reintentar toda esa ventana por un fallo
# puntual. TTL corto solo para esa rama; "sin titulares" (sin excepcion) sigue
# usando el TTL largo, igual que get_fundamentals/get_next_earnings_date.
_FAILURE_CACHE_TTL_SECONDS = 15 * 60
_cache: dict[str, tuple[float, "NewsSentiment | None", bool]] = {}  # (timestamp, value, ok)

_MODEL = "claude-haiku-4-5"
# Mas alla de unos pocos titulares no suma precision a la clasificacion y
# si suma tokens (costo): se toman los mas recientes nada mas.
_MAX_HEADLINES = 8
# El SDK de Anthropic, sin `timeout` explicito, usa un default de 600s (10
# minutos) de read timeout. Esta llamada es sincronica DENTRO del scan de
# señales (ver apply_news_sentiment_adjustment): clasificar 8 titulares no
# deberia tardar mas que esto en un dia normal, y si Claude esta colgado o
# con un incidente, preferimos que falle rapido y caiga al cache de falla de
# 15 min (ver _classify) en vez de frenar el scan completo varios minutos
# por un simbolo del shortlist.
_CLAUDE_TIMEOUT_SECONDS = 20.0


class NewsSentiment(BaseModel):
    sentiment: Literal["positive", "negative", "neutral"]
    summary: str


def _fetch_headlines(symbol: str) -> list[str]:
    """Titulares recientes de Yahoo Finance para `symbol` (via yfinance,
    gratis). Extraccion defensiva: el shape exacto de cada item del feed
    (titulo anidado en "content" vs. titulo plano) no esta documentado
    formalmente y puede variar entre versiones de yfinance; probar ambos
    evita perder todos los titulares por un cambio de formato silencioso."""
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        return []
    headlines: list[str] = []
    for item in items[:_MAX_HEADLINES]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        title = content.get("title") if isinstance(content, dict) else None
        if not title:
            title = item.get("title")
        if title:
            headlines.append(str(title))
    return headlines


def _call_claude(symbol: str, headlines: list[str]) -> NewsSentiment:
    """Aislado en su propia funcion (en vez de inline en _classify) para que
    los tests puedan reemplazarla sin tocar el SDK de Anthropic ni la red."""
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_CLAUDE_TIMEOUT_SECONDS)
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
    mueve (ya esta percentilado 0-100, asi que un ajuste fijo y acotado no
    puede sacar a un candidato fuera de ese rango de forma descontrolada).
    Sin dato (None) tampoco mueve el score: ver docstring de
    get_news_sentiment. No reordena `results`: el llamador debe volver a
    ordenar despues de este ajuste, igual que despues de
    apply_cross_sectional_normalization.
    """
    shortlist_size = max(1, round(top_n * shortlist_multiplier))
    for result in results[:shortlist_size]:
        sentiment_data = get_news_sentiment(result.symbol)
        if sentiment_data is None:
            continue
        result.news_sentiment = sentiment_data.sentiment
        result.news_summary = sentiment_data.summary
        if sentiment_data.sentiment == "positive":
            result.score = round(result.score + max_adjustment, 2)
        elif sentiment_data.sentiment == "negative":
            result.score = round(result.score - max_adjustment, 2)
