from __future__ import annotations

import time
from datetime import datetime, timezone

from ..indicators import rate_of_change, rsi
from ..market_data import MarketDataError, get_daily_bars, is_bars_cached
from ..models import SignalResult
from ..scoring import apply_cross_sectional_normalization
from ..screener_config import ScreenerConfig
from ..sectors import get_sector
from .common import (
    avg_dollar_volume,
    avg_volume,
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

    def evaluate_symbol(self, symbol: str, force: bool = False) -> SignalResult | None:
        cfg = self.config
        opp = cfg.opportunistic
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days, force=force)
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
        last_sector_rel_strength = sector_relative_strength(symbol, ctx["momentum_3m_pct"], cfg.lookback_days, force=force)

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
        if momentum_ok and rsi_ok and volatility_ok and liquidity_ok and room_to_grow_ok:
            earnings_ok, days_to_earnings = earnings_blackout_ok(symbol, cfg.earnings_blackout_days, force=force)

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
        if not earnings_ok:
            notes.append(f"Earnings estimados en {days_to_earnings} dia(s): dentro de la ventana de blackout.")

        components = {
            "momentum": last_roc,
            "volatility": volatility_pct,
            "rsi_recovery": last_rsi - opp.rsi_min,
            "room_to_grow": abs(last_from_high or 0.0),
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
            passes_filters=momentum_ok and rsi_ok and volatility_ok and liquidity_ok and room_to_grow_ok and earnings_ok,
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
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
        opp = self.config.opportunistic
        apply_cross_sectional_normalization(results, {
            "momentum": opp.score_weight_momentum,
            "volatility": opp.score_weight_volatility,
            "rsi_recovery": opp.score_weight_rsi_recovery,
            "room_to_grow": opp.score_weight_room_to_grow,
            "macd_turn": opp.score_weight_macd_turn,
            "sector_relative_strength": opp.score_weight_sector_relative_strength,
        })
        results.sort(key=lambda r: r.score, reverse=True)
        return results
