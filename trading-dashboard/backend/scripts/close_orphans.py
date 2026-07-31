"""Cierra las posiciones de IBKR que no pertenecen a ningun fondo (huerfanas).

Contexto: hasta el fix del aviso 10349 (ver _is_spurious_cancel en broker.py),
una compra podia llenar en IBKR sin quedar registrada en el fondo. El resultado
son posiciones reales, sin stop y sin dueno contable. Al 2026-07-31 habia 34.

POR QUE UN SCRIPT APARTE Y NO EL BACKEND:
estas posiciones no estan en ningun fondo, asi que no hay ledger contra el cual
registrar la venta -- los endpoints de cierre del backend piden fund_id. Ademas
conviene correr esto con el backend PARADO, para que sus loops
(_reconcile_protective_stops_cycle cada 5min, _auto_exit_monitor_loop) no
recoloquen un stop que el script acaba de cancelar.

ORDEN DE OPERACIONES (importante):
primero se CANCELA el stop protector de cada simbolo y recien despues se manda
la orden de cierre. Al reves, el stop queda huerfano: se dispara mas tarde,
vende acciones que ya no existen y deja una posicion CORTA. Ese es exactamente
el incidente de AEHR/TAP, y CEG -4 es un caso vivo del mismo patron.

USO:
    # 1. parar el backend (sus loops pelean con el script)
    sudo systemctl stop trading-dashboard

    # 2. ver el plan sin tocar nada (default: no manda ninguna orden)
    .venv/bin/python scripts/close_orphans.py --keep MOS,APO,CCI,STLD,IR,NOC,IDXX,JKHY,IONQ

    # 3. ejecutar de verdad
    .venv/bin/python scripts/close_orphans.py --keep ... --execute

    # 4. volver a levantar
    sudo systemctl start trading-dashboard

--keep es la lista de simbolos que SI pertenecen a un fondo. Si funds.json es
legible se toma de ahi y --keep solo sirve para contrastar (el script aborta si
no coinciden, en vez de adivinar cual de las dos fuentes vale).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ib_async import IB, MarketOrder, Stock
from ib_async.order import OrderStatus

BASE_DIR = Path(__file__).resolve().parent.parent
FUNDS_PATH = BASE_DIR / "funds.json"

# clientId distinto al del backend (17): IBKR rechaza dos clientes con el mismo
# id, y ademas asi las ordenes de este script quedan identificables en TWS.
CLIENT_ID = 42
HOST = "127.0.0.1"
PORT = 4002  # IB Gateway paper. Live (4001) requiere cambiarlo a mano, a proposito.


def _fund_symbols() -> dict[str, float] | None:
    """Posiciones abiertas segun funds.json, o None si no se puede leer.

    No es fatal que no se pueda: el archivo esta en modo 0600 y este script
    puede correr como otro usuario. En ese caso manda --keep."""
    try:
        raw = json.loads(FUNDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    funds = raw.get("funds", raw)
    if isinstance(funds, dict):
        funds = list(funds.values())
    out: dict[str, float] = {}
    for f in funds:
        for sym, pos in (f.get("positions") or {}).items():
            qty = float(pos.get("quantity") or 0)
            if qty:
                out[sym] = out.get(sym, 0.0) + qty
    return out


async def _cancel_protective_stops(ib: IB, symbols: set[str], execute: bool) -> list[int]:
    """Cancela toda orden viva de los simbolos objetivo. Devuelve los orderId
    cancelados. Se cancela CUALQUIER orden abierta del simbolo, no solo el
    stop: una LMT de compra sin llenar tambien tiene que morir, si no vuelve a
    abrir la posicion que estamos cerrando."""
    cancelled = []
    for trade in ib.openTrades():
        sym = trade.contract.symbol.replace(" ", "-")
        if sym not in symbols:
            continue
        if trade.orderStatus.status in OrderStatus.DoneStates:
            continue
        print(
            f"  cancelar orden {trade.order.orderId:>9}  {sym:<6} "
            f"{trade.order.orderType:<4} {trade.order.action:<4} "
            f"{trade.order.totalQuantity:>6.0f}  ({trade.orderStatus.status})"
        )
        cancelled.append(trade.order.orderId)
        if execute:
            ib.cancelOrder(trade.order)
    if execute and cancelled:
        # Dar tiempo a que IBKR confirme las cancelaciones ANTES de vender:
        # ese es el punto de todo el ejercicio.
        await asyncio.sleep(3)
    return cancelled


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--keep",
        default="",
        help="simbolos que SI pertenecen a un fondo, separados por coma",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="mandar las ordenes de verdad (sin esto solo imprime el plan)",
    )
    args = ap.parse_args()

    keep_cli = {s.strip().upper() for s in args.keep.split(",") if s.strip()}
    keep_file = _fund_symbols()

    if keep_file is None:
        if not keep_cli:
            print("ERROR: funds.json no es legible y no se paso --keep.", file=sys.stderr)
            return 2
        keep = keep_cli
        print(f"funds.json no legible -- se usa --keep ({len(keep)} simbolos)")
    else:
        keep = set(keep_file)
        print(f"funds.json leido: {len(keep)} simbolos con posicion abierta")
        if keep_cli and keep_cli != keep:
            # No elegir por nosotros cual fuente vale: que lo resuelva quien corre.
            print("ERROR: --keep no coincide con funds.json.", file=sys.stderr)
            print(f"  solo en funds.json: {sorted(keep - keep_cli)}", file=sys.stderr)
            print(f"  solo en --keep    : {sorted(keep_cli - keep)}", file=sys.stderr)
            return 2

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        # reqAllOpenOrders, NO el openTrades() que llega solo: ib_async solo
        # entrega automaticamente las ordenes de ESTE clientId, y los stops
        # protectores los coloco el backend con clientId 17. Sin esta llamada
        # el script no ve un solo stop, cree que no hay nada que cancelar, y
        # vende dejando los stops vivos -- que es justamente el escenario de
        # corto fantasma que este script existe para evitar.
        await ib.reqAllOpenOrdersAsync()
        await asyncio.sleep(2)  # que lleguen positions/openOrders iniciales
        positions = {
            p.contract.symbol.replace(" ", "-"): p.position
            for p in ib.positions()
            if p.position
        }

        orphans = {s: q for s, q in positions.items() if s not in keep}
        print(f"\nIBKR: {len(positions)} simbolos | del fondo: {len(keep)} | "
              f"huerfanos: {len(orphans)}\n")
        if not orphans:
            print("nada que cerrar.")
            return 0

        print("PASO 1 -- cancelar ordenes vivas de los huerfanos")
        await _cancel_protective_stops(ib, set(orphans), args.execute)

        print("\nPASO 2 -- cerrar posiciones")
        trades = []
        for sym in sorted(orphans):
            qty = orphans[sym]
            side = "SELL" if qty > 0 else "BUY"  # BUY cubre un corto (ej. CEG -4)
            print(f"  {side:<4} {abs(qty):>6.0f} {sym}")
            if not args.execute:
                continue
            contract = Stock(sym.replace("-", " "), "SMART", "USD")
            await ib.qualifyContractsAsync(contract)
            if not contract.conId:
                print(f"       !! {sym} no califica en IBKR, se saltea")
                continue
            order = MarketOrder(side, abs(qty))
            order.tif = "DAY"
            trades.append((sym, ib.placeOrder(contract, order)))

        if not args.execute:
            print("\n(dry-run: no se mando ninguna orden -- agregar --execute)")
            return 0

        print("\nesperando fills...")
        for _ in range(30):
            await asyncio.sleep(2)
            if all(t.orderStatus.status in OrderStatus.DoneStates for _, t in trades):
                break

        print("\nRESULTADO")
        fallidas = []
        for sym, t in trades:
            st = t.orderStatus
            print(f"  {sym:<6} {st.status:<12} {st.filled:>6.0f}/{t.order.totalQuantity:<6.0f}"
                  f" @ {st.avgFillPrice or 0:.2f}")
            if st.filled < t.order.totalQuantity:
                fallidas.append(sym)

        restantes = {
            p.contract.symbol.replace(" ", "-"): p.position
            for p in ib.positions()
            if p.position
        }
        sobran = {s: q for s, q in restantes.items() if s not in keep}
        print(f"\nposiciones restantes fuera del fondo: {len(sobran)}")
        if sobran:
            print(f"  {sobran}")
        if fallidas:
            print(f"\nsin llenar del todo: {fallidas}")
            print("REVISAR EN TWS antes de volver a levantar el backend.")
            return 1
        return 0
    finally:
        ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
