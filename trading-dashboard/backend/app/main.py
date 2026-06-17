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
from .broker import IBKRBroker, IBKRConnectionError
from .config import settings
from .models import OrderRequest, PendingOrder
from .rules import RulesConfig, RulesEngine

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

rules_config = RulesConfig.load(settings.rules_path)
rules_engine = RulesEngine(rules_config)
audit = AuditLog(settings.audit_db_path)
broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id)

state: dict = {
    "halted": False,
    "connected": False,
    "pending_orders": {},  # id -> PendingOrder
}
clients: list[WebSocket] = []


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
        "mode": settings.trading_mode,
        "connected": state["connected"],
        "halted": state["halted"],
        "ib_host": settings.ib_host,
        "ib_port": settings.ib_port,
    }


@app.post("/api/halt")
def set_halt(value: bool, _: None = Depends(require_api_key)):
    state["halted"] = value
    audit.record("halt_toggle", {"value": value}, {})
    return {"halted": state["halted"]}


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
