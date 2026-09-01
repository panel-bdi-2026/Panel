from __future__ import annotations

"""Texto legible de "por qué se compró" a partir de una señal guardada en el
audit log (ver find_trade_context/get_trade_context en main.py).

Deliberadamente NO usa un LLM: los datos ya están, son numéricos y bien
definidos (SignalResult, ver models.py) -- traducirlos a prosa de forma
determinística es instantáneo, gratis, y no puede "inventar" un número que
no esté respaldado por el signal real. Cada estrategia tiene su propia
lógica de decisión (momentum/oportunista son técnicas, largo plazo/
dividendos son fundamentales), así que el texto se arma distinto por
strategy_id en vez de un template genérico sobre todos los campos.
"""

_STRATEGY_LABELS = {
    "momentum": "Momentum",
    "opportunistic": "Oportunista",
    "long_term": "Largo plazo",
    "dividend": "Dividendos",
}


def _fmt_pct(v: "float | None", decimals: int = 1) -> "str | None":
    if v is None:
        return None
    return f"{v:+.{decimals}f}%"


def _momentum_rationale(s: dict) -> str:
    parts = []
    m3 = s.get("momentum_3m_pct")
    m1 = s.get("momentum_1m_pct")
    if m3 is not None:
        trend = "confirmando una tendencia alcista sostenida" if s.get("trend_ok") else "aunque el filtro de tendencia no la confirma del todo"
        extra = f" (y {_fmt_pct(m1)} en el último mes)" if m1 is not None else ""
        parts.append(f"Entró por momentum: subió {_fmt_pct(m3)} en 3 meses{extra}, {trend}.")
    rsi = s.get("rsi")
    macd = s.get("macd_histogram_pct")
    if rsi is not None:
        zone = "sin señales de sobrecompra" if rsi < 70 else "ya en zona de sobrecompra, aunque el resto de la señal compensó"
        macd_note = " y el MACD en cruce positivo" if macd is not None and macd > 0 else ""
        parts.append(f"RSI en {rsi:.0f}, {zone}{macd_note}.")
    return " ".join(parts)


def _opportunistic_rationale(s: dict) -> str:
    parts = []
    m3 = s.get("momentum_3m_pct")
    m1 = s.get("momentum_1m_pct")
    if m3 is not None and m1 is not None:
        drop = "venía de una caída" if m3 < 0 else "venía lateral/positivo"
        parts.append(f"Entró por rebote técnico: {drop} de {_fmt_pct(m3)} en 3 meses, pero mostró un giro reciente ({_fmt_pct(m1)} en el último mes).")
    rsi = s.get("rsi")
    macd = s.get("macd_histogram_pct")
    if rsi is not None:
        recovery = "recuperándose desde sobreventa" if rsi < 50 else "en zona neutral"
        macd_note = ", con el MACD recién cruzando a positivo" if macd is not None and macd > 0 else ""
        parts.append(f"RSI {recovery} ({rsi:.0f}){macd_note}.")
    high = s.get("pct_from_52w_high")
    if high is not None and high < 0:
        parts.append(f"Todavía a {abs(high):.0f}% del máximo de 52 semanas: queda margen de recuperación antes de quedarse sin espacio.")
    return " ".join(parts)


def _long_term_rationale(s: dict) -> str:
    parts = []
    pe = s.get("pe_ratio")
    peg = s.get("peg_ratio")
    if pe is not None:
        peg_note = f" (PEG {peg:.2f})" if peg is not None else ""
        parts.append(f"Entró por valuación: PE de {pe:.1f}{peg_note}, dentro del rango que la estrategia considera razonable para mantener a largo plazo.")
    else:
        parts.append("Entró por fundamentos de largo plazo (sin dato de PE disponible al momento de la señal).")
    rec = s.get("analyst_recommendation")
    pb = s.get("price_to_book")
    extra = []
    if rec:
        extra.append(f"consenso de analistas: {rec}")
    if pb is not None:
        extra.append(f"precio/valor libro {pb:.1f}")
    if extra:
        parts.append(f"Además, {', '.join(extra)}.")
    return " ".join(parts)


def _dividend_rationale(s: dict) -> str:
    parts = []
    dy = s.get("dividend_yield_pct")
    payout = s.get("payout_ratio_pct")
    if dy is not None:
        payout_note = f", con un payout ratio de {payout:.0f}% (sostenible según el filtro de la estrategia)" if payout is not None else ""
        parts.append(f"Entró por dividendo: yield de {dy:.2f}%{payout_note}.")
    else:
        parts.append("Entró por perfil de dividendos (sin dato de yield disponible al momento de la señal).")
    return " ".join(parts)


_STRATEGY_BUILDERS = {
    "momentum": _momentum_rationale,
    "opportunistic": _opportunistic_rationale,
    "long_term": _long_term_rationale,
    "dividend": _dividend_rationale,
}


def build_buy_rationale(signal: dict) -> str:
    """Texto de 2-4 oraciones explicando por qué la señal pasó el filtro de
    su estrategia, a partir de los campos ya calculados de SignalResult
    (nunca inventa un número que no esté en `signal`)."""
    strategy_id = signal.get("strategy_id") or "momentum"
    builder = _STRATEGY_BUILDERS.get(strategy_id)
    body = builder(signal) if builder else ""
    if not body:
        label = _STRATEGY_LABELS.get(strategy_id, strategy_id)
        body = f"Pasó los filtros de la estrategia {label} en el momento del scan."

    sector = signal.get("sector")
    srs = signal.get("sector_relative_strength_pct")
    if sector and srs is not None:
        cmp = "por encima" if srs > 0 else "por debajo"
        body += f" El sector {sector} está {cmp} del promedio del mercado ({_fmt_pct(srs)})."

    return body.strip()
