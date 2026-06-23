from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd

from .indicators import (
    atr,
    bollinger_percent_b,
    macd,
    market_regime_ok,
    momentum_12_1,
    pct_from_high,
    rate_of_change,
    rsi,
    sma,
)
from .market_data import MarketDataError, get_daily_bars, get_next_earnings_date, is_bars_cached
from .models import SignalResult
from .news_sentiment import apply_news_sentiment_adjustment
from .scoring import apply_cross_sectional_normalization
from .screener_config import ScreenerConfig
from .sector_strength import sector_relative_strength
from .sectors import get_sector


class MomentumScreener:
    """Escanea un universo de acciones y rankea oportunidades momentum/tecnicas.

    Esto NO ejecuta ordenes ni las aprueba: produce candidatos para que tu
    decidas si convertirlos en una orden, la cual de todas formas tiene que
    pasar por el RulesEngine (whitelist, stop-loss, limites, etc.) como
    cualquier otra. No es una recomendacion de inversion.
    """

    # Identifica esta estrategia en el registro de app/main.py (ver
    # app/strategies/ para las demas estrategias).
    id = "momentum"
    name = "Momentum"
    description = (
        "Corto/mediano plazo (semanas/meses): acciones con tendencia alcista "
        "confirmada (precio sobre sus medias móviles), buen impulso reciente "
        "y RSI en rango saludable. Disponible para escaneo en vivo y backtest."
    )
    supports_backtest = True

    def __init__(self, config: ScreenerConfig):
        self.config = config

    def reload(self, config: ScreenerConfig) -> None:
        self.config = config

    def _benchmark_context(self, force: bool = False) -> tuple[float | None, bool]:
        """Retorna (roc_3m, regime_ok) del benchmark.

        roc_3m es el momentum usado para la fuerza relativa de cada simbolo.
        regime_ok indica si el benchmark esta en regimen alcista de fondo
        segun indicators.market_regime_ok (pendiente de la SMA de regimen
        positiva Y momentum absoluto positivo, no solo precio > SMA: evita
        el whipsaw de un cruce binario en una SMA plana); si no hay
        suficiente historia para calcularlo, se asume regime_ok=True en vez
        de bloquear todo el scan por falta de dato.
        """
        try:
            bars = get_daily_bars(self.config.benchmark_symbol, self.config.lookback_days, force=force)
        except MarketDataError:
            return None, True
        close = bars["Close"]
        roc = rate_of_change(close, self.config.momentum_lookback_days)
        value = roc.iloc[-1] if len(roc) else None
        roc_3m = float(value) if value is not None and not pd.isna(value) else None

        regime_ok = True
        if self.config.regime_filter_enabled and len(close):
            regime_series = market_regime_ok(
                close,
                self.config.regime_sma_period,
                self.config.regime_slope_lookback_days,
                self.config.regime_absolute_momentum_lookback_days,
            )
            last_value = regime_series.iloc[-1]
            if not pd.isna(last_value):
                regime_ok = bool(last_value)
        return roc_3m, regime_ok

    def evaluate_symbol(
        self,
        symbol: str,
        benchmark_roc_3m: float | None,
        regime_ok: bool = True,
        force: bool = False,
    ) -> SignalResult | None:
        cfg = self.config
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days, force=force)
        except MarketDataError:
            return None
        if len(bars) < max(
            cfg.sma_slow + cfg.momentum_lookback_days // 2, cfg.momentum_12_1_lookback_days + 1
        ):
            return None

        close = bars["Close"]
        sma_fast_s = sma(close, cfg.sma_fast)
        sma_slow_s = sma(close, cfg.sma_slow)
        roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
        roc_1m = rate_of_change(close, cfg.momentum_short_days)
        roc_12_1 = momentum_12_1(close, cfg.momentum_12_1_lookback_days, cfg.momentum_12_1_skip_days)
        rsi_s = rsi(close, cfg.rsi_period)
        atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
        avg_volume_s = bars["Volume"].rolling(20, min_periods=1).mean()
        avg_dollar_volume_s = avg_volume_s * close
        from_high_s = pct_from_high(close, 252)
        _, _, macd_hist_s = macd(close)
        bollinger_pct_b_s = bollinger_percent_b(close)

        if (
            pd.isna(sma_slow_s.iloc[-1])
            or pd.isna(roc_3m.iloc[-1])
            or pd.isna(roc_12_1.iloc[-1])
            or pd.isna(atr_s.iloc[-1])
        ):
            return None

        last_price = float(close.iloc[-1])
        last_rsi = float(rsi_s.iloc[-1])
        last_roc_3m = float(roc_3m.iloc[-1])
        last_roc_1m = float(roc_1m.iloc[-1]) if not pd.isna(roc_1m.iloc[-1]) else 0.0
        last_roc_12_1 = float(roc_12_1.iloc[-1])
        last_avg_vol = float(avg_volume_s.iloc[-1])
        last_avg_dollar_vol = float(avg_dollar_volume_s.iloc[-1])
        last_atr = float(atr_s.iloc[-1])
        last_from_high = float(from_high_s.iloc[-1]) if not pd.isna(from_high_s.iloc[-1]) else None
        last_macd_hist_pct = (
            float(macd_hist_s.iloc[-1]) / last_price * 100
            if not pd.isna(macd_hist_s.iloc[-1]) and last_price
            else None
        )
        last_bollinger_pct_b = float(bollinger_pct_b_s.iloc[-1]) if not pd.isna(bollinger_pct_b_s.iloc[-1]) else None
        last_sector_rel_strength = sector_relative_strength(symbol, last_roc_3m, cfg.lookback_days, force=force)

        trend_ok = bool(last_price > sma_fast_s.iloc[-1] > sma_slow_s.iloc[-1])
        # Filtro en volumen en dolares, no en cantidad de acciones: una accion de
        # bajo precio puede superar un umbral de acciones y seguir siendo poco
        # liquida en terminos de dinero realmente operado por dia.
        liquidity_ok = last_avg_dollar_vol >= cfg.min_avg_dollar_volume
        rsi_ok = cfg.rsi_min <= last_rsi <= cfg.rsi_max

        # last_from_high es <= 0 (0 = en el maximo, -8 = 8% por debajo). Si no
        # hay dato (None) el filtro no bloquea.
        near_high_ok = (
            not cfg.near_high_filter_enabled
            or last_from_high is None
            or last_from_high >= -cfg.max_pct_below_52w_high
        )

        # El llamado de earnings es una request de red aparte (y mas lenta que
        # las barras, que ya estan cacheadas localmente): solo vale la pena
        # pagarla si el simbolo ya paso el resto de filtros, que son gratis
        # (se calculan sobre datos que ya estan en memoria). Para el resto del
        # universo (la inmensa mayoria en un universo grande) asumimos
        # earnings_ok=True ya que no afecta passes_filters, que de todas
        # formas va a ser False por otro motivo.
        days_to_earnings: int | None = None
        earnings_ok = True
        if trend_ok and liquidity_ok and rsi_ok and regime_ok and near_high_ok:
            earnings_date = get_next_earnings_date(symbol, force=force)
            days_to_earnings = (earnings_date - datetime.now(timezone.utc).date()).days if earnings_date else None
            earnings_ok = days_to_earnings is None or not (0 <= days_to_earnings <= cfg.earnings_blackout_days)

        notes: list[str] = []
        if not liquidity_ok:
            notes.append("Volumen promedio por debajo del minimo de liquidez configurado.")
        if not rsi_ok:
            notes.append(f"RSI {last_rsi:.1f} fuera del rango configurado ({cfg.rsi_min}-{cfg.rsi_max}).")
        if not trend_ok:
            notes.append("No cumple el filtro de tendencia (precio > SMA rapida > SMA lenta).")
        if not regime_ok:
            notes.append("Filtro de regimen: el benchmark esta por debajo de su SMA de regimen.")
        if not earnings_ok:
            notes.append(f"Earnings estimados en {days_to_earnings} dia(s): dentro de la ventana de blackout.")
        if not near_high_ok:
            notes.append(
                f"A {abs(last_from_high):.1f}% del maximo de 52 semanas: mas lejos del "
                f"umbral de proximidad configurado ({cfg.max_pct_below_52w_high}%)."
            )

        relative_strength = last_roc_3m - benchmark_roc_3m if benchmark_roc_3m is not None else 0.0

        # Fuerza continua de la tendencia (no solo SI/NO): promedio de cuanto
        # el precio esta por encima de la SMA rapida y cuanto la SMA rapida
        # esta por encima de la SMA lenta, ambos en %. Negativo si el stack
        # esta invertido. Reemplaza el viejo +-10 fijo: una tendencia muy
        # fuerte ahora puntua mas que una apenas confirmada, en vez de
        # tratarlas igual con el mismo valor binario.
        sma_fast_last = sma_fast_s.iloc[-1]
        sma_slow_last = sma_slow_s.iloc[-1]
        if not pd.isna(sma_fast_last) and sma_fast_last and sma_slow_last:
            trend_strength_pct = (
                (last_price - sma_fast_last) / sma_fast_last * 100
                + (sma_fast_last - sma_slow_last) / sma_slow_last * 100
            ) / 2
        else:
            trend_strength_pct = 10.0 if trend_ok else -10.0

        components = {
            "relative_strength": relative_strength,
            "momentum_12_1": last_roc_12_1,
            "trend": trend_strength_pct,
            "rsi": last_rsi - 50,
            "macd": last_macd_hist_pct if last_macd_hist_pct is not None else 0.0,
            "bollinger": last_bollinger_pct_b if last_bollinger_pct_b is not None else 0.5,
            "sector_relative_strength": last_sector_rel_strength if last_sector_rel_strength is not None else 0.0,
        }
        score = (
            cfg.score_weight_relative_strength * components["relative_strength"]
            + cfg.score_weight_momentum_12_1 * components["momentum_12_1"]
            + cfg.score_weight_trend * components["trend"]
            + cfg.score_weight_rsi * components["rsi"]
            + cfg.score_weight_macd * components["macd"]
            + cfg.score_weight_bollinger * components["bollinger"]
            + cfg.score_weight_sector_relative_strength * components["sector_relative_strength"]
        )

        stop_loss_price = max(0.0, last_price - cfg.stop_loss_atr_multiplier * last_atr)
        stop_loss_pct = (last_price - stop_loss_price) / last_price * 100 if last_price else 0.0

        return SignalResult(
            symbol=symbol,
            as_of=datetime.now(timezone.utc),
            last_price=round(last_price, 2),
            score=round(score, 2),
            momentum_3m_pct=round(last_roc_3m, 2),
            momentum_1m_pct=round(last_roc_1m, 2),
            trend_ok=trend_ok,
            rsi=round(last_rsi, 1),
            avg_volume=round(last_avg_vol, 0),
            pct_from_52w_high=round(last_from_high, 2) if last_from_high is not None else None,
            suggested_stop_loss_price=round(stop_loss_price, 2),
            suggested_stop_loss_pct=round(stop_loss_pct, 2),
            passes_filters=trend_ok and liquidity_ok and rsi_ok and regime_ok and earnings_ok and near_high_ok,
            liquidity_ok=liquidity_ok,
            earnings_ok=earnings_ok,
            regime_ok=regime_ok,
            near_high_ok=near_high_ok,
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
            macd_histogram_pct=round(last_macd_hist_pct, 2) if last_macd_hist_pct is not None else None,
            bollinger_pct_b=round(last_bollinger_pct_b, 2) if last_bollinger_pct_b is not None else None,
            sector_relative_strength_pct=round(last_sector_rel_strength, 2) if last_sector_rel_strength is not None else None,
            score_components=components,
        )

    def scan(self, force: bool = False) -> list[SignalResult]:
        benchmark_roc, regime_ok = self._benchmark_context(force=force)
        results = []
        delay = self.config.scan_request_delay_seconds
        for i, symbol in enumerate(self.config.universe):
            # Pausa entre simbolos para no rafagar la API gratuita de Yahoo
            # Finance con un universo grande (ver scan_request_delay_seconds),
            # salvo que el dato ya este cacheado (ej. otra estrategia ya
            # escaneo este simbolo en este mismo ciclo): ahi no hay fetch real
            # que espaciar.
            if i > 0 and delay > 0 and (force or not is_bars_cached(symbol, self.config.lookback_days)):
                time.sleep(delay)
            result = self.evaluate_symbol(symbol, benchmark_roc, regime_ok=regime_ok, force=force)
            if result is not None:
                results.append(result)
        if self.config.universe and not results:
            raise MarketDataError(
                "No se pudo obtener datos de mercado para ningun simbolo del universo "
                "configurado. Puede ser un problema de conectividad o el limite de la "
                "API gratuita de datos."
            )
        apply_cross_sectional_normalization(results, {
            "relative_strength": self.config.score_weight_relative_strength,
            "momentum_12_1": self.config.score_weight_momentum_12_1,
            "trend": self.config.score_weight_trend,
            "rsi": self.config.score_weight_rsi,
            "macd": self.config.score_weight_macd,
            "bollinger": self.config.score_weight_bollinger,
            "sector_relative_strength": self.config.score_weight_sector_relative_strength,
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
