from __future__ import annotations

import asyncio
import math
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audit import AuditLog
from .backtest import BacktestError, run_backtest
from .broker import IBKRBroker, IBKRConnectionError, StopLossRejectedError
from .config import settings
from .funds import FundsStore
from .indicators import sma
from .market_data import MarketDataError, get_daily_bars
from .models import OrderRequest, OrderType, PendingOrder, SignalResult, Side
from .rules import RulesConfig, RulesEngine
from .screener import MomentumScreener
from .screener_config import ScreenerConfig
from .state_store import load_state, save_state

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

rules_config = RulesConfig.load(settings.rules_path)
rules_engine = RulesEngine(rules_config)
audit = AuditLog(settings.audit_db_path)
broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)

screener_config = ScreenerConfig.load(settings.screener_path)
screener = MomentumScreener(screener_config)


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
signal_cache: dict = {"as_of": None, "results": []}

# Serializa scan y backtest (manuales y el ciclo proactivo en background):
# ambos golpean la misma API gratuita de datos para todo el universo
# configurado, y dejarlos correr en paralelo (ej. alguien pide un backtest
# mientras el scan proactivo esta en ciclo) duplica la tasa de pedidos y
# aumenta el riesgo de bloqueo temporal de la API. No hace falta mas que un
# lock simple: esto no es trafico de alta concurrencia.
_market_scan_lock = asyncio.Lock()

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


def _draft_order_from_signal(result: SignalResult) -> PendingOrder | None:
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
        account_summary, position_qty, result.last_price, result.suggested_stop_loss_price
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
    audit.record("signal_order_drafted", order.model_dump(), {"id": pending_id, "score": result.score})
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
    candidates = [f for f in funds_store.list() if f.auto_trading_enabled and f.owned_quantity(symbol) == 0]
    if not candidates:
        return

    account_summary = broker.get_account_summary()
    position_qty = broker.get_position_qty(symbol)
    sizing = rules_engine.suggested_quantity(
        account_summary, position_qty, result.last_price, result.suggested_stop_loss_price
    )
    if sizing.quantity <= 0:
        return

    fund = None
    quantity = 0.0
    for candidate in candidates:
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
    audit.record("auto_trade_executed", order.model_dump(), {"fund_id": fund.id, **result_payload})


async def _run_signal_scan_cycle() -> None:
    """Un ciclo del escaneo proactivo: corre el screener, detecta simbolos que
    recien empiezan a pasar los filtros (transicion no-pasa -> pasa) y les
    arma una orden de compra en borrador (ver _draft_order_from_signal).

    Separado de _signal_scan_loop (que solo aporta el sleep + while True) para
    poder testear un ciclo de una sola vez sin lidiar con un loop infinito.
    """
    if not screener_config.auto_scan_enabled or state["halted"] or not state["connected"]:
        return
    try:
        async with _market_scan_lock:
            results = await asyncio.to_thread(screener.scan)
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
    drafted = [p for p in (_draft_order_from_signal(r) for r in new_signals) if p is not None]

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
    rules_config = RulesConfig(**body.rules)
    rules_config.save(settings.rules_path)
    rules_engine.reload(rules_config)
    audit.record("rules_updated", body.rules, {})
    return rules_config.model_dump()


@app.get("/api/audit")
def get_audit(limit: int = 100, _: None = Depends(require_api_key)):
    return audit.recent(limit)


@app.get("/api/signals/config")
def get_screener_config(_: None = Depends(require_api_key)):
    return screener_config.model_dump()


class ScreenerUpdate(BaseModel):
    config: dict


@app.put("/api/signals/config")
def update_screener_config(body: ScreenerUpdate, _: None = Depends(require_api_key)):
    global screener_config
    screener_config = ScreenerConfig(**body.config)
    screener_config.save(settings.screener_path)
    screener.reload(screener_config)
    _sync_whitelist_with_universe()
    # Tras un cambio manual de config, los filtros pudieron cambiar por
    # completo: se descarta la base de simbolos "pasando" para que el proximo
    # ciclo del scan proactivo no trate la config nueva como transiciones
    # reales (vuelve a ser un primer ciclo, solo establece base).
    _signal_state["previously_passing"] = None
    audit.record("screener_config_updated", body.config, {})
    return screener_config.model_dump()


@app.get("/api/signals/scan")
async def scan_signals(force: bool = False, _: None = Depends(require_api_key)):
    """Radar de oportunidades momentum/tecnico. No es una recomendacion de
    inversion ni ejecuta nada: solo rankea candidatos del universo configurado
    en screener.yaml. Cacheado para no agotar la cuota de la API gratuita de
    datos en cada refresh del dashboard.

    Requiere API key: aunque no mueve dinero, escanear (sobre todo con
    force=true) golpea la API gratuita de datos para todo el universo, asi que
    dejarlo abierto seria un vector de DoS / de agotar la cuota.

    async + asyncio.to_thread (en vez de un def sincrono comun): con el
    universo del S&P 500 completo un scan tarda varios minutos, y un endpoint
    sincrono ocuparia ese tiempo un thread del pool compartido por TODOS los
    demas endpoints de la API, pudiendo demorar pedidos no relacionados. El
    lock evita que un scan se cruce con un backtest o con el ciclo proactivo
    en background, que pegan a la misma API de datos."""
    now = datetime.now(timezone.utc)
    cached_at = signal_cache["as_of"]
    if not force and cached_at and (now - cached_at).total_seconds() < SIGNAL_CACHE_TTL_SECONDS:
        return {"as_of": cached_at, "cached": True, "results": signal_cache["results"]}
    try:
        async with _market_scan_lock:
            results = await asyncio.to_thread(screener.scan, force=force)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al escanear el mercado: {exc}")
    signal_cache["as_of"] = now
    signal_cache["results"] = [r.model_dump() for r in results]
    return {"as_of": now, "cached": False, "results": signal_cache["results"]}


@app.get("/api/signals/backtest")
async def backtest_strategy(_: None = Depends(require_api_key)):
    """Backtest simplificado de la estrategia momentum sobre el universo
    configurado. Ver docstring de run_backtest() para las simplificaciones
    asumidas (sin comisiones/slippage, curva de equity aproximada).

    Requiere API key: es la operacion mas pesada del backend (descarga anos de
    historia de todo el universo), dejarla abierta seria un vector de DoS.
    async + asyncio.to_thread + lock por el mismo motivo que scan_signals."""
    try:
        async with _market_scan_lock:
            return await asyncio.to_thread(run_backtest, screener_config)
    except BacktestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orders/size-suggestion")
def order_size_suggestion(
    symbol: str,
    entry_price: float,
    stop_loss_price: float,
    _: None = Depends(require_api_key),
):
    """Sugiere una cantidad para una compra en base al riesgo (ver
    RulesEngine.suggested_quantity). No es una orden ni se aplica sola: el
    usuario la ve en el ticket de orden y puede ajustarla antes de enviar, y
    de todas formas pasa por rules_engine.evaluate() al enviarse como
    cualquier otra orden."""
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    account_summary = broker.get_account_summary()
    position_qty = broker.get_position_qty(symbol.strip().upper())
    return rules_engine.suggested_quantity(account_summary, position_qty, entry_price, stop_loss_price)


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
