from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd

from ..indicators import market_regime_ok, rate_of_change, rsi
from ..market_data import MarketDataError, get_daily_bars, is_bars_cached
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
    sector_relative_strength,
)


class OpportunisticStrategy:
    """Corto plazo (dias/semanas): acciones algo mas volatiles que Momentum,
    que recien muestran señales de giro al alza (RSI saliendo de zona baja,
    retorno reciente positivo) y que todavia cotizan bien por debajo de su
    maximo de 52 semanas -- el "espacio de crecimiento" que Momentum
    descarta a proposito (Momentum busca lideres ya cerca de maximos)."""

    id = "opportunistic"
    name = "Oportunista"
    description = (
        "Corto plazo (días/semanas): acciones con señal de giro al alza, "
        "lejos todavía de su máximo de 52 semanas. Disponible para escaneo "
        "en vivo y backtest."
    )
    supports_backtest = True

    def __init__(self, config: ScreenerConfig):
        self.config = config

    def reload(self, config: ScreenerConfig) -> None:
        self.config = config

    def _benchmark_regime_ok(self, force: bool = False, cache_only: bool = False) -> bool:
        """Igual que MomentumScreener._benchmark_context, pero solo el
        booleano de regimen: Oportunista no usa el ROC del benchmark para
        fuerza relativa (no tiene ese componente de score). Usa su propio
        flag (opportunistic_regime_filter_enabled), no el de Momentum: un
        re-test del filtro compartido (experimento P2, 2026-06-24) mostro que
        ayuda a Momentum pero empeora a Oportunista, cuya logica central es
        comprar giros/reversiones, que aparecen justo cuando el mercado no
        esta en tendencia alcista limpia."""
        cfg = self.config
        if not cfg.opportunistic_regime_filter_enabled:
            return True
        try:
            bench_bars = get_daily_bars(cfg.benchmark_symbol, cfg.lookback_days, force=force, cache_only=cache_only)
        except MarketDataError:
            return True
        close = bench_bars["Close"]
        if not len(close):
            return True
        regime_series = market_regime_ok(
            close, cfg.regime_sma_period, cfg.regime_slope_lookback_days,
            cfg.regime_absolute_momentum_lookback_days,
        )
        last_value = regime_series.iloc[-1]
        return bool(last_value) if not pd.isna(last_value) else True

    def evaluate_symbol(self, symbol: str, regime_ok: bool = True, force: bool = False, cache_only: bool = False) -> SignalResult | None:
        cfg = self.config
        opp = cfg.opportunistic
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days, force=force, cache_only=cache_only)
        except MarketDataError:
            return None
        if len(bars) < opp.momentum_lookback_days + 5:
            return None

        ctx = context_technicals(bars, cfg.atr_period)
        if ctx is None:
            return None

        close = bars["Close"]
        roc_short = rate_of_change(close, opp.momentum_lookback_days)
        rsi_short = rsi(close, opp.rsi_period)
        if roc_short.empty or rsi_short.empty:
            return None

        last_price = ctx["last_price"]
        last_roc = float(roc_short.iloc[-1])
        last_rsi = float(rsi_short.iloc[-1])
        last_atr = ctx["atr"]
        last_from_high = ctx["pct_from_52w_high"]
        last_avg_dollar_vol = avg_dollar_volume(bars)
        last_avg_vol = avg_volume(bars)
        last_sector_rel_strength = sector_relative_strength(symbol, ctx["momentum_3m_pct"], cfg.lookback_days, force=force, cache_only=cache_only)

        # ATR como % del precio: piso de volatilidad para diferenciarse de
        # Momentum, que no exige ningun minimo.
        volatility_pct = (last_atr / last_price * 100) if last_price else 0.0

        momentum_ok = last_roc > 0
        rsi_ok = opp.rsi_min <= last_rsi <= opp.rsi_max
        volatility_ok = volatility_pct >= opp.min_volatility_pct
        liquidity_ok = last_avg_dollar_vol >= cfg.min_avg_dollar_volume
        room_to_grow_ok = last_from_high is None or last_from_high <= -opp.min_pct_below_52w_high

        days_to_earnings: int | None = None
        earnings_ok = True
        if momentum_ok and rsi_ok and volatility_ok and liquidity_ok and room_to_grow_ok and regime_ok:
            earnings_ok, days_to_earnings = earnings_blackout_ok(symbol, cfg.earnings_blackout_days, force=force, cache_only=cache_only)

        notes: list[str] = []
        if not momentum_ok:
            notes.append("Retorno reciente no es positivo: sin señal de giro al alza.")
        if not rsi_ok:
            notes.append(f"RSI {last_rsi:.1f} fuera del rango de recuperacion configurado ({opp.rsi_min}-{opp.rsi_max}).")
        if not volatility_ok:
            notes.append(f"Volatilidad {volatility_pct:.1f}% (ATR/precio) por debajo del minimo ({opp.min_volatility_pct}%).")
        if not liquidity_ok:
            notes.append("Volumen promedio por debajo del minimo de liquidez configurado.")
        if not room_to_grow_ok:
            notes.append(f"Solo {abs(last_from_high):.1f}% por debajo del maximo de 52 semanas: poco espacio de crecimiento.")
        if not regime_ok:
            notes.append("Filtro de regimen: el benchmark no esta en regimen alcista de fondo.")
        if not earnings_ok:
            notes.append(f"Earnings estimados en {days_to_earnings} dia(s): dentro de la ventana de blackout.")

        # volatility y room_to_grow usan band_score (no monotonico: "mas no es
        # siempre mejor", ver common.py): demasiada volatilidad o estar
        # demasiado lejos del maximo de 52 semanas dejan de ser una mejor
        # señal de "giro al alza" y pasan a ser solo mas riesgo/una caida
        # sostenida, asi que el centro de cada rango configurado puntua mas
        # alto que cualquiera de los dos extremos.
        volatility_mid = (opp.min_volatility_pct + opp.max_volatility_pct) / 2
        volatility_half_range = max(1.0, (opp.max_volatility_pct - opp.min_volatility_pct) / 2)
        room_to_grow_mid = (opp.min_pct_below_52w_high + opp.max_pct_below_52w_high) / 2
        room_to_grow_half_range = max(1.0, (opp.max_pct_below_52w_high - opp.min_pct_below_52w_high) / 2)
        room_to_grow_raw = abs(last_from_high) if last_from_high is not None else 0.0

        components = {
            "momentum": last_roc,
            "volatility": band_score(volatility_pct, volatility_mid, volatility_half_range),
            "rsi_recovery": last_rsi - opp.rsi_min,
            "room_to_grow": band_score(room_to_grow_raw, room_to_grow_mid, room_to_grow_half_range),
            "macd_turn": ctx["macd_histogram_pct"] if ctx["macd_histogram_pct"] is not None else 0.0,
            "sector_relative_strength": last_sector_rel_strength if last_sector_rel_strength is not None else 0.0,
        }
        score = (
            opp.score_weight_momentum * components["momentum"]
            + opp.score_weight_volatility * components["volatility"]
            + opp.score_weight_rsi_recovery * components["rsi_recovery"]
            + opp.score_weight_room_to_grow * components["room_to_grow"]
            + opp.score_weight_macd_turn * components["macd_turn"]
            + opp.score_weight_sector_relative_strength * components["sector_relative_strength"]
        )

        stop_loss_price = max(0.0, last_price - opp.stop_loss_atr_multiplier * last_atr)
        stop_loss_pct = (last_price - stop_loss_price) / last_price * 100 if last_price else 0.0

        return SignalResult(
            symbol=symbol,
            as_of=datetime.now(timezone.utc),
            last_price=round(last_price, 2),
            score=round(score, 2),
            momentum_3m_pct=round(ctx["momentum_3m_pct"], 2),
            momentum_1m_pct=round(ctx["momentum_1m_pct"], 2),
            trend_ok=ctx["trend_ok"],
            rsi=round(last_rsi, 1),
            avg_volume=round(last_avg_vol, 0),
            pct_from_52w_high=round(last_from_high, 2) if last_from_high is not None else None,
            suggested_stop_loss_price=round(stop_loss_price, 2),
            suggested_stop_loss_pct=round(stop_loss_pct, 2),
            passes_filters=(
                momentum_ok and rsi_ok and volatility_ok and liquidity_ok and room_to_grow_ok
                and regime_ok and earnings_ok
            ),
            liquidity_ok=liquidity_ok,
            earnings_ok=earnings_ok,
            regime_ok=regime_ok,
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
            macd_histogram_pct=round(ctx["macd_histogram_pct"], 2) if ctx["macd_histogram_pct"] is not None else None,
            bollinger_pct_b=round(ctx["bollinger_pct_b"], 2) if ctx["bollinger_pct_b"] is not None else None,
            sector_relative_strength_pct=round(last_sector_rel_strength, 2) if last_sector_rel_strength is not None else None,
            score_components=components,
        )

    def scan(self, force: bool = False, cache_only: bool = False) -> list[SignalResult]:
        results = []
        regime_ok = self._benchmark_regime_ok(force=force, cache_only=cache_only)
        delay = self.config.scan_request_delay_seconds
        for i, symbol in enumerate(self.config.universe):
            # Salteado si el dato ya esta cacheado o si es cache_only (sin red).
            if not cache_only and i > 0 and delay > 0 and (force or not is_bars_cached(symbol, self.config.lookback_days)):
                time.sleep(delay)
            result = self.evaluate_symbol(symbol, regime_ok=regime_ok, force=force, cache_only=cache_only)
            if result is not None:
                results.append(result)
        if self.config.universe and not results:
            raise MarketDataError(
                "No se pudo obtener datos de mercado para ningun simbolo del universo "
                "configurado. Puede ser un problema de conectividad o el limite de la "
                "API gratuita de datos."
            )
        opp = self.config.opportunistic
        weights = {
            "momentum": opp.score_weight_momentum,
            "volatility": opp.score_weight_volatility,
            "rsi_recovery": opp.score_weight_rsi_recovery,
            "room_to_grow": opp.score_weight_room_to_grow,
            "macd_turn": opp.score_weight_macd_turn,
            "sector_relative_strength": opp.score_weight_sector_relative_strength,
        }
        passing = [r for r in results if r.passes_filters]
        non_passing = [r for r in results if not r.passes_filters]
        apply_cross_sectional_normalization(passing, weights)
        apply_cross_sectional_normalization(non_passing, weights)
        for r in passing:
            r.score = round(50.0 + r.score * 0.5, 2)
        for r in non_passing:
            r.score = round(r.score * 0.49, 2)
        results = sorted(passing + non_passing, key=lambda r: r.score, reverse=True)
        if self.config.news_sentiment_enabled:
            apply_news_sentiment_adjustment(
                results,
                self.config.top_n,
                self.config.news_sentiment_shortlist_multiplier,
                self.config.news_sentiment_max_adjustment,
            )
            results.sort(key=lambda r: r.score, reverse=True)
        return results
