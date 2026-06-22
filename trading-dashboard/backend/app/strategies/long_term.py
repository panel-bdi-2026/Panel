from __future__ import annotations

import time
from datetime import datetime, timezone

from ..market_data import MarketDataError, get_daily_bars, get_fundamentals, is_bars_cached
from ..models import SignalResult
from ..news_sentiment import apply_news_sentiment_adjustment
from ..scoring import apply_cross_sectional_normalization
from ..screener_config import ScreenerConfig
from ..sectors import get_sector
from .common import (
    avg_dollar_volume,
    avg_volume,
    context_technicals,
    earnings_blackout_ok,
    extra_fundamentals_context,
    ownership_alignment_score,
    safety_score,
    sector_relative_strength,
)


class LongTermStrategy:
    """Largo plazo (meses/1 año): fundamentales fuertes (crecimiento de
    ganancias, ROE) y precio bajo respecto a su valoracion (PE), buscando
    apreciacion en el mediano plazo. No es momentum/tecnica: el gating
    principal es sobre datos fundamentales de yfinance, no sobre indicadores
    de precio (que solo se calculan para mostrar contexto y para sugerir un
    stop-loss).

    Sin historia de fundamentals point-in-time en yfinance gratuito, esta
    estrategia NO es backtesteable: solo disponible para escaneo en vivo
    (ver supports_backtest=False, chequeado en main.py antes de llamar a
    /api/signals/backtest)."""

    id = "long_term"
    name = "Largo plazo"
    description = (
        "Largo plazo (meses/1 año): fundamentales de crecimiento y "
        "rentabilidad con valoración razonable. Solo escaneo en vivo (sin "
        "backtest)."
    )
    supports_backtest = False

    def __init__(self, config: ScreenerConfig):
        self.config = config

    def reload(self, config: ScreenerConfig) -> None:
        self.config = config

    def evaluate_symbol(self, symbol: str, force: bool = False) -> SignalResult | None:
        cfg = self.config
        lt = cfg.long_term
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days, force=force)
        except MarketDataError:
            return None
        ctx = context_technicals(bars, cfg.atr_period)
        if ctx is None:
            return None

        fundamentals = get_fundamentals(symbol, force=force)
        # trailing_pe (ganancias ya reportadas) y forward_pe (estimado por
        # analistas a futuro) son metodologias distintas: mezclarlas sin
        # distincion comparaba, entre simbolos distintos, un PE real contra
        # una proyeccion sin que el usuario lo supiera. Se prefiere trailing
        # (dato real) y forward queda solo como respaldo cuando no hay
        # trailing, con una nota explicita de que es una estimacion.
        pe = fundamentals.get("trailing_pe")
        pe_is_forward_estimate = False
        if pe is None:
            pe = fundamentals.get("forward_pe")
            pe_is_forward_estimate = pe is not None
        earnings_growth = fundamentals.get("earnings_growth")
        revenue_growth = fundamentals.get("revenue_growth")
        roe = fundamentals.get("return_on_equity")
        debt_to_equity = fundamentals.get("debt_to_equity")
        profit_margins = fundamentals.get("profit_margins")
        price_to_book = fundamentals.get("price_to_book")
        extra, extra_notes = extra_fundamentals_context(fundamentals, lt.max_beta, lt.max_short_interest_pct)
        peg = extra["peg_ratio"]

        last_price = ctx["last_price"]
        last_avg_dollar_vol = avg_dollar_volume(bars)
        last_avg_vol = avg_volume(bars)
        # Informativo solamente (no entra a `score`, ver docstring de la
        # clase): esta estrategia es deliberadamente fundamentals-first, no
        # tecnica, asi que mezclar señales de precio en su ranking diluiria
        # la tesis que el usuario explicitamente pidio para esta estrategia.
        last_sector_rel_strength = sector_relative_strength(symbol, ctx["momentum_3m_pct"], cfg.lookback_days, force=force)

        # Valoracion y crecimiento son los dos pilares explicitamente
        # pedidos ("fundamentales fuertes" + "bajo su valoracion"): si faltan,
        # no se puede evaluar la tesis y la fila no pasa el filtro (a
        # diferencia de ROE/margen/deuda, que son bonus de score, no gating,
        # porque yfinance los reporta de forma mas inconsistente).
        value_ok = pe is not None and 0 < pe <= lt.max_pe_ratio
        growth_ok = earnings_growth is not None and earnings_growth * 100 >= lt.min_earnings_growth_pct
        liquidity_ok = last_avg_dollar_vol >= cfg.min_avg_dollar_volume

        days_to_earnings: int | None = None
        earnings_ok = True
        if value_ok and growth_ok and liquidity_ok:
            earnings_ok, days_to_earnings = earnings_blackout_ok(symbol, cfg.earnings_blackout_days, force=force)

        notes: list[str] = []
        if pe is None:
            notes.append("Sin dato de PE disponible: no se puede evaluar la valoracion.")
        elif not value_ok:
            notes.append(f"PE {pe:.1f} excede el maximo configurado ({lt.max_pe_ratio}).")
        if pe_is_forward_estimate:
            notes.append("PE calculado con forward PE (estimado por analistas): no hay PE trailing disponible.")
        if earnings_growth is None:
            notes.append("Sin dato de crecimiento de ganancias disponible.")
        elif not growth_ok:
            notes.append(
                f"Crecimiento de ganancias {earnings_growth * 100:.1f}% por debajo del minimo "
                f"({lt.min_earnings_growth_pct}%)."
            )
        if not liquidity_ok:
            notes.append("Volumen promedio por debajo del minimo de liquidez configurado.")
        if not earnings_ok:
            notes.append(f"Earnings estimados en {days_to_earnings} dia(s): dentro de la ventana de blackout.")
        if roe is None:
            notes.append("Sin dato de ROE disponible (no afecta el filtro, solo el score).")
        if debt_to_equity is not None and debt_to_equity > lt.max_debt_to_equity:
            notes.append(f"Deuda/equity {debt_to_equity:.0f} por encima del umbral preferido ({lt.max_debt_to_equity}).")
        notes.extend(extra_notes)

        # Earnings yield (100/PE) y book yield (100/price_to_book): invertir
        # el ratio en vez de restarlo de un tope (como antes) hace que ambas
        # mitades de "value" se combinen en la misma escala (% de retorno
        # implicito), no en unidades de PE. PE/PB negativo (ganancias o
        # patrimonio negativo) puede dar un numero arbitrariamente grande en
        # valor absoluto al invertirlo: sin este resguardo, una empresa con
        # perdidas se premiaria sin limite como si fuera la mejor oportunidad
        # de valor, cuando en realidad ni siquiera pasa el filtro (value_ok
        # exige pe > 0). Ausente (None) tambien cae en 0.0: no hay base para
        # asumir que una empresa sin el dato es mas barata que una que si lo
        # reporta.
        earnings_yield_pct = (100.0 / pe) if pe is not None and pe > 0 else 0.0
        book_yield_pct = (100.0 / price_to_book) if price_to_book is not None and price_to_book > 0 else 0.0
        value_score = 0.6 * earnings_yield_pct + 0.4 * book_yield_pct
        # Mismo resguardo que value_score: un PEG negativo (crecimiento de
        # ganancias negativo) no es "barato", es una division por un numero
        # negativo que daria un score arbitrariamente alto sin merecerlo.
        peg_score = (lt.max_peg_ratio - peg) if peg is not None and peg > 0 else 0.0
        # Crecimiento de ganancias y de ingresos (60/40): revenue_growth se
        # extraia pero no se usaba en ningun lado. Si solo hay un dato
        # disponible, se usa entero (sin reescalar por el peso del que
        # falta): asumir 0.0 para el que falta penalizaria a una empresa solo
        # porque yfinance no reporto ese campo, no porque su crecimiento sea
        # malo.
        earnings_growth_pct = earnings_growth * 100 if earnings_growth is not None else None
        revenue_growth_pct = revenue_growth * 100 if revenue_growth is not None else None
        if earnings_growth_pct is not None and revenue_growth_pct is not None:
            growth_pct = 0.6 * earnings_growth_pct + 0.4 * revenue_growth_pct
        elif earnings_growth_pct is not None:
            growth_pct = earnings_growth_pct
        elif revenue_growth_pct is not None:
            growth_pct = revenue_growth_pct
        else:
            growth_pct = 0.0
        roe_pct = (roe or 0.0) * 100
        margin_pct = (profit_margins or 0.0) * 100
        # Piotroski-lite: rentabilidad (ROE + margen, el "margin" que antes
        # era una componente de score separada) con una penalizacion graduada
        # por apalancamiento -- no gating (max_debt_to_equity sigue siendo
        # solo una nota informativa, ver mas arriba), pero un apalancamiento
        # alto si pesa en contra de la calidad. Penaliza solo el exceso sobre
        # max_debt_to_equity (debajo de ese umbral, sin penalizacion), y la
        # combinacion no puede quedar negativa.
        debt_penalty = (
            max(0.0, debt_to_equity - lt.max_debt_to_equity) / lt.max_debt_to_equity * 50.0
            if debt_to_equity is not None
            else 0.0
        )
        quality_score = max(0.0, (roe_pct + margin_pct) / 2 - debt_penalty)
        safety = safety_score(extra["current_ratio"], extra["quick_ratio"], extra["free_cash_flow"], extra["beta"])
        ownership_alignment = ownership_alignment_score(extra["insider_ownership_pct"], extra["institutional_ownership_pct"])

        components = {
            "value": value_score,
            "growth": growth_pct,
            "quality": quality_score,
            "peg": peg_score,
            "safety": safety,
            "ownership_alignment": ownership_alignment,
        }
        score = (
            lt.score_weight_value * components["value"]
            + lt.score_weight_growth * components["growth"]
            + lt.score_weight_quality * components["quality"]
            + lt.score_weight_peg * components["peg"]
            + lt.score_weight_safety * components["safety"]
            + lt.score_weight_ownership_alignment * components["ownership_alignment"]
        )

        stop_loss_price = max(0.0, last_price - lt.stop_loss_atr_multiplier * ctx["atr"])
        stop_loss_pct = (last_price - stop_loss_price) / last_price * 100 if last_price else 0.0

        return SignalResult(
            symbol=symbol,
            as_of=datetime.now(timezone.utc),
            last_price=round(last_price, 2),
            score=round(score, 2),
            momentum_3m_pct=round(ctx["momentum_3m_pct"], 2),
            momentum_1m_pct=round(ctx["momentum_1m_pct"], 2),
            trend_ok=ctx["trend_ok"],
            rsi=round(ctx["rsi"], 1),
            avg_volume=round(last_avg_vol, 0),
            pct_from_52w_high=round(ctx["pct_from_52w_high"], 2) if ctx["pct_from_52w_high"] is not None else None,
            suggested_stop_loss_price=round(stop_loss_price, 2),
            suggested_stop_loss_pct=round(stop_loss_pct, 2),
            passes_filters=value_ok and growth_ok and liquidity_ok and earnings_ok,
            liquidity_ok=liquidity_ok,
            earnings_ok=earnings_ok,
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
            pe_ratio=round(pe, 2) if pe is not None else None,
            price_to_book=round(price_to_book, 2) if price_to_book is not None else None,
            peg_ratio=round(peg, 2) if peg is not None else None,
            beta=round(extra["beta"], 2) if extra["beta"] is not None else None,
            analyst_recommendation=extra["analyst_recommendation"],
            insider_ownership_pct=round(extra["insider_ownership_pct"], 2) if extra["insider_ownership_pct"] is not None else None,
            institutional_ownership_pct=round(extra["institutional_ownership_pct"], 2) if extra["institutional_ownership_pct"] is not None else None,
            short_pct_of_float=round(extra["short_pct_of_float"], 2) if extra["short_pct_of_float"] is not None else None,
            current_ratio=round(extra["current_ratio"], 2) if extra["current_ratio"] is not None else None,
            quick_ratio=round(extra["quick_ratio"], 2) if extra["quick_ratio"] is not None else None,
            free_cash_flow=round(extra["free_cash_flow"], 2) if extra["free_cash_flow"] is not None else None,
            macd_histogram_pct=round(ctx["macd_histogram_pct"], 2) if ctx["macd_histogram_pct"] is not None else None,
            bollinger_pct_b=round(ctx["bollinger_pct_b"], 2) if ctx["bollinger_pct_b"] is not None else None,
            sector_relative_strength_pct=round(last_sector_rel_strength, 2) if last_sector_rel_strength is not None else None,
            score_components=components,
        )

    def scan(self, force: bool = False) -> list[SignalResult]:
        results = []
        delay = self.config.scan_request_delay_seconds
        for i, symbol in enumerate(self.config.universe):
            # Salteado si el dato ya esta cacheado (ej. otra estrategia ya
            # escaneo este simbolo en este ciclo): no hay fetch real que
            # espaciar (ver is_bars_cached).
            if i > 0 and delay > 0 and (force or not is_bars_cached(symbol, self.config.lookback_days)):
                time.sleep(delay)
            result = self.evaluate_symbol(symbol, force=force)
            if result is not None:
                results.append(result)
        if self.config.universe and not results:
            raise MarketDataError(
                "No se pudo obtener datos de mercado para ningun simbolo del universo "
                "configurado. Puede ser un problema de conectividad o el limite de la "
                "API gratuita de datos."
            )
        lt = self.config.long_term
        apply_cross_sectional_normalization(results, {
            "value": lt.score_weight_value,
            "growth": lt.score_weight_growth,
            "quality": lt.score_weight_quality,
            "peg": lt.score_weight_peg,
            "safety": lt.score_weight_safety,
            "ownership_alignment": lt.score_weight_ownership_alignment,
        })
        results.sort(key=lambda r: r.score, reverse=True)
        if self.config.news_sentiment_enabled:
            apply_news_sentiment_adjustment(
                results,
                self.config.top_n,
                self.config.news_sentiment_shortlist_multiplier,
                self.config.news_sentiment_max_adjustment,
            )
            results.sort(key=lambda r: r.score, reverse=True)
        return results
