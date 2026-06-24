from __future__ import annotations

import statistics
import time
from collections import Counter

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
from .market_data import MarketDataError, get_daily_bars
from .models import BacktestSummary, BacktestTrade, EquityCurvePoint, WalkForwardFold, WalkForwardResult
from .screener_config import GROWTH_TICKERS, ScreenerConfig
from .sector_strength import sector_relative_strength_series
from .sectors import get_sector

# Misma ventana fija que _CONTEXT_MOMENTUM_3M_DAYS en strategies/common.py
# (el momentum "de contexto" que usa Oportunista para su propio
# sector_relative_strength) y _MOMENTUM_3M_DAYS en sector_strength.py: se
# duplica el valor en vez de importarlo porque este modulo no puede depender
# de strategies/ sin arriesgar el mismo ciclo de import ya documentado en
# sector_strength.py (strategies/common.py -> strategies/__init__.py ->
# screener.py).
_OPPORTUNISTIC_CONTEXT_MOMENTUM_DAYS = 63


class BacktestError(RuntimeError):
    pass


def _backtest_universe(cfg: ScreenerConfig) -> list[str]:
    """cfg.universe sin los simbolos de GROWTH_TICKERS (ver el comentario de
    look-ahead de inclusion en screener_config.py): esos tickers se eligieron
    buscando hoy nombres que ya tuvieron una corrida fuerte reciente, asi que
    dejarlos en el universo de un backtest historico infla el resultado con
    ganadores que solo estan ahi porque ya se sabe que ganaron. Se filtra por
    membership (no por orden de la lista original) para que tambien excluya
    estos simbolos si el usuario los agrego a mano a un universe custom."""
    return [s for s in cfg.universe if s not in GROWTH_TICKERS]


def _band_score_series(value: pd.Series, center: float, half_range: float) -> pd.Series:
    """Vectorizado, misma formula que band_score en strategies/common.py (no
    se importa de ahi por el mismo motivo que _OPPORTUNISTIC_CONTEXT_MOMENTUM_DAYS:
    evitar el ciclo de import documentado en sector_strength.py)."""
    half_range = max(1e-9, half_range)
    return 100.0 * (1.0 - (value - center).abs().div(half_range).clip(upper=1.0))


def _effective_slippage_pct(
    base_slippage_pct: float, dollar_volume: float, threshold: float, multiplier: float
) -> float:
    """slippage_pct base, multiplicado si dollar_volume (volumen promedio en
    USD del dia de ESTE fill puntual) esta por debajo de threshold. threshold
    <= 0 deshabilita (siempre devuelve el base). Un dollar_volume NaN (sin
    suficiente historia todavia) tampoco aplica el multiplicador, mismo
    criterio de "sin dato no bloquea/no penaliza" que el resto de los gates
    del backtest."""
    if threshold <= 0 or pd.isna(dollar_volume) or dollar_volume >= threshold:
        return base_slippage_pct
    return base_slippage_pct * multiplier


def _trailing_stop_price(current_stop: float, price_today: float, atr_today: float, stop_loss_atr_multiplier: float) -> float:
    """Replica _check_fund_trailing_stop (main.py): sube (nunca baja) el
    stop-loss a candidate_stop = precio_de_hoy - ATR_de_hoy * multiplo, la
    misma distancia en ATR que el stop inicial. Se llama con el cierre del
    dia DESPUES de chequear el stop existente contra el minimo intradiario de
    ese mismo dia (ver _simulate_symbol): el stop nuevo recien protege a
    partir del dia siguiente, nunca retroactivamente contra el minimo de
    hoy, igual que en vivo el trailing solo mueve el stop ya colocado en el
    broker para adelante, nunca reabre la vela que ya paso."""
    candidate_stop = price_today - stop_loss_atr_multiplier * atr_today
    return max(current_stop, candidate_stop)


def _trade_daily_marks(
    bars: pd.DataFrame, entry_idx: int, exit_idx: int, entry_fill: float, ret_pct: float
) -> dict:
    """Factor de retorno acumulado dia por dia de una operacion individual,
    usando el cierre real de cada dia que estuvo abierta (no una interpolacion
    lineal entre 0% y el retorno final). El ultimo dia se corrige para que el
    factor coincida exactamente con el retorno final ya neto de
    comision/slippage (ret_pct) en vez del cierre crudo, que no refleja esos
    costos ni un eventual fill de stop-loss en el minimo intradiario."""
    close = bars["Close"]
    marks = {bars.index[i]: float(close.iloc[i]) / entry_fill for i in range(entry_idx, exit_idx + 1)}
    marks[bars.index[exit_idx]] = 1 + ret_pct / 100
    return marks


def _cross_sectional_score_panel(raw_components: dict[str, pd.DataFrame], weights: dict[str, float]) -> pd.DataFrame:
    """Replica, dia por dia, la misma logica de apply_cross_sectional_normalization
    (ver scoring.py) que usa el scan en vivo: en vez de un percentil por
    simbolo en un solo instante, calcula un percentil por simbolo en CADA
    fecha de `raw_components`, contra el resto de simbolos que tengan un dato
    valido ese mismo dia -- no contra el universo de HOY, que no es el
    universo que realmente estaba "vivo" en cada momento historico.

    raw_components: {nombre_de_score_component: DataFrame fecha x simbolo},
    ya con los mismos defaults para dato faltante que aplica la version en
    vivo (ver _momentum_raw_components / _opportunistic_raw_components). Un
    NaN que sobrevive hasta aca significa "todavia no hay suficiente historia
    para este simbolo en esta fecha" (excluye a ese simbolo del ranking
    cross-sectional ese dia), no "dato faltante con default" -- igual que
    evaluate_symbol no devuelve señal en absoluto sin esa historia minima.

    Devuelve un DataFrame fecha x simbolo con el score final (suma pesada de
    percentiles 0-100 por componente).
    """
    score_df = None
    for key, weight in weights.items():
        raw_df = raw_components[key]
        ranks = raw_df.rank(axis=1, method="average")
        n_valid = raw_df.notna().sum(axis=1)
        denom = (n_valid - 1).where(n_valid > 1)  # NaN si hay <2 simbolos validos ese dia: percentil indefinido
        pct = ranks.sub(1).div(denom, axis=0) * 100
        contribution = weight * pct
        score_df = contribution if score_df is None else score_df + contribution
    return score_df


def _momentum_raw_components(
    bars_by_symbol: dict[str, pd.DataFrame], cfg: ScreenerConfig, bench_bars: pd.DataFrame, history_days: int
) -> dict[str, pd.DataFrame]:
    """Componentes crudos (sin normalizar, historia completa) del score de
    Momentum para cada simbolo, replicando uno a uno los score_components de
    evaluate_symbol (ver screener.py) pero como Series dia por dia en vez de
    un solo iloc[-1]. Insumo de _cross_sectional_score_panel."""
    benchmark_roc = rate_of_change(bench_bars["Close"], cfg.momentum_lookback_days)

    keys = (
        "relative_strength", "momentum_12_1", "trend",
        "rsi", "macd", "bollinger", "sector_relative_strength",
    )
    per_symbol: dict[str, dict[str, pd.Series]] = {key: {} for key in keys}

    for symbol, bars in bars_by_symbol.items():
        close = bars["Close"]
        sma_fast_s = sma(close, cfg.sma_fast)
        sma_slow_s = sma(close, cfg.sma_slow)
        roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
        roc_12_1 = momentum_12_1(close, cfg.momentum_12_1_lookback_days, cfg.momentum_12_1_skip_days)
        rsi_s = rsi(close, cfg.rsi_period)
        _, _, macd_hist_s = macd(close)
        bollinger_s = bollinger_percent_b(close)
        macd_pct_s = (macd_hist_s / close * 100).where(close != 0)
        aligned_bench_roc = benchmark_roc.reindex(close.index, method="ffill")

        # Misma puerta que evaluate_symbol (mas atr, que no es parte del
        # score: se chequea aparte en _simulate_symbol antes de usarlo para
        # el stop-loss). Sin esto, un simbolo con poca historia entraria al
        # ranking cross-sectional con momentum/tendencia indefinidos.
        valid = ~(sma_slow_s.isna() | roc_3m.isna() | roc_12_1.isna())

        # Fuerza continua de la tendencia, mismo calculo que screener.py:
        # promedio de cuanto el precio esta por encima de la SMA rapida y
        # cuanto la SMA rapida esta por encima de la SMA lenta, ambos en %.
        # Si falta alguna SMA (NaN o cero) se cae al viejo +-10 fijo, igual
        # que el fallback en evaluate_symbol.
        trend_ok = (close > sma_fast_s) & (sma_fast_s > sma_slow_s)
        trend_fallback = pd.Series(-10.0, index=close.index)
        trend_fallback[trend_ok] = 10.0
        trend_strength_pct = (
            (close - sma_fast_s) / sma_fast_s.where(sma_fast_s != 0) * 100
            + (sma_fast_s - sma_slow_s) / sma_slow_s.where(sma_slow_s != 0) * 100
        ) / 2
        trend_component = trend_strength_pct.fillna(trend_fallback)

        sector_rel = sector_relative_strength_series(symbol, roc_3m, history_days)
        sector_component = sector_rel.fillna(0.0) if sector_rel is not None else pd.Series(0.0, index=close.index)

        per_symbol["relative_strength"][symbol] = (roc_3m - aligned_bench_roc).where(valid)
        per_symbol["momentum_12_1"][symbol] = roc_12_1.where(valid)
        per_symbol["trend"][symbol] = trend_component.where(valid)
        per_symbol["rsi"][symbol] = (rsi_s - 50).where(valid)
        per_symbol["macd"][symbol] = macd_pct_s.fillna(0.0).where(valid)
        per_symbol["bollinger"][symbol] = bollinger_s.fillna(0.5).where(valid)
        per_symbol["sector_relative_strength"][symbol] = sector_component.where(valid)

    return {key: pd.concat(series_dict, axis=1) for key, series_dict in per_symbol.items()}


def _simulate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    cfg: ScreenerConfig,
    score_series: pd.Series,
    benchmark_regime_ok: pd.Series,
    marks_by_trade_id: dict | None = None,
) -> list[BacktestTrade]:
    """Simula la entrada/salida de Momentum sobre historia, score-driven (ver
    _cross_sectional_score_panel) en vez del antiguo AND booleano de filtros
    tecnicos: entra cuando score_series supera cfg.backtest_score_entry_threshold
    Y, ademas, el regimen de mercado, la proximidad al maximo de 52 semanas y
    la liquidez minima lo permiten -- esos tres NO son parte del score (son
    gates booleanos puros igual que en el scan en vivo, ver screener.py), asi
    que se siguen chequeando aparte. Sale por stop-loss (basado en el ATR del
    dia de la senal, igual que la sugerencia en vivo, y si cfg.trailing_stop_enabled
    el stop sube dia a dia con el ATR de cada dia en posicion -- nunca baja --
    igual que _check_fund_trailing_stop en main.py), por tiempo maximo en la
    posicion, o por ruptura de tendencia (el cierre cae por debajo de la SMA
    rapida) -- igual que _check_fund_exit en main.py, que es la UNICA salida
    que el motor de auto-trading en vivo ejecuta hoy. No se modela una salida
    por score porque en vivo no existe: haria falta un rescan cross-sectional
    de todo el universo en cada chequeo de salida (caro, y ademas el monitor
    de salida corre independiente del scan periodico, ver
    _run_auto_exit_monitor_cycle en main.py), asi que modelarla aca solo
    inflaria el backtest con una mecanica que la cuenta en vivo nunca
    ejecuta. Una sola posicion por simbolo a la vez.

    Como el score recien se conoce al cierre del dia que lo confirma, el fill
    de entrada se simula a la apertura del dia siguiente (no al cierre del
    dia de la senal, que seria mirar al futuro). El stop-loss se chequea
    contra el minimo intradiario (no el cierre): si el precio perfora el stop
    durante el dia, en la realidad se sale ahi (o peor, si abre con un gap por
    debajo del stop), no se espera al cierre. Tambien se restan comision y
    slippage estimados, para no inflar los retornos respecto a la operatoria
    real.
    """
    close = bars["Close"]
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
    from_high_s = pct_from_high(close, 252)
    sma_fast_s = sma(close, cfg.sma_fast)
    # Mismo criterio de liquidez que el scan en vivo (ver screener.py): volumen
    # promedio en DOLARES de los ultimos 20 dias, no en cantidad de acciones.
    dollar_volume_s = bars["Volume"].rolling(20, min_periods=1).mean() * close

    notional_per_trade = cfg.backtest_assumed_capital_usd / cfg.top_n if cfg.top_n else 0.0

    trades: list[BacktestTrade] = []
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_price = 0.0
    entry_atr = 0.0
    pending_entry_atr = None  # ATR del dia en que se detecto la senal (ver mas abajo)

    # Si el filtro de cercania al maximo de 52 semanas esta activo, incluye
    # 252 (igual que from_high_s, ver pct_from_high): sin esto, el primer
    # tramo del backtest podia evaluar near_high_ok con fh todavia NaN (no
    # hay 252 dias previos), y un NaN se trata como "sin dato, no bloquea" --
    # dejaba pasar entradas en ese tramo inicial sin que el filtro las
    # hubiera evaluado de verdad. Con el filtro apagado (near_high_ok siempre
    # True) no hace falta esperar esos 252 dias: nada en la entrada depende
    # de fh.
    near_high_min_days = 252 if cfg.near_high_filter_enabled else 0
    start_idx = (
        max(
            cfg.sma_slow,
            cfg.momentum_lookback_days,
            cfg.momentum_12_1_lookback_days,
            cfg.atr_period,
            near_high_min_days,
        )
        + 1
    )
    for i in range(start_idx, len(bars)):
        date = bars.index[i]
        price = float(close.iloc[i])
        low_price = float(bars["Low"].iloc[i])
        open_price = float(bars["Open"].iloc[i])

        if pending_entry_atr is not None:
            # La senal se detecto con el cierre del dia anterior: el score
            # recien se conoce una vez cerrado ese dia, asi que en la
            # realidad la orden se coloca al dia siguiente. Entrar al cierre
            # del mismo dia de la senal seria mirar al futuro; el fill
            # realista es la apertura de este dia.
            in_position = True
            entry_price = open_price
            entry_idx = i
            entry_date = date
            entry_atr = pending_entry_atr
            stop_price = entry_price - cfg.stop_loss_atr_multiplier * pending_entry_atr
            pending_entry_atr = None
            continue

        if in_position:
            held_days = i - entry_idx
            hit_stop = low_price <= stop_price
            timed_out = held_days >= cfg.max_holding_days
            sma_fast_today = sma_fast_s.iloc[i]
            trend_broke = not pd.isna(sma_fast_today) and price < sma_fast_today
            if hit_stop or timed_out or trend_broke:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                # Si hubo gap por debajo del stop, el fill realista es el open
                # (peor que el stop); si no, se asume fill al precio del stop.
                raw_exit_price = min(open_price, stop_price) if hit_stop else price

                entry_slippage_pct = _effective_slippage_pct(
                    cfg.slippage_pct,
                    dollar_volume_s.iloc[entry_idx],
                    cfg.low_liquidity_dollar_volume_threshold,
                    cfg.low_liquidity_slippage_multiplier,
                )
                exit_slippage_pct = _effective_slippage_pct(
                    cfg.slippage_pct,
                    dollar_volume_s.iloc[i],
                    cfg.low_liquidity_dollar_volume_threshold,
                    cfg.low_liquidity_slippage_multiplier,
                )
                entry_fill = entry_price * (1 + entry_slippage_pct / 100)
                exit_fill = raw_exit_price * (1 - exit_slippage_pct / 100)
                commission_pct = (
                    (2 * cfg.commission_per_trade_usd / notional_per_trade) * 100
                    if notional_per_trade
                    else 0.0
                )
                ret_pct = (exit_fill - entry_fill) / entry_fill * 100 - commission_pct

                trade = BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=date,
                    entry_price=round(entry_price, 2),
                    exit_price=round(raw_exit_price, 2),
                    return_pct=round(ret_pct, 2),
                    exit_reason=exit_reason,
                    entry_atr_pct=round(entry_atr / entry_price * 100, 4) if entry_price else None,
                )
                trades.append(trade)
                if marks_by_trade_id is not None:
                    marks_by_trade_id[id(trade)] = _trade_daily_marks(bars, entry_idx, i, entry_fill, ret_pct)
                in_position = False
            elif cfg.trailing_stop_enabled and not pd.isna(atr_s.iloc[i]):
                stop_price = _trailing_stop_price(stop_price, price, float(atr_s.iloc[i]), cfg.stop_loss_atr_multiplier)
            continue

        if pd.isna(atr_s.iloc[i]):
            continue

        score_today = score_series.get(date)
        if score_today is None or pd.isna(score_today) or score_today < cfg.backtest_score_entry_threshold:
            continue

        regime_ok = bool(benchmark_regime_ok.iloc[i]) if i < len(benchmark_regime_ok) else True
        fh = from_high_s.iloc[i]
        near_high_ok = (
            not cfg.near_high_filter_enabled
            or pd.isna(fh)
            or fh >= -cfg.max_pct_below_52w_high
        )
        liquidity_ok = dollar_volume_s.iloc[i] >= cfg.min_avg_dollar_volume

        if regime_ok and near_high_ok and liquidity_ok:
            pending_entry_atr = atr_s.iloc[i]

    return trades


def _opportunistic_raw_components(
    bars_by_symbol: dict[str, pd.DataFrame], cfg: ScreenerConfig, history_days: int
) -> dict[str, pd.DataFrame]:
    """Componentes crudos (sin normalizar, historia completa) del score de
    Oportunista para cada simbolo, replicando uno a uno los score_components
    de evaluate_symbol (ver strategies/opportunistic.py) pero como Series dia
    por dia en vez de un solo iloc[-1]. Insumo de _cross_sectional_score_panel.

    El componente sector_relative_strength usa la ventana fija de 63 dias
    (_OPPORTUNISTIC_CONTEXT_MOMENTUM_DAYS, igual que ctx["momentum_3m_pct"] en
    context_technicals), NO opp.momentum_lookback_days: esa ventana corta es
    la que usa el componente "momentum" (señal de giro de corto plazo), una
    ventana distinta con un proposito distinto en la misma estrategia."""
    opp = cfg.opportunistic
    keys = ("momentum", "volatility", "rsi_recovery", "room_to_grow", "macd_turn", "sector_relative_strength")
    per_symbol: dict[str, dict[str, pd.Series]] = {key: {} for key in keys}

    # Mismos centro/medio-rango que evaluate_symbol (ver opportunistic.py):
    # banded, no monotonico, calculados una sola vez fuera del loop por
    # simbolo ya que no dependen del simbolo.
    volatility_mid = (opp.min_volatility_pct + opp.max_volatility_pct) / 2
    volatility_half_range = max(1.0, (opp.max_volatility_pct - opp.min_volatility_pct) / 2)
    room_to_grow_mid = (opp.min_pct_below_52w_high + opp.max_pct_below_52w_high) / 2
    room_to_grow_half_range = max(1.0, (opp.max_pct_below_52w_high - opp.min_pct_below_52w_high) / 2)

    for symbol, bars in bars_by_symbol.items():
        close = bars["Close"]
        atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
        roc_short = rate_of_change(close, opp.momentum_lookback_days)
        rsi_s = rsi(close, opp.rsi_period)
        from_high_s = pct_from_high(close, 252)
        _, _, macd_hist_s = macd(close)
        macd_pct_s = (macd_hist_s / close * 100).where(close != 0)
        roc_3m_context = rate_of_change(close, _OPPORTUNISTIC_CONTEXT_MOMENTUM_DAYS)

        # Misma puerta que "ctx is not None" (atr/momentum de contexto de 63
        # dias) mas roc_short no-NaN en evaluate_symbol; RSI nunca es NaN (ver
        # indicators.py).
        valid = ~(atr_s.isna() | roc_3m_context.isna() | roc_short.isna())

        volatility_pct_s = (atr_s / close * 100).where(close != 0)
        volatility_component = _band_score_series(volatility_pct_s, volatility_mid, volatility_half_range)
        room_to_grow_raw = from_high_s.abs().fillna(0.0)
        room_to_grow_component = _band_score_series(room_to_grow_raw, room_to_grow_mid, room_to_grow_half_range)

        sector_rel = sector_relative_strength_series(symbol, roc_3m_context, history_days)
        sector_component = sector_rel.fillna(0.0) if sector_rel is not None else pd.Series(0.0, index=close.index)

        per_symbol["momentum"][symbol] = roc_short.where(valid)
        per_symbol["volatility"][symbol] = volatility_component.where(valid)
        per_symbol["rsi_recovery"][symbol] = (rsi_s - opp.rsi_min).where(valid)
        per_symbol["room_to_grow"][symbol] = room_to_grow_component.where(valid)
        per_symbol["macd_turn"][symbol] = macd_pct_s.fillna(0.0).where(valid)
        per_symbol["sector_relative_strength"][symbol] = sector_component.where(valid)

    return {key: pd.concat(series_dict, axis=1) for key, series_dict in per_symbol.items()}


def _simulate_symbol_opportunistic(
    symbol: str,
    bars: pd.DataFrame,
    cfg: ScreenerConfig,
    score_series: pd.Series,
    benchmark_regime_ok: pd.Series,
    marks_by_trade_id: dict | None = None,
) -> list[BacktestTrade]:
    """Misma logica score-driven que _simulate_symbol (ver ese docstring para
    el detalle de fills/stop-loss/comision/slippage/salida), aplicada a
    Oportunista: a diferencia de Momentum, las 4 condiciones booleanas
    originales (momentum corto positivo, RSI en zona de recuperacion,
    volatilidad minima, espacio de crecimiento) ya son, cada una, un
    componente del score (ver _opportunistic_raw_components). Los gates
    booleanos aparte del umbral de score son la liquidez minima (igual que
    Momentum, que tampoco la incluye en su score) y el regimen del benchmark:
    comprar caidas (la esencia de Oportunista) en un mercado en regimen
    bajista de fondo es comprar cuchillos cayendo, asi que este filtro ahora
    se aplica tambien aqui (antes solo bloqueaba nuevas entradas de
    Momentum).

    Entra cuando score_series supera opp.backtest_score_entry_threshold y la
    liquidez minima lo permite; sale por stop-loss, tiempo maximo en la
    posicion, o ruptura de tendencia (el cierre cae por debajo de
    cfg.sma_fast, la MISMA SMA global que usa _check_fund_exit en main.py
    para TODAS las estrategias, Oportunista incluida: el monitor de salida en
    vivo no es especifico por estrategia)."""
    opp = cfg.opportunistic
    close = bars["Close"]
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
    sma_fast_s = sma(close, cfg.sma_fast)
    dollar_volume_s = bars["Volume"].rolling(20, min_periods=1).mean() * close

    notional_per_trade = cfg.backtest_assumed_capital_usd / cfg.top_n if cfg.top_n else 0.0

    trades: list[BacktestTrade] = []
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_price = 0.0
    entry_atr = 0.0
    pending_entry_atr = None

    start_idx = max(opp.momentum_lookback_days, cfg.atr_period, 252) + 1
    for i in range(start_idx, len(bars)):
        date = bars.index[i]
        price = float(close.iloc[i])
        low_price = float(bars["Low"].iloc[i])
        open_price = float(bars["Open"].iloc[i])

        if pending_entry_atr is not None:
            in_position = True
            entry_price = open_price
            entry_idx = i
            entry_date = date
            entry_atr = pending_entry_atr
            stop_price = entry_price - opp.stop_loss_atr_multiplier * pending_entry_atr
            pending_entry_atr = None
            continue

        if in_position:
            held_days = i - entry_idx
            hit_stop = low_price <= stop_price
            timed_out = held_days >= opp.max_holding_days
            sma_fast_today = sma_fast_s.iloc[i]
            trend_broke = not pd.isna(sma_fast_today) and price < sma_fast_today
            if hit_stop or timed_out or trend_broke:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                raw_exit_price = min(open_price, stop_price) if hit_stop else price

                entry_slippage_pct = _effective_slippage_pct(
                    cfg.slippage_pct,
                    dollar_volume_s.iloc[entry_idx],
                    cfg.low_liquidity_dollar_volume_threshold,
                    cfg.low_liquidity_slippage_multiplier,
                )
                exit_slippage_pct = _effective_slippage_pct(
                    cfg.slippage_pct,
                    dollar_volume_s.iloc[i],
                    cfg.low_liquidity_dollar_volume_threshold,
                    cfg.low_liquidity_slippage_multiplier,
                )
                entry_fill = entry_price * (1 + entry_slippage_pct / 100)
                exit_fill = raw_exit_price * (1 - exit_slippage_pct / 100)
                commission_pct = (
                    (2 * cfg.commission_per_trade_usd / notional_per_trade) * 100
                    if notional_per_trade
                    else 0.0
                )
                ret_pct = (exit_fill - entry_fill) / entry_fill * 100 - commission_pct

                trade = BacktestTrade(
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=date,
                    entry_price=round(entry_price, 2),
                    exit_price=round(raw_exit_price, 2),
                    return_pct=round(ret_pct, 2),
                    exit_reason=exit_reason,
                    entry_atr_pct=round(entry_atr / entry_price * 100, 4) if entry_price else None,
                )
                trades.append(trade)
                if marks_by_trade_id is not None:
                    marks_by_trade_id[id(trade)] = _trade_daily_marks(bars, entry_idx, i, entry_fill, ret_pct)
                in_position = False
            elif cfg.trailing_stop_enabled and not pd.isna(atr_s.iloc[i]):
                stop_price = _trailing_stop_price(stop_price, price, float(atr_s.iloc[i]), cfg.stop_loss_atr_multiplier)
            continue

        if pd.isna(atr_s.iloc[i]):
            continue

        score_today = score_series.get(date)
        if score_today is None or pd.isna(score_today) or score_today < opp.backtest_score_entry_threshold:
            continue

        if dollar_volume_s.iloc[i] < cfg.min_avg_dollar_volume:
            continue

        regime_ok = bool(benchmark_regime_ok.iloc[i]) if i < len(benchmark_regime_ok) else True
        if not regime_ok:
            continue

        pending_entry_atr = atr_s.iloc[i]

    return trades


def _collect_opportunistic_trades(cfg: ScreenerConfig) -> tuple[list[BacktestTrade], dict, pd.DataFrame]:
    """Simula la estrategia Oportunista sobre todo el universo configurado y
    devuelve las operaciones resultantes (ya capadas a top_n posiciones
    concurrentes), el dict de marcas diarias por operacion (ver
    _trade_daily_marks) y la historia del benchmark. Separado de
    run_opportunistic_backtest para que run_opportunistic_backtest_walk_forward
    pueda reusar la misma simulacion sin volver a pedir datos de mercado.

    Dos pasadas, igual que _collect_momentum_trades: primero se piden las
    barras de TODO el universo (con la misma pausa anti-rate-limit que antes),
    despues se calcula el panel de score cross-sectional dia por dia sobre ese
    universo ya descargado (ver _cross_sectional_score_panel), y recien
    despues se simula cada simbolo -- no se puede saber el percentil de un
    simbolo en una fecha dada sin tener primero la historia de todos los
    demas para esa misma fecha."""
    history_days = int(cfg.backtest_years * 365)
    opp = cfg.opportunistic

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    if cfg.regime_filter_enabled:
        benchmark_regime_ok = market_regime_ok(
            bench_bars["Close"], cfg.regime_sma_period, cfg.regime_slope_lookback_days,
            cfg.regime_absolute_momentum_lookback_days,
        )
    else:
        benchmark_regime_ok = pd.Series(True, index=bench_bars.index)

    bars_by_symbol: dict[str, pd.DataFrame] = {}
    delay = cfg.scan_request_delay_seconds
    for i, symbol in enumerate(_backtest_universe(cfg)):
        if i > 0 and delay > 0:
            time.sleep(delay)
        try:
            bars = get_daily_bars(symbol, history_days)
        except MarketDataError:
            continue
        if len(bars) < opp.momentum_lookback_days + 252:
            continue
        bars_by_symbol[symbol] = bars

    # Mismo guard que _collect_momentum_trades: pd.concat sobre un dict vacio
    # en _opportunistic_raw_components rompe con ValueError en vez de la
    # BacktestError de "sin operaciones" que ya se usa mas abajo.
    if not bars_by_symbol:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    raw_components = _opportunistic_raw_components(bars_by_symbol, cfg, history_days)
    score_panel = _cross_sectional_score_panel(raw_components, {
        "momentum": opp.score_weight_momentum,
        "volatility": opp.score_weight_volatility,
        "rsi_recovery": opp.score_weight_rsi_recovery,
        "room_to_grow": opp.score_weight_room_to_grow,
        "macd_turn": opp.score_weight_macd_turn,
        "sector_relative_strength": opp.score_weight_sector_relative_strength,
    })

    all_trades: list[BacktestTrade] = []
    marks_by_trade_id: dict = {}
    for symbol, bars in bars_by_symbol.items():
        aligned_regime_ok = benchmark_regime_ok.reindex(bars.index, method="ffill").fillna(True)
        all_trades.extend(
            _simulate_symbol_opportunistic(symbol, bars, cfg, score_panel[symbol], aligned_regime_ok, marks_by_trade_id)
        )

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)
    all_trades = cap_concurrent_positions(all_trades, cfg.top_n, cfg.max_concurrent_positions_per_sector)

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    return all_trades, marks_by_trade_id, bench_bars


def run_opportunistic_backtest(cfg: ScreenerConfig) -> BacktestSummary:
    """Backtest de la estrategia Oportunista, en paralelo a run_backtest
    (Momentum). Misma estructura y mismas simplificaciones documentadas ahi
    (curva de equity diaria real con cupo top_n, sin supervivencia historica
    del universo); aca solo cambia la logica de entrada/salida por simbolo
    (ver _simulate_symbol_opportunistic)."""
    all_trades, marks_by_trade_id, bench_bars = _collect_opportunistic_trades(cfg)
    return _compute_summary_stats(
        all_trades, cfg.top_n, bench_bars, marks_by_trade_id,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
    )


def run_opportunistic_backtest_walk_forward(cfg: ScreenerConfig, n_folds: int = 3) -> WalkForwardResult:
    """Validacion out-of-sample de la estrategia Oportunista. Ver docstring de
    run_backtest_walk_forward (Momentum) para el alcance y las limitaciones:
    misma logica, solo cambia la simulacion subyacente."""
    all_trades, marks_by_trade_id, bench_bars = _collect_opportunistic_trades(cfg)
    return _build_walk_forward_result(
        all_trades, cfg.top_n, bench_bars, marks_by_trade_id, n_folds,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
    )


def cap_concurrent_positions(
    trades: list[BacktestTrade], top_n: int, max_per_sector: int | None = None
) -> list[BacktestTrade]:
    """Filtra una lista de operaciones a un maximo de top_n posiciones abiertas
    a la vez, recorriendolas por fecha de entrada y descartando las que no
    tendrian cupo libre (como en la operatoria real, donde no se pueden tener
    mas de top_n posiciones simultaneas). Sin top_n valido devuelve la lista
    intacta. Se asume que `trades` ya viene ordenada por fecha de entrada.

    Si max_per_sector esta activo (ver
    ScreenerConfig.max_concurrent_positions_per_sector), ademas descarta una
    operacion si su sector (get_sector) ya tiene esa cantidad de posiciones
    abiertas a la vez, aunque todavia haya cupo libre en top_n: sin esto, el
    cupo global no evita terminar con, por ejemplo, top_n posiciones todas del
    mismo sector (una sola apuesta concentrada, no una cartera diversificada)."""
    if not top_n or top_n <= 0:
        return list(trades)
    taken: list[BacktestTrade] = []
    open_exit_dates: list = []
    open_sector_exit_dates: dict[str, list] = {}
    for t in trades:
        open_exit_dates = [d for d in open_exit_dates if d > t.entry_date]
        if len(open_exit_dates) >= top_n:
            continue  # sin cupo libre: en la realidad no se podria abrir
        sector = get_sector(t.symbol) if max_per_sector else None
        if sector is not None and max_per_sector:
            sector_dates = [d for d in open_sector_exit_dates.get(sector, []) if d > t.entry_date]
            open_sector_exit_dates[sector] = sector_dates
            if len(sector_dates) >= max_per_sector:
                continue  # sin cupo libre en el sector: ya esta concentrado
        taken.append(t)
        open_exit_dates.append(t.exit_date)
        if sector is not None and max_per_sector:
            open_sector_exit_dates[sector].append(t.exit_date)
    return taken


def _trade_weights(all_trades: list[BacktestTrade], top_n: int, vol_weighting_enabled: bool) -> dict[int, float]:
    """Pondera cada operacion para _daily_equity_curve, keyeado por id(trade)
    (mismo criterio que marks_by_trade_id). Por defecto (vol_weighting_enabled
    apagado, o falta el ATR de entrada de alguna operacion) devuelve el mismo
    peso uniforme 1/top_n para todas -- igual comportamiento que antes de que
    existiera este campo, sin excepciones.

    Si esta activo, pondera cada posicion ~ 1/ATR_entrada (mismo criterio que
    el sizing por riesgo en vivo, ver rules.suggested_quantity: menos peso a
    lo mas volatil) en vez de equiponderar, normalizado para que el promedio
    de los multiplicadores sea 1 (mismo presupuesto total de capital que el
    esquema equiponderado, para que las dos curvas sigan siendo comparables).
    El multiplicador se acota a [0.5, 2.0] para que un simbolo con ATR casi
    nulo no termine dominando la cartera simulada -- mismo espiritu que los
    topes de tamaño de posicion del sizing en vivo
    (max_position_pct_of_equity/max_order_value_usd)."""
    base_weight = 1.0 / top_n if top_n else 0.0
    if not vol_weighting_enabled or not all_trades:
        return {id(t): base_weight for t in all_trades}
    inv_atrs = []
    for t in all_trades:
        if t.entry_atr_pct is None or t.entry_atr_pct <= 0:
            # Falta el dato en alguna operacion (ej. trade sintetico de test):
            # no se puede ponderar el conjunto de forma consistente, se cae a
            # equiponderar todo en vez de mezclar criterios distintos.
            return {id(t): base_weight for t in all_trades}
        inv_atrs.append(1.0 / t.entry_atr_pct)
    avg_inv_atr = sum(inv_atrs) / len(inv_atrs)
    weights: dict[int, float] = {}
    for t, inv_atr in zip(all_trades, inv_atrs):
        multiplier = inv_atr / avg_inv_atr if avg_inv_atr else 1.0
        multiplier = max(0.5, min(2.0, multiplier))
        weights[id(t)] = base_weight * multiplier
    return weights


def _daily_equity_curve(
    all_trades: list[BacktestTrade],
    top_n: int,
    bench_bars: pd.DataFrame,
    marks_by_trade_id: dict | None = None,
    invest_idle_cash_in_benchmark: bool = False,
    vol_weighting_enabled: bool = False,
) -> tuple[list[EquityCurvePoint], list[float], float, float]:
    """Construye la curva de equity dia por dia (no solo en cada evento de
    salida): cada operacion abierta aporta un retorno NO realizado, ponderado
    por 1/top_n igual que una operacion cerrada. Antes la curva solo se
    actualizaba al cerrar una operacion, como si una posicion abierta no
    existiera (no aportaba nada, ni ganancia ni perdida) hasta su cierre --
    eso ocultaba el drawdown combinado real cuando varias operaciones se
    solapan en el tiempo.

    El retorno no realizado dia por dia viene de marks_by_trade_id (ver
    _trade_daily_marks): el camino real de cierre de cada operacion, generado
    por quien construyo `all_trades` (_simulate_symbol /
    _simulate_symbol_opportunistic). Si una operacion no tiene marca para una
    fecha dada (trades sinteticos de test, o un desajuste de calendario entre
    el simbolo y el benchmark) se cae de vuelta a una interpolacion lineal
    entre 0% (en su entry_date) y su return_pct final (en su exit_date), que
    sigue siendo mas fiel que tratar la operacion como un salto instantaneo
    en su fecha de cierre.

    El calendario de fechas es el del benchmark (mismos dias de trading que
    las acciones individuales) recortado al rango de la primera entrada a la
    ultima salida, mas las fechas exactas de entrada/salida de cada operacion
    por si alguna no cae justo en una fecha del benchmark.

    Ademas de la exposicion (fraccion de capital con alguna posicion abierta)
    se usa esa misma fraccion, dia por dia, para componer un benchmark
    "ajustado por exposicion": cuanto hubiera devuelto el benchmark si solo se
    hubiera estado invertido en el la misma fraccion de capital que la
    estrategia realmente tuvo desplegada cada dia (en vez de 100% todo el
    periodo). Usa el precio de cierre mas reciente conocido en o antes de cada
    fecha (bench_close.asof) para tolerar fechas del calendario que no caen
    justo en una barra del benchmark (igual motivo que el resto de la funcion).

    Si invest_idle_cash_in_benchmark esta activo (ver ScreenerConfig), el
    cash que ese mismo dia NO esta desplegado en ninguna posicion (1 -
    exposicion) se simula invertido en el benchmark en vez de quieto a 0%:
    mismo mecanismo de compounding dia por dia que el benchmark ajustado por
    exposicion de arriba, pero ponderado por el COMPLEMENTO de la exposicion
    (1 - exposicion_hoy) en vez de la exposicion misma, y su crecimiento se
    suma directamente al factor de equity de la estrategia (no es solo una
    cifra de comparacion aparte: cambia strategy_cumulative_return_pct,
    max_drawdown_pct y sharpe_ratio, ya que esos se calculan sobre
    daily_equity).

    Si vol_weighting_enabled esta activo (ver ScreenerConfig), cada operacion
    pesa ~ 1/ATR_entrada en vez del mismo 1/top_n para todas (ver
    _trade_weights) -- alinea el peso simulado de cada posicion con el
    sizing por riesgo ATR que usa el sizing en vivo (rules.suggested_quantity),
    en vez de equiponderar una cartera que en la realidad nunca se opera asi.

    Devuelve (equity_curve, equity_diaria_en_factor, avg_exposure_pct,
    exposure_adjusted_benchmark_return_pct).
    """
    weights = _trade_weights(all_trades, top_n, vol_weighting_enabled)
    bench_close = bench_bars["Close"]
    start, end = all_trades[0].entry_date, all_trades[-1].exit_date
    calendar = sorted(
        set(bench_bars.index[(bench_bars.index >= start) & (bench_bars.index <= end)])
        | {t.entry_date for t in all_trades}
        | {t.exit_date for t in all_trades}
    )
    pos_by_date = {d: i for i, d in enumerate(calendar)}

    trades_by_entry = sorted(all_trades, key=lambda t: t.entry_date)
    next_entry_idx = 0
    open_trades: list[BacktestTrade] = []
    closed_factor = 1.0
    equity_curve: list[EquityCurvePoint] = []
    daily_equity: list[float] = []
    exposure_sum = 0.0
    bench_factor = 1.0
    idle_cash_factor = 1.0
    prev_bench_price = None

    for day_idx, date in enumerate(calendar):
        while next_entry_idx < len(trades_by_entry) and trades_by_entry[next_entry_idx].entry_date <= date:
            open_trades.append(trades_by_entry[next_entry_idx])
            next_entry_idx += 1

        still_open = []
        for t in open_trades:
            if t.exit_date <= date:
                closed_factor *= 1 + weights[id(t)] * t.return_pct / 100
            else:
                still_open.append(t)
        open_trades = still_open

        unrealized_pct = 0.0
        for t in open_trades:
            marks = marks_by_trade_id.get(id(t)) if marks_by_trade_id else None
            factor = marks.get(date) if marks else None
            if factor is None:
                entry_pos, exit_pos = pos_by_date[t.entry_date], pos_by_date[t.exit_date]
                frac = (day_idx - entry_pos) / (exit_pos - entry_pos) if exit_pos > entry_pos else 1.0
                factor = 1 + t.return_pct / 100 * frac
            unrealized_pct += weights[id(t)] * (factor - 1) * 100

        exposure_today = sum(weights[id(t)] for t in open_trades)
        exposure_sum += exposure_today

        bench_price = bench_close.asof(date)
        if prev_bench_price is not None and not pd.isna(bench_price) and prev_bench_price != 0:
            bench_return_today = bench_price / prev_bench_price - 1
            bench_factor *= 1 + exposure_today * bench_return_today
            if invest_idle_cash_in_benchmark:
                idle_cash_factor *= 1 + (1 - exposure_today) * bench_return_today
        if not pd.isna(bench_price):
            prev_bench_price = bench_price

        equity = closed_factor * (1 + unrealized_pct / 100)
        if invest_idle_cash_in_benchmark:
            equity += idle_cash_factor - 1
        daily_equity.append(equity)

        equity_curve.append(EquityCurvePoint(date=date, equity_pct=round((equity - 1) * 100, 2)))

    avg_exposure_pct = round(exposure_sum / len(calendar) * 100, 1)
    exposure_adjusted_benchmark_return_pct = round((bench_factor - 1) * 100, 2)
    return equity_curve, daily_equity, avg_exposure_pct, exposure_adjusted_benchmark_return_pct


def _trade_alpha_pct(trade: BacktestTrade, bench_close: pd.Series) -> float | None:
    """Alpha de una operacion individual: su return_pct menos lo que hizo el
    benchmark close-a-close en la misma ventana exacta entry_date->exit_date
    (no el periodo completo del backtest). Usa bench_close.asof (el ultimo
    precio conocido en o antes de la fecha) en vez de exigir coincidencia
    exacta de calendario, igual motivo que el resto del archivo: una accion
    individual puede tener una fecha sin barra exacta en el benchmark.

    None si el benchmark no tiene NINGUN precio conocido en o antes de alguna
    de las dos fechas (asof devuelve NaN cuando la fecha pedida es anterior a
    toda la historia disponible -- tipico de operaciones sinteticas en tests
    que no comparten calendario con bench_bars) o si el precio de entrada del
    benchmark es 0."""
    entry_bench = bench_close.asof(trade.entry_date)
    exit_bench = bench_close.asof(trade.exit_date)
    if pd.isna(entry_bench) or pd.isna(exit_bench) or entry_bench == 0:
        return None
    bench_return_pct = (exit_bench / entry_bench - 1) * 100
    return round(trade.return_pct - bench_return_pct, 2)


def _compute_summary_stats(
    all_trades: list[BacktestTrade],
    top_n: int,
    bench_bars: pd.DataFrame,
    marks_by_trade_id: dict | None = None,
    invest_idle_cash_in_benchmark: bool = False,
    vol_weighting_enabled: bool = False,
) -> BacktestSummary:
    """Calcula las metricas resumen a partir de la lista final de operaciones
    (ya filtrada por cap_concurrent_positions). Separado de run_backtest para
    poder testearlo con operaciones sinteticas, sin pasar por todo el pipeline
    de datos de mercado."""
    returns = [t.return_pct for t in all_trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    win_rate = len(wins) / len(returns) * 100
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    # gross_loss == 0 es ambiguo: puede ser "nunca hubo perdidas" (profit factor
    # infinito, no representable en JSON estandar) o "no hubo ni ganancias ni
    # perdidas" (indefinido). profit_factor_is_infinite distingue ambos casos
    # para el frontend sin depender de float('inf').
    profit_factor = (gross_profit / gross_loss) if gross_loss else None
    profit_factor_is_infinite = gross_loss == 0 and gross_profit > 0
    expectancy = sum(returns) / len(returns)

    equity_curve, daily_equity, avg_exposure_pct, exposure_adjusted_benchmark_return_pct = _daily_equity_curve(
        all_trades, top_n, bench_bars, marks_by_trade_id, invest_idle_cash_in_benchmark, vol_weighting_enabled
    )

    bench_close = bench_bars["Close"]
    trades_with_alpha = [t.model_copy(update={"alpha_pct": _trade_alpha_pct(t, bench_close)}) for t in all_trades]
    alpha_values = [t.alpha_pct for t in trades_with_alpha if t.alpha_pct is not None]
    avg_alpha_pct = round(sum(alpha_values) / len(alpha_values), 2) if alpha_values else None

    peak = 1.0
    max_drawdown = 0.0
    for equity in daily_equity:
        peak = max(peak, equity)
        drawdown = (equity - peak) / peak * 100
        max_drawdown = min(max_drawdown, drawdown)
    cumulative_return = (daily_equity[-1] - 1) * 100

    benchmark_cumulative = float(bench_bars["Close"].iloc[-1] / bench_bars["Close"].iloc[0] - 1) * 100

    # Sharpe a partir de los retornos DIARIOS de la curva de equity (no de los
    # retornos por operacion): asi es comparable con un Sharpe convencional
    # (anualizado por sqrt(252), dias de trading/año), y no solo una
    # aproximacion ad-hoc por "operaciones por año" inferidas del periodo. Con
    # menos de 2 operaciones el resultado no es estadisticamente significativo
    # sin importar cuantos puntos diarios genere esa unica operacion, asi que
    # se exige el mismo minimo de 2 operaciones que antes.
    sharpe_ratio = None
    if len(all_trades) >= 2 and len(daily_equity) >= 3:
        daily_returns = [daily_equity[i] / daily_equity[i - 1] - 1 for i in range(1, len(daily_equity))]
        std_r = statistics.stdev(daily_returns)
        if std_r > 0:
            sharpe_ratio = (statistics.mean(daily_returns) / std_r) * (252 ** 0.5)

    return BacktestSummary(
        start_date=all_trades[0].entry_date,
        end_date=all_trades[-1].exit_date,
        total_trades=len(all_trades),
        win_rate_pct=round(win_rate, 1),
        avg_return_pct=round(expectancy, 2),
        avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        profit_factor_is_infinite=profit_factor_is_infinite,
        expectancy_pct=round(expectancy, 2),
        strategy_cumulative_return_pct=round(cumulative_return, 2),
        benchmark_cumulative_return_pct=round(benchmark_cumulative, 2),
        exposure_adjusted_benchmark_return_pct=exposure_adjusted_benchmark_return_pct,
        avg_alpha_pct=avg_alpha_pct,
        max_drawdown_pct=round(max_drawdown, 2),
        sharpe_ratio=round(sharpe_ratio, 2) if sharpe_ratio is not None else None,
        avg_exposure_pct=avg_exposure_pct,
        exit_reason_counts=dict(Counter(t.exit_reason for t in all_trades)),
        trades=trades_with_alpha[-50:],
        equity_curve=equity_curve,
    )


def _build_walk_forward_result(
    all_trades: list[BacktestTrade],
    top_n: int,
    bench_bars: pd.DataFrame,
    marks_by_trade_id: dict,
    n_folds: int,
    invest_idle_cash_in_benchmark: bool = False,
    vol_weighting_enabled: bool = False,
) -> WalkForwardResult:
    """Particiona [primera_entrada, ultima_salida] del benchmark en n_folds
    ventanas consecutivas de igual duracion calendario (no de igual cantidad
    de operaciones) y corre _compute_summary_stats por separado en cada una,
    asignando cada operacion a un fold por su entry_date (nunca se parte una
    operacion entre dos folds). Reusa all_trades/marks_by_trade_id/bench_bars
    ya generados por _collect_momentum_trades / _collect_opportunistic_trades:
    no vuelve a simular ni a pedir datos de mercado.

    Un fold sin operaciones (o cuyo tramo de benchmark no tiene barras, en los
    bordes) no puede pasar por _compute_summary_stats (asume al menos una
    operacion); para esos casos devuelve un WalkForwardFold con total_trades=0
    y el resto de las metricas en None en vez de fallar."""
    full_start, full_end = bench_bars.index[0], bench_bars.index[-1]
    total_seconds = (full_end - full_start).total_seconds()
    bounds = [full_start + pd.Timedelta(seconds=total_seconds * i / n_folds) for i in range(n_folds + 1)]

    folds: list[WalkForwardFold] = []
    for i in range(n_folds):
        fold_start, fold_end = bounds[i], bounds[i + 1]
        is_last = i == n_folds - 1
        fold_trades = [
            t
            for t in all_trades
            if fold_start <= t.entry_date and (t.entry_date <= fold_end if is_last else t.entry_date < fold_end)
        ]
        fold_bench_bars = bench_bars.loc[fold_start:fold_end]
        if not fold_trades or fold_bench_bars.empty:
            folds.append(WalkForwardFold(start_date=fold_start, end_date=fold_end, total_trades=len(fold_trades)))
            continue
        summary = _compute_summary_stats(
            fold_trades, top_n, fold_bench_bars, marks_by_trade_id, invest_idle_cash_in_benchmark, vol_weighting_enabled
        )
        folds.append(
            WalkForwardFold(
                start_date=fold_start,
                end_date=fold_end,
                total_trades=summary.total_trades,
                win_rate_pct=summary.win_rate_pct,
                avg_return_pct=summary.avg_return_pct,
                strategy_cumulative_return_pct=summary.strategy_cumulative_return_pct,
                benchmark_cumulative_return_pct=summary.benchmark_cumulative_return_pct,
                exposure_adjusted_benchmark_return_pct=summary.exposure_adjusted_benchmark_return_pct,
                avg_alpha_pct=summary.avg_alpha_pct,
                max_drawdown_pct=summary.max_drawdown_pct,
                sharpe_ratio=summary.sharpe_ratio,
            )
        )
    return WalkForwardResult(n_folds=n_folds, folds=folds)


def _collect_momentum_trades(cfg: ScreenerConfig) -> tuple[list[BacktestTrade], dict, pd.DataFrame]:
    """Simula la estrategia Momentum sobre todo el universo configurado y
    devuelve las operaciones resultantes (ya capadas a top_n posiciones
    concurrentes), el dict de marcas diarias por operacion (ver
    _trade_daily_marks) y la historia del benchmark. Separado de run_backtest
    para que run_backtest_walk_forward pueda reusar la misma simulacion sin
    volver a pedir datos de mercado.

    Dos pasadas: primero se piden las barras de TODO el universo (con la
    misma pausa anti-rate-limit que antes, ver scan_request_delay_seconds),
    despues se calcula el panel de score cross-sectional dia por dia sobre
    ese universo ya descargado (ver _cross_sectional_score_panel), y recien
    despues se simula cada simbolo -- no se puede saber el percentil de un
    simbolo en una fecha dada sin tener primero la historia de todos los
    demas para esa misma fecha (a diferencia del scan en vivo, que solo
    necesita el ranking de HOY)."""
    history_days = int(cfg.backtest_years * 365)

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    if cfg.regime_filter_enabled:
        benchmark_regime_ok = market_regime_ok(
            bench_bars["Close"], cfg.regime_sma_period, cfg.regime_slope_lookback_days,
            cfg.regime_absolute_momentum_lookback_days,
        )
    else:
        benchmark_regime_ok = pd.Series(True, index=bench_bars.index)

    bars_by_symbol: dict[str, pd.DataFrame] = {}
    delay = cfg.scan_request_delay_seconds
    for i, symbol in enumerate(_backtest_universe(cfg)):
        # Misma pausa anti-rate-limit que el scan en vivo (ver
        # scan_request_delay_seconds): el backtest pega tantos pedidos como
        # simbolos tenga el universo configurado, y con el S&P 500 completo
        # eso son varios cientos de requests seguidos a la API gratuita.
        if i > 0 and delay > 0:
            time.sleep(delay)
        try:
            bars = get_daily_bars(symbol, history_days)
        except MarketDataError:
            continue
        if len(bars) < max(cfg.sma_slow + cfg.momentum_lookback_days, cfg.momentum_12_1_lookback_days + 1):
            continue
        bars_by_symbol[symbol] = bars

    # Sin esto, pd.concat sobre un dict vacio en _momentum_raw_components
    # rompe con ValueError en vez de la misma BacktestError de "sin
    # operaciones" que ya se usa mas abajo cuando ningun simbolo paso el
    # filtro de entrada (caso equivalente: ningun simbolo tiene historia).
    if not bars_by_symbol:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    raw_components = _momentum_raw_components(bars_by_symbol, cfg, bench_bars, history_days)
    score_panel = _cross_sectional_score_panel(raw_components, {
        "relative_strength": cfg.score_weight_relative_strength,
        "momentum_12_1": cfg.score_weight_momentum_12_1,
        "trend": cfg.score_weight_trend,
        "rsi": cfg.score_weight_rsi,
        "macd": cfg.score_weight_macd,
        "bollinger": cfg.score_weight_bollinger,
        "sector_relative_strength": cfg.score_weight_sector_relative_strength,
    })

    all_trades: list[BacktestTrade] = []
    marks_by_trade_id: dict = {}
    for symbol, bars in bars_by_symbol.items():
        aligned_regime_ok = benchmark_regime_ok.reindex(bars.index, method="ffill").fillna(True)
        all_trades.extend(_simulate_symbol(symbol, bars, cfg, score_panel[symbol], aligned_regime_ok, marks_by_trade_id))

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)

    # Cartera con top_n cupos concurrentes: antes se contaba CADA señal de cada
    # simbolo como una operacion, pero la curva de equity ponderaba cada una
    # como 1/top_n. Con mas de top_n posiciones abiertas a la vez eso
    # subrepresentaba el capital realmente usado e inflaba el retorno. Ahora se
    # descartan las operaciones que no tendrian cupo libre (ver
    # cap_concurrent_positions), igual que en la operatoria real.
    all_trades = cap_concurrent_positions(all_trades, cfg.top_n, cfg.max_concurrent_positions_per_sector)

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    return all_trades, marks_by_trade_id, bench_bars


def run_backtest(cfg: ScreenerConfig) -> BacktestSummary:
    """Backtest simplificado de la estrategia momentum sobre el universo configurado.

    La curva de equity, el max_drawdown_pct y el sharpe_ratio se calculan dia
    por dia sobre una cartera con cupo para top_n posiciones concurrentes (ver
    _daily_equity_curve): una posicion abierta aporta su retorno no realizado
    a la curva todos los dias que esta abierta (no solo en su cierre), usando
    el cierre real de mercado de cada dia (ver _trade_daily_marks), asi que el
    drawdown combinado de operaciones solapadas en el tiempo queda reflejado
    con el camino de precio real, no una aproximacion. El sharpe_ratio se
    anualiza con sqrt(252) sobre esos retornos diarios, igual que un Sharpe
    convencional.

    Simplificaciones explicitas que siguen sin modelarse (no es un backtester
    de produccion): el universo de simbolos es el configurado HOY (sin
    supervivencia historica -- una accion que quebro o fue excluida del
    indice durante el periodo analizado no aparece, lo que tipicamente infla
    el resultado frente a la realidad de esa epoca). Si modela comision/
    slippage estimados y un fill de stop-loss realista (minimo intradiario, no
    el cierre), y entra a la apertura del dia siguiente a la senal (no al
    cierre del mismo dia, que seria mirar al futuro). Esto ultimo lo hace mas
    conservador que el motor de auto-trading en vivo, que coloca la orden ya
    con el ultimo cierre conocido en el mismo ciclo de scan. Sirve para
    validar la direccion de la idea antes de arriesgar capital real, no como
    promesa de resultados futuros.
    """
    all_trades, marks_by_trade_id, bench_bars = _collect_momentum_trades(cfg)
    return _compute_summary_stats(
        all_trades, cfg.top_n, bench_bars, marks_by_trade_id,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
    )


def run_backtest_walk_forward(cfg: ScreenerConfig, n_folds: int = 3) -> WalkForwardResult:
    """Validacion out-of-sample de los thresholds configurados (RSI, SMAs,
    ATR, filtros de regimen/52 semanas, etc.): particiona el periodo operado
    en n_folds ventanas consecutivas de igual duracion calendario y calcula
    las metricas resumen de cada una por separado (ver
    _build_walk_forward_result), sin volver a pedir ni simular nada (reusa
    las mismas operaciones que generaria run_backtest sobre todo el periodo).

    Util para detectar si el resultado del backtest completo esta
    concentrado en un tramo de tiempo favorable puntual (ej. un solo mercado
    alcista) en vez de sostenerse a traves de distintos regimenes -- algo que
    el resumen de todo el periodo de una sola vez no puede mostrar.

    Importante: esto NO es walk-forward optimization en el sentido clasico.
    No hay refitting de parametros por ventana -- los thresholds configurados
    son siempre los mismos en todos los folds, porque esta herramienta no
    hace optimizacion de parametros. "Out of sample" aca significa: ¿el mismo
    set de reglas fijo (el que ya esta configurado) se sostiene en distintos
    tramos de tiempo, o gano todo en un solo tramo favorable?"""
    all_trades, marks_by_trade_id, bench_bars = _collect_momentum_trades(cfg)
    return _build_walk_forward_result(
        all_trades, cfg.top_n, bench_bars, marks_by_trade_id, n_folds,
        cfg.invest_idle_cash_in_benchmark, cfg.backtest_vol_weighting_enabled,
    )
