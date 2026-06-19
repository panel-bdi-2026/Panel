from __future__ import annotations

import statistics
import time

import pandas as pd

from .indicators import atr, pct_from_high, rate_of_change, rsi, sma
from .market_data import MarketDataError, get_daily_bars
from .models import BacktestSummary, BacktestTrade, EquityCurvePoint
from .screener_config import ScreenerConfig


class BacktestError(RuntimeError):
    pass


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


def _simulate_symbol(
    symbol: str,
    bars: pd.DataFrame,
    cfg: ScreenerConfig,
    benchmark_roc: pd.Series,
    benchmark_regime_ok: pd.Series,
    marks_by_trade_id: dict | None = None,
) -> list[BacktestTrade]:
    """Simula la misma logica de entrada/salida del screener sobre historia.

    Entra cuando se cumplen los mismos filtros que en `scan()` (tendencia,
    momentum, RSI, fuerza relativa vs benchmark, regimen de mercado). Como
    esos indicadores recien se conocen al cierre del dia que los confirma, el
    fill de entrada se simula a la apertura del dia siguiente (no al cierre
    del dia de la senal, que seria mirar al futuro). Sale por stop-loss
    (basado en el ATR del dia de la senal, igual que la sugerencia en vivo),
    por ruptura de tendencia, o por tiempo maximo en la posicion. Una sola
    posicion por simbolo a la vez.

    El stop-loss se chequea contra el minimo intradiario (no el cierre): si el
    precio perfora el stop durante el dia, en la realidad se sale ahi (o peor,
    si abre con un gap por debajo del stop), no se espera al cierre. Tambien
    se restan comision y slippage estimados, para no inflar los retornos
    respecto a la operatoria real.
    """
    close = bars["Close"]
    sma_fast_s = sma(close, cfg.sma_fast)
    sma_slow_s = sma(close, cfg.sma_slow)
    roc_3m = rate_of_change(close, cfg.momentum_lookback_days)
    roc_1m = rate_of_change(close, cfg.momentum_short_days)
    rsi_s = rsi(close, cfg.rsi_period)
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
    from_high_s = pct_from_high(close, 252)

    notional_per_trade = cfg.backtest_assumed_capital_usd / cfg.top_n if cfg.top_n else 0.0

    trades: list[BacktestTrade] = []
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_price = 0.0
    pending_entry_atr = None  # ATR del dia en que se detecto la senal (ver mas abajo)

    start_idx = max(cfg.sma_slow, cfg.momentum_lookback_days, cfg.atr_period) + 1
    for i in range(start_idx, len(bars)):
        date = bars.index[i]
        price = float(close.iloc[i])
        low_price = float(bars["Low"].iloc[i])
        open_price = float(bars["Open"].iloc[i])

        if pending_entry_atr is not None:
            # La senal se detecto con el cierre del dia anterior: los
            # indicadores (SMA, RSI, momentum) recien se conocen una vez
            # cerrado ese dia, asi que en la realidad la orden se coloca al
            # dia siguiente. Entrar al cierre del mismo dia de la senal seria
            # mirar al futuro; el fill realista es la apertura de este dia.
            in_position = True
            entry_price = open_price
            entry_idx = i
            entry_date = date
            stop_price = entry_price - cfg.stop_loss_atr_multiplier * pending_entry_atr
            pending_entry_atr = None
            continue

        if in_position:
            held_days = i - entry_idx
            hit_stop = low_price <= stop_price
            timed_out = held_days >= cfg.max_holding_days
            trend_broke = price < sma_fast_s.iloc[i]
            if hit_stop or timed_out or trend_broke:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                # Si hubo gap por debajo del stop, el fill realista es el open
                # (peor que el stop); si no, se asume fill al precio del stop.
                raw_exit_price = min(open_price, stop_price) if hit_stop else price

                entry_fill = entry_price * (1 + cfg.slippage_pct / 100)
                exit_fill = raw_exit_price * (1 - cfg.slippage_pct / 100)
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
                )
                trades.append(trade)
                if marks_by_trade_id is not None:
                    marks_by_trade_id[id(trade)] = _trade_daily_marks(bars, entry_idx, i, entry_fill, ret_pct)
                in_position = False
            continue

        if pd.isna(sma_slow_s.iloc[i]) or pd.isna(atr_s.iloc[i]) or pd.isna(roc_3m.iloc[i]):
            continue

        trend_ok = price > sma_fast_s.iloc[i] > sma_slow_s.iloc[i]
        rsi_ok = cfg.rsi_min <= rsi_s.iloc[i] <= cfg.rsi_max
        bench_roc = benchmark_roc.iloc[i] if i < len(benchmark_roc) and not pd.isna(benchmark_roc.iloc[i]) else None
        rel_strength_ok = bench_roc is None or roc_3m.iloc[i] > bench_roc
        momentum_ok = roc_3m.iloc[i] > 0 and (pd.isna(roc_1m.iloc[i]) or roc_1m.iloc[i] > 0)
        regime_ok = bool(benchmark_regime_ok.iloc[i]) if i < len(benchmark_regime_ok) else True
        fh = from_high_s.iloc[i]
        near_high_ok = (
            not cfg.near_high_filter_enabled
            or pd.isna(fh)
            or fh >= -cfg.max_pct_below_52w_high
        )

        if trend_ok and rsi_ok and rel_strength_ok and momentum_ok and regime_ok and near_high_ok:
            pending_entry_atr = atr_s.iloc[i]

    return trades


def _simulate_symbol_opportunistic(
    symbol: str, bars: pd.DataFrame, cfg: ScreenerConfig, marks_by_trade_id: dict | None = None
) -> list[BacktestTrade]:
    """Misma logica de _simulate_symbol pero con los filtros de entrada/salida
    de la estrategia Oportunista (ver strategies/opportunistic.py): momentum
    de corto plazo positivo, RSI en zona de recuperacion (no de tendencia
    establecida como Momentum) y precio bien por debajo del maximo de 52
    semanas (espacio de crecimiento, lo opuesto al filtro de Momentum).
    Sale por stop-loss, tiempo maximo en la posicion, o RSI sobrecomprado
    (señal de que el rebote de corto plazo ya se jugo)."""
    opp = cfg.opportunistic
    close = bars["Close"]
    roc_short = rate_of_change(close, opp.momentum_lookback_days)
    rsi_s = rsi(close, opp.rsi_period)
    atr_s = atr(bars["High"], bars["Low"], close, cfg.atr_period)
    from_high_s = pct_from_high(close, 252)

    notional_per_trade = cfg.backtest_assumed_capital_usd / cfg.top_n if cfg.top_n else 0.0

    trades: list[BacktestTrade] = []
    in_position = False
    entry_price = 0.0
    entry_idx = 0
    entry_date = None
    stop_price = 0.0
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
            stop_price = entry_price - opp.stop_loss_atr_multiplier * pending_entry_atr
            pending_entry_atr = None
            continue

        if in_position:
            held_days = i - entry_idx
            hit_stop = low_price <= stop_price
            timed_out = held_days >= opp.max_holding_days
            rsi_exhausted = rsi_s.iloc[i] > opp.rsi_max
            if hit_stop or timed_out or rsi_exhausted:
                exit_reason = "stop_loss" if hit_stop else ("max_holding_days" if timed_out else "trend_break")
                raw_exit_price = min(open_price, stop_price) if hit_stop else price

                entry_fill = entry_price * (1 + cfg.slippage_pct / 100)
                exit_fill = raw_exit_price * (1 - cfg.slippage_pct / 100)
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
                )
                trades.append(trade)
                if marks_by_trade_id is not None:
                    marks_by_trade_id[id(trade)] = _trade_daily_marks(bars, entry_idx, i, entry_fill, ret_pct)
                in_position = False
            continue

        if pd.isna(atr_s.iloc[i]) or pd.isna(roc_short.iloc[i]):
            continue

        last_atr = atr_s.iloc[i]
        volatility_pct = (last_atr / price * 100) if price else 0.0
        fh = from_high_s.iloc[i]

        momentum_ok = roc_short.iloc[i] > 0
        rsi_ok = opp.rsi_min <= rsi_s.iloc[i] <= opp.rsi_max
        volatility_ok = volatility_pct >= opp.min_volatility_pct
        room_to_grow_ok = pd.isna(fh) or fh <= -opp.min_pct_below_52w_high

        if momentum_ok and rsi_ok and volatility_ok and room_to_grow_ok:
            pending_entry_atr = last_atr

    return trades


def run_opportunistic_backtest(cfg: ScreenerConfig) -> BacktestSummary:
    """Backtest de la estrategia Oportunista, en paralelo a run_backtest
    (Momentum). Misma estructura y mismas simplificaciones documentadas ahi
    (curva de equity diaria con cupo top_n, sin supervivencia historica del
    universo); aca solo cambia la logica de entrada/salida por simbolo (ver
    _simulate_symbol_opportunistic)."""
    history_days = int(cfg.backtest_years * 365)

    all_trades: list[BacktestTrade] = []
    marks_by_trade_id: dict = {}
    delay = cfg.scan_request_delay_seconds
    for i, symbol in enumerate(cfg.universe):
        if i > 0 and delay > 0:
            time.sleep(delay)
        try:
            bars = get_daily_bars(symbol, history_days)
        except MarketDataError:
            continue
        if len(bars) < cfg.opportunistic.momentum_lookback_days + 252:
            continue
        all_trades.extend(_simulate_symbol_opportunistic(symbol, bars, cfg, marks_by_trade_id))

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)
    all_trades = cap_concurrent_positions(all_trades, cfg.top_n)

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    return _compute_summary_stats(all_trades, cfg.top_n, bench_bars, marks_by_trade_id)


def cap_concurrent_positions(trades: list[BacktestTrade], top_n: int) -> list[BacktestTrade]:
    """Filtra una lista de operaciones a un maximo de top_n posiciones abiertas
    a la vez, recorriendolas por fecha de entrada y descartando las que no
    tendrian cupo libre (como en la operatoria real, donde no se pueden tener
    mas de top_n posiciones simultaneas). Sin top_n valido devuelve la lista
    intacta. Se asume que `trades` ya viene ordenada por fecha de entrada."""
    if not top_n or top_n <= 0:
        return list(trades)
    taken: list[BacktestTrade] = []
    open_exit_dates: list = []
    for t in trades:
        open_exit_dates = [d for d in open_exit_dates if d > t.entry_date]
        if len(open_exit_dates) >= top_n:
            continue  # sin cupo libre: en la realidad no se podria abrir
        taken.append(t)
        open_exit_dates.append(t.exit_date)
    return taken


def _daily_equity_curve(
    all_trades: list[BacktestTrade], top_n: int, bench_bars: pd.DataFrame, marks_by_trade_id: dict | None = None,
) -> tuple[list[EquityCurvePoint], list[float], float]:
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

    Devuelve (equity_curve, equity_diaria_en_factor, avg_exposure_pct).
    """
    weight = 1.0 / top_n
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

    for day_idx, date in enumerate(calendar):
        while next_entry_idx < len(trades_by_entry) and trades_by_entry[next_entry_idx].entry_date <= date:
            open_trades.append(trades_by_entry[next_entry_idx])
            next_entry_idx += 1

        still_open = []
        for t in open_trades:
            if t.exit_date <= date:
                closed_factor *= 1 + weight * t.return_pct / 100
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
            unrealized_pct += weight * (factor - 1) * 100

        equity = closed_factor * (1 + unrealized_pct / 100)
        daily_equity.append(equity)
        exposure_sum += len(open_trades) * weight
        equity_curve.append(EquityCurvePoint(date=date, equity_pct=round((equity - 1) * 100, 2)))

    avg_exposure_pct = round(exposure_sum / len(calendar) * 100, 1)
    return equity_curve, daily_equity, avg_exposure_pct


def _compute_summary_stats(
    all_trades: list[BacktestTrade], top_n: int, bench_bars: pd.DataFrame, marks_by_trade_id: dict | None = None,
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

    equity_curve, daily_equity, avg_exposure_pct = _daily_equity_curve(all_trades, top_n, bench_bars, marks_by_trade_id)

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
        max_drawdown_pct=round(max_drawdown, 2),
        sharpe_ratio=round(sharpe_ratio, 2) if sharpe_ratio is not None else None,
        avg_exposure_pct=avg_exposure_pct,
        trades=all_trades[-50:],
        equity_curve=equity_curve,
    )


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
    history_days = int(cfg.backtest_years * 365)

    try:
        bench_bars = get_daily_bars(cfg.benchmark_symbol, history_days)
    except MarketDataError as exc:
        raise BacktestError(str(exc)) from exc

    benchmark_roc = rate_of_change(bench_bars["Close"], cfg.momentum_lookback_days)

    if cfg.regime_filter_enabled:
        bench_regime_sma = sma(bench_bars["Close"], cfg.regime_sma_period)
        benchmark_regime_ok = (bench_bars["Close"] > bench_regime_sma) | bench_regime_sma.isna()
    else:
        benchmark_regime_ok = pd.Series(True, index=bench_bars.index)

    all_trades: list[BacktestTrade] = []
    marks_by_trade_id: dict = {}
    delay = cfg.scan_request_delay_seconds
    for i, symbol in enumerate(cfg.universe):
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
        if len(bars) < cfg.sma_slow + cfg.momentum_lookback_days:
            continue
        aligned_bench_roc = benchmark_roc.reindex(bars.index, method="ffill")
        aligned_regime_ok = benchmark_regime_ok.reindex(bars.index, method="ffill").fillna(True)
        all_trades.extend(_simulate_symbol(symbol, bars, cfg, aligned_bench_roc, aligned_regime_ok, marks_by_trade_id))

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    all_trades.sort(key=lambda t: t.entry_date)

    # Cartera con top_n cupos concurrentes: antes se contaba CADA señal de cada
    # simbolo como una operacion, pero la curva de equity ponderaba cada una
    # como 1/top_n. Con mas de top_n posiciones abiertas a la vez eso
    # subrepresentaba el capital realmente usado e inflaba el retorno. Ahora se
    # descartan las operaciones que no tendrian cupo libre (ver
    # cap_concurrent_positions), igual que en la operatoria real.
    all_trades = cap_concurrent_positions(all_trades, cfg.top_n)

    if not all_trades:
        raise BacktestError("No se generaron operaciones con estos parametros en el periodo analizado.")

    return _compute_summary_stats(all_trades, cfg.top_n, bench_bars, marks_by_trade_id)
