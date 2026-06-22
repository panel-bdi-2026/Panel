from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from ..indicators import atr, bollinger_percent_b, macd, pct_from_high, rate_of_change, rsi, sma
from ..market_data import get_next_earnings_date
from ..sector_strength import sector_relative_strength as _sector_relative_strength

# Ventanas fijas para los campos de contexto tecnico (momentum_3m_pct,
# momentum_1m_pct, rsi, trend_ok) que se muestran en la tabla unificada del
# radar para cualquier estrategia, independientemente de las ventanas propias
# que cada estrategia use para filtrar/puntuar. Son los mismos defaults que
# Momentum usaba antes de hacerlos configurables, para que el significado de
# esas columnas sea consistente entre estrategias.
_CONTEXT_MOMENTUM_3M_DAYS = 63
_CONTEXT_MOMENTUM_1M_DAYS = 21
_CONTEXT_RSI_PERIOD = 14
_CONTEXT_SMA_FAST = 20
_CONTEXT_SMA_SLOW = 50


def context_technicals(bars: pd.DataFrame, atr_period: int) -> dict | None:
    """Indicadores tecnicos de contexto (ver constantes arriba). Devuelve
    None si no hay suficiente historia de precios para calcularlos."""
    close = bars["Close"]
    if len(close) < _CONTEXT_MOMENTUM_3M_DAYS:
        return None

    roc_3m = rate_of_change(close, _CONTEXT_MOMENTUM_3M_DAYS)
    roc_1m = rate_of_change(close, _CONTEXT_MOMENTUM_1M_DAYS)
    rsi_s = rsi(close, _CONTEXT_RSI_PERIOD)
    atr_s = atr(bars["High"], bars["Low"], close, atr_period)
    sma_fast_s = sma(close, _CONTEXT_SMA_FAST)
    sma_slow_s = sma(close, _CONTEXT_SMA_SLOW)
    from_high_s = pct_from_high(close, 252)
    _, _, macd_hist_s = macd(close)
    bollinger_pct_b_s = bollinger_percent_b(close)

    if pd.isna(roc_3m.iloc[-1]) or pd.isna(atr_s.iloc[-1]):
        return None

    last_price = float(close.iloc[-1])
    trend_ok = bool(
        not pd.isna(sma_fast_s.iloc[-1])
        and not pd.isna(sma_slow_s.iloc[-1])
        and last_price > sma_fast_s.iloc[-1] > sma_slow_s.iloc[-1]
    )
    last_from_high = float(from_high_s.iloc[-1]) if not pd.isna(from_high_s.iloc[-1]) else None
    # Histograma de MACD normalizado como % del precio: en valor absoluto
    # ($) no es comparable entre simbolos de escala de precio muy distinta
    # (ej. una accion de USD 5 vs una de USD 500), lo que rompe el ranking
    # cross-sectional (ver apply_cross_sectional_normalization).
    last_macd_hist_pct = (
        float(macd_hist_s.iloc[-1]) / last_price * 100
        if not pd.isna(macd_hist_s.iloc[-1]) and last_price
        else None
    )
    last_bollinger_pct_b = float(bollinger_pct_b_s.iloc[-1]) if not pd.isna(bollinger_pct_b_s.iloc[-1]) else None

    return {
        "last_price": last_price,
        "momentum_3m_pct": float(roc_3m.iloc[-1]),
        "momentum_1m_pct": float(roc_1m.iloc[-1]) if not pd.isna(roc_1m.iloc[-1]) else 0.0,
        "rsi": float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else 50.0,
        "atr": float(atr_s.iloc[-1]),
        "trend_ok": trend_ok,
        "pct_from_52w_high": last_from_high,
        "macd_histogram_pct": last_macd_hist_pct,
        "bollinger_pct_b": last_bollinger_pct_b,
    }


# Re-exportado para que las estrategias en app/strategies/ lo importen junto
# con el resto del contexto tecnico compartido sin saber que en realidad vive
# en app/sector_strength.py (ver ese modulo: separado de aca para que
# screener.py pueda usarlo sin un import circular con app/strategies/__init__.py,
# que a su vez importa screener.py).
sector_relative_strength = _sector_relative_strength


def avg_dollar_volume(bars: pd.DataFrame) -> float:
    """Volumen promedio en DOLARES (precio x acciones) de los ultimos 20
    dias: mismo criterio de liquidez que Momentum (ver screener.py), para que
    el filtro no deje pasar acciones baratas con poco dinero realmente
    operado por dia."""
    avg_volume_s = bars["Volume"].rolling(20, min_periods=1).mean()
    return float((avg_volume_s * bars["Close"]).iloc[-1])


def avg_volume(bars: pd.DataFrame) -> float:
    return float(bars["Volume"].rolling(20, min_periods=1).mean().iloc[-1])


def earnings_blackout_ok(symbol: str, blackout_days: int, force: bool = False) -> tuple[bool, "int | None"]:
    """(ok, dias_a_earnings). ok=False solo si hay una fecha de earnings
    conocida dentro de la ventana de blackout; sin dato, no bloquea (ver
    razonamiento en get_next_earnings_date)."""
    earnings_date = get_next_earnings_date(symbol, force=force)
    days = (earnings_date - datetime.now(timezone.utc).date()).days if earnings_date else None
    ok = days is None or not (0 <= days <= blackout_days)
    return ok, days


def _as_pct(fraction: "float | None") -> "float | None":
    return fraction * 100 if fraction is not None else None


def band_score(value: float, center: float, half_range: float) -> float:
    """100 en `value == center`, decae linealmente a 0 a `half_range` de
    distancia de `center` (clampeado a [0, 100]). Para componentes donde
    "mas no es siempre mejor" (ej. volatilidad, distancia al maximo de 52
    semanas, dividend yield, payout ratio): a diferencia de un componente
    monotonico (mas alto = mejor, sin techo), aca el ranking cross-sectional
    (ver apply_cross_sectional_normalization) premia estar cerca del punto
    ideal, no estar en el extremo. El llamador decide como resolver un valor
    faltante (None) ANTES de llamar a esta funcion: no hay una unica
    semantica correcta de "falta el dato" para todos los componentes (ej.
    payout ratio ausente conviene asumirlo en el centro del rango sostenible,
    pero dividend yield ausente conviene tratarlo como 0, el peor caso)."""
    half_range = max(1e-9, half_range)
    return 100.0 * (1.0 - min(1.0, abs(value - center) / half_range))


def safety_score(
    current_ratio: "float | None",
    quick_ratio: "float | None",
    free_cash_flow: "float | None",
    beta: "float | None",
) -> float:
    """Solidez financiera/riesgo 0-100: liquidez de corto plazo (current/
    quick ratio), generacion de caja (FCF positivo, solo como bonus binario:
    un FCF mayor en valor absoluto no es "mejor" de forma comparable entre
    empresas de escala muy distinta) y volatilidad (beta, cuanto mas bajo
    mas "seguro"). Cada componente ausente se excluye del promedio en vez de
    penalizar (no hay dato para opinar, no es lo mismo que un dato malo); si
    no hay ningun dato disponible, devuelve 50.0 (neutral)."""
    parts: list[float] = []
    if current_ratio is not None:
        parts.append(min(100.0, max(0.0, current_ratio / 2.0 * 100.0)))
    if quick_ratio is not None:
        parts.append(min(100.0, max(0.0, quick_ratio / 1.5 * 100.0)))
    if free_cash_flow is not None:
        parts.append(100.0 if free_cash_flow > 0 else 0.0)
    if beta is not None:
        parts.append(min(100.0, max(0.0, 100.0 - beta * 50.0)))
    return sum(parts) / len(parts) if parts else 50.0


def ownership_alignment_score(insider_pct: "float | None", institutional_pct: "float | None") -> float:
    """Alineacion de incentivos 0-100: mas ownership de insiders (skin in
    the game de fundadores/management) y mas ownership institucional
    (validacion de inversores profesionales) puntua mejor. Cada componente
    ausente se excluye del promedio; si no hay ningun dato, devuelve 50.0
    (neutral)."""
    parts: list[float] = []
    if insider_pct is not None:
        # 10% de ownership de insiders ya se considera alto para una empresa
        # publica tipica: a partir de ahi no suma mas (clampeado a 100).
        parts.append(min(100.0, max(0.0, insider_pct * 10.0)))
    if institutional_pct is not None:
        # ~83% de ownership institucional ya se considera alto.
        parts.append(min(100.0, max(0.0, institutional_pct * 1.2)))
    return sum(parts) / len(parts) if parts else 50.0


def extra_fundamentals_context(fundamentals: dict, max_beta: float, max_short_interest_pct: float) -> tuple[dict, list[str]]:
    """Fundamentales adicionales "casi gratis" (vienen en la misma respuesta
    de info de yfinance que Largo plazo y Dividendos ya pedian, sin requests
    extra): PEG, beta, recomendacion de analistas, ownership de insiders/
    institucional, interes en corto y ratios de liquidez/FCF. Ninguno de
    estos bloquea passes_filters: son notas informativas (no afectan
    `score`), igual que max_debt_to_equity en LongTermConfig. Comun a ambas
    estrategias para no duplicar la misma extraccion y las mismas notas dos
    veces."""
    peg = fundamentals.get("peg_ratio")
    peg_is_trailing_estimate = False
    if peg is None:
        peg = fundamentals.get("peg_ratio_trailing")
        peg_is_trailing_estimate = peg is not None
    beta = fundamentals.get("beta")
    recommendation = fundamentals.get("recommendation_key")
    insider_pct = _as_pct(fundamentals.get("insider_ownership"))
    institutional_pct = _as_pct(fundamentals.get("institutional_ownership"))
    short_pct = _as_pct(fundamentals.get("short_pct_of_float"))
    current_ratio = fundamentals.get("current_ratio")
    quick_ratio = fundamentals.get("quick_ratio")
    free_cash_flow = fundamentals.get("free_cash_flow")

    notes: list[str] = []
    if peg_is_trailing_estimate:
        notes.append("PEG calculado con trailing PEG ratio: no hay PEG estandar disponible.")
    if beta is not None and beta > max_beta:
        notes.append(
            f"Beta {beta:.2f} por encima del umbral preferido ({max_beta}): mas volatil que el "
            "mercado (no bloquea, solo informativo)."
        )
    if short_pct is not None and short_pct > max_short_interest_pct:
        notes.append(
            f"Interes en corto {short_pct:.1f}% por encima del umbral preferido "
            f"({max_short_interest_pct}%) (no bloquea, solo informativo)."
        )
    if current_ratio is not None and current_ratio < 1.0:
        notes.append(f"Current ratio {current_ratio:.2f} por debajo de 1.0 (no bloquea, solo informativo).")
    if quick_ratio is not None and quick_ratio < 1.0:
        notes.append(f"Quick ratio {quick_ratio:.2f} por debajo de 1.0 (no bloquea, solo informativo).")
    if free_cash_flow is not None and free_cash_flow < 0:
        notes.append("Free cash flow negativo (no bloquea, solo informativo).")
    if recommendation in {"sell", "strong_sell", "underperform"}:
        notes.append(f"Consenso de analistas: {recommendation} (no bloquea, solo informativo).")

    # Sin redondear: el llamador (evaluate_symbol de cada estrategia) redondea
    # recien al construir el SignalResult, igual que el resto de los campos
    # fundamentales existentes (ver pe_ratio en long_term.py/dividend.py). peg
    # ratio ademas se usa crudo en el calculo de score de Largo plazo.
    ctx = {
        "peg_ratio": peg,
        "beta": beta,
        "analyst_recommendation": recommendation,
        "insider_ownership_pct": insider_pct,
        "institutional_ownership_pct": institutional_pct,
        "short_pct_of_float": short_pct,
        "current_ratio": current_ratio,
        "quick_ratio": quick_ratio,
        "free_cash_flow": free_cash_flow,
    }
    return ctx, notes
