from __future__ import annotations

import asyncio
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
from .broker import IBKRBroker, IBKRConnectionError
from .config import settings
from .market_data import MarketDataError
from .models import OrderRequest, PendingOrder
from .rules import RulesConfig, RulesEngine
from .screener import MomentumScreener
from .screener_config import ScreenerConfig

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

rules_config = RulesConfig.load(settings.rules_path)
rules_engine = RulesEngine(rules_config)
audit = AuditLog(settings.audit_db_path)
broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)

screener_config = ScreenerConfig.load(settings.screener_path)
screener = MomentumScreener(screener_config)

state: dict = {
    "mode": settings.trading_mode,
    "halted": False,
    "connected": False,
    "pending_orders": {},  # id -> PendingOrder
}
clients: list[WebSocket] = []

SIGNAL_CACHE_TTL_SECONDS = 900  # evita re-escanear el mercado en cada refresh
signal_cache: dict = {"as_of": None, "results": []}


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="API key invalida.")


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
        dead = []
        for ws in clients:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.remove(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await broker.connect()
        state["connected"] = True
    except IBKRConnectionError as exc:
        state["connected"] = False
        print(f"[WARN] {exc}")
    task = asyncio.create_task(_broadcast_loop())
    yield
    task.cancel()
    broker.disconnect()


app = FastAPI(title="IBKR Trading Dashboard", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    audit.record("mode_switched", {"mode": mode}, {"ib_port": target_port})
    return {"mode": state["mode"], "connected": state["connected"], "halted": state["halted"]}


@app.get("/api/account")
def get_account():
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    return broker.get_account_summary()


@app.get("/api/positions")
async def get_positions():
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    return await broker.get_positions()


@app.get("/api/rules")
def get_rules():
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
def get_audit(limit: int = 100):
    return audit.recent(limit)


@app.get("/api/signals/config")
def get_screener_config():
    return screener_config.model_dump()


class ScreenerUpdate(BaseModel):
    config: dict


@app.put("/api/signals/config")
def update_screener_config(body: ScreenerUpdate, _: None = Depends(require_api_key)):
    global screener_config
    screener_config = ScreenerConfig(**body.config)
    screener_config.save(settings.screener_path)
    screener.reload(screener_config)
    audit.record("screener_config_updated", body.config, {})
    return screener_config.model_dump()


@app.get("/api/signals/scan")
def scan_signals(force: bool = False):
    """Radar de oportunidades momentum/tecnico. No es una recomendacion de
    inversion ni ejecuta nada: solo rankea candidatos del universo configurado
    en screener.yaml. Cacheado para no agotar la cuota de la API gratuita de
    datos en cada refresh del dashboard."""
    now = datetime.now(timezone.utc)
    cached_at = signal_cache["as_of"]
    if not force and cached_at and (now - cached_at).total_seconds() < SIGNAL_CACHE_TTL_SECONDS:
        return {"as_of": cached_at, "cached": True, "results": signal_cache["results"]}
    try:
        results = screener.scan()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error al escanear el mercado: {exc}")
    signal_cache["as_of"] = now
    signal_cache["results"] = [r.model_dump() for r in results]
    return {"as_of": now, "cached": False, "results": signal_cache["results"]}


@app.get("/api/signals/backtest")
def backtest_strategy():
    """Backtest simplificado de la estrategia momentum sobre el universo
    configurado. Ver docstring de run_backtest() para las simplificaciones
    asumidas (sin comisiones/slippage, curva de equity aproximada)."""
    try:
        return run_backtest(screener_config)
    except BacktestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orders/pending")
def list_pending_orders():
    return list(state["pending_orders"].values())


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
    trades_today = audit.count_trades_today()

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
        audit.record("order_pending_approval", order.model_dump(), {"id": pending_id})
        return {"status": "pending_approval", "pending_order": pending.model_dump()}

    result = await broker.place_order(order)
    audit.record("order_executed", order.model_dump(), result)
    return {"status": "executed", "result": result}


@app.post("/api/orders/{order_id}/approve")
async def approve_order(order_id: str, _: None = Depends(require_api_key)):
    pending = state["pending_orders"].pop(order_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Orden pendiente no encontrada.")
    result = await broker.place_order(pending.order)
    audit.record("order_executed_after_approval", pending.order.model_dump(), result)
    return {"status": "executed", "result": result}


@app.post("/api/orders/{order_id}/reject")
def reject_order(order_id: str, _: None = Depends(require_api_key)):
    pending = state["pending_orders"].pop(order_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Orden pendiente no encontrada.")
    audit.record("order_rejected", pending.order.model_dump(), {"id": order_id})
    return {"status": "rejected"}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.remove(websocket)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
