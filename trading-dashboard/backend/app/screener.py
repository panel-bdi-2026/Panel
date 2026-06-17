from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .indicators import atr, pct_from_high, rate_of_change, rsi, sma
from .market_data import MarketDataError, get_daily_bars
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

    def _benchmark_roc(self) -> float | None:
        try:
            bars = get_daily_bars(self.config.benchmark_symbol, self.config.lookback_days)
        except MarketDataError:
            return None
        roc = rate_of_change(bars["Close"], self.config.momentum_lookback_days)
        value = roc.iloc[-1] if len(roc) else None
        return float(value) if value is not None and not pd.isna(value) else None

    def evaluate_symbol(self, symbol: str, benchmark_roc_3m: float | None) -> SignalResult | None:
        cfg = self.config
        try:
            bars = get_daily_bars(symbol, cfg.lookback_days)
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
        from_high_s = pct_from_high(close, 252)

        if pd.isna(sma_slow_s.iloc[-1]) or pd.isna(roc_3m.iloc[-1]) or pd.isna(atr_s.iloc[-1]):
            return None

        last_price = float(close.iloc[-1])
        last_rsi = float(rsi_s.iloc[-1])
        last_roc_3m = float(roc_3m.iloc[-1])
        last_roc_1m = float(roc_1m.iloc[-1]) if not pd.isna(roc_1m.iloc[-1]) else 0.0
        last_avg_vol = float(avg_volume_s.iloc[-1])
        last_atr = float(atr_s.iloc[-1])
        last_from_high = float(from_high_s.iloc[-1]) if not pd.isna(from_high_s.iloc[-1]) else None

        trend_ok = bool(last_price > sma_fast_s.iloc[-1] > sma_slow_s.iloc[-1])
        liquidity_ok = last_avg_vol >= cfg.min_avg_volume
        rsi_ok = cfg.rsi_min <= last_rsi <= cfg.rsi_max

        notes: list[str] = []
        if not liquidity_ok:
            notes.append("Volumen promedio por debajo del minimo de liquidez configurado.")
        if not rsi_ok:
            notes.append(f"RSI {last_rsi:.1f} fuera del rango configurado ({cfg.rsi_min}-{cfg.rsi_max}).")
        if not trend_ok:
            notes.append("No cumple el filtro de tendencia (precio > SMA rapida > SMA lenta).")

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
            passes_filters=trend_ok and liquidity_ok and rsi_ok,
            notes=notes,
        )

    def scan(self) -> list[SignalResult]:
        benchmark_roc = self._benchmark_roc()
        results = []
        for symbol in self.config.universe:
            result = self.evaluate_symbol(symbol, benchmark_roc)
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
