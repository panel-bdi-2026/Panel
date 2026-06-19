from __future__ import annotations

import asyncio
import math
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .audit import AuditLog
from .backtest import BacktestError, run_backtest, run_opportunistic_backtest
from .broker import IBKRBroker, IBKRConnectionError, StopLossRejectedError
from .config import settings
from .funds import FundsStore
from .indicators import sma
from .market_data import MarketDataError, get_daily_bars
from .models import OrderRequest, OrderType, PendingOrder, Position, SignalResult, Side, validate_symbol
from .rules import RulesConfig, RulesEngine
from .screener import MomentumScreener
from .screener_config import ScreenerConfig
from .sectors import get_sector, refresh_sector
from .state_store import load_state, save_state
from .strategies import STRATEGY_CLASSES, reload_strategy_registry

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

rules_config = RulesConfig.load(settings.rules_path)
rules_engine = RulesEngine(rules_config)
audit = AuditLog(settings.audit_db_path)
broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)

screener_config = ScreenerConfig.load(settings.screener_path)
screener = MomentumScreener(screener_config)

# screener (singleton de Momentum, instanciado arriba) es el mismo objeto que
# strategy_registry["momentum"]: los tests existentes (test_signal_engine.py)
# hacen monkeypatch.setattr(main_module.screener, "scan", ...) directo sobre
# la instancia, asi que no se puede reconstruir un MomentumScreener nuevo aca.
strategy_registry: dict[str, object] = {screener.id: screener}
for _strategy_cls in STRATEGY_CLASSES:
    if _strategy_cls is MomentumScreener:
        continue
    _instance = _strategy_cls(screener_config)
    strategy_registry[_instance.id] = _instance


def _sync_whitelist_with_universe() -> None:
    """Mantiene `rules_config.symbol_whitelist` igual al `universe` del
    screener: el radar de oportunidades ya elige el universo de forma
    autonoma (ver screener.yaml), asi que la whitelist deja de mantenerse a
    mano por separado y simplemente lo refleja. Si el universo quedara vacio,
    la whitelist tambien queda vacia, y la traba de "lista blanca vacia
    bloquea todo" en RulesEngine.evaluate() sigue aplicando igual."""
    if rules_config.symbol_whitelist != screener_config.universe:
        rules_config.symbol_whitelist = list(screener_config.universe)
        rules_config.save(settings.rules_path)
        rules_engine.reload(rules_config)


_sync_whitelist_with_universe()

funds_store = FundsStore(settings.funds_path)

# Estado persistido (halted, mode, ordenes pendientes) para que sobreviva a un
# reinicio del backend: sin esto, un reinicio (intencional o por un crash)
# perdia el halt o las ordenes pendientes de aprobacion y volvia silenciosamente
# a los valores por defecto (trading activo).
_persisted = load_state(settings.state_path)

state: dict = {
    "mode": _persisted.get("mode", settings.trading_mode),
    "halted": _persisted.get("halted", False),
    "connected": False,
    "pending_orders": {
        pid: PendingOrder(**p) for pid, p in _persisted.get("pending_orders", {}).items()
    },
}
clients: list[WebSocket] = []

SIGNAL_CACHE_TTL_SECONDS = 900  # evita re-escanear el mercado en cada refresh
# Cacheado por strategy_id: cada estrategia escanea el mismo universo pero con
# filtros/scores distintos, asi que comparten cache llevaria a devolver
# resultados de una estrategia bajo el nombre de otra.
signal_cache: dict[str, dict] = {}

# Serializa scan y backtest (manuales y el ciclo proactivo en background):
# ambos golpean la misma API gratuita de datos para todo el universo
# configurado, y dejarlos correr en paralelo (ej. alguien pide un backtest
# mientras el scan proactivo esta en ciclo) duplica la tasa de pedidos y
# aumenta el riesgo de bloqueo temporal de la API. No hace falta mas que un
# lock simple: esto no es trafico de alta concurrencia.
_market_scan_lock = asyncio.Lock()

# Serializa toda la secuencia "validar fondo -> enviar al broker -> aplicar el
# fill" entre submit_order, approve_order, _try_auto_trade_entry y
# _check_fund_exit. Sin esto, dos de estos flujos corriendo concurrentemente
# sobre el MISMO fondo (ej. una orden manual y el motor de auto-trading, o dos
# ordenes manuales seguidas) pueden validar ambas contra el mismo cash_usd
# desactualizado -- ninguna ve el efecto de la otra hasta que record_fill ya
# corrio -- y terminar gastando mas cash del que el fondo realmente tiene. El
# lock se sostiene durante el await a broker.place_order a proposito: cerrar
# la ventana de carrera exige que ninguna otra validacion para el mismo fondo
# pueda colarse entre "ya valide" y "ya aplique el fill".
_funds_order_lock = asyncio.Lock()

# Recuerda que simbolos pasaban los filtros del screener en el ultimo ciclo del
# scan proactivo, para poder detectar TRANSICIONES (no pasaba -> pasa) en vez
# de redraftear el mismo simbolo en cada ciclo mientras siga pasando. None
# significa "todavia no hay base": el primer ciclo solo la establece, sin
# generar borradores, para no inundar la cola de pendientes apenas arranca el
# backend o se cambia la config del screener.
_signal_state: dict = {"previously_passing": None}


def _persist_state() -> None:
    save_state(settings.state_path, {
        "mode": state["mode"],
        "halted": state["halted"],
        "pending_orders": {pid: p.model_dump() for pid, p in state["pending_orders"].items()},
    })


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    # compare_digest en vez de != para no filtrar la API key por timing (una
    # comparacion de strings comun corta apenas encuentra el primer caracter
    # distinto, lo que en teoria permite adivinarla caracter por caracter
    # midiendo tiempos de respuesta).
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="API key invalida.")


async def _broadcast(payload: dict) -> None:
    dead = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        if not clients or not state["connected"]:
            continue
        try:
            payload = {
                "type": "update",
                "account": broker.get_account_summary().model_dump(),
                "positions": [p.model_dump() for p in await broker.get_positions()],
            }
        except Exception as exc:
            payload = {"type": "error", "message": str(exc)}
        await _broadcast(payload)


def _compute_sector_exposure(positions: list[Position], exclude_symbol: str) -> dict[str, float]:
    """Valor de mercado (USD, en valor absoluto) agrupado por sector GICS de
    las posiciones actuales, para que RulesEngine.evaluate() pueda chequear
    max_sector_concentration_pct. `exclude_symbol` se descarta del total
    porque ese simbolo ya se suma aparte como `resulting_value` (la orden en
    evaluacion), para no contarlo dos veces. Posiciones en simbolos sin sector
    conocido (get_sector devuelve None) no aportan al total: no hay forma de
    saber a que sector concentrarlas."""
    exposure: dict[str, float] = {}
    for p in positions:
        if p.symbol == exclude_symbol:
            continue
        sector = get_sector(p.symbol)
        if sector is None:
            continue
        price = p.market_price if p.market_price is not None else p.avg_cost
        exposure[sector] = exposure.get(sector, 0.0) + abs(p.quantity) * price
    return exposure


def _draft_order_from_signal(result: SignalResult, positions: list[Position]) -> PendingOrder | None:
    """Convierte una señal recien pasada a passes_filters=True en una orden de
    compra en borrador, sizeada por riesgo via RulesEngine.suggested_quantity().

    Se salta el draft (sin loggear error, es esperable que pase seguido) si ya
    hay una posicion abierta o una orden pendiente en ese simbolo, o si el
    sizing por riesgo da cantidad cero (sin equity/cuenta no conectada).

    Importante: el draft SIEMPRE queda en pending_orders para aprobacion
    manual, sin importar lo que diga decision.requires_manual_approval. Una
    orden generada sin intervencion humana nunca debe poder ejecutarse sola,
    aunque su valor este por debajo de manual_approval_threshold_usd.
    """
    symbol = result.symbol
    if any(p.order.symbol == symbol for p in state["pending_orders"].values()):
        return None

    position_qty = broker.get_position_qty(symbol)
    if position_qty != 0:
        return None

    account_summary = broker.get_account_summary()
    sizing = rules_engine.suggested_quantity(
        account_summary.net_liquidation, position_qty, result.last_price, result.suggested_stop_loss_price
    )
    if sizing.quantity <= 0:
        return None

    order = OrderRequest(
        symbol=symbol,
        side=Side.BUY,
        quantity=sizing.quantity,
        order_type=OrderType.LMT,
        limit_price=result.last_price,
        stop_loss_price=result.suggested_stop_loss_price,
    )
    trades_today = audit.count_trades_today(rules_config.trading_hours_timezone)
    decision = rules_engine.evaluate(
        order=order,
        account=account_summary,
        current_position_qty=position_qty,
        reference_price=result.last_price,
        trades_today=trades_today,
        halted=state["halted"],
        order_sector=get_sector(symbol),
        sector_exposure_usd=_compute_sector_exposure(positions, symbol),
    )
    if not decision.approved:
        audit.record("signal_order_rejected", order.model_dump(), decision.model_dump())
        return None

    pending_id = str(uuid.uuid4())
    pending = PendingOrder(
        id=pending_id,
        order=order,
        decision=decision,
        created_at=datetime.now(timezone.utc),
        source="signal_engine",
    )
    state["pending_orders"][pending_id] = pending
    _persist_state()
    audit.record("signal_order_drafted", order.model_dump(), {"id": pending_id, "signal": result.model_dump()})
    return pending


async def _try_auto_trade_entry(result: SignalResult) -> None:
    """Para fondos con auto_trading_enabled, ejecuta la compra de inmediato
    (sin aprobacion manual) en vez de dejarla en borrador. Solo corre en modo
    paper: el auto-trading nunca opera en live, sin importar el toggle del
    fondo. La senal se asigna a un solo fondo (el primero con cupo, por orden
    de creacion, que pueda afrontar al menos 1 unidad) para que varios fondos
    no compitan por el mismo simbolo a la vez."""
    if state["mode"] != "paper" or result.last_price <= 0:
        return
    symbol = result.symbol
    async with _funds_order_lock:
        candidates = [f for f in funds_store.list() if f.auto_trading_enabled and f.owned_quantity(symbol) == 0]
        if not candidates:
            return

        account_summary = broker.get_account_summary()
        position_qty = broker.get_position_qty(symbol)
        sector_exposure_usd = _compute_sector_exposure(await broker.get_positions(), symbol)

        fund = None
        quantity = 0.0
        for candidate in candidates:
            sizing = rules_engine.suggested_quantity(
                candidate.equity_estimate(), position_qty, result.last_price, result.suggested_stop_loss_price
            )
            if sizing.quantity <= 0:
                continue
            affordable_qty = math.floor(candidate.cash_usd / result.last_price)
            qty = min(sizing.quantity, affordable_qty)
            if qty > 0:
                fund = candidate
                quantity = qty
                break
        if fund is None:
            return

        order = OrderRequest(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            order_type=OrderType.LMT,
            limit_price=result.last_price,
            stop_loss_price=result.suggested_stop_loss_price,
            fund_id=fund.id,
        )
        trades_today = audit.count_trades_today(rules_config.trading_hours_timezone)
        decision = rules_engine.evaluate(
            order=order,
            account=account_summary,
            current_position_qty=position_qty,
            reference_price=result.last_price,
            trades_today=trades_today,
            halted=state["halted"],
            order_sector=get_sector(symbol),
            sector_exposure_usd=sector_exposure_usd,
        )
        if not decision.approved:
            audit.record("auto_trade_rejected", order.model_dump(), decision.model_dump())
            return

        try:
            result_payload = await broker.place_order(order)
        except StopLossRejectedError as exc:
            audit.record("auto_trade_stop_loss_rejected", order.model_dump(), {"error": str(exc)})
            return

        funds_store.record_fill(
            fund.id, symbol, Side.BUY, quantity, result.last_price, stop_loss_price=result.suggested_stop_loss_price
        )
        audit.record(
            "auto_trade_executed",
            order.model_dump(),
            {"fund_id": fund.id, "signal": result.model_dump(), **result_payload},
        )


async def _run_signal_scan_cycle() -> None:
    """Un ciclo del escaneo proactivo: corre el screener, detecta simbolos que
    recien empiezan a pasar los filtros (transicion no-pasa -> pasa) y les
    arma una orden de compra en borrador (ver _draft_order_from_signal).

    Separado de _signal_scan_loop (que solo aporta el sleep + while True) para
    poder testear un ciclo de una sola vez sin lidiar con un loop infinito.
    """
    if not screener_config.auto_scan_enabled or state["halted"] or not state["connected"]:
        return
    active_strategy = strategy_registry[screener_config.strategy_id]
    try:
        async with _market_scan_lock:
            results = await asyncio.to_thread(active_strategy.scan)
    except Exception as exc:
        audit.record("signal_scan_failed", {}, {"error": str(exc)})
        return

    top_results = results[: screener_config.top_n]
    passing_now = {r.symbol for r in top_results if r.passes_filters}
    previously_passing = _signal_state["previously_passing"]

    if previously_passing is None:
        # Primer ciclo: solo establece la base. Sin esto, cada simbolo que ya
        # viniera pasando los filtros desde antes de que arrancara el backend
        # (o desde el ultimo cambio de config) se draftearia de una al primer
        # ciclo, en vez de solo los que cambian de estado.
        _signal_state["previously_passing"] = passing_now
        return

    new_symbols = passing_now - previously_passing
    _signal_state["previously_passing"] = passing_now
    if not new_symbols:
        return

    # new_signals queda ordenado por score (top_results ya viene ordenado), asi
    # que al recortar por el cap se conservan las señales mas fuertes. El cap es
    # el menor entre el tope por ciclo y los cupos libres respecto a top_n
    # (contando lo que ya esta pendiente), para no sobre-asignar la cartera de
    # un golpe. Errar hacia MENOS ordenes automaticas es el lado seguro.
    new_signals = [r for r in top_results if r.symbol in new_symbols]

    # Intenta primero la entrada automatica por fondo (ver _try_auto_trade_entry):
    # se ejecuta antes del draft manual y usa el mismo tope por ciclo, asi que si
    # un fondo auto-trading ya tomo la señal, el draft manual de abajo la salta
    # solo (chequea la posicion real en el broker, que ya quedo en no-cero).
    for r in new_signals[: screener_config.max_auto_drafts_per_cycle]:
        await _try_auto_trade_entry(r)

    free_slots = max(0, screener_config.top_n - len(state["pending_orders"]))
    cap = min(screener_config.max_auto_drafts_per_cycle, free_slots)
    new_signals = new_signals[:cap]
    positions = await broker.get_positions()
    drafted = [p for p in (_draft_order_from_signal(r, positions) for r in new_signals) if p is not None]

    await _broadcast({
        "type": "signal_alert",
        "new_signals": [r.model_dump() for r in new_signals],
        "drafted_orders": [p.model_dump() for p in drafted],
    })


async def _signal_scan_loop() -> None:
    """Escaneo proactivo en background: a diferencia de /api/signals/scan (que
    solo corre cuando alguien abre el dashboard), este loop corre solo cada
    auto_scan_interval_minutes."""
    while True:
        await asyncio.sleep(screener_config.auto_scan_interval_minutes * 60)
        await _run_signal_scan_cycle()


async def _risk_monitor_loop() -> None:
    """Kill switch automatico: a diferencia de RulesEngine.evaluate(), que solo
    chequea daily_loss_limit_pct cuando llega una orden nueva, esto corre en
    background y pausa el trading aunque no se envie ninguna orden mientras la
    cuenta sigue perdiendo (ej. por posiciones abiertas moviendose en contra)."""
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        if not state["connected"] or state["halted"]:
            continue
        try:
            account = broker.get_account_summary()
        except Exception:
            continue
        if account.daily_pnl_pct <= -abs(rules_config.daily_loss_limit_pct):
            state["halted"] = True
            _persist_state()
            audit.record("auto_halt_daily_loss_limit", {}, {"daily_pnl_pct": account.daily_pnl_pct})
            print(
                f"[KILL SWITCH] Perdida diaria {account.daily_pnl_pct:.2f}% "
                "alcanzo el limite. Trading pausado automaticamente."
            )


async def _check_fund_exit(fund_id: str, symbol: str) -> None:
    """Evalua si una posicion abierta por el motor de auto-trading debe
    cerrarse, y si corresponde la vende entera (siempre atada a ese fund_id).

    Tres motivos posibles, en este orden:
    1. Reconciliacion: si la cantidad real en el broker es menor a la que
       registra el ledger del fondo, el stop-loss que se coloco como orden
       bracket al abrir la posicion (ver broker.place_order) ya se ejecuto
       del lado de IBKR sin pasar por record_fill. Se reconcilia la
       diferencia para que el fondo no quede con una posicion fantasma. El
       precio exacto del fill no esta disponible sin consultar el historial
       de ejecuciones de IBKR; se aproxima con el stop_loss_price registrado
       al abrir la posicion (misma simplificacion ya documentada en el resto
       del ledger de fondos).
    2. max_holding_days: misma regla que ya se simula en backtest.py, ahora
       aplicada en vivo sobre la fecha real de apertura.
    3. trend_break: el precio cierra por debajo de la SMA rapida del
       screener, igual que en backtest.py.
    """
    async with _funds_order_lock:
        fund = funds_store.get(fund_id)
        if fund is None:
            return
        position = fund.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return

        broker_qty = broker.get_position_qty(symbol)
        if broker_qty < position.quantity:
            closed_qty = position.quantity - max(broker_qty, 0.0)
            fill_price = position.stop_loss_price or position.avg_cost
            funds_store.record_fill(fund_id, symbol, Side.SELL, closed_qty, fill_price)
            audit.record(
                "auto_trade_stop_loss_reconciled",
                {"fund_id": fund_id, "symbol": symbol},
                {"quantity": closed_qty, "price": fill_price},
            )
            fund = funds_store.get(fund_id)
            position = fund.positions.get(symbol) if fund else None
            if position is None or position.quantity <= 0:
                return

        held_days = (datetime.now(timezone.utc) - position.opened_at).days if position.opened_at else 0
        timed_out = held_days >= screener_config.max_holding_days

        trend_broke = False
        try:
            bars = await asyncio.to_thread(get_daily_bars, symbol, screener_config.sma_fast + 5)
            sma_fast_s = sma(bars["Close"], screener_config.sma_fast)
            if len(sma_fast_s) and not bool(sma_fast_s.isna().iloc[-1]):
                trend_broke = float(bars["Close"].iloc[-1]) < float(sma_fast_s.iloc[-1])
        except MarketDataError:
            pass

        if not (timed_out or trend_broke):
            return

        reference_price = await broker.get_reference_price(symbol)
        if not reference_price:
            return

        order = OrderRequest(
            symbol=symbol, side=Side.SELL, quantity=position.quantity, order_type=OrderType.MKT, fund_id=fund_id
        )
        try:
            result_payload = await broker.place_order(order)
        except StopLossRejectedError as exc:
            audit.record("auto_trade_exit_failed", order.model_dump(), {"error": str(exc)})
            return

        funds_store.record_fill(fund_id, symbol, Side.SELL, position.quantity, reference_price)
        reason = "max_holding_days" if timed_out else "trend_break"
        audit.record("auto_trade_exit", order.model_dump(), {"reason": reason, **result_payload})
    await _broadcast({"type": "auto_trade_exit", "fund_id": fund_id, "symbol": symbol, "reason": reason})


async def _run_auto_exit_monitor_cycle() -> None:
    """Revisa, para cada fondo con auto-trading activado, sus posiciones
    abiertas y las cierra si corresponde (ver _check_fund_exit). Solo en modo
    paper y con la cuenta conectada y sin halt, igual que la entrada
    automatica."""
    if state["mode"] != "paper" or state["halted"] or not state["connected"]:
        return
    for fund in funds_store.list():
        if not fund.auto_trading_enabled:
            continue
        for symbol in list(fund.positions.keys()):
            try:
                await _check_fund_exit(fund.id, symbol)
            except Exception as exc:
                audit.record("auto_trade_exit_check_failed", {"fund_id": fund.id, "symbol": symbol}, {"error": str(exc)})


async def _auto_exit_monitor_loop() -> None:
    """Monitoreo de salida en background, a la misma cadencia que el escaneo
    proactivo de entradas: las señales (trend-break, max-holding-days) se
    basan en cierres diarios, asi que chequear mas seguido no aporta nada y
    solo gastaria cuota de la API de datos de mercado."""
    while True:
        await asyncio.sleep(screener_config.auto_scan_interval_minutes * 60)
        await _run_auto_exit_monitor_cycle()


async def _restore_persisted_mode() -> None:
    """Si el estado persistido indica un modo distinto al que arranco el
    broker (ej. el backend se reinicio mientras estaba en modo live), reconecta
    al puerto correspondiente para que state['mode'] no mienta sobre a que
    cuenta esta conectado realmente el broker."""
    if not state["connected"] or state["mode"] == settings.trading_mode:
        return
    if state["mode"] == "live" and not settings.live_confirm:
        print(
            "[WARN] El estado persistido indica modo live pero LIVE_CONFIRM no "
            "esta definido en este arranque. Se mantiene modo paper."
        )
        state["mode"] = settings.trading_mode
        _persist_state()
        return
    target_port = settings.ib_port_live if state["mode"] == "live" else settings.ib_port_paper
    if target_port is None:
        state["mode"] = settings.trading_mode
        _persist_state()
        return
    try:
        await broker.reconnect(settings.ib_host, target_port, settings.ib_client_id)
        state["connected"] = True
        print(f"[INFO] Modo restaurado desde estado persistido: {state['mode']}")
    except IBKRConnectionError as exc:
        state["connected"] = False
        state["mode"] = settings.trading_mode
        _persist_state()
        print(f"[WARN] No se pudo restaurar el modo persistido tras el reinicio: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await broker.connect()
        state["connected"] = True
    except IBKRConnectionError as exc:
        state["connected"] = False
        print(f"[WARN] {exc}")
    await _restore_persisted_mode()
    task = asyncio.create_task(_broadcast_loop())
    risk_task = asyncio.create_task(_risk_monitor_loop())
    signal_task = asyncio.create_task(_signal_scan_loop())
    exit_monitor_task = asyncio.create_task(_auto_exit_monitor_loop())
    yield
    task.cancel()
    risk_task.cancel()
    signal_task.cancel()
    exit_monitor_task.cancel()
    broker.disconnect()


app = FastAPI(title="IBKR Trading Dashboard", lifespan=lifespan)
# Solo se habilita CORS si se configuraron origenes explicitos (ALLOWED_ORIGINS
# en el .env). Por defecto la lista esta vacia y no se agrega el middleware: el
# dashboard se sirve desde el mismo origen que la API, asi que no necesita CORS,
# y antes "*" dejaba que cualquier sitio web hiciera requests al backend.
if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/status")
def status():
    return {
        "mode": state["mode"],
        "connected": state["connected"],
        "halted": state["halted"],
        "ib_host": broker.host,
        "ib_port": broker.port,
    }


@app.post("/api/halt")
def set_halt(value: bool, _: None = Depends(require_api_key)):
    state["halted"] = value
    _persist_state()
    audit.record("halt_toggle", {"value": value}, {})
    return {"halted": state["halted"]}


class ModeUpdate(BaseModel):
    mode: str


@app.post("/api/mode")
async def set_mode(body: ModeUpdate, _: None = Depends(require_api_key)):
    """Cambia entre paper y live reconectando a IBKR en el puerto correspondiente.

    Requiere que el backend haya arrancado con LIVE_CONFIRM definido en el
    .env: ese flag se sigue pidiendo una sola vez, al desplegar el backend,
    para habilitar la posibilidad de operar en live en este servidor. Una vez
    habilitado, este endpoint permite alternar entre paper y live sin
    reiniciar. Por seguridad, cada cambio de modo deja el trading pausado
    (kill switch) hasta que se reanude manualmente desde el dashboard.
    """
    mode = body.mode.strip().lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=422, detail="mode debe ser 'paper' o 'live'.")
    if mode == state["mode"]:
        return {"mode": state["mode"], "connected": state["connected"], "halted": state["halted"]}

    if mode == "live" and not settings.live_confirm:
        raise HTTPException(
            status_code=403,
            detail=(
                "Este servidor no tiene habilitado el modo live. Define "
                "LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY en el .env y "
                "reinicia el backend una vez para habilitarlo; despues vas a "
                "poder alternar entre paper y live desde este boton sin reiniciar."
            ),
        )

    target_port = settings.ib_port_live if mode == "live" else settings.ib_port_paper
    if target_port is None:
        raise HTTPException(
            status_code=422,
            detail="Define IB_PORT_LIVE en el .env (puerto de tu cuenta live en TWS/IB Gateway) antes de activar modo live.",
        )

    try:
        await broker.reconnect(settings.ib_host, target_port, settings.ib_client_id)
    except IBKRConnectionError as exc:
        state["connected"] = False
        audit.record("mode_switch_failed", {"target_mode": mode}, {"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"No se pudo conectar en modo {mode}: {exc}")

    state["mode"] = mode
    state["connected"] = True
    state["halted"] = True
    _persist_state()
    audit.record("mode_switched", {"mode": mode}, {"ib_port": target_port})
    return {"mode": state["mode"], "connected": state["connected"], "halted": state["halted"]}


@app.get("/api/account")
def get_account(_: None = Depends(require_api_key)):
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    return broker.get_account_summary()


@app.get("/api/positions")
async def get_positions(_: None = Depends(require_api_key)):
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    return await broker.get_positions()


@app.get("/api/rules")
def get_rules(_: None = Depends(require_api_key)):
    return rules_config.model_dump()


class RulesUpdate(BaseModel):
    rules: dict


@app.put("/api/rules")
def update_rules(body: RulesUpdate, _: None = Depends(require_api_key)):
    global rules_config
    try:
        new_config = RulesConfig(**body.rules)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    rules_config = new_config
    rules_config.save(settings.rules_path)
    rules_engine.reload(rules_config)
    audit.record("rules_updated", body.rules, {})
    return rules_config.model_dump()


@app.get("/api/audit")
def get_audit(limit: int = 100, _: None = Depends(require_api_key)):
    return audit.recent(limit)


@app.get("/api/strategies")
def list_strategies(_: None = Depends(require_api_key)):
    """Metadata de las estrategias disponibles, para el selector del
    dashboard: id/name para mostrar, supports_backtest para saber si ofrecer
    el boton de backtest o no (Largo plazo y Dividendos no lo soportan)."""
    return [
        {"id": s.id, "name": s.name, "supports_backtest": s.supports_backtest}
        for s in strategy_registry.values()
    ]


@app.get("/api/signals/config")
def get_screener_config(_: None = Depends(require_api_key)):
    return screener_config.model_dump()


class ScreenerUpdate(BaseModel):
    config: dict


@app.put("/api/signals/config")
def update_screener_config(body: ScreenerUpdate, _: None = Depends(require_api_key)):
    global screener_config
    try:
        new_config = ScreenerConfig(**body.config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    screener_config = new_config
    screener_config.save(settings.screener_path)
    reload_strategy_registry(strategy_registry, screener_config)
    _sync_whitelist_with_universe()
    # Tras un cambio manual de config, los filtros pudieron cambiar por
    # completo: se descarta la base de simbolos "pasando" para que el proximo
    # ciclo del scan proactivo no trate la config nueva como transiciones
    # reales (vuelve a ser un primer ciclo, solo establece base).
    _signal_state["previously_passing"] = None
    audit.record("screener_config_updated", body.config, {})
    return screener_config.model_dump()


async def _get_or_scan(strategy_id: str, force: bool) -> tuple[datetime, bool, list[dict]]:
    """Resultados de strategy.scan() para `strategy_id`: desde signal_cache si
    esta fresco, corriendo el scan si no. Comun a /api/signals/scan y
    /api/signals/scan/all para que ambos compartan el mismo cache por
    estrategia (ver SIGNAL_CACHE_TTL_SECONDS) en vez de pagar la cuota de la
    API de datos dos veces por lo mismo."""
    now = datetime.now(timezone.utc)
    cached = signal_cache.get(strategy_id)
    if not force and cached and (now - cached["as_of"]).total_seconds() < SIGNAL_CACHE_TTL_SECONDS:
        return cached["as_of"], True, cached["results"]
    strategy = strategy_registry[strategy_id]
    async with _market_scan_lock:
        results = await asyncio.to_thread(strategy.scan, force=force)
    signal_cache[strategy_id] = {"as_of": now, "results": [r.model_dump() for r in results]}
    return now, False, signal_cache[strategy_id]["results"]


@app.get("/api/signals/scan")
async def scan_signals(force: bool = False, strategy_id: str | None = None, _: None = Depends(require_api_key)):
    """Radar de oportunidades. No es una recomendacion de inversion ni ejecuta
    nada: solo rankea candidatos del universo configurado en screener.yaml
    segun la estrategia activa (`strategy_id`, default la persistida en
    screener_config). Cacheado por estrategia para no agotar la cuota de la
    API gratuita de datos en cada refresh del dashboard.

    Requiere API key: aunque no mueve dinero, escanear (sobre todo con
    force=true) golpea la API gratuita de datos para todo el universo, asi que
    dejarlo abierto seria un vector de DoS / de agotar la cuota.

    async + asyncio.to_thread (en vez de un def sincrono comun): con el
    universo del S&P 500 completo un scan tarda varios minutos, y un endpoint
    sincrono ocuparia ese tiempo un thread del pool compartido por TODOS los
    demas endpoints de la API, pudiendo demorar pedidos no relacionados. El
    lock evita que un scan se cruce con un backtest o con el ciclo proactivo
    en background, que pegan a la misma API de datos."""
    resolved_id = strategy_id or screener_config.strategy_id
    if resolved_id not in strategy_registry:
        raise HTTPException(status_code=422, detail=f"strategy_id desconocido: {resolved_id}")
    try:
        as_of, cached, results = await _get_or_scan(resolved_id, force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al escanear el mercado: {exc}")
    return {"as_of": as_of, "cached": cached, "results": results}


@app.get("/api/signals/scan/all")
async def scan_signals_all_strategies(force: bool = False, _: None = Depends(require_api_key)):
    """Como /api/signals/scan pero corre TODAS las estrategias registradas
    sobre el mismo universo y devuelve, por simbolo, el score que le dio cada
    una. Pensado para el radar: el mismo candidato puede rankear distinto en
    Momentum, Oportunista, Largo plazo y Dividendos, y comparar eso lado a
    lado es mas util que tener que cambiar de estrategia una por una.

    Comparte signal_cache (por estrategia) con /api/signals/scan via
    _get_or_scan: si ya escaneaste alguna estrategia hace poco, esta no la
    vuelve a correr."""
    now = datetime.now(timezone.utc)
    merged: dict[str, dict] = {}
    for strategy_id in strategy_registry:
        try:
            _, _, results = await _get_or_scan(strategy_id, force)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Error al escanear el mercado ({strategy_id}): {exc}")
        for r in results:
            entry = merged.setdefault(r["symbol"], {"symbol": r["symbol"], "sector": r.get("sector"), "scores": {}})
            entry["scores"][strategy_id] = r["score"]
            if entry["sector"] is None and r.get("sector") is not None:
                entry["sector"] = r["sector"]
    return {"as_of": now, "results": list(merged.values())}


class SectorRefreshRequest(BaseModel):
    symbols: Optional[list[str]] = None


@app.post("/api/sectors/refresh")
async def refresh_sectors(body: SectorRefreshRequest, _: None = Depends(require_api_key)):
    """Resuelve en vivo (yfinance) el sector de los simbolos sin clasificar
    (ver app/sectors.py: get_sector()/refresh_sector()). Pensado para usarse
    despues de agregar un ticker nuevo al universo desde el dashboard, ya que
    el mapeo estatico TICKER_SECTOR no lo va a tener todavia.

    Sin `symbols` en el body, refresca el universo configurado completo (solo
    los simbolos sin sector conocido; los ya clasificados no se re-consultan).

    Requiere API key y comparte _market_scan_lock con scan_signals/backtest:
    golpea la misma API de datos de terceros por simbolo."""
    symbols = body.symbols if body.symbols is not None else screener_config.universe
    targets = sorted({s.upper() for s in symbols if get_sector(s) is None})
    resolved: dict[str, Optional[str]] = {}
    async with _market_scan_lock:
        for symbol in targets:
            resolved[symbol] = await asyncio.to_thread(refresh_sector, symbol)
    return {
        "resolved": resolved,
        "unresolved": [s for s, sector in resolved.items() if sector is None],
    }


_BACKTEST_RUNNERS = {
    "momentum": run_backtest,
    "opportunistic": run_opportunistic_backtest,
}


@app.get("/api/signals/backtest")
async def backtest_strategy(strategy_id: str | None = None, _: None = Depends(require_api_key)):
    """Backtest simplificado sobre el universo configurado, de la estrategia
    indicada (default la persistida en screener_config). Ver docstring de
    run_backtest()/run_opportunistic_backtest() para las simplificaciones
    asumidas (sin comisiones/slippage, curva de equity aproximada).

    Largo plazo y Dividendos NO son backtesteables (supports_backtest=False):
    sin historia point-in-time de fundamentales en yfinance gratuito no hay
    forma de simular sus filtros en el pasado sin inventar datos.

    Requiere API key: es la operacion mas pesada del backend (descarga anos de
    historia de todo el universo), dejarla abierta seria un vector de DoS.
    async + asyncio.to_thread + lock por el mismo motivo que scan_signals."""
    resolved_id = strategy_id or screener_config.strategy_id
    strategy = strategy_registry.get(resolved_id)
    if strategy is None:
        raise HTTPException(status_code=422, detail=f"strategy_id desconocido: {resolved_id}")
    if not strategy.supports_backtest:
        raise HTTPException(
            status_code=422,
            detail=(
                f"La estrategia '{strategy.name}' no soporta backtest: no hay historia "
                "point-in-time de sus datos fundamentales disponible en la fuente de "
                "datos gratuita. Solo esta disponible para escaneo en vivo."
            ),
        )
    runner = _BACKTEST_RUNNERS[resolved_id]
    try:
        async with _market_scan_lock:
            return await asyncio.to_thread(runner, screener_config)
    except BacktestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orders/size-suggestion")
def order_size_suggestion(
    symbol: str,
    entry_price: float,
    stop_loss_price: float,
    fund_id: str | None = None,
    _: None = Depends(require_api_key),
):
    """Sugiere una cantidad para una compra en base al riesgo (ver
    RulesEngine.suggested_quantity). No es una orden ni se aplica sola: el
    usuario la ve en el ticket de orden y puede ajustarla antes de enviar, y
    de todas formas pasa por rules_engine.evaluate() al enviarse como
    cualquier otra orden.

    Si se pasa fund_id, el sizing se dimensiona contra el equity_estimate()
    de ese fondo en vez del equity de toda la cuenta de IBKR -- mismo motivo
    que en _try_auto_trade_entry."""
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    position_qty = broker.get_position_qty(symbol)
    if fund_id:
        fund = funds_store.get(fund_id)
        if fund is None:
            raise HTTPException(status_code=404, detail="Fondo no encontrado.")
        equity = fund.equity_estimate()
    else:
        equity = broker.get_account_summary().net_liquidation
    return rules_engine.suggested_quantity(equity, position_qty, entry_price, stop_loss_price)


@app.get("/api/orders/pending")
def list_pending_orders(_: None = Depends(require_api_key)):
    return list(state["pending_orders"].values())


def _validate_fund_order(order: OrderRequest, reference_price: float) -> None:
    """Valida una orden atada a un fondo ANTES de que llegue al RulesEngine ni
    al broker. Esta es la capa que protege holdings que no pertenecen al
    fondo: una venta nunca puede superar lo que el ledger del fondo dice que
    posee de ese simbolo, sin importar cuanto haya realmente en la cuenta de
    IBKR (que puede incluir posiciones preexistentes o de otro fondo)."""
    fund = funds_store.get(order.fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    if order.side == Side.SELL:
        owned = fund.owned_quantity(order.symbol)
        if order.quantity > owned:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"El fondo '{fund.name}' solo tiene {owned:g} de {order.symbol} "
                    "registradas en su ledger. No se puede vender mas de lo que el "
                    "fondo posee (esto protege holdings que no pertenecen a este fondo)."
                ),
            )
    else:
        estimated_cost = reference_price * order.quantity
        if not fund.can_afford(estimated_cost):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"El fondo '{fund.name}' tiene ${fund.cash_usd:,.2f} disponibles, "
                    f"insuficiente para esta compra (estimado ${estimated_cost:,.2f})."
                ),
            )


@app.post("/api/orders")
async def submit_order(order: OrderRequest, _: None = Depends(require_api_key)):
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")

    account_summary = broker.get_account_summary()
    position_qty = broker.get_position_qty(order.symbol)
    reference_price = order.limit_price
    if reference_price is None:
        reference_price = await broker.get_reference_price(order.symbol)
    if not reference_price:
        raise HTTPException(
            status_code=422,
            detail="No se pudo obtener un precio de referencia para validar la orden. Usa una orden LMT con precio definido.",
        )

    async with _funds_order_lock:
        if order.fund_id:
            _validate_fund_order(order, reference_price)

        trades_today = audit.count_trades_today(rules_config.trading_hours_timezone)

        decision = rules_engine.evaluate(
            order=order,
            account=account_summary,
            current_position_qty=position_qty,
            reference_price=reference_price,
            trades_today=trades_today,
            halted=state["halted"],
            order_sector=get_sector(order.symbol),
            sector_exposure_usd=_compute_sector_exposure(await broker.get_positions(), order.symbol),
        )

        audit.record("order_submitted", order.model_dump(), decision.model_dump())

        if not decision.approved:
            raise HTTPException(status_code=422, detail=decision.model_dump())

        if decision.requires_manual_approval:
            pending_id = str(uuid.uuid4())
            pending = PendingOrder(
                id=pending_id,
                order=order,
                decision=decision,
                created_at=datetime.now(timezone.utc),
            )
            state["pending_orders"][pending_id] = pending
            _persist_state()
            audit.record("order_pending_approval", order.model_dump(), {"id": pending_id})
            return {"status": "pending_approval", "pending_order": pending.model_dump()}

        try:
            result = await broker.place_order(order)
        except StopLossRejectedError as exc:
            audit.record("stop_loss_rejected", order.model_dump(), {"error": str(exc)})
            raise HTTPException(status_code=502, detail=str(exc))
        audit.record("order_executed", order.model_dump(), result)
        if order.fund_id:
            # reference_price ya se uso para validar/sizear la orden: se reusa como
            # aproximacion del fill (place_order() no espera ni devuelve el fill
            # real de IBKR hoy). Documentado como simplificacion, igual que en backtest.py.
            funds_store.record_fill(order.fund_id, order.symbol, order.side, order.quantity, reference_price)
        return {"status": "executed", "result": result}


@app.post("/api/orders/{order_id}/approve")
async def approve_order(order_id: str, _: None = Depends(require_api_key)):
    pending = state["pending_orders"].pop(order_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Orden pendiente no encontrada.")
    _persist_state()
    async with _funds_order_lock:
        try:
            result = await broker.place_order(pending.order)
        except StopLossRejectedError as exc:
            audit.record("stop_loss_rejected", pending.order.model_dump(), {"error": str(exc)})
            raise HTTPException(status_code=502, detail=str(exc))
        audit.record("order_executed_after_approval", pending.order.model_dump(), result)
        if pending.order.fund_id:
            fill_price = pending.order.limit_price
            if fill_price is None:
                fill_price = await broker.get_reference_price(pending.order.symbol)
            if fill_price:
                funds_store.record_fill(
                    pending.order.fund_id, pending.order.symbol, pending.order.side, pending.order.quantity, fill_price
                )
        return {"status": "executed", "result": result}


@app.post("/api/orders/{order_id}/reject")
def reject_order(order_id: str, _: None = Depends(require_api_key)):
    pending = state["pending_orders"].pop(order_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Orden pendiente no encontrada.")
    _persist_state()
    audit.record("order_rejected", pending.order.model_dump(), {"id": order_id})
    return {"status": "rejected"}


class FundCreate(BaseModel):
    name: str
    initial_capital_usd: float
    auto_trading_enabled: bool = False


def _fund_view(fund) -> dict:
    return {
        **fund.model_dump(),
        "realized_pnl_total": round(fund.realized_pnl_total(), 2),
        "net_contributed_capital": round(fund.net_contributed_capital(), 2),
    }


def _validate_capital_allocation(
    amount: float, current_fund_cash: float = 0.0, exclude_fund_id: str | None = None
) -> None:
    """Valida que asignarle `amount` adicional a un fondo no haga que la suma
    de cash_usd de todos los fondos supere el cash real de la cuenta de
    IBKR. Sin esto, la separacion entre fondos seria una ilusion: un fondo
    podria "creer" que tiene plata que en realidad ya esta asignada a otro
    fondo o no existe en la cuenta real."""
    real_cash = broker.get_account_summary().cash
    already_allocated = funds_store.total_allocated_cash(exclude_fund_id=exclude_fund_id)
    if already_allocated + current_fund_cash + amount > real_cash:
        raise HTTPException(
            status_code=422,
            detail=(
                f"La cuenta de IBKR tiene ${real_cash:,.2f} de cash real, de los cuales "
                f"${already_allocated + current_fund_cash:,.2f} ya estan asignados a fondos. "
                f"No se puede asignar ${amount:,.2f} mas sin superar el cash real disponible."
            ),
        )


@app.get("/api/funds")
def list_funds(_: None = Depends(require_api_key)):
    return [_fund_view(f) for f in funds_store.list()]


@app.post("/api/funds")
def create_fund(body: FundCreate, _: None = Depends(require_api_key)):
    """Crea un fondo: una porcion de capital con su propia contabilidad
    (cash_usd, posiciones, PnL realizado), separada de la cuenta consolidada
    de IBKR y de cualquier otro fondo. Ver funds.py para el detalle del
    ledger y de por que una venta atada a un fondo nunca puede tocar
    holdings que no se registraron en el."""
    if body.initial_capital_usd <= 0:
        raise HTTPException(status_code=422, detail="initial_capital_usd debe ser mayor a 0.")
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    _validate_capital_allocation(body.initial_capital_usd)
    fund = funds_store.create(body.name.strip(), body.initial_capital_usd, body.auto_trading_enabled)
    audit.record("fund_created", body.model_dump(), {"id": fund.id})
    return _fund_view(fund)


_EMPTY_ROI_HISTORY = {
    "dates": [],
    "fund_cumulative_return_pct": [],
    "benchmark_cumulative_return_pct": [],
}


def _compute_roi_history(funds: list) -> dict:
    """Compara el retorno acumulado (time-weighted) del capital combinado de
    TODOS los fondos contra el del S&P 500 (SPY) en la misma ventana, para
    responder "le estoy ganando al mercado". Se mide combinado (no fondo por
    fondo) porque lo que importa para esa pregunta es el capital total que el
    usuario le asigno a esta herramienta, no como se reparte entre fondos.

    Es time-weighted (no dollar-weighted como `net_contributed_capital`/ROI de
    cada fondo individual en `_fund_view`): un aporte o retiro no debe inflar
    ni desinflar la curva solo por su timing, o se estaria confundiendo
    timing de cash-flow con habilidad de inversion. El precio de cierre de
    SPY del dia de cada flujo/fill es la unica fuente de calendario de
    trading: si no esta disponible, no hay nada confiable contra que
    comparar, asi que se devuelve la forma vacia en vez de inventar fechas.
    """
    all_flows = [(f.created_at, f) for fund in funds for f in fund.capital_flows]
    if not all_flows:
        return dict(_EMPTY_ROI_HISTORY)

    start_date = min(created_at for created_at, _ in all_flows).date()
    today = datetime.now(timezone.utc).date()
    lookback_days = max((today - start_date).days + 15, 15)

    try:
        bench_bars = get_daily_bars("SPY", lookback_days)
    except MarketDataError:
        return dict(_EMPTY_ROI_HISTORY)

    calendar_index = bench_bars.index[bench_bars.index.date >= start_date]
    if len(calendar_index) == 0:
        return dict(_EMPTY_ROI_HISTORY)
    bench_close = bench_bars["Close"].reindex(calendar_index)

    symbols = {t.symbol for fund in funds for t in fund.trades}
    symbol_close: dict[str, "pd.Series | None"] = {}
    last_trade_price: dict[str, float] = {}
    for symbol in symbols:
        try:
            bars = get_daily_bars(symbol, lookback_days)
            symbol_close[symbol] = bars["Close"].reindex(calendar_index).ffill()
        except MarketDataError:
            symbol_close[symbol] = None

    # Eventos (flujos de capital + fills de TODOS los fondos) agrupados por
    # dia de calendario de trading: se aplican todos los de un mismo dia
    # antes de marcar a mercado ese dia, sin importar de que fondo vinieron
    # (la curva combina el capital de todos como si fuera uno solo).
    events_by_day: dict = {}
    for fund in funds:
        for flow in fund.capital_flows:
            events_by_day.setdefault(flow.created_at.date(), []).append(("flow", flow.amount))
        for trade in fund.trades:
            events_by_day.setdefault(trade.executed_at.date(), []).append(
                ("trade", trade.side, trade.symbol, trade.quantity, trade.price)
            )

    cash = 0.0
    positions: dict[str, float] = {}
    dates: list[str] = []
    fund_cum_pct: list[float] = []
    bench_cum_pct: list[float] = []
    cum = 0.0
    prev_equity = None
    bench_start_close = None

    for day_ts in calendar_index:
        day = day_ts.date()
        net_flow = 0.0
        for event in events_by_day.get(day, []):
            if event[0] == "flow":
                amount = event[1]
                cash += amount
                net_flow += amount
            else:
                _, side, symbol, quantity, price = event
                if side == Side.BUY:
                    cash -= quantity * price
                    positions[symbol] = positions.get(symbol, 0.0) + quantity
                else:
                    cash += quantity * price
                    positions[symbol] = positions.get(symbol, 0.0) - quantity
                last_trade_price[symbol] = price

        positions_value = 0.0
        for symbol, qty in positions.items():
            close_series = symbol_close.get(symbol)
            price = None
            if close_series is not None:
                val = close_series.get(day_ts)
                if val is not None and not pd.isna(val):
                    price = float(val)
            if price is None:
                price = last_trade_price.get(symbol, 0.0)
            positions_value += qty * price
        equity = cash + positions_value

        if prev_equity is None:
            if equity > 0:
                prev_equity = equity
                cum = 0.0
            else:
                continue  # sin equity todavia: no emitir un 0% ficticio
        else:
            r_t = (equity - net_flow - prev_equity) / prev_equity if prev_equity > 0 else 0.0
            cum = (1 + cum) * (1 + r_t) - 1
            prev_equity = equity

        bench_close_t = bench_close.get(day_ts)
        if bench_close_t is None or pd.isna(bench_close_t):
            continue
        if bench_start_close is None:
            bench_start_close = float(bench_close_t)
        bench_cum = bench_close_t / bench_start_close - 1

        dates.append(day.isoformat())
        fund_cum_pct.append(round(cum * 100, 2))
        bench_cum_pct.append(round(bench_cum * 100, 2))

    if not dates:
        return dict(_EMPTY_ROI_HISTORY)

    return {
        "dates": dates,
        "fund_cumulative_return_pct": fund_cum_pct,
        "benchmark_cumulative_return_pct": bench_cum_pct,
    }


@app.get("/api/funds/roi-history")
def get_funds_roi_history(_: None = Depends(require_api_key)):
    """Retorno acumulado time-weighted del capital combinado de todos los
    fondos vs. el S&P 500 (SPY) en la misma ventana -- ver _compute_roi_history
    para el detalle de por que es time-weighted y no dollar-weighted como el
    ROI por fondo de `_fund_view`. Es aparte del bar chart de ROI actual por
    fondo (ese es un snapshot del momento, este es una serie historica)."""
    return _compute_roi_history(funds_store.list())


# IMPORTANTE: esta ruta con parametro dinamico {fund_id} debe registrarse
# DESPUES de "/api/funds/roi-history" (arriba) -- FastAPI/Starlette matchea
# rutas en orden de registro, asi que si quedara antes capturaria
# "roi-history" como un fund_id literal y la ruta de mas arriba nunca se
# alcanzaria (404 enmascarado como "fondo no encontrado").
@app.get("/api/funds/{fund_id}")
def get_fund(fund_id: str, _: None = Depends(require_api_key)):
    fund = funds_store.get(fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    return _fund_view(fund)


class CapitalFlowCreate(BaseModel):
    amount: float
    note: Optional[str] = None


@app.post("/api/funds/{fund_id}/capital-flows")
def create_capital_flow(fund_id: str, body: CapitalFlowCreate, _: None = Depends(require_api_key)):
    """Aporta (amount > 0) o retira (amount < 0) capital virtual de un fondo
    ya existente -- mismas validaciones que la creacion (ver
    _validate_capital_allocation), mas el chequeo de que un retiro no deje
    cash_usd negativo (no se puede retirar plata que esta en posiciones
    abiertas; hay que vender primero)."""
    fund = funds_store.get(fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    if body.amount == 0:
        raise HTTPException(status_code=422, detail="El monto no puede ser cero.")
    if body.amount < 0:
        if -body.amount > fund.cash_usd:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"El fondo '{fund.name}' solo tiene ${fund.cash_usd:,.2f} de cash "
                    "disponibles para retirar (no se puede retirar plata que esta en "
                    "posiciones abiertas; vende primero)."
                ),
            )
    else:
        if not state["connected"]:
            raise HTTPException(status_code=503, detail="No conectado a IBKR.")
        _validate_capital_allocation(body.amount, current_fund_cash=fund.cash_usd, exclude_fund_id=fund_id)
    funds_store.apply_capital_flow(fund_id, body.amount, body.note)
    audit.record("fund_capital_flow", {"fund_id": fund_id, **body.model_dump()}, {})
    return _fund_view(funds_store.get(fund_id))


class FundAutoTradingUpdate(BaseModel):
    enabled: bool


@app.put("/api/funds/{fund_id}/auto-trading")
def set_fund_auto_trading(fund_id: str, body: FundAutoTradingUpdate, _: None = Depends(require_api_key)):
    """Prende/apaga el toggle de auto-trading del fondo. Con enabled=true, el
    motor proactivo (ver _try_auto_trade_entry / _check_fund_exit en este
    mismo modulo) compra y vende dentro de este fondo sin aprobacion manual,
    pero solo en modo paper (ver README)."""
    fund = funds_store.set_auto_trading(fund_id, body.enabled)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    audit.record("fund_auto_trading_toggled", {"fund_id": fund_id, "enabled": body.enabled}, {})
    return _fund_view(fund)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, api_key: str = ""):
    # Un navegador no puede mandar headers personalizados en el handshake de
    # un WebSocket, asi que la API key viaja como query param (?api_key=...)
    # en vez del header X-API-Key que usa el resto de los endpoints.
    if not api_key or not secrets.compare_digest(api_key, settings.api_key):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.remove(websocket)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
