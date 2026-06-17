from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .indicators import atr, pct_from_high, rate_of_change, rsi, sma
from .market_data import MarketDataError, get_daily_bars, get_next_earnings_date
from .models import SignalResult
from .screener_config import ScreenerConfig


class MomentumScreener:
    """Escanea un universo de acciones y rankea oportunidades momentum/tecnicas.

    Esto NO ejecuta ordenes ni las aprueba: produce candidatos para que tu
    decidas si convertirlos en una orden, la cual de todas formas tiene que
    pasar por el RulesEngine (whitelist, stop-loss, limites, etc.) como
    cualquier otra. No es una recomendacion de inversion.
    """

    def __init__(self, config: ScreenerConfig):
        self.config = config

    def reload(self, config: ScreenerConfig) -> None:
        self.config = config

    def _benchmark_context(self, force: bool = False) -> tuple[float | None, bool]:
        """Retorna (roc_3m, regime_ok) del benchmark.

        roc_3m es el momentum usado para la fuerza relativa de cada simbolo.
        regime_ok indica si el benchmark esta por encima de su propia SMA de
        regimen (mercado en tendencia alcista de fondo); si no hay suficiente
        historia para calcularla, se asume regime_ok=True en vez de bloquear
        todo el scan por falta de dato.
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
            regime_sma = sma(close, self.config.regime_sma_period)
            last_sma = regime_sma.iloc[-1]
            if not pd.isna(last_sma):
                regime_ok = bool(close.iloc[-1] > last_sma)
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
        if len(bars) < cfg.sma_slow + cfg.momentum_lookback_days // 2:
            return None

        close = bars["Close"]
        sma_fast_s = sma(close, cfg.sma_fast)
        sma_slow_s = sma(close, cfg.sma_slow)
        roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
        roc_1m = rate_of_change(close, cfg.momentum_short_days)
        rsi_s = rsi(close, cfg.rsi_period)
        atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
        avg_volume_s = bars["Volume"].rolling(20, min_periods=1).mean()
        avg_dollar_volume_s = avg_volume_s * close
        from_high_s = pct_from_high(close, 252)

        if pd.isna(sma_slow_s.iloc[-1]) or pd.isna(roc_3m.iloc[-1]) or pd.isna(atr_s.iloc[-1]):
            return None

        last_price = float(close.iloc[-1])
        last_rsi = float(rsi_s.iloc[-1])
        last_roc_3m = float(roc_3m.iloc[-1])
        last_roc_1m = float(roc_1m.iloc[-1]) if not pd.isna(roc_1m.iloc[-1]) else 0.0
        last_avg_vol = float(avg_volume_s.iloc[-1])
        last_avg_dollar_vol = float(avg_dollar_volume_s.iloc[-1])
        last_atr = float(atr_s.iloc[-1])
        last_from_high = float(from_high_s.iloc[-1]) if not pd.isna(from_high_s.iloc[-1]) else None

        trend_ok = bool(last_price > sma_fast_s.iloc[-1] > sma_slow_s.iloc[-1])
        # Filtro en volumen en dolares, no en cantidad de acciones: una accion de
        # bajo precio puede superar un umbral de acciones y seguir siendo poco
        # liquida en terminos de dinero realmente operado por dia.
        liquidity_ok = last_avg_dollar_vol >= cfg.min_avg_dollar_volume
        rsi_ok = cfg.rsi_min <= last_rsi <= cfg.rsi_max

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

        relative_strength = last_roc_3m - benchmark_roc_3m if benchmark_roc_3m is not None else 0.0

        score = (
            0.35 * relative_strength
            + 0.25 * last_roc_3m
            + 0.15 * last_roc_1m
            + 0.15 * (10 if trend_ok else -10)
            + 0.10 * (last_rsi - 50)
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
            passes_filters=trend_ok and liquidity_ok and rsi_ok and regime_ok and earnings_ok,
            notes=notes,
        )

    def scan(self, force: bool = False) -> list[SignalResult]:
        benchmark_roc, regime_ok = self._benchmark_context(force=force)
        results = []
        for symbol in self.config.universe:
            result = self.evaluate_symbol(symbol, benchmark_roc, regime_ok=regime_ok, force=force)
            if result is not None:
                results.append(result)
        if self.config.universe and not results:
            raise MarketDataError(
                "No se pudo obtener datos de mercado para ningun simbolo del universo "
                "configurado. Puede ser un problema de conectividad o el limite de la "
                "API gratuita de datos."
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results
