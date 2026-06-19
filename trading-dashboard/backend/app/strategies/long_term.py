from __future__ import annotations

import time
from datetime import datetime, timezone

from ..market_data import MarketDataError, get_daily_bars, get_fundamentals
from ..models import SignalResult
from ..screener_config import ScreenerConfig
from ..sectors import get_sector
from .common import avg_dollar_volume, avg_volume, context_technicals, earnings_blackout_ok


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
        pe = fundamentals.get("trailing_pe") or fundamentals.get("forward_pe")
        earnings_growth = fundamentals.get("earnings_growth")
        revenue_growth = fundamentals.get("revenue_growth")
        roe = fundamentals.get("return_on_equity")
        debt_to_equity = fundamentals.get("debt_to_equity")
        profit_margins = fundamentals.get("profit_margins")

        last_price = ctx["last_price"]
        last_avg_dollar_vol = avg_dollar_volume(bars)
        last_avg_vol = avg_volume(bars)

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

        value_score = (lt.max_pe_ratio - pe) if pe is not None else 0.0
        growth_pct = (earnings_growth or 0.0) * 100
        roe_pct = (roe or 0.0) * 100
        margin_pct = (profit_margins or 0.0) * 100

        score = (
            lt.score_weight_value * value_score
            + lt.score_weight_growth * growth_pct
            + lt.score_weight_quality * roe_pct
            + lt.score_weight_margin * margin_pct
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
            notes=notes,
            strategy_id=self.id,
            sector=get_sector(symbol),
            pe_ratio=round(pe, 2) if pe is not None else None,
        )

    def scan(self, force: bool = False) -> list[SignalResult]:
        results = []
        delay = self.config.scan_request_delay_seconds
        for i, symbol in enumerate(self.config.universe):
            if i > 0 and delay > 0:
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
        results.sort(key=lambda r: r.score, reverse=True)
        return results
