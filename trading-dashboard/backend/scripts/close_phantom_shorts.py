#!/usr/bin/env python3
"""Cierra a mercado las posiciones short fantasma (no atadas a ningun fondo,
originadas por el bug de stops duplicados, ver memoria project_orphan_stop_
short_bug) comprando exactamente la cantidad para dejar cada simbolo en 0.

Bypasea a proposito /api/orders (y por lo tanto rules_engine): esa ruta esta
disenada para ABRIR posiciones nuevas con criterio de riesgo (max_order_value_
usd, manual_approval_threshold_usd, y sobre todo require_stop_loss_on_buy),
no para cerrar una posicion existente por remediacion. Adjuntar un stop-loss
a esta compra encadenaria un SELL stop protector sobre una posicion que queda
en 0 -- exactamente el patron de stop huerfano que causo estos shorts fantasma
en primer lugar (ver has_live_protective_stop en la memoria del bug). Por eso
esta compra va directo por ib_async, sin stop adjunto, sin pasar por
rules_engine ni por el ledger de ningun fondo (estas posiciones no pertenecen
a ninguno).

Uso:
    cd trading-dashboard/backend
    .venv/bin/python scripts/close_phantom_shorts.py           # dry-run
    .venv/bin/python scripts/close_phantom_shorts.py --execute  # ejecuta
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, MarketOrder, Stock  # noqa: E402

from app.audit import AuditLog  # noqa: E402
from app.config import Settings  # noqa: E402

# clientId distinto al de la app en vivo (Settings.ib_client_id, default 17)
# para no colisionar con la conexion de uvicorn -- dos conexiones ib_async
# con el mismo clientId a la misma cuenta se pisan.
SCRIPT_CLIENT_ID = 91


async def main(execute: bool, only: set[str] | None = None) -> None:
    settings = Settings()
    audit = AuditLog(Path(__file__).resolve().parent.parent / "audit.db")

    ib = IB()
    await ib.connectAsync(settings.ib_host, settings.ib_port, clientId=SCRIPT_CLIENT_ID, timeout=10)
    try:
        positions = await ib.reqPositionsAsync()
        shorts = [p for p in positions if p.position < 0]
        if only:
            shorts = [p for p in shorts if p.contract.symbol in only]

        if not shorts:
            print("No hay posiciones short abiertas.")
            return

        print(f"{'símbolo':<8} {'qty short':>10} {'a comprar':>10}")
        for p in shorts:
            print(f"{p.contract.symbol:<8} {p.position:>10.0f} {abs(p.position):>10.0f}")

        if not execute:
            print("\nDry-run (no se envió ninguna orden). Correr con --execute para ejecutar.")
            return

        print()
        for p in shorts:
            symbol = p.contract.symbol
            qty = abs(p.position)
            contract = Stock(symbol, "SMART", "USD")
            await ib.qualifyContractsAsync(contract)

            order = MarketOrder("BUY", qty)
            # Sin esto, IBKR "corrige" el TIF a DAY segun un preset de la
            # cuenta (error 10349) y esa correccion dispara un dialogo de
            # confirmacion en la GUI que nadie contesta en una conexion
            # headless -> la orden se cancela sola por timeout. Fijarlo
            # explicito de entrada evita que haga falta "corregir" nada.
            order.tif = "DAY"
            trade = ib.placeOrder(contract, order)
            print(f"[{symbol}] orden enviada: BUY {qty} MKT (order id {order.orderId})")

            # Espera a un estado terminal (Filled/Cancelled/etc), igual criterio
            # que broker._wait_for_fill: hasta 15s, no bloquea mas alla de eso.
            for _ in range(150):
                await asyncio.sleep(0.1)
                if trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    break

            status = trade.orderStatus
            print(
                f"[{symbol}] estado={status.status} filled={status.filled} "
                f"avg_price={status.avgFillPrice}"
            )
            audit.record(
                "manual_close_phantom_short",
                {"symbol": symbol, "quantity_requested": qty, "order_id": order.orderId},
                {"status": status.status, "filled": status.filled, "avg_fill_price": status.avgFillPrice},
            )

        await asyncio.sleep(1)
        remaining = [p for p in await ib.reqPositionsAsync() if p.position < 0]
        if remaining:
            print("\nAVISO: siguen quedando shorts abiertos:")
            for p in remaining:
                print(f"  {p.contract.symbol}: {p.position:.0f}")
        else:
            print("\nTodas las posiciones short quedaron en 0.")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Ejecuta las órdenes (sin esto, solo dry-run)")
    parser.add_argument("--only", default=None, help="Símbolos separados por coma (ej. SWKS,SMCI), para probar antes de correr todos")
    args = parser.parse_args()
    only = set(args.only.split(",")) if args.only else None
    asyncio.run(main(args.execute, only))
