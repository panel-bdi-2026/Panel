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
    band_score,
    context_technicals,
    earnings_blackout_ok,
    extra_fundamentals_context,
    ownership_alignment_score,
    safety_score,
    sector_relative_strength,
)


def _dividend_yield_pct(raw: "float | None") -> "float | None":
    """yfinance reporto dividendYield como fraccion (ej. 0.025 = 2.5%)
    durante años, pero algunas respuestas recientes lo devuelven ya en
    porcentaje (ej. 2.5). No hay un campo separado que distinga el formato,
    asi que se normaliza por magnitud: un yield real de mercado no supera ~1
    (100%), entonces un valor < 1 se interpreta como fraccion y se multiplica
    por 100; un valor >= 1 se asume ya en porcentaje."""
    if raw is None:
        return None
    return raw * 100 if raw < 1 else raw


class DividendStrategy:
    """Maximiza dividend yield: prioriza buen yield con un payout ratio
    sostenible (ni tan bajo que no sea el foco, ni tan alto que arriesgue un
    recorte) y valora fundamentales sanos (ROE, margen) como criterio
    secundario. Mismo motivo que LongTermStrategy para no ser
    backtesteable: sin historia point-in-time de estos datos en yfinance."""

    id = "dividend"
    name = "Dividendos"
    description = (
        "Dividend yield: buen yield con payout sostenible y fundamentales "
        "sanos. Solo escaneo en vivo (sin backtest)."
    )
    supports_backtest = False

    def __init__(self, config: ScreenerConfig):
        self.config = config

    def reload(self, config: ScreenerConfig) -> None:
        self.config = config

    def evaluate_symbol(self, symbol: str, force: bool = False, cache_only: bool = False) -> SignalResult | None:
        cfg = self.config
        dv = cfg.dividend
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days, force=force, cache_only=cache_only)
        except MarketDataError:
            return None
        ctx = context_technicals(bars, cfg.atr_period)
        if ctx is None:
            return None

        fundamentals = get_fundamentals(symbol, force=force, cache_only=cache_only)
        div_yield_pct = _dividend_yield_pct(fundamentals.get("dividend_yield"))
        payout_ratio = fundamentals.get("payout_ratio")
        payout_pct = payout_ratio * 100 if payout_ratio is not None else None
        roe = fundamentals.get("return_on_equity")
        profit_margins = fundamentals.get("profit_margins")
        extra, extra_notes = extra_fundamentals_context(fundamentals, dv.max_beta, dv.max_short_interest_pct)

        last_price = ctx["last_price"]
        last_avg_dollar_vol = avg_dollar_volume(bars)
        last_avg_vol = avg_volume(bars)
        # Informativo solamente (no entra a `score`): igual razonamiento que
        # en LongTermStrategy, esta estrategia tambien es fundamentals-first.
        last_sector_rel_strength = sector_relative_strength(symbol, ctx["momentum_3m_pct"], cfg.lookback_days, force=force, cache_only=cache_only)

        yield_ok = div_yield_pct is not None and div_yield_pct >= dv.min_dividend_yield_pct
        payout_ok = payout_pct is not None and dv.min_payout_ratio_pct <= payout_pct <= dv.max_payout_ratio_pct
        liquidity_ok = last_avg_dollar_vol >= cfg.min_avg_dollar_volume

        days_to_earnings: int | None = None
        earnings_ok = True
        if yield_ok and payout_ok and liquidity_ok:
            earnings_ok, days_to_earnings = earnings_blackout_ok(symbol, cfg.earnings_blackout_days, force=force, cache_only=cache_only)

        notes: list[str] = []
        if div_yield_pct is None:
            notes.append("Sin dato de dividend yield disponible.")
        elif not yield_ok:
            notes.append(f"Dividend yield {div_yield_pct:.2f}% por debajo del minimo ({dv.min_dividend_yield_pct}%).")
        if payout_pct is None:
            notes.append("Sin dato de payout ratio disponible.")
        elif not payout_ok:
            notes.append(
                f"Payout ratio {payout_pct:.1f}% fuera del rango sostenible configurado "
                f"({dv.min_payout_ratio_pct}%-{dv.max_payout_ratio_pct}%)."
            )
        if not liquidity_ok:
            notes.append("Volumen promedio por debajo del minimo de liquidez configurado.")
        if not earnings_ok:
            notes.append(f"Earnings estimados en {days_to_earnings} dia(s): dentro de la ventana de blackout.")
        if roe is not None and roe * 100 < dv.min_return_on_equity_pct:
            notes.append(f"ROE {roe * 100:.1f}% por debajo del umbral preferido ({dv.min_return_on_equity_pct}%) (no bloquea, solo score).")
        if profit_margins is not None and profit_margins * 100 < dv.min_profit_margin_pct:
            notes.append(
                f"Margen de ganancia {profit_margins * 100:.1f}% por debajo del umbral "
                f"preferido ({dv.min_profit_margin_pct}%) (no bloquea, solo score)."
            )
        notes.extend(extra_notes)

        # Yield y payout_quality usan band_score (no monotonico, ver
        # common.py): un yield muy por encima del rango sostenible suele ser
        # "yield trap" (no mejor oportunidad), y un payout cerca del centro
        # del rango configurado da mas margen antes de un recorte de
        # dividendo que cualquiera de los dos extremos. Yield ausente se
        # trata como 0 (el peor caso, ya bloquea passes_filters); payout
        # ausente se asume en el centro del rango sostenible (no hay base
        # para asumir lo peor solo porque falta el dato).
        yield_mid = (dv.min_dividend_yield_pct + dv.max_dividend_yield_pct) / 2
        yield_half_range = max(1.0, (dv.max_dividend_yield_pct - dv.min_dividend_yield_pct) / 2)
        yield_score = band_score(div_yield_pct if div_yield_pct is not None else 0.0, yield_mid, yield_half_range)

        payout_mid = (dv.min_payout_ratio_pct + dv.max_payout_ratio_pct) / 2
        payout_half_range = max(1.0, (dv.max_payout_ratio_pct - dv.min_payout_ratio_pct) / 2)
        payout_quality = band_score(payout_pct if payout_pct is not None else payout_mid, payout_mid, payout_half_range)

        roe_pct = (roe or 0.0) * 100
        margin_pct = (profit_margins or 0.0) * 100
        safety = safety_score(extra["current_ratio"], extra["quick_ratio"], extra["free_cash_flow"], extra["beta"])
        ownership_alignment = ownership_alignment_score(extra["insider_ownership_pct"], extra["institutional_ownership_pct"])

        components = {
            "yield": yield_score,
            "payout_quality": payout_quality,
            "quality": (roe_pct + margin_pct) / 2,
            "safety": safety,
            "ownership_alignment": ownership_alignment,
        }
        score = (
            dv.score_weight_yield * components["yield"]
            + dv.score_weight_payout_quality * components["payout_quality"]
            + dv.score_weight_quality * components["quality"]
            + dv.score_weight_safety * components["safety"]
            + dv.score_weight_ownership_alignment * components["ownership_alignment"]
        )

        stop_loss_price = max(0.0, last_price - dv.stop_loss_atr_multiplier * ctx["atr"])
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
            passes_filters=yield_ok and payout_ok and liquidity_ok and earnings_ok,
            liquidity_ok=liquidity_ok,
            earnings_ok=earnings_ok,
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
            dividend_yield_pct=round(div_yield_pct, 2) if div_yield_pct is not None else None,
            payout_ratio_pct=round(payout_pct, 2) if payout_pct is not None else None,
            peg_ratio=round(extra["peg_ratio"], 2) if extra["peg_ratio"] is not None else None,
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

    def scan(self, force: bool = False, cache_only: bool = False) -> list[SignalResult]:
        results = []
        delay = self.config.scan_request_delay_seconds
        for i, symbol in enumerate(self.config.universe):
            # Salteado si el dato ya esta cacheado o si es cache_only (sin red).
            if not cache_only and i > 0 and delay > 0 and (force or not is_bars_cached(symbol, self.config.lookback_days)):
                time.sleep(delay)
            result = self.evaluate_symbol(symbol, force=force, cache_only=cache_only)
            if result is not None:
                results.append(result)
        if self.config.universe and not results:
            raise MarketDataError(
                "No se pudo obtener datos de mercado para ningun simbolo del universo "
                "configurado. Puede ser un problema de conectividad o el limite de la "
                "API gratuita de datos."
            )
        dv = self.config.dividend
        apply_cross_sectional_normalization(results, {
            "yield": dv.score_weight_yield,
            "payout_quality": dv.score_weight_payout_quality,
            "quality": dv.score_weight_quality,
            "safety": dv.score_weight_safety,
            "ownership_alignment": dv.score_weight_ownership_alignment,
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
