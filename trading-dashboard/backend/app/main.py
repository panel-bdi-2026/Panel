from __future__ import annotations

import asyncio
import logging
import math
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import pandas as pd
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from pydantic import BaseModel, Field, ValidationError, field_validator

from .alerts import send_alert
from .audit import AuditLog
from .backtest import (
    BacktestError,
    run_backtest,
    run_backtest_walk_forward,
    run_opportunistic_backtest,
    run_opportunistic_backtest_walk_forward,
)
from .broker import IBKRBroker, IBKRConnectionError, StopLossRejectedError
from .config import settings
from .funds import FundsStore, FundValidationError
from .indicators import atr, market_regime_ok, rate_of_change, sma
from .market_data import (
    MarketDataError,
    get_bars_failure_stats,
    get_daily_bars,
    get_fundamentals,
    is_bars_cached,
    is_fundamentals_cached,
)
from .models import OrderRequest, OrderType, PendingOrder, Position, SignalResult, Side, validate_symbol
from .rules import RulesConfig, RulesEngine
from .screener import MomentumScreener
from .screener_config import ScreenerConfig
from .sectors import SECTOR_ETF, get_sector, refresh_sector
from .session_store import SessionStore
from .state_store import load_state, save_state
from .strategies import STRATEGY_CLASSES, reload_strategy_registry

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

# uvicorn (lanzado via su CLI, ver deploy/systemd/trading-dashboard.service) solo
# configura sus propios loggers ("uvicorn", "uvicorn.access", etc.), no el root
# logger. Sin este basicConfig, logger.info() de este modulo y de app/broker.py
# no llegarian a ningun handler (el root logger por defecto solo emite WARNING+
# via su lastResort handler) y quedarian invisibles en journalctl pese a llamar
# al logger correctamente.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Este modulo asume UN SOLO proceso worker. screener_config, state, clients,
# signal_cache y los asyncio.Lock de mas abajo viven en memoria de proceso: si
# se corre con `uvicorn ... --workers N>1` (o gunicorn con varios workers),
# cada proceso tendria su propia copia de este estado, los locks no
# sincronizarian nada entre procesos, y dos workers podrian validar la misma
# orden contra el mismo cash_usd desactualizado sin verse entre si. Escalar
# horizontalmente requeriria mover este estado a un store compartido (Redis,
# Postgres, etc.) primero. Los artefactos de deploy en deploy/ ya arrancan un
# solo proceso; no agregar --workers sin resolver esto antes.
rules_config = RulesConfig.load(settings.rules_path)
rules_engine = RulesEngine(rules_config)
audit = AuditLog(settings.audit_db_path)
broker = IBKRBroker(settings.ib_host, settings.ib_port, settings.ib_client_id, settings.ib_market_data_type)

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
_startup_time = datetime.now(timezone.utc)

# Almacén SQLite de sesiones y signal_state (sobreviven reinicios).
_store = SessionStore(settings.state_path.parent / "state.db")
_restored = _store.load_signal_state()

state: dict = {
    "mode": _persisted.get("mode", settings.trading_mode),
    "halted": _persisted.get("halted", False),
    "connected": False,
    "pending_orders": {
        pid: PendingOrder(**p) for pid, p in _persisted.get("pending_orders", {}).items()
    },
    # Maximo historico de equity de la cuenta observado por el kill switch de
    # drawdown acumulado (ver _risk_monitor_loop). Persistido (a diferencia
    # de connected) a proposito: si no sobreviviera un restart, un crash a
    # mitad de un drawdown fuerte "perdonaria" la caida silenciosamente (el
    # proximo arranque tomaria la equity actual, ya deprimida, como nuevo
    # maximo) justo cuando el circuit breaker mas necesita seguir midiendo
    # contra el maximo real. None hasta la primera lectura exitosa de
    # account_summary si nunca se persistio nada (primer arranque).
    "peak_equity_usd": _persisted.get("peak_equity_usd"),
    # Flag de degradacion parcial del feed de datos de mercado (ver
    # _check_market_data_degradation). No persistido a proposito: es una
    # senal operativa transitoria (se recalcula del cache real en el proximo
    # ciclo), no un estado de seguridad de trading como halted/peak_equity_usd
    # que deba sobrevivir un restart.
    "market_data_degraded": False,
}
clients: list[WebSocket] = []

SIGNAL_CACHE_TTL_SECONDS = 900  # evita re-escanear el mercado en cada refresh
# Cacheado por strategy_id: cada estrategia escanea el mismo universo pero con
# filtros/scores distintos, asi que comparten cache llevaria a devolver
# resultados de una estrategia bajo el nombre de otra.
signal_cache: dict[str, dict] = {}

# Vista sintetica "General" del radar (ver _scan_general): no es una
# estrategia real de strategy_registry, asi que necesita su propio id para
# que /api/signals/scan la distinga de un strategy_id invalido.
GENERAL_VIEW_ID = "general"

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

# Serializa el ciclo leer-mezclar-guardar de update_screener_config (PUT
# /api/signals/config): esa funcion es sync (FastAPI la corre en un
# threadpool, no en el event loop), asi que el lock tiene que ser de
# threading, no de asyncio. Sin el, dos PUT casi simultaneos (ej. dos
# pestañas, o el modal de settings guardando justo cuando addTickerToUniverse
# dispara el suyo) podrian leer el mismo screener_config viejo antes de que
# cualquiera escriba, y el segundo en escribir pisaria el cambio del primero
# a pesar del merge (ver deep_merge_dict mas abajo).
_screener_config_lock = asyncio.Lock()

# Recuerda que simbolos pasaban los filtros del screener en el ultimo ciclo del
# scan proactivo, para poder detectar TRANSICIONES (no pasaba -> pasa) en vez
# de redraftear el mismo simbolo en cada ciclo mientras siga pasando. None
# significa "todavia no hay base": el primer ciclo solo la establece, sin
# generar borradores, para no inundar la cola de pendientes apenas arranca el
# backend o se cambia la config del screener.
_signal_state: dict = {
    "previously_passing": _restored["previously_passing"],
    "previously_passing_by_strategy": _restored["previously_passing_by_strategy"],
}

# Radar en vivo (ver _hot_set_loop / _price_rotation_loop mas abajo):
# _hot_symbols son los simbolos con streaming persistente activo en IBKR en
# este momento (recalculado cada vez que se refresca signal_cache, ver
# _run_hot_set_cycle); _live_prices son los ultimos snapshots capturados por
# la rotacion sobre el resto del universo. _rotation_cursor recuerda por
# donde sigue la proxima rotacion (round-robin sobre el universo). Mismo
# alcance/limitacion de proceso unico que signal_cache (ver el comentario de
# arriba sobre multiples workers).
_hot_symbols: set[str] = set()
_live_prices: dict[str, float] = {}
_live_prices_as_of: dict[str, datetime] = {}
_rotation_cursor = 0
_data_refresh_cursor = 0


def _persist_state() -> None:
    # No deja propagar la excepcion: save_state puede fallar (disco lleno,
    # permisos) y esta funcion se llama desde loops de fondo recurrentes
    # (ej. _risk_monitor_loop, el kill switch). Si una excepcion sin atrapar
    # mata esa tarea de asyncio, el kill switch deja de correr en silencio --
    # mucho peor que perder una persistencia puntual del estado en disco.
    try:
        save_state(settings.state_path, {
            "mode": state["mode"],
            "halted": state["halted"],
            "pending_orders": {pid: p.model_dump() for pid, p in state["pending_orders"].items()},
            "peak_equity_usd": state["peak_equity_usd"],
        })
    except OSError:
        logger.exception("no se pudo persistir el estado en %s", settings.state_path)


SESSION_COOKIE_NAME = "session"
# 7 dias, no 30: la cookie es httponly pero NO Secure (ver comentario abajo),
# asi que viaja en claro sobre la red Tailscale; un TTL de 30 dias dejaba una
# ventana innecesariamente larga para cualquier token que se filtrara (ej. un
# log, un dispositivo de la tailnet comprometido). 7 dias sigue evitando tener
# que loguearse a diario sin sostener un token valido casi un mes.
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

# Sesiones de login (ver /api/login, /api/logout). Viven en memoria de
# proceso (misma limitacion de un-solo-worker que el resto del estado de
# arriba): un restart del backend desloguea a todo el mundo. La cookie de
# sesion es httponly (JS no puede leerla) pero NO Secure, porque el deploy
# documentado (ver deploy/README.md) sirve el dashboard por HTTP plano sobre
# una red privada de Tailscale, no HTTPS.
def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _store.create(token)
    return token


def _session_valid(token: str) -> bool:
    return _store.valid(token)


async def _cache_eviction_loop() -> None:
    """Elimina entradas expiradas del cache de market_data cada hora para
    evitar acumulación ilimitada de DataFrames en universos grandes."""
    while True:
        await asyncio.sleep(3600)
        await asyncio.to_thread(market_data.evict_stale_cache)


async def _session_cleanup_loop() -> None:
    """Elimina tokens expirados de SQLite cada hora."""
    while True:
        await asyncio.sleep(3600)
        await asyncio.to_thread(_store.cleanup_expired)


async def _health_alert_loop() -> None:
    """Detecta cambios en el estado de salud del sistema y envía alertas por
    email cuando aparecen o se resuelven problemas.

    Solo envía email en transiciones (aparece/desaparece un problema) para
    no inundar el correo. El primer chequeo arranca 90 segundos después del
    startup para darle tiempo al broker de conectar antes de evaluar.
    """
    await asyncio.sleep(90)

    alerting: set[str] = set()

    while True:
        now = datetime.now(timezone.utc)
        current: set[str] = set()
        if not state["connected"]:
            current.add("IBKR desconectado")
        if state.get("market_data_degraded"):
            current.add("datos de mercado degradados")
        if state.get("halted"):
            current.add("trading detenido (halted)")
        scan_ages = [
            (now - e["as_of"]).total_seconds()
            for e in signal_cache.values()
            if e.get("as_of")
        ]
        if scan_ages and all(a > 2700 for a in scan_ages):
            current.add("scan paralizado (>45 min sin actualizar)")

        new_issues = current - alerting
        resolved = alerting - current

        now_str = datetime.now().strftime("%H:%M:%S")

        if new_issues:
            subject = "⚠ Alerta: " + ", ".join(sorted(new_issues))
            body = f"Problemas detectados a las {now_str}:\n\n"
            body += "\n".join(f"• {i}" for i in sorted(new_issues))
            send_alert(settings, subject, body)

        for issue in resolved:
            send_alert(
                settings,
                f"✓ Resuelto: {issue}",
                f"El problema fue resuelto a las {now_str}:\n\n• {issue}",
            )

        alerting = current
        await asyncio.sleep(settings.health_alert_interval_seconds)


def require_api_key(
    x_api_key: Optional[str] = Header(default=None),
    session: Optional[str] = Cookie(default=None),
) -> None:
    # compare_digest en vez de != para no filtrar la API key por timing (una
    # comparacion de strings comun corta apenas encuentra el primer caracter
    # distinto, lo que en teoria permite adivinarla caracter por caracter
    # midiendo tiempos de respuesta). Acepta el header X-API-Key de siempre
    # (para scripts/automatizacion) o una cookie de sesion valida (la que usa
    # el dashboard despues de /api/login).
    if x_api_key and secrets.compare_digest(x_api_key, settings.api_key):
        return
    if session and _session_valid(session):
        return
    raise HTTPException(status_code=401, detail="No autenticado.")


async def _broadcast(payload: dict) -> None:
    # Itera sobre una copia: cada await ws.send_json cede el control del loop
    # de eventos, y en esa ventana otra corutina puede conectar/desconectar
    # un cliente y mutar `clients` (list.append/remove) mientras este for
    # todavia la recorre -- iterar la lista en vivo arriesga un
    # RuntimeError: list changed size during iteration.
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in clients:
            clients.remove(ws)


def _sync_connection_state() -> None:
    """Sincroniza state["connected"] con el estado real del socket de IBKR.

    Sin esto, una desconexion a mitad de sesion (TWS/IB Gateway cerrado, caida
    de red) dejaba state["connected"] en True para siempre: lo unico que lo
    escribia era el connect/reconnect inicial y el endpoint /api/mode, nunca
    un chequeo periodico. Como casi todo el resto del backend (auto-trading,
    auto-exit, trailing stop, hot-set, escaneo de señales) usa ese flag como
    circuit breaker antes de llamar al broker, quedaba "conectado" en el
    estado compartido mucho despues de que la conexion real habia muerto."""
    actually_connected = broker.is_connected()
    if actually_connected == state["connected"]:
        return
    state["connected"] = actually_connected
    if actually_connected:
        logger.warning("Conexion a IBKR restablecida (detectado en broadcast_loop)")
    else:
        logger.warning("Conexion a IBKR perdida a mitad de sesion (detectado en broadcast_loop)")
        audit.record("ibkr_disconnected", {}, {})


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        _sync_connection_state()
        if not clients or not state["connected"]:
            continue
        try:
            payload = {
                "type": "update",
                "account": (await broker.get_account_summary()).model_dump(),
                "positions": [p.model_dump() for p in await broker.get_positions()],
            }
        except Exception as exc:
            # No se reenvia str(exc) crudo a todos los clientes conectados: el
            # detalle (puede incluir trazas/info interna de ib_async) queda en
            # el log del servidor, el cliente solo recibe un mensaje generico.
            logger.warning("Error en broadcast_loop al leer cuenta/posiciones: %s", exc)
            payload = {"type": "error", "message": "No se pudieron obtener los datos de cuenta/posiciones."}
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


def _compute_total_position_value(positions: list[Position], exclude_symbol: str) -> float:
    """Valor de mercado (USD, valor absoluto) de TODAS las posiciones
    actuales salvo `exclude_symbol` (ya se suma aparte como resulting_value
    dentro de RulesEngine.evaluate()), para que pueda chequear
    max_total_exposure_pct -- exposicion BRUTA de toda la cartera, sin
    importar el sector (a diferencia de max_sector_concentration_pct/
    _compute_sector_exposure, que solo mira un sector a la vez). Sin este
    limite, alcanzar el tope de varios sectores distintos a la vez podia
    dejar la cuenta totalmente invertida (o mas, si hay margen) sin que
    ninguna regla individual lo bloqueara."""
    total = 0.0
    for p in positions:
        if p.symbol == exclude_symbol:
            continue
        price = p.market_price if p.market_price is not None else p.avg_cost
        total += abs(p.quantity) * price
    return total


def _compute_open_portfolio_risk_usd(exclude_symbol: "str | None" = None) -> float:
    """Suma en USD del riesgo (precio de entrada - stop-loss) x cantidad de
    TODAS las posiciones abiertas con stop-loss registrado, en TODOS los
    fondos -- para que RulesEngine.evaluate() pueda chequear
    max_portfolio_heat_pct: cuanto se perderia en total si TODOS los stops
    abiertos se tocaran a la vez, no solo el riesgo de la operacion
    individual en evaluacion (risk_per_trade_pct). `exclude_symbol` se
    descarta porque su riesgo (si ya tiene una posicion abierta) se vuelve a
    sumar aparte a partir de la orden nueva en evaluacion, para no contarlo
    dos veces.

    Alcance: solo cubre posiciones atadas a un fondo (las unicas que
    guardan stop_loss_price localmente, ver funds.py) -- una posicion fuera
    de un fondo no aporta a esta suma."""
    total = 0.0
    for fund in funds_store.list():
        for symbol, pos in fund.positions.items():
            if symbol == exclude_symbol or pos.quantity <= 0 or pos.stop_loss_price is None:
                continue
            total += max(0.0, pos.avg_cost - pos.stop_loss_price) * pos.quantity
    return total


def _compute_sector_position_count(positions: list[Position], exclude_symbol: str) -> dict[str, int]:
    """Cantidad de simbolos DISTINTOS con posicion abierta agrupados por
    sector GICS, para que RulesEngine.evaluate() pueda chequear
    max_concurrent_positions_per_sector -- espejo de _compute_sector_exposure,
    pero contando posiciones en vez de sumar USD (ese limite es de cantidad,
    no de exposicion). `exclude_symbol` se descarta del conteo por el mismo
    motivo que en _compute_sector_exposure: el propio simbolo de la orden en
    evaluacion no debe contarse dos veces si ya tiene una posicion abierta."""
    counts: dict[str, int] = {}
    for p in positions:
        if p.symbol == exclude_symbol or p.quantity == 0:
            continue
        sector = get_sector(p.symbol)
        if sector is None:
            continue
        counts[sector] = counts.get(sector, 0) + 1
    return counts


async def _draft_fund_order_from_signal(result: SignalResult, strategy_id: str) -> PendingOrder | None:
    """Convierte una señal que recien cruzo el umbral de auto-trading (ver
    _live_score_entry_threshold/operational_gates_ok) en una orden de compra
    en borrador, atada a un fondo REAL y dimensionada contra SU capital
    disponible (fund.equity_estimate()) -- reemplaza a la vieja
    _draft_order_from_signal (retirada), que dimensionaba contra el equity
    de TODA la cuenta sin atar la orden a ningun fondo. Eso generaba
    borradores de ~$4-5k topeados por max_order_value_usd, sin ninguna
    relacion con el capital que el fondo realmente tenia disponible.

    Candidatos: los mismos criterios que _try_auto_trade_entry
    (auto_trading_enabled, sin posicion ya abierta en el simbolo, estrategia
    coincidente). Se llega aca solo cuando _try_auto_trade_entry ya proceso
    la señal ese ciclo sin auto-ejecutarla (tipicamente por el tope de
    max_auto_drafts_per_cycle, o el fondo sin cash suficiente): el borrador
    es la misma oportunidad que el fondo hubiera tomado automaticamente,
    para que el usuario decida a mano. Si NINGUN fondo sigue `strategy_id`
    (con auto_trading_enabled), no se genera ningun borrador -- asi, una
    estrategia que ningun fondo sigue (ej. Momentum, si el/los fondos activos
    eligieron Oportunista) deja de inundar la cola de pendientes sin
    necesitar un chequeo aparte: simplemente no hay candidatos.

    Se salta el draft (sin loggear error, es esperable que pase seguido) si
    ya hay una posicion abierta o una orden pendiente en ese simbolo, o si el
    sizing por riesgo da cantidad cero para todos los candidatos.

    Importante: el draft SIEMPRE queda en pending_orders para aprobacion
    manual, sin importar lo que diga decision.requires_manual_approval. Una
    orden generada sin intervencion humana nunca debe poder ejecutarse sola,
    aunque su valor este por debajo de manual_approval_threshold_usd.
    """
    symbol = result.symbol
    if any(p.order.symbol == symbol for p in state["pending_orders"].values()):
        return None

    # Limitacion conocida, sin resolver a proposito: candidates preserva el
    # orden de funds_store.list() (orden de INSERCION del dict subyacente,
    # es decir orden de creacion del fondo -- FIFO), y mas abajo se toma el
    # primer candidato con cash suficiente. Con 2+ fondos activos siguiendo
    # la MISMA estrategia, el fondo mas viejo (creado primero) absorbe cada
    # señal nueva mientras tenga cash, y los mas nuevos solo reciben lo que
    # el primero no pudo pagar -- no hay rotacion ni reparto proporcional
    # entre fondos candidatos. Aceptable con el uso actual (un fondo activo
    # por estrategia a la vez), pero a tener en cuenta si se activan varios
    # fondos en paralelo sobre la misma estrategia: el despliegue de capital
    # entre ellos no sera parejo.
    candidates = [
        f for f in funds_store.list()
        if f.auto_trading_enabled
        and f.owned_quantity(symbol) == 0
        and (f.strategy_id or screener_config.strategy_id) == strategy_id
    ]
    if not candidates:
        return None

    position_qty = broker.get_position_qty(symbol)
    if position_qty != 0:
        return None

    # result.last_price viene del cierre de la barra diaria evaluada en el
    # ultimo scan (hasta auto_scan_interval_minutes de antiguedad): se pide un
    # precio fresco a IBKR para el sizing/limit/chequeo de riesgo, que es
    # donde la antiguedad del precio realmente importa. Si el broker no
    # responde (desconectado, simbolo sin datos), se cae al precio de la
    # señal antes que dejar el draft sin precio.
    live_price = await broker.get_reference_price(symbol) or result.last_price

    account_summary = await broker.get_account_summary()
    fund = None
    quantity = 0.0
    for candidate in candidates:
        sizing = rules_engine.suggested_quantity(
            candidate.equity_estimate(), position_qty, live_price, result.suggested_stop_loss_price,
            score=result.score,
        )
        if sizing.quantity <= 0:
            continue
        affordable_qty = math.floor(candidate.cash_usd / live_price)
        qty = min(sizing.quantity, affordable_qty)
        if qty > 0:
            fund = candidate
            quantity = qty
            break
    if fund is None:
        return None

    order = OrderRequest(
        symbol=symbol,
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.LMT,
        limit_price=live_price,
        stop_loss_price=result.suggested_stop_loss_price,
        fund_id=fund.id,
    )
    positions = await broker.get_positions()
    trades_today = audit.count_trades_today(rules_config.trading_hours_timezone)
    trades_today_for_fund = audit.count_trades_today(rules_config.trading_hours_timezone, fund_id=fund.id)
    decision = rules_engine.evaluate(
        order=order,
        account=account_summary,
        current_position_qty=position_qty,
        reference_price=live_price,
        trades_today=trades_today,
        halted=state["halted"],
        order_sector=get_sector(symbol),
        sector_exposure_usd=_compute_sector_exposure(positions, symbol),
        max_concurrent_positions_per_sector=screener_config.max_concurrent_positions_per_sector,
        sector_position_count=_compute_sector_position_count(positions, symbol),
        total_position_value_usd=_compute_total_position_value(positions, symbol),
        open_portfolio_risk_usd=_compute_open_portfolio_risk_usd(symbol),
        trades_today_for_fund=trades_today_for_fund,
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
        strategy_id=strategy_id,
    )
    state["pending_orders"][pending_id] = pending
    _persist_state()
    audit.record(
        "signal_order_drafted", order.model_dump(),
        {"id": pending_id, "signal": result.model_dump(), "strategy_id": strategy_id},
    )
    return pending


async def _ensure_protective_stop(fund_id: str, symbol: str, stop_price: float) -> None:
    """Si `symbol` no tiene un stop-loss de venta vivo en IBKR (ver
    broker.has_live_protective_stop), coloca uno nuevo standalone al precio
    conocido y lo registra en el fondo.

    Se llama tanto despues de un fill tardio (ver _register_fund_fill_
    reconciliation) como desde la reconciliacion de arranque
    (_reconcile_unfilled_on_startup): el stop original de una orden que
    parecio "Cancelled" transitoriamente en IBKR (ver
    broker._register_stop_reconciliation, que cancela el stop cuando el
    padre aparenta estar muerto) probablemente ya se cancelo, dejando la
    posicion sin proteccion real una vez que el fill tardio finalmente
    llega -- este es exactamente el mecanismo que dejo una posicion real
    (MAMA, ver incidente de esta noche) sin ningun stop viviendola.

    Best-effort: cualquier error se loguea pero no interrumpe el flujo que
    llama (un fallo aca no debe tirar abajo el arranque del backend ni la
    reconciliacion de otros simbolos)."""
    try:
        if broker.has_live_protective_stop(symbol):
            return
        fund = funds_store.get(fund_id)
        if fund is None:
            return
        qty = fund.owned_quantity(symbol)
        if qty <= 0:
            return
        new_stop_order_id = await broker.place_protective_stop(symbol, qty, stop_price)
        if new_stop_order_id is not None:
            funds_store.set_stop_order_id(fund_id, symbol, new_stop_order_id)
            audit.record(
                "auto_trade_protective_stop_placed",
                {"fund_id": fund_id, "symbol": symbol},
                {"stop_order_id": new_stop_order_id, "stop_price": stop_price},
            )
            logger.info(
                "Stop-loss protector colocado para %s (fondo %s, orden %s, precio $%.4f)",
                symbol, fund_id, new_stop_order_id, stop_price,
            )
    except Exception:
        logger.exception(
            "No se pudo asegurar el stop-loss protector de %s (fondo %s)", symbol, fund_id
        )


def _register_fund_fill_reconciliation(
    order_id: "int | None",
    fund_id: str,
    symbol: str,
    side: Side,
    requested_qty: float,
    already_filled_qty: float,
    already_avg_price: float,
    stop_loss_price: "float | None",
    stop_order_id: "int | None",
    audit_action: str,
) -> None:
    """Si `already_filled_qty` es menor que `requested_qty` (la ventana de
    espera sincronica de broker.place_order, 5s, vio menos de lo pedido -- 0
    o una parte), se suscribe al fill tardio de `order_id` (ver
    broker.subscribe_fill) para registrar en el ledger del fondo lo que
    termine de llenar DESPUES, dentro de esta misma sesion de proceso.

    A diferencia de la version anterior (que solo cubria el caso 0%), esto
    tambien cubre un fill PARCIAL que se completa mas tarde: sin esto, la
    porcion adicional quedaba en IBKR sin que el ledger del fondo se
    enterara nunca (no se relanza como "*_submitted_unfilled" en el audit,
    asi que tampoco lo agarra _reconcile_unfilled_on_startup). El precio
    incremental se calcula netando el costo ya registrado del costo TOTAL
    final que reporta el callback (unico dato que ib_async expone), para que
    el costo promedio de la porcion nueva sea el correcto y no el promedio
    de toda la orden.

    Comun a _try_auto_trade_entry, submit_order y approve_order: antes de
    este fix, solo el auto-trade tenia esta red de seguridad -- una orden
    MANUAL con el mismo problema de fill tardio quedaba completamente sin
    cubrir hasta el proximo reinicio del backend."""
    if order_id is None or already_filled_qty >= requested_qty:
        return

    _already_qty = already_filled_qty
    _already_cost = already_filled_qty * already_avg_price

    def _on_late_fill(total_filled: float, avg_price: float) -> None:
        incremental_qty = total_filled - _already_qty
        if incremental_qty <= 0:
            return
        incremental_cost = avg_price * total_filled - _already_cost
        incremental_price = incremental_cost / incremental_qty
        funds_store.record_fill(
            fund_id, symbol, side, incremental_qty, incremental_price,
            stop_loss_price=stop_loss_price,
            stop_order_id=stop_order_id,
            commission=screener_config.commission_per_trade_usd,
        )
        audit.record(
            audit_action,
            {"symbol": symbol, "side": side.value, "fund_id": fund_id,
             "quantity": incremental_qty, "order_id": order_id},
            {"filled_qty": incremental_qty, "avg_fill_price": incremental_price,
             "total_filled_qty": total_filled, "fund_id": fund_id, "order_id": order_id},
        )
        logger.info(
            "Fill tardío registrado: %s %.4f %s × $%.4f (fondo %s, orden %s)",
            side.value, incremental_qty, symbol, incremental_price, fund_id, order_id,
        )
        # El callback de ib_async es sincronico, pero corre dentro del loop
        # de asyncio ya en marcha (ib_async es asyncio-nativo): create_task
        # agenda la verificacion/colocacion del stop sin bloquear el callback
        # ni requerir que sea async. Solo aplica a compras (una venta no deja
        # una posicion que proteger).
        if side == Side.BUY and stop_loss_price:
            asyncio.create_task(_ensure_protective_stop(fund_id, symbol, stop_loss_price))

    if not broker.subscribe_fill(order_id, _on_late_fill):
        logger.warning(
            "subscribe_fill: orden %s no encontrada en trades() — "
            "fill tardío solo detectable en próximo startup via reconciliación",
            order_id,
        )


async def _try_auto_trade_entry(result: SignalResult, strategy_id: str | None = None) -> None:
    """Para fondos con auto_trading_enabled cuya estrategia coincida con
    `strategy_id`, ejecuta la compra de inmediato (sin aprobacion manual) en
    vez de dejarla en borrador. Solo corre en modo paper: el auto-trading
    nunca opera en live, sin importar el toggle del fondo. La senal se asigna
    a un solo fondo (el primero con cupo, por orden de creacion, que pueda
    afrontar al menos 1 unidad) para que varios fondos con la misma estrategia
    no compitan por el mismo simbolo a la vez.

    `strategy_id` default None = la estrategia activa global
    (screener_config.strategy_id), igual que el comportamiento previo a que
    existiera fund.strategy_id. Un fondo con fund.strategy_id=None tambien
    sigue siempre esa misma estrategia global (nunca queda "huerfano" si la
    estrategia activa global cambia)."""
    if strategy_id is None:
        strategy_id = screener_config.strategy_id
    if state["mode"] != "paper" or result.last_price <= 0:
        return
    symbol = result.symbol
    async with _funds_order_lock:
        # Misma limitacion FIFO documentada en _draft_fund_order_from_signal:
        # candidates preserva el orden de creacion de los fondos, y mas abajo
        # gana el primero con cash suficiente -- sin rotacion entre 2+ fondos
        # activos que sigan la misma estrategia.
        candidates = [
            f for f in funds_store.list()
            if f.auto_trading_enabled
            and f.owned_quantity(symbol) == 0
            and (f.strategy_id or screener_config.strategy_id) == strategy_id
        ]
        if not candidates:
            return

        # Igual que en _draft_fund_order_from_signal: result.last_price puede
        # tener hasta auto_scan_interval_minutes de antiguedad, asi que se
        # refresca contra IBKR antes de sizear/ejecutar (que es lo sensible
        # al precio del momento), no antes de evaluar la señal en si.
        live_price = await broker.get_reference_price(symbol) or result.last_price

        account_summary = await broker.get_account_summary()
        position_qty = broker.get_position_qty(symbol)
        current_positions = await broker.get_positions()
        sector_exposure_usd = _compute_sector_exposure(current_positions, symbol)
        sector_position_count = _compute_sector_position_count(current_positions, symbol)

        # Si el stop-loss sugerido por la estrategia (basado en ATR) excede el
        # maximo permitido, se ajusta al tope en vez de rechazar la orden: el
        # riesgo se mantiene dentro del limite configurado y el auto-trade puede
        # proceder. El sizing se recalcula con el stop ajustado para que el
        # riesgo en dolares siga siendo coherente con risk_per_trade_pct.
        effective_stop_loss_price = result.suggested_stop_loss_price
        stop_adjusted_from_pct: float | None = None
        if live_price > 0 and effective_stop_loss_price > 0:
            implied_stop_pct = (live_price - effective_stop_loss_price) / live_price * 100
            if implied_stop_pct > rules_config.max_stop_loss_pct:
                stop_adjusted_from_pct = round(implied_stop_pct, 2)
                effective_stop_loss_price = round(
                    live_price * (1 - rules_config.max_stop_loss_pct / 100), 2
                )

        fund = None
        quantity = 0.0
        for candidate in candidates:
            sizing = rules_engine.suggested_quantity(
                candidate.equity_estimate(), position_qty, live_price, effective_stop_loss_price,
                score=result.score,
            )
            if sizing.quantity <= 0:
                continue
            affordable_qty = math.floor(candidate.cash_usd / live_price)
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
            limit_price=live_price,
            stop_loss_price=effective_stop_loss_price,
            fund_id=fund.id,
        )
        trades_today = audit.count_trades_today(rules_config.trading_hours_timezone)
        trades_today_for_fund = audit.count_trades_today(rules_config.trading_hours_timezone, fund_id=fund.id)
        decision = rules_engine.evaluate(
            order=order,
            account=account_summary,
            current_position_qty=position_qty,
            reference_price=live_price,
            trades_today=trades_today,
            halted=state["halted"],
            order_sector=get_sector(symbol),
            sector_exposure_usd=sector_exposure_usd,
            max_concurrent_positions_per_sector=screener_config.max_concurrent_positions_per_sector,
            sector_position_count=sector_position_count,
            total_position_value_usd=_compute_total_position_value(current_positions, symbol),
            open_portfolio_risk_usd=_compute_open_portfolio_risk_usd(symbol),
            trades_today_for_fund=trades_today_for_fund,
        )
        if not decision.approved:
            audit.record("auto_trade_rejected", order.model_dump(), decision.model_dump())
            return

        try:
            result_payload = await broker.place_order(order)
        except StopLossRejectedError as exc:
            # Si la compra llegó a ejecutarse antes de que fallara el stop,
            # registrar el fill en el fondo y colocar un stop de emergencia,
            # para no dejar la posición completamente sin protección ni sin
            # contabilizar. Sin este bloque, la posición quedaba huérfana en
            # IBKR y el scanner la volvía a intentar en cada ciclo.
            if exc.filled_qty > 0:
                fill_px = exc.avg_fill_price or live_price
                funds_store.record_fill(
                    fund.id, symbol, Side.BUY, exc.filled_qty, fill_px,
                    stop_loss_price=effective_stop_loss_price,
                    commission=screener_config.commission_per_trade_usd,
                )
                asyncio.create_task(
                    _ensure_protective_stop(fund.id, symbol, effective_stop_loss_price)
                )
            audit.record(
                "auto_trade_stop_loss_rejected",
                order.model_dump(),
                {
                    "error": str(exc),
                    "order_id": exc.order_id,
                    "stop_order_id": exc.stop_order_id,
                    "filled_qty": exc.filled_qty,
                    "avg_fill_price": exc.avg_fill_price,
                    "fund_id": fund.id,
                },
            )
            return

        filled_qty = result_payload.get("filled_qty") or 0.0
        if filled_qty > 0:
            fill_price = result_payload.get("avg_fill_price") or live_price
            funds_store.record_fill(
                fund.id, symbol, Side.BUY, filled_qty, fill_price,
                stop_loss_price=effective_stop_loss_price,
                stop_order_id=result_payload.get("stop_order_id"),
                commission=screener_config.commission_per_trade_usd,
            )
        audit_extra: dict = {"fund_id": fund.id, "signal": result.model_dump(), **result_payload}
        if stop_adjusted_from_pct is not None:
            audit_extra["stop_loss_adjusted_from_pct"] = stop_adjusted_from_pct
            audit_extra["stop_loss_adjusted_to_pct"] = rules_config.max_stop_loss_pct
        audit.record(
            "auto_trade_executed" if filled_qty > 0 else "auto_trade_submitted_unfilled",
            order.model_dump(),
            audit_extra,
        )

        # Si la orden no llenó del todo dentro de la ventana de 5s de
        # place_order() (nada, o solo una parte), nos suscribimos al fill
        # tardío -- ver _register_fund_fill_reconciliation. No sobrevive
        # reinicios: para ese caso existe _reconcile_unfilled_on_startup()
        # en lifespan.
        _register_fund_fill_reconciliation(
            order_id=result_payload.get("order_id"),
            fund_id=fund.id,
            symbol=symbol,
            side=Side.BUY,
            requested_qty=quantity,
            already_filled_qty=filled_qty,
            already_avg_price=result_payload.get("avg_fill_price") or live_price,
            stop_loss_price=effective_stop_loss_price,
            stop_order_id=result_payload.get("stop_order_id"),
            audit_action="auto_trade_fill_late",
        )


def _live_score_entry_threshold(strategy_id: str) -> float:
    """Umbral de score para el trigger de auto-trading en el scan en vivo,
    distinto por estrategia. Momentum/Oportunista reusan su umbral de
    backtest (backtest_score_entry_threshold): una sola fuente de verdad
    para "que tan bueno es lo bastante bueno" en cada una, en vez de
    duplicar el numero en un campo aparte. Largo Plazo/Dividendos no son
    backtesteables (sin historia point-in-time de fundamentals), asi que
    tienen su propio campo live_score_entry_threshold."""
    cfg = screener_config
    if strategy_id == "momentum":
        return cfg.backtest_score_entry_threshold
    if strategy_id == "opportunistic":
        return cfg.opportunistic.backtest_score_entry_threshold
    if strategy_id == "long_term":
        return cfg.long_term.live_score_entry_threshold
    if strategy_id == "dividend":
        return cfg.dividend.live_score_entry_threshold
    raise ValueError(f"strategy_id desconocido: {strategy_id!r}")


class _ExitParams(NamedTuple):
    """Parametros de salida por tiempo/tendencia para _check_fund_exit,
    propios de cada estrategia (ver _strategy_exit_params). `max_holding_days`
    None = sin limite de tiempo; `trend_break_enabled` False = no se chequea
    ruptura de tendencia en absoluto para esa estrategia (ni se pide la barra
    de precio de mas, ver _check_fund_exit). `sector_exit_roc_days` 0 =
    sin chequeo de sector."""
    max_holding_days: "int | None"
    trend_break_enabled: bool
    sma_period: "int | None"
    sector_exit_roc_days: int
    sector_exit_roc_threshold: float
    take_profit_pct: float  # 0 = deshabilitado


def _strategy_exit_params(strategy_id: str) -> _ExitParams:
    """Antes de este fix, _check_fund_exit usaba SIEMPRE
    screener_config.max_holding_days/sma_fast (los campos globales de
    Momentum) para decidir cuando cerrar una posicion, sin importar la
    estrategia real del fondo -- un fondo Oportunista (max_holding_days
    propio de 15 dias) se cerraba en cambio a los 20 dias de Momentum, y un
    futuro fondo Largo Plazo/Dividendos (tesis a meses/año, fundamentals-
    first) se hubiera cerrado por una ruptura de SMA de 20 dias que no tiene
    ninguna relacion con su tesis.

    - momentum: usa sus propios max_holding_days/sma_fast (la ruptura de
      tendencia es parte central de su tesis).
    - opportunistic: solo max_holding_days propio (15 por defecto); SIN
      ruptura de tendencia -- su entrada tampoco exige ninguna condicion de
      tendencia (ver strategies/opportunistic.py), asi que salir por romper
      una SMA que nunca formo parte de la señal de entrada no tiene tesis
      detras, solo agregaba una salida prestada de Momentum.
    - long_term / dividend: sin limite de tiempo (fundamentals-first, tesis
      a meses/año) y sin ruptura de tendencia -- solo salen por stop-loss.
    """
    cfg = screener_config
    if strategy_id == "momentum":
        return _ExitParams(cfg.max_holding_days, True, cfg.sma_fast, 0, 0.0, 0.0)
    if strategy_id == "opportunistic":
        opp = cfg.opportunistic
        return _ExitParams(opp.max_holding_days, False, None, opp.sector_exit_roc_days, opp.sector_exit_roc_threshold, opp.take_profit_pct)
    if strategy_id in ("long_term", "dividend"):
        return _ExitParams(None, False, None, 0, 0.0, 0.0)
    raise ValueError(f"strategy_id desconocido: {strategy_id!r}")


async def _check_market_data_degradation() -> None:
    """Detecta un feed de datos de mercado parcialmente degradado (ver
    market_data.get_bars_failure_stats) y lo hace visible en audit/WS: a
    diferencia de un feed totalmente caido (que ya corta el scan de entrada
    con MarketDataError, ver _run_score_recompute_cycle), un feed que solo
    falla para una FRACCION del universo deja pasar el scan "normalmente"
    con menos simbolos evaluados, sin ninguna señal para el usuario de que
    los scores/rankings actuales estan basados en datos incompletos.

    Debounced via state["market_data_degraded"]: solo emite audit/broadcast
    en la TRANSICION de estado (sano->degradado o degradado->sano), no en
    cada ciclo mientras el estado no cambia -- sin esto, cada ciclo de
    recompute (cada auto_scan_interval_minutes) inundaria el audit log
    mientras el feed sigue degradado.
    """
    failed, total = get_bars_failure_stats(screener_config.universe, screener_config.lookback_days)
    if total == 0:
        return
    failure_pct = failed / total * 100
    is_degraded = failure_pct >= screener_config.market_data_degradation_alert_pct
    was_degraded = state["market_data_degraded"]
    if is_degraded == was_degraded:
        return

    state["market_data_degraded"] = is_degraded
    payload = {"failed": failed, "total": total, "failure_pct": round(failure_pct, 1)}
    if is_degraded:
        audit.record("market_data_degraded", {}, payload)
        logger.warning(
            "Feed de datos de mercado degradado: %d/%d simbolos (%.1f%%) sin datos frescos.",
            failed, total, failure_pct,
        )
    else:
        audit.record("market_data_recovered", {}, payload)
        logger.info("Feed de datos de mercado recuperado: %d/%d simbolos sin datos frescos.", failed, total)
    await _broadcast({"type": "market_data_degraded", "degraded": is_degraded, **payload})


async def _run_score_recompute_cycle() -> None:
    """Un ciclo de recalculo de scores sobre el cache de datos: lee el cache de
    market_data.py (sin tocar la red), detecta simbolos que recien empiezan a
    pasar los filtros (transicion no-pasa -> pasa) y les arma una orden de
    compra en borrador. El refresco de datos lo hace _run_data_refresh_cycle
    por separado. No toma _market_scan_lock: no hay I/O de red de por medio.

    Separado de _score_recompute_loop (que solo aporta el sleep + while True)
    para poder testear un ciclo de una sola vez sin lidiar con un loop infinito.
    """
    if not screener_config.auto_scan_enabled or state["halted"] or not state["connected"]:
        return
    active_strategy = strategy_registry[screener_config.strategy_id]
    try:
        results = await asyncio.to_thread(active_strategy.scan, cache_only=True)
    except MarketDataError:
        # Cache todavia frio (ej. justo tras un restart, antes de que
        # _data_refresh_loop complete su primera pasada): reintentar en el
        # proximo ciclo sin loguear error -- es un estado transitorio normal.
        return
    except Exception as exc:
        audit.record("signal_scan_failed", {}, {"error": str(exc)})
        return

    await _check_market_data_degradation()

    now = datetime.now(timezone.utc)
    signal_cache[screener_config.strategy_id] = {
        "as_of": now,
        "results": [r.model_dump() for r in results],
    }

    top_results = results[: screener_config.top_n]
    # Gatillo de auto-trading: score >= umbral en vivo Y gates operativos
    # (liquidez, blackout de earnings, regimen, cercania al maximo de 52
    # semanas), NO passes_filters completo. passes_filters exige ademas los
    # filtros de CALIDAD propios de cada estrategia (ej. RSI en rango, yield
    # minimo): un score alto ya resume esa calidad de forma continua, asi que
    # exigir el AND booleano completo descartaria señales fuertes por un solo
    # filtro de calidad mas estricto que el listón de auto-trading (ver
    # operational_gates_ok en models.py).
    threshold = _live_score_entry_threshold(screener_config.strategy_id)
    passing_now = {r.symbol for r in top_results if r.score >= threshold and r.operational_gates_ok}
    # El lock es el mismo que toma update_screener_config (corre en un thread
    # del pool, no en el event loop, por ser un endpoint sync): sin compartirlo,
    # un reset de _signal_state tras un cambio de config en pleno vuelo de este
    # ciclo se podia perder -- este ciclo leia el valor previo al reset y lo
    # pisaba de nuevo con passing_now al escribir, devolviendo intacta la base
    # vieja que el reset queria descartar.
    async with _screener_config_lock:
        previously_passing = _signal_state["previously_passing"]
        _signal_state["previously_passing"] = passing_now
        _by_snap = dict(_signal_state["previously_passing_by_strategy"])

    await asyncio.to_thread(_store.save_signal_state, passing_now, _by_snap)

    if previously_passing is None:
        # Primer ciclo (o el primero tras un reset de config): solo establece
        # la base, sin generar borradores. Sin esto, cada simbolo que ya
        # viniera pasando los filtros desde antes de que arrancara el backend
        # (o desde el ultimo cambio de config) se draftearia de una al primer
        # ciclo, en vez de solo los que cambian de estado.
        pass
    else:
        new_symbols = passing_now - previously_passing
        if new_symbols:
            # new_signals queda ordenado por score (top_results ya viene
            # ordenado), asi que al recortar por el cap se conservan las
            # señales mas fuertes. El cap es el menor entre el tope por ciclo
            # y los cupos libres respecto a top_n (contando lo que ya esta
            # pendiente), para no sobre-asignar la cartera de un golpe. Errar
            # hacia MENOS ordenes automaticas es el lado seguro.
            new_signals = [r for r in top_results if r.symbol in new_symbols]

            # Intenta primero la entrada automatica por fondo (ver
            # _try_auto_trade_entry): se ejecuta antes del draft manual y usa
            # el mismo tope por ciclo, asi que si un fondo auto-trading ya
            # tomo la señal, el draft manual de abajo la salta solo (chequea
            # la posicion real en el broker, que ya quedo en no-cero).
            for r in new_signals[: screener_config.max_auto_drafts_per_cycle]:
                await _try_auto_trade_entry(r, screener_config.strategy_id)

            free_slots = max(0, screener_config.top_n - len(state["pending_orders"]))
            cap = min(screener_config.max_auto_drafts_per_cycle, free_slots)
            new_signals = new_signals[:cap]
            drafted = []
            for r in new_signals:
                p = await _draft_fund_order_from_signal(r, screener_config.strategy_id)
                if p is not None:
                    drafted.append(p)

            await _broadcast({
                "type": "signal_alert",
                "new_signals": [r.model_dump() for r in new_signals],
                "drafted_orders": [p.model_dump() for p in drafted],
            })

    # Fondos en auto-trading que eligieron explicitamente una estrategia
    # distinta a la activa global (ver fund.strategy_id) necesitan que ESA
    # estrategia tambien se escanee en este ciclo: si no, solo entrarian
    # señales de la estrategia global, sin importar lo que el fondo configuro.
    # Independiente de si hubo señales nuevas en la estrategia global (arriba):
    # son escaneos no relacionados.
    fund_strategy_ids = {
        f.strategy_id
        for f in funds_store.list()
        if f.auto_trading_enabled
        and not f.closed
        and f.strategy_id
        and f.strategy_id != screener_config.strategy_id
    }
    for extra_strategy_id in fund_strategy_ids:
        await _run_fund_strategy_auto_trade_scan(extra_strategy_id)


async def _run_fund_strategy_auto_trade_scan(strategy_id: str) -> None:
    """Mismo patron de deteccion de transiciones que _run_score_recompute_cycle,
    pero para una estrategia que ningun fondo usa como estrategia activa
    global: solo se llega aca cuando al menos un fondo en auto-trading elige
    explicitamente esta estrategia (fund.strategy_id), distinta de
    screener_config.strategy_id.

    Igual que _run_score_recompute_cycle, arma borradores manuales
    (_draft_fund_order_from_signal) para las señales que _try_auto_trade_entry
    no llego a auto-ejecutar ese ciclo (tope de max_auto_drafts_per_cycle
    superado, o ningun fondo candidato con cash suficiente) -- antes de este
    fix, esas señales se perdian en silencio sin dejar ningun rastro para
    que el usuario las tome a mano. Lleva su propio "previously_passing" por
    estrategia (ver _signal_state["previously_passing_by_strategy"]) para no
    compartir base con la estrategia activa global ni con otras estrategias
    de otros fondos. No toma _market_scan_lock: igual que
    _run_score_recompute_cycle, usa solo el cache de datos.
    """
    active_strategy = strategy_registry[strategy_id]
    try:
        results = await asyncio.to_thread(active_strategy.scan, cache_only=True)
    except MarketDataError:
        return
    except Exception as exc:
        audit.record("signal_scan_failed", {"strategy_id": strategy_id}, {"error": str(exc)})
        return

    top_results = results[: screener_config.top_n]
    threshold = _live_score_entry_threshold(strategy_id)
    passing_now = {r.symbol for r in top_results if r.score >= threshold and r.operational_gates_ok}

    # Mismo lock que _run_signal_scan_cycle y update_screener_config: ademas de
    # la razon de ahi, update_screener_config REEMPLAZA el dict completo
    # (_signal_state["previously_passing_by_strategy"] = {}), no lo muta in
    # place -- sin el lock, este ciclo podia guardarse una referencia al dict
    # VIEJO antes del reemplazo y escribir ahi, perdiendo la escritura sin que
    # _signal_state la vea nunca.
    async with _screener_config_lock:
        by_strategy = _signal_state["previously_passing_by_strategy"]
        previously_passing = by_strategy.get(strategy_id)
        by_strategy[strategy_id] = passing_now
        _pp_snap = _signal_state["previously_passing"]
        _by_snap2 = dict(by_strategy)

    await asyncio.to_thread(_store.save_signal_state, _pp_snap, _by_snap2)

    if previously_passing is None:
        return

    new_symbols = passing_now - previously_passing
    if not new_symbols:
        return
    new_signals = [r for r in top_results if r.symbol in new_symbols]
    for r in new_signals[: screener_config.max_auto_drafts_per_cycle]:
        await _try_auto_trade_entry(r, strategy_id)

    free_slots = max(0, screener_config.top_n - len(state["pending_orders"]))
    cap = min(screener_config.max_auto_drafts_per_cycle, free_slots)
    drafted = []
    for r in new_signals[:cap]:
        p = await _draft_fund_order_from_signal(r, strategy_id)
        if p is not None:
            drafted.append(p)

    if drafted:
        await _broadcast({
            "type": "signal_alert",
            "new_signals": [r.model_dump() for r in new_signals[:cap]],
            "drafted_orders": [p.model_dump() for p in drafted],
        })


async def _score_recompute_loop() -> None:
    """Recalculo de scores en background: corre cache_only (sin red) cada
    auto_scan_interval_minutes. El refresco de datos lo hace _data_refresh_loop."""
    while True:
        await asyncio.sleep(screener_config.auto_scan_interval_minutes * 60)
        await _run_score_recompute_cycle()


async def _risk_monitor_loop() -> None:
    """Kill switch automatico: a diferencia de RulesEngine.evaluate(), que solo
    chequea daily_loss_limit_pct cuando llega una orden nueva, esto corre en
    background y pausa el trading aunque no se envie ninguna orden mientras la
    cuenta sigue perdiendo (ej. por posiciones abiertas moviendose en contra).

    Dos circuit breakers independientes, sobre el mismo account_summary leido
    una sola vez por ciclo:
    1. daily_loss_limit_pct: perdida del DIA actual (se resetea solo, junto
       con daily_pnl_pct de IBKR).
    2. max_drawdown_pct: caida ACUMULADA desde el maximo historico de equity
       (state["peak_equity_usd"], persistido -- ver su definicion mas
       arriba). No se resetea nunca: una racha de perdidas repartida en
       varios dias, cada uno por debajo del umbral diario, igual la dispara
       si la suma cruza este umbral mas holgado.
    """
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        if not state["connected"] or state["halted"]:
            continue
        try:
            account = await broker.get_account_summary()
        except Exception:
            logger.exception("Kill switch: no se pudo leer el resumen de cuenta, se reintenta en el proximo ciclo")
            continue

        if account.daily_pnl_pct <= -abs(rules_config.daily_loss_limit_pct):
            state["halted"] = True
            _persist_state()
            audit.record("auto_halt_daily_loss_limit", {}, {"daily_pnl_pct": account.daily_pnl_pct})
            logger.warning(
                "[KILL SWITCH] Perdida diaria %.2f%% alcanzo el limite. Trading pausado automaticamente.",
                account.daily_pnl_pct,
            )
            continue

        equity = account.net_liquidation
        peak = state["peak_equity_usd"]
        if peak is None or equity > peak:
            state["peak_equity_usd"] = equity
            _persist_state()
        elif peak > 0:
            drawdown_pct = (peak - equity) / peak * 100
            if drawdown_pct >= abs(rules_config.max_drawdown_pct):
                state["halted"] = True
                _persist_state()
                audit.record(
                    "auto_halt_max_drawdown", {},
                    {"drawdown_pct": round(drawdown_pct, 2), "peak_equity_usd": peak, "equity_usd": equity},
                )
                logger.warning(
                    "[KILL SWITCH] Drawdown acumulado %.2f%% (maximo $%.2f -> actual $%.2f) alcanzo el limite. "
                    "Trading pausado automaticamente.",
                    drawdown_pct, peak, equity,
                )


async def _check_fund_scale_out(fund_id: str, symbol: str) -> None:
    """Si screener_config.scale_out_enabled y la posicion todavia no tuvo su
    salida parcial (position.scaled_out_at is None), vende
    scale_out_pct% de la posicion en cuanto la ganancia no realizada alcanza
    scale_out_at_r_multiple veces el riesgo inicial ("R" = avg_cost -
    initial_stop_loss_price, congelado al abrir la posicion -- ver
    FundPosition.initial_stop_loss_price), y mueve el stop-loss del remanente
    a breakeven (avg_cost) para que esa parte de la posicion quede sin riesgo
    de perdida neta.

    Debe llamarse SIEMPRE con _funds_order_lock ya tomado por el caller (ver
    _check_fund_exit): no lo toma por si sola para evitar un deadlock, ya que
    asyncio.Lock no es reentrante.
    """
    if not screener_config.scale_out_enabled:
        return
    fund = funds_store.get(fund_id)
    if fund is None:
        return
    position = fund.positions.get(symbol)
    if (
        position is None
        or position.quantity <= 0
        or position.scaled_out_at is not None
        or position.stop_order_id is None
        or position.initial_stop_loss_price is None
    ):
        return

    initial_risk = position.avg_cost - position.initial_stop_loss_price
    if initial_risk <= 0:
        return

    live_price = await broker.get_reference_price(symbol)
    if not live_price:
        return

    r_multiple = (live_price - position.avg_cost) / initial_risk
    if r_multiple < screener_config.scale_out_at_r_multiple:
        return

    sell_qty = math.floor(position.quantity * screener_config.scale_out_pct / 100)
    if sell_qty <= 0 or sell_qty >= position.quantity:
        return

    order = OrderRequest(
        symbol=symbol, side=Side.SELL, quantity=sell_qty, order_type=OrderType.MKT, fund_id=fund_id
    )
    try:
        result_payload = await broker.place_order(order)
    except StopLossRejectedError as exc:
        audit.record("auto_trade_scale_out_failed", order.model_dump(), {"error": str(exc)})
        return

    filled_qty = result_payload.get("filled_qty") or 0.0
    fill_price = result_payload.get("avg_fill_price") or live_price
    if filled_qty <= 0:
        audit.record("auto_trade_scale_out_unfilled", order.model_dump(), result_payload)
        return

    funds_store.record_fill(
        fund_id, symbol, Side.SELL, filled_qty, fill_price,
        commission=screener_config.commission_per_trade_usd,
    )
    funds_store.mark_scaled_out(fund_id, symbol)

    new_stop = position.avg_cost
    if broker.modify_stop_price(position.stop_order_id, new_stop):
        funds_store.update_stop_loss(fund_id, symbol, new_stop)

    audit.record(
        "auto_trade_scale_out", order.model_dump(),
        {"r_multiple": round(r_multiple, 2), "new_stop": new_stop, **result_payload},
    )
    await _broadcast({
        "type": "auto_trade_scale_out", "fund_id": fund_id, "symbol": symbol,
        "quantity": filled_qty, "price": fill_price,
    })


async def _check_fund_exit(fund_id: str, symbol: str) -> None:
    """Evalua si una posicion abierta por el motor de auto-trading debe
    cerrarse, y si corresponde la vende entera (siempre atada a ese fund_id).

    Antes de evaluar el cierre total, llama a _check_fund_scale_out: si
    scale_out_enabled esta activo y todavia no se hizo la salida parcial de
    esta posicion, puede vender una porcion y mover el stop a breakeven,
    dejando la posicion abierta (mas chica) para lo que sigue de esta
    funcion.

    Tres motivos posibles de cierre TOTAL, en este orden:
    1. Reconciliacion: si el stop-loss que se coloco como orden bracket al
       abrir la posicion (ver broker.place_order) ya se ejecuto del lado de
       IBKR sin pasar por record_fill, hay que reconciliar la diferencia para
       que el fondo no quede con una posicion fantasma. Se prefiere
       broker.get_trade_fill(stop_order_id), que da el `remaining` de ESA
       orden puntual: a diferencia de broker.get_position_qty (agregado de
       TODA la cuenta, sin nocion de fondos), no se confunde si otro fondo
       tiene una posicion abierta en el mismo simbolo. Solo si esa orden no
       se encuentra (reconexion entre sesiones, o posicion abierta antes de
       este cambio sin stop_order_id guardado) se cae al agregado de cuenta
       como antes -- aceptando el riesgo de colision entre fondos que ya
       tenia ese camino, documentado donde se usa abajo.
    2. max_holding_days: misma regla que ya se simula en backtest.py, ahora
       aplicada en vivo sobre la fecha real de apertura. Propio de la
       estrategia del fondo (ver _strategy_exit_params), no siempre el mismo
       numero.
    3. trend_break: el precio cierra por debajo de la SMA rapida del
       screener, igual que en backtest.py. Solo aplica si la estrategia del
       fondo la tiene habilitada (ver _strategy_exit_params) -- Momentum si,
       el resto no.
    """
    async with _funds_order_lock:
        fund = funds_store.get(fund_id)
        if fund is None:
            return
        position = fund.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return

        reconciled_via_order = False
        if position.stop_order_id is not None:
            fill = broker.get_trade_fill(position.stop_order_id)
            if fill is not None:
                _status, _filled, avg_fill_price, remaining = fill
                order_remaining = max(remaining, 0.0)
                if order_remaining < position.quantity:
                    closed_qty = position.quantity - order_remaining
                    fill_price = (
                        avg_fill_price if avg_fill_price is not None
                        else (position.stop_loss_price or position.avg_cost)
                    )
                    funds_store.record_fill(
                        fund_id, symbol, Side.SELL, closed_qty, fill_price,
                        commission=screener_config.commission_per_trade_usd,
                    )
                    audit.record(
                        "auto_trade_stop_loss_reconciled",
                        {"fund_id": fund_id, "symbol": symbol},
                        # approximate=True marca que fill_price es el stop teorico
                        # (o avg_cost), no el fill real reportado por IBKR -- pasa
                        # cuando avgFillPrice viene vacio/0 para esta orden. Sin
                        # esta marca un PnL con error sistematico queda indistinguible
                        # de uno exacto en el audit log.
                        {"quantity": closed_qty, "price": fill_price, "approximate": avg_fill_price is None},
                    )
                    fund = funds_store.get(fund_id)
                    position = fund.positions.get(symbol) if fund else None
                    if position is None or position.quantity <= 0:
                        return
                reconciled_via_order = True

        # Sin stop_order_id, o la orden ya no esta en self.ib.trades() de esta
        # sesion (ver get_trade_fill, tipicamente tras un restart/reconnect):
        # unico caso donde se cae al agregado de TODA la cuenta, que puede
        # confundir posiciones de otros fondos en el mismo simbolo -- mejor
        # que no reconciliar nada. El precio aca siempre es el stop teorico
        # (no hay forma de recuperar el fill real sin rastro de la orden en
        # esta sesion), por eso approximate=True siempre en esta rama.
        if not reconciled_via_order:
            broker_qty = broker.get_position_qty(symbol)
            if broker_qty < position.quantity:
                closed_qty = position.quantity - max(broker_qty, 0.0)
                fill_price = position.stop_loss_price or position.avg_cost
                funds_store.record_fill(
                    fund_id, symbol, Side.SELL, closed_qty, fill_price,
                    commission=screener_config.commission_per_trade_usd,
                )
                audit.record(
                    "auto_trade_stop_loss_reconciled",
                    {"fund_id": fund_id, "symbol": symbol},
                    {"quantity": closed_qty, "price": fill_price, "approximate": True},
                )
                fund = funds_store.get(fund_id)
                position = fund.positions.get(symbol) if fund else None
                if position is None or position.quantity <= 0:
                    return

        await _check_fund_scale_out(fund_id, symbol)
        fund = funds_store.get(fund_id)
        position = fund.positions.get(symbol) if fund else None
        if position is None or position.quantity <= 0:
            return

        exit_params = _strategy_exit_params(fund.strategy_id or screener_config.strategy_id)

        held_days = (datetime.now(timezone.utc) - position.opened_at).days if position.opened_at else 0
        timed_out = exit_params.max_holding_days is not None and held_days >= exit_params.max_holding_days

        trend_broke = False
        if exit_params.trend_break_enabled:
            try:
                bars = await asyncio.to_thread(get_daily_bars, symbol, exit_params.sma_period + 5)
                sma_fast_s = sma(bars["Close"], exit_params.sma_period)
                if len(sma_fast_s) and not bool(sma_fast_s.isna().iloc[-1]):
                    trend_broke = float(bars["Close"].iloc[-1]) < float(sma_fast_s.iloc[-1])
            except MarketDataError:
                pass

        sector_broke = False
        if exit_params.sector_exit_roc_days > 0:
            # Solo disparar cuando el régimen global es alcista: en régimen
            # bajista el filtro de entrada ya bloqueó nuevas posiciones, y
            # expulsar las existentes por sector les quita tiempo de recuperarse
            # antes del stop natural (el walk-forward mostró retorno negativo en
            # mercados bajistas sostenidos con sector exit activo sin esta guarda).
            # El regime vuelve a habilitarlo automáticamente al recuperar.
            cfg = screener_config
            regime_bullish = True
            if cfg.opportunistic_regime_filter_enabled:
                try:
                    bench_lookback = (
                        cfg.regime_sma_period + cfg.regime_slope_lookback_days
                        + cfg.regime_absolute_momentum_lookback_days + 10
                    )
                    bench_bars = await asyncio.to_thread(get_daily_bars, cfg.benchmark_symbol, bench_lookback)
                    regime_s = market_regime_ok(
                        bench_bars["Close"], cfg.regime_sma_period,
                        cfg.regime_slope_lookback_days, cfg.regime_absolute_momentum_lookback_days,
                    )
                    if len(regime_s) and not pd.isna(regime_s.iloc[-1]):
                        regime_bullish = bool(regime_s.iloc[-1])
                except MarketDataError:
                    pass  # fallback conservador: asumir alcista y dejar que el sector decida

            if regime_bullish:
                sector = get_sector(symbol)
                etf = SECTOR_ETF.get(sector) if sector else None
                if etf:
                    try:
                        n = exit_params.sector_exit_roc_days
                        etf_bars = await asyncio.to_thread(get_daily_bars, etf, n + 5)
                        etf_roc = rate_of_change(etf_bars["Close"], n)
                        if len(etf_roc) and not pd.isna(etf_roc.iloc[-1]):
                            sector_broke = float(etf_roc.iloc[-1]) < exit_params.sector_exit_roc_threshold
                    except MarketDataError:
                        pass

        # Precio de referencia: se pide una sola vez cuando hay al menos un
        # trigger activo o cuando take-profit está habilitado. Así se evita
        # llamar al broker en cada ciclo (sin exit triggers y sin TP) y
        # también se evita una segunda llamada redundante si la primera ya
        # sirvió para el check de take-profit.
        hit_target = False
        reference_price: "float | None" = None
        if timed_out or trend_broke or sector_broke or exit_params.take_profit_pct > 0:
            reference_price = await broker.get_reference_price(symbol)
            if reference_price and exit_params.take_profit_pct > 0 and position.avg_cost > 0:
                hit_target = reference_price >= position.avg_cost * (1 + exit_params.take_profit_pct / 100)

        if not (timed_out or trend_broke or sector_broke or hit_target):
            return

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

        # Solo se registra en el ledger del fondo lo que el broker confirmo
        # como realmente ejecutado (mismo criterio que _try_auto_trade_entry):
        # asumir que se lleno la cantidad pedida sin chequear filled_qty podia
        # dejar el ledger con la posicion en cero mientras IBKR todavia la
        # tenia abierta (orden parcial o todavia en curso).
        filled_qty = result_payload.get("filled_qty") or 0.0
        fill_price = result_payload.get("avg_fill_price") or reference_price
        if filled_qty > 0:
            funds_store.record_fill(
                fund_id, symbol, Side.SELL, filled_qty, fill_price,
                commission=screener_config.commission_per_trade_usd,
            )
        reason = ("take_profit" if hit_target else ("max_holding_days" if timed_out else ("trend_break" if trend_broke else "sector_exit")))
        audit.record(
            "auto_trade_exit" if filled_qty > 0 else "auto_trade_exit_unfilled",
            order.model_dump(),
            {"reason": reason, **result_payload},
        )
    await _broadcast({"type": "auto_trade_exit", "fund_id": fund_id, "symbol": symbol, "reason": reason})


async def _check_fund_trailing_stop(fund_id: str, symbol: str) -> None:
    """Si trailing_stop_enabled, sube (nunca baja) el stop-loss ya colocado en
    IBKR de una posicion abierta a medida que el precio se mueve a favor,
    usando la misma distancia en ATR que el stop inicial
    (stop_loss_atr_multiplier): nuevo_stop = precio_en_vivo - ATR_actual *
    stop_loss_atr_multiplier.

    El precio se pide a IBKR en vivo (broker.get_reference_price) en vez de
    tomar el cierre de la ultima barra diaria: a diferencia del ATR (que no
    cambia significativamente segundo a segundo y se sigue tomando de la
    barra cacheada), el precio si necesita ser actual para que el trailing
    reaccione dentro del mismo ciclo rapido (poll_interval_seconds, ver
    _trailing_stop_loop) en el que se mueve el mercado, en vez de esperar al
    proximo cierre diario.

    Solo actua si hay un stop_order_id de ESTA sesion para modificar en IBKR
    (ver broker.modify_stop_price; self.ib.trades() no cubre sesiones
    anteriores, misma limitacion que get_trade_fill) y si esa modificacion
    tuvo exito -- el ledger del fondo nunca debe registrar un stop mas
    favorable que el que de verdad protege la posicion en el broker."""
    if not screener_config.trailing_stop_enabled:
        return
    fund = funds_store.get(fund_id)
    if fund is None:
        return
    position = fund.positions.get(symbol)
    if position is None or position.quantity <= 0 or position.stop_order_id is None:
        return

    try:
        bars = await asyncio.to_thread(get_daily_bars, symbol, screener_config.atr_period + 5)
    except MarketDataError:
        return
    atr_s = atr(bars["High"], bars["Low"], bars["Close"], screener_config.atr_period)
    if not len(atr_s) or bool(atr_s.isna().iloc[-1]):
        return

    live_price = await broker.get_reference_price(symbol)
    if live_price is None:
        return

    trail_mult = screener_config.trailing_stop_atr_multiplier if screener_config.trailing_stop_atr_multiplier is not None else screener_config.stop_loss_atr_multiplier
    activation_pct = screener_config.trailing_stop_activation_pct
    if activation_pct > 0:
        avg_cost = position.avg_cost or 0.0
        if avg_cost > 0 and live_price < avg_cost * (1 + activation_pct / 100):
            return
    new_stop = live_price - float(atr_s.iloc[-1]) * trail_mult
    current_stop = position.stop_loss_price
    if current_stop is not None and new_stop <= current_stop:
        return

    if not broker.modify_stop_price(position.stop_order_id, new_stop):
        return
    funds_store.update_stop_loss(fund_id, symbol, new_stop)
    audit.record(
        "auto_trade_trailing_stop_updated",
        {"fund_id": fund_id, "symbol": symbol},
        {"old_stop": current_stop, "new_stop": new_stop},
    )


async def _ensure_missing_protective_stops() -> None:
    """Para cada posición en fondos con auto-trading que no tenga un stop-loss
    activo en IBKR, coloca uno. Cubre dos casos:

    1. stop_loss_price ya conocido pero sin stop_order_id ni orden IBKR viva
       (ej: aprobación manual de borrador que no pasó por el flujo de auto-trade,
       o stop que murió y no se recolocó).
    2. stop_loss_price nulo: calcula uno basado en ATR de la estrategia del fondo
       y lo coloca desde cero.

    Idempotente: broker.has_live_protective_stop evita duplicados. Solo actúa
    en modo paper con la cuenta conectada y sin halt."""
    if state["mode"] != "paper" or state["halted"] or not state["connected"]:
        return
    for fund in funds_store.list():
        if not fund.auto_trading_enabled:
            continue
        for symbol, position in list(fund.positions.items()):
            if position.quantity <= 0:
                continue
            if broker.has_live_protective_stop(symbol):
                continue
            stop_px = position.stop_loss_price
            if stop_px is None:
                # Calcular stop ATR con el multiplicador de la estrategia del fondo
                try:
                    bars = await asyncio.to_thread(
                        get_daily_bars, symbol, screener_config.atr_period + 5
                    )
                except MarketDataError:
                    continue
                atr_s = atr(bars["High"], bars["Low"], bars["Close"], screener_config.atr_period)
                if not len(atr_s) or bool(atr_s.isna().iloc[-1]):
                    continue
                stop_mult = (
                    screener_config.opportunistic.stop_loss_atr_multiplier
                    if fund.strategy_id == "opportunistic"
                    else screener_config.stop_loss_atr_multiplier
                )
                stop_px = round(position.avg_cost - float(atr_s.iloc[-1]) * stop_mult, 2)
                if stop_px <= 0:
                    continue
                funds_store.update_stop_loss(fund.id, symbol, stop_px)
                logger.info(
                    "missing_stop: calculado stop ATR para %s → $%.2f (mult=%.1f)",
                    symbol, stop_px, stop_mult,
                )
            await _ensure_protective_stop(fund.id, symbol, stop_px)


async def _run_auto_exit_monitor_cycle() -> None:
    """Revisa, para cada fondo con auto-trading activado, sus posiciones
    abiertas, y evalua si corresponde cerrarlas (ver _check_fund_exit). El
    trailing stop corre por separado en _trailing_stop_loop, a una cadencia
    mas rapida (ver ese comentario). Solo en modo paper y con la cuenta
    conectada y sin halt, igual que la entrada automatica."""
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


async def _run_trailing_stop_monitor_cycle() -> None:
    """Mismo recorrido de fondos/posiciones que _run_auto_exit_monitor_cycle,
    pero solo para el trailing stop (ver _check_fund_trailing_stop), separado
    para poder correr a una cadencia mas rapida (poll_interval_seconds) sin
    repetir tambien el chequeo de salida por trend-break/max-holding-days, que
    no se beneficia de revisarse mas seguido.

    También llama a _ensure_missing_protective_stops para cubrir posiciones
    que no tienen stop activo en IBKR (ej: aprobadas en batch sin pasar
    por el flujo de auto-trade)."""
    if state["mode"] != "paper" or state["halted"] or not state["connected"]:
        return
    try:
        await _ensure_missing_protective_stops()
    except Exception as exc:
        logger.warning("ensure_missing_protective_stops failed: %s", exc)
    for fund in funds_store.list():
        if not fund.auto_trading_enabled:
            continue
        for symbol in list(fund.positions.keys()):
            try:
                await _check_fund_trailing_stop(fund.id, symbol)
            except Exception as exc:
                audit.record(
                    "auto_trade_trailing_stop_check_failed", {"fund_id": fund.id, "symbol": symbol}, {"error": str(exc)}
                )


async def _trailing_stop_loop() -> None:
    """Sube el trailing stop a la misma cadencia que _broadcast_loop
    (poll_interval_seconds) en vez de auto_scan_interval_minutes: a
    diferencia de la señal de entrada/salida (atada a cierres diarios), el
    trailing stop solo necesita precio actual -- ya disponible cada pocos
    segundos via la misma conexion a IBKR -- y el ATR de la ultima barra
    cacheada (eso si, sin necesidad de ser "en vivo"). Sin este loop
    separado, una caida o suba fuerte del precio entre ciclos de 30 min
    quedaria sin reflejarse en el stop hasta el proximo scan."""
    while True:
        await asyncio.sleep(settings.poll_interval_seconds)
        await _run_trailing_stop_monitor_cycle()


async def _run_hot_set_cycle() -> None:
    """Recalcula el hot-set del radar en vivo: los simbolos con mayor score
    MAXIMO entre las 4 estrategias, hasta live_hot_symbols_cap. Dinamico:
    diffea contra el hot-set anterior y solo suscribe/desuscribe streaming de
    IBKR lo que cambio, no todo el set en cada ciclo.

    Lee directamente de signal_cache (sin disparar scans de red): el cache lo
    mantienen _score_recompute_loop y los endpoints HTTP. Si el cache esta vacio
    (ej. justo tras un restart), no hay hot-set todavia y se vuelve en el
    proximo ciclo. Esto evita que _hot_set_loop dispare un scan completo bajo
    _market_scan_lock en el arranque, que era lo que mantenia market_scan_busy
    en True indefinidamente.

    Las altas se intentan antes que las bajas: si suscribir el nuevo hot-set
    falla (ej. error de IBKR), el hot-set anterior queda intacto en vez de
    quedar a mitad de camino sin las lineas viejas ni las nuevas."""
    global _hot_symbols
    if not screener_config.live_radar_enabled or not state["connected"]:
        return
    try:
        strategy_filter = screener_config.live_radar_strategy_filter
        best_by_symbol: dict[str, dict] = {}
        for strategy_id, cached in signal_cache.items():
            if strategy_filter is not None and strategy_id not in strategy_filter:
                continue
            for r in cached.get("results", []):
                current = best_by_symbol.get(r["symbol"])
                if current is None or r["score"] > current["score"]:
                    best_by_symbol[r["symbol"]] = r
        if not best_by_symbol:
            return
        ranked = sorted(best_by_symbol.values(), key=lambda r: r["score"], reverse=True)
    except Exception as exc:
        logger.warning("No se pudo recalcular el hot-set del radar en vivo: %s", exc)
        return
    cap = max(0, screener_config.live_hot_symbols_cap)
    new_hot = {r["symbol"] for r in ranked[:cap]}
    to_add = new_hot - _hot_symbols
    to_remove = _hot_symbols - new_hot
    if to_add:
        try:
            await broker.stream_subscribe(list(to_add))
        except Exception as exc:
            logger.warning("No se pudo suscribir streaming de IBKR para %s: %s", sorted(to_add), exc)
            return
    if to_remove:
        broker.stream_unsubscribe(list(to_remove))
    _hot_symbols = new_hot


async def _hot_set_loop() -> None:
    """Misma cadencia que el TTL del cache de señales: no tiene sentido
    recalcular el hot-set mas seguido que lo que tarda en cambiar algun score
    (signal_cache no se refresca antes de eso de todas formas)."""
    while True:
        await _run_hot_set_cycle()
        await asyncio.sleep(SIGNAL_CACHE_TTL_SECONDS)


async def _run_price_rotation_cycle() -> None:
    """Snapshot rotativo sobre el resto del universo (los simbolos que no
    estan en el hot-set), usando las lineas de market data que el hot-set no
    esta ocupando. Cada ciclo pide un lote nuevo (round-robin) para cubrir
    todo el universo en varias pasadas en vez de intentarlo de una sola vez,
    de forma que entre el hot-set y la rotacion nunca se exceda de golpe el
    limite gratuito de 100 lineas simultaneas de IBKR."""
    global _rotation_cursor
    if not screener_config.live_radar_enabled or not state["connected"]:
        return
    cold_symbols = [s for s in screener_config.universe if s not in _hot_symbols]
    if not cold_symbols:
        return
    free_lines = max(0, 100 - len(_hot_symbols))
    batch_size = min(screener_config.live_rotation_batch_size, free_lines, len(cold_symbols))
    if batch_size <= 0:
        return
    n = len(cold_symbols)
    start = _rotation_cursor % n
    batch = [cold_symbols[(start + i) % n] for i in range(batch_size)]
    _rotation_cursor = (start + batch_size) % n
    try:
        prices = await broker.get_snapshot_prices(batch)
    except Exception as exc:
        logger.warning("Error al rotar precios en vivo del radar: %s", exc)
        return
    _live_prices.update(prices)
    now = datetime.now(timezone.utc)
    for symbol in prices:
        _live_prices_as_of[symbol] = now


async def _price_rotation_loop() -> None:
    while True:
        await _run_price_rotation_cycle()
        await asyncio.sleep(settings.poll_interval_seconds)


async def _run_data_refresh_cycle() -> None:
    """Refresco trickle de datos de yfinance: recorre el universo en lotes
    chicos round-robin, manteniendo caliente el cache de market_data.py para
    que _run_score_recompute_cycle pueda correr siempre con cache_only=True.
    Modelado sobre _run_price_rotation_cycle: lotes chicos, cursor propio,
    bajo _market_scan_lock solo durante el fetch real (no durante el sleep
    entre simbolos)."""
    global _data_refresh_cursor
    universe = screener_config.universe
    if not universe:
        return
    n = len(universe)
    batch_size = screener_config.data_refresh_batch_size
    start = _data_refresh_cursor % n
    batch = [universe[(start + i) % n] for i in range(min(batch_size, n))]
    _data_refresh_cursor = (start + len(batch)) % n
    delay = screener_config.scan_request_delay_seconds
    first_fetch = True
    for symbol in batch:
        bars_cached = is_bars_cached(symbol, screener_config.lookback_days)
        funds_cached = is_fundamentals_cached(symbol)
        if bars_cached and funds_cached:
            continue
        if not first_fetch and delay > 0:
            await asyncio.sleep(delay)
        first_fetch = False
        if not bars_cached:
            try:
                # Sin _market_scan_lock: get_daily_bars/_fundamentals_locks por
                # simbolo ya previenen fetches duplicados concurrentes.
                await asyncio.to_thread(get_daily_bars, symbol, screener_config.lookback_days)
            except Exception:
                pass  # fallo cacheado; no reintentar hasta que expire el TTL
        if not funds_cached:
            try:
                await asyncio.to_thread(get_fundamentals, symbol)
            except Exception:
                pass


async def _data_refresh_loop() -> None:
    """Trickle feed de yfinance en background: mantiene caliente el cache de
    barras de precio para que _score_recompute_loop nunca tenga que esperar a
    la red."""
    while True:
        await _run_data_refresh_cycle()
        await asyncio.sleep(settings.poll_interval_seconds)


async def _restore_persisted_mode() -> None:
    """Si el estado persistido indica un modo distinto al que arranco el
    broker (ej. el backend se reinicio mientras estaba en modo live), reconecta
    al puerto correspondiente para que state['mode'] no mienta sobre a que
    cuenta esta conectado realmente el broker.

    Si el modo restaurado es live, el trading queda pausado (kill switch)
    aunque state['halted'] persistido fuera False: mismo criterio que
    set_mode() al cambiar a live explicitamente desde el dashboard. Sin esto,
    reiniciar el backend mientras estaba en live y sin halt reanudaria el
    motor de auto-trading operando con dinero real sin ninguna confirmacion
    humana posterior al reinicio.
    """
    if not state["connected"] or state["mode"] == settings.trading_mode:
        return
    if state["mode"] == "live" and not settings.live_confirm:
        logger.warning(
            "El estado persistido indica modo live pero LIVE_CONFIRM no "
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
        if state["mode"] == "live":
            state["halted"] = True
        _persist_state()
        logger.info("Modo restaurado desde estado persistido: %s", state["mode"])
    except IBKRConnectionError as exc:
        state["connected"] = False
        state["mode"] = settings.trading_mode
        _persist_state()
        logger.warning("No se pudo restaurar el modo persistido tras el reinicio: %s", exc)


async def _reconcile_unfilled_on_startup() -> None:
    """Al arrancar (o reconectar), compara entradas *_submitted_unfilled del
    audit con las posiciones reales de IBKR. Si IBKR tiene acciones de un
    símbolo que el fondo correspondiente no registra, asume que el fill llegó
    tarde (después del timeout de _wait_for_fill o de un reinicio) y lo
    registra retroactivamente usando el avg_cost de IBKR como precio proxy.
    Si la orden TODAVÍA no muestra una posición en IBKR (sigue pendiente),
    se suscribe al fill tardío en vez de descartarla para siempre -- ver el
    incidente que motivó este fix, dos párrafos abajo.

    Es seguro correrlo varias veces: solo reconcilia la diferencia positiva
    entre lo que IBKR tiene y lo que el fondo ya registra, nunca duplica.

    Antes de este fix, esta función sólo miraba el estado de IBKR UNA VEZ al
    arrancar: si la orden aún no había llenado en ese instante exacto (pero
    seguía viva en IBKR), quedaba descartada para siempre sin que nada
    volviera a chequearla. Esto dejó una posición real (MAMA) completamente
    huérfana durante ~3hs: IBKR reportó la orden como "Cancelled" a los 5s de
    _wait_for_fill (un falso negativo transitorio de paper trading), lo cual
    canceló su stop-loss protector, y recién llenó de verdad varias horas
    después -- coincidiendo con dos reinicios del backend en el medio, cada
    uno perdiendo la suscripción de fill tardío en memoria y encontrando,
    en su chequeo único de arranque, que la posición todavía no existía.
    """
    entries = audit.get_untracked_fills(since_days=7)
    if not entries:
        return

    try:
        ibkr_positions = {p.symbol: p for p in await broker.get_positions()}
    except Exception as exc:
        logger.warning("reconcile_unfilled: no se pudieron leer posiciones de IBKR: %s", exc)
        return

    for entry in entries:
        p   = entry.get("payload", {})
        r   = entry.get("result", {})
        sym = p.get("symbol")
        fund_id = r.get("fund_id") or p.get("fund_id")
        requested_qty = float(p.get("quantity") or 0)
        stop_px = p.get("stop_loss_price")
        order_id = r.get("order_id")

        if not (sym and fund_id and requested_qty > 0):
            continue

        fund = funds_store.get(fund_id)
        if fund is None:
            continue

        ibkr_pos = ibkr_positions.get(sym)
        if ibkr_pos is None:
            # La orden no muestra posicion en IBKR TODAVIA, pero puede seguir
            # viva y llenar mas tarde (ver docstring). self.ib.trades() SI
            # recuerda ordenes abiertas de sesiones anteriores tras
            # reconectar (confirmado en los logs del incidente de MAMA), asi
            # que subscribe_fill puede encontrarla igual y capturar el fill
            # sin importar cuanto tarde, mientras el proceso siga vivo.
            _register_fund_fill_reconciliation(
                order_id=order_id,
                fund_id=fund_id,
                symbol=sym,
                side=Side.BUY,
                requested_qty=requested_qty,
                already_filled_qty=0.0,
                already_avg_price=0.0,
                stop_loss_price=stop_px,
                stop_order_id=r.get("stop_order_id"),
                audit_action="auto_trade_reconciled_late",
            )
            continue

        # Diferencia entre lo que IBKR tiene y lo que el fondo ya registra;
        # acotada a lo pedido en la orden para no sobre-asignar si el usuario
        # tiene acciones adicionales en la cuenta general.
        untracked = min(requested_qty, ibkr_pos.quantity - fund.owned_quantity(sym))
        if untracked <= 0:
            continue

        fill_price = ibkr_pos.avg_cost
        logger.info(
            "reconcile_unfilled: %s %.0f × $%.4f → fondo %s (audit id %s)",
            sym, untracked, fill_price, fund_id, entry["id"],
        )
        funds_store.record_fill(
            fund_id, sym, Side.BUY, untracked, fill_price,
            stop_loss_price=stop_px,
            commission=screener_config.commission_per_trade_usd,
        )
        audit.record(
            "auto_trade_reconciled",
            {"symbol": sym, "side": "BUY", "fund_id": fund_id,
             "quantity": untracked, "source_audit_id": entry["id"]},
            {"filled_qty": untracked, "avg_fill_price": fill_price, "fund_id": fund_id},
        )
        # La posicion recien reconciliada puede no tener ningun stop vivo
        # protegiendola (ver docstring de _ensure_protective_stop): se
        # coloca uno nuevo si hace falta, antes de seguir con el resto de
        # las entradas.
        if stop_px:
            await _ensure_protective_stop(fund_id, sym, stop_px)

    # Segunda pasada: posiciones en IBKR que no están en ningún fondo.
    # Cubre el caso donde órdenes se ejecutaron antes de que los trades
    # estuvieran atados al fondo (código anterior al fix #28), o cuando un
    # crash-loop impidió que record_fill se guardara a disco.
    try:
        ibkr_positions_all = {p.symbol: p for p in await broker.get_positions()}
    except Exception as exc:
        logger.warning("reconcile_orphan: no se pudieron leer posiciones de IBKR: %s", exc)
        return

    all_funds = funds_store.list()
    auto_fund = next((f for f in all_funds if f.auto_trading_enabled), None)
    if auto_fund is None:
        return

    for sym, ibkr_pos in ibkr_positions_all.items():
        covered_qty = sum(f.owned_quantity(sym) for f in all_funds)
        orphan_qty = ibkr_pos.quantity - covered_qty
        if orphan_qty <= 0:
            continue
        # Solo reconciliar posiciones que el propio sistema sometió alguna vez:
        # posiciones colocadas manualmente en IBKR fuera del sistema se ignoran
        # para no mezclar capital externo con el presupuesto del fondo.
        if not audit.was_submitted_by_system(sym):
            logger.warning(
                "reconcile_orphan: %s tiene %.0f acc en IBKR sin entrada en el sistema "
                "— se IGNORA (posición externa, colocar manualmente si corresponde)",
                sym, orphan_qty,
            )
            continue
        fill_price = ibkr_pos.avg_cost
        stop_px = audit.get_last_stop_price(sym)
        logger.info(
            "reconcile_orphan: %s %.0f × $%.4f → fondo %s (stop=%s)",
            sym, orphan_qty, fill_price, auto_fund.id,
            f"${stop_px:.4f}" if stop_px else "no encontrado",
        )
        funds_store.record_fill(
            auto_fund.id, sym, Side.BUY, orphan_qty, fill_price,
            stop_loss_price=stop_px,
            commission=0.0,
        )
        audit.record(
            "auto_trade_reconciled",
            {"symbol": sym, "side": "BUY", "fund_id": auto_fund.id,
             "quantity": orphan_qty, "source": "orphan_ibkr_position"},
            {"filled_qty": orphan_qty, "avg_fill_price": fill_price,
             "fund_id": auto_fund.id, "stop_loss_price": stop_px},
        )
        if stop_px:
            await _ensure_protective_stop(auto_fund.id, sym, stop_px)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await broker.connect()
        state["connected"] = True
    except IBKRConnectionError as exc:
        state["connected"] = False
        logger.warning("%s", exc)
    if state["connected"]:
        await _reconcile_unfilled_on_startup()
    await _restore_persisted_mode()
    task = asyncio.create_task(_broadcast_loop())
    risk_task = asyncio.create_task(_risk_monitor_loop())
    score_recompute_task = asyncio.create_task(_score_recompute_loop())
    data_refresh_task = asyncio.create_task(_data_refresh_loop())
    exit_monitor_task = asyncio.create_task(_auto_exit_monitor_loop())
    trailing_stop_task = asyncio.create_task(_trailing_stop_loop())
    hot_set_task = asyncio.create_task(_hot_set_loop())
    price_rotation_task = asyncio.create_task(_price_rotation_loop())
    session_cleanup_task = asyncio.create_task(_session_cleanup_loop())
    cache_eviction_task = asyncio.create_task(_cache_eviction_loop())
    health_alert_task = asyncio.create_task(_health_alert_loop())
    yield
    background_tasks = [
        task, risk_task, score_recompute_task, data_refresh_task,
        exit_monitor_task, trailing_stop_task, hot_set_task, price_rotation_task,
        session_cleanup_task, cache_eviction_task, health_alert_task,
    ]
    for background_task in background_tasks:
        background_task.cancel()
    # cancel() solo pide la cancelacion; sin esperar a que de verdad terminen,
    # el proceso puede salir con las tareas todavia pendientes (cleanup propio
    # de cada loop sin correr, y la CancelledError resultante nunca recuperada
    # -- asyncio la reporta como "exception was never retrieved").
    await asyncio.gather(*background_tasks, return_exceptions=True)
    broker.disconnect()


app = FastAPI(title="IBKR Trading Dashboard", lifespan=lifespan)

# Rate limiting: 200 req/min general, 10/min en login.
# La red ya está filtrada por Tailscale; estos límites protegen contra
# loops accidentales o scripts mal configurados, no contra ataques externos.
_limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
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


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not request.url.path.startswith("/api/"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        )
    return response


@app.get("/healthz")
def healthz():
    """Liveness check sin autenticacion: solo confirma que el proceso
    responde, sin exponer ningun dato de cuenta/posiciones/ordenes (eso
    requiere API key, ver require_api_key). Pensado para un monitor externo
    (uptime checks, watchdog) o un readiness probe -- no es informacion
    sensible, asi que dejarlo sin autenticar no es un vector de fuga de
    datos como lo seria cualquier otro endpoint de /api."""
    return {"status": "ok"}


class LoginRequest(BaseModel):
    password: str


@app.post("/api/login")
@_limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response):
    if not secrets.compare_digest(body.password, settings.api_key):
        raise HTTPException(status_code=401, detail="Contrasena invalida.")
    token = _create_session()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(default=None)):
    if session:
        _store.delete(session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/health")
def health_check():
    """Chequeo de salud del sistema. Sin autenticación para permitir
    monitoreo externo (UptimeRobot, cron, etc.).

    Retorna 200 si todo está bien, 503 si hay algún problema activo.
    """
    now = datetime.now(timezone.utc)
    issues: list[str] = []
    if not state["connected"]:
        issues.append("ibkr_disconnected")
    if state.get("market_data_degraded"):
        issues.append("market_data_degraded")
    if state.get("halted"):
        issues.append("trading_halted")

    # Detectar scan paralizado: si todos los cachés tienen más de 45 min
    scan_ages = []
    for entry in signal_cache.values():
        as_of = entry.get("as_of")
        if as_of:
            age = (now - as_of).total_seconds()
            scan_ages.append(age)
    scan_stale = bool(scan_ages) and all(a > 2700 for a in scan_ages)  # 45 min
    if scan_stale:
        issues.append("scan_stale")

    last_scan_at = None
    if scan_ages:
        # La entrada más reciente entre todas las estrategias
        most_recent = max(
            (e["as_of"] for e in signal_cache.values() if e.get("as_of")),
            default=None,
        )
        if most_recent:
            last_scan_at = most_recent.isoformat()

    payload = {
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "timestamp": now.isoformat(),
        "mode": state["mode"],
        "halted": state.get("halted", False),
        "uptime_seconds": int((now - _startup_time).total_seconds()),
        "last_scan_at": last_scan_at,
    }
    if issues:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/api/status")
def status(_: None = Depends(require_api_key)):
    return {
        "mode": state["mode"],
        "connected": state["connected"],
        "halted": state["halted"],
        "ib_host": broker.host,
        "ib_port": broker.port,
        # Sync, sin tocar el lock (solo .locked(), no lo adquiere): permite que
        # el dashboard explique un "0/50 streaming en vivo" sin esperar a que
        # se libere -- a diferencia de /api/signals/scan, que si la cache esta
        # vencida se queda esperando el mismo lock que un backtest puede tener
        # tomado por varios minutos (ver _market_scan_lock).
        "market_scan_busy": _market_scan_lock.locked(),
        "live_radar_enabled": screener_config.live_radar_enabled,
        "live_hot_count": len(_hot_symbols),
        "live_hot_cap": screener_config.live_hot_symbols_cap,
        "live_radar_strategy_filter": screener_config.live_radar_strategy_filter,
        "market_data_degraded": state["market_data_degraded"],
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
async def get_account(_: None = Depends(require_api_key)):
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    return await broker.get_account_summary()


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
    rules_engine.reload(new_config)
    rules_config = new_config
    rules_config.save(settings.rules_path)
    audit.record("rules_updated", body.rules, {})
    return rules_config.model_dump()


@app.get("/api/audit")
def get_audit(limit: int = Query(default=100, ge=1, le=1000), _: None = Depends(require_api_key)):
    return audit.recent(limit)


@app.get("/api/strategies")
def list_strategies(_: None = Depends(require_api_key)):
    """Metadata de las estrategias disponibles, para el selector del
    dashboard: id/name para mostrar, description para el resumen junto al
    selector, supports_backtest para saber si ofrecer el boton de backtest
    o no (Largo plazo y Dividendos no lo soportan)."""
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "supports_backtest": s.supports_backtest,
        }
        for s in strategy_registry.values()
    ]


@app.get("/api/signals/config")
def get_screener_config(_: None = Depends(require_api_key)):
    return screener_config.model_dump()


class ScreenerUpdate(BaseModel):
    config: dict


def _deep_merge_dict(base: dict, updates: dict) -> dict:
    """Mezcla `updates` sobre `base` recursivamente para los sub-objetos
    anidados (opportunistic/long_term/dividend), pero reemplaza listas y
    escalares tal cual (ej. `universe`: no hay forma no ambigua de "mezclar"
    dos listas de tickers, asi que quien llama siempre manda la lista
    completa que quiere). Sin esto, un PUT que solo busca tocar un campo
    (ej. agregar un ticker, o cambiar un peso de una sola estrategia)
    reemplazaba TODA la config con `ScreenerConfig(**body.config)`, perdiendo
    en silencio cualquier campo/sub-objeto que el caller no conociera al
    armar su payload."""
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _refresh_missing_sectors_in_background(symbols: list[str]) -> None:
    """Resuelve el sector de `symbols` contra Yahoo Finance (ver
    sectors.refresh_sector) en un hilo aparte, sin bloquear la respuesta del
    PUT que los agrego al universo. Antes de este fix, un simbolo nuevo
    quedaba silenciosamente sin sector (get_sector() devuelve None) hasta que
    alguien lo notaba y apretaba manualmente "🌐 Refrescar sectores" en el
    Radar -- mientras tanto, max_sector_concentration_pct/
    max_concurrent_positions_per_sector no lo cubrian en absoluto (sin dato,
    esas reglas no bloquean, ver RulesEngine.evaluate()). Best-effort: si
    yfinance falla para alguno, sigue sin sector, igual que si nunca se
    hubiera llamado."""
    for symbol in symbols:
        try:
            refresh_sector(symbol)
        except Exception:
            logger.warning("No se pudo auto-resolver el sector de %s", symbol, exc_info=True)


@app.put("/api/signals/config")
async def update_screener_config(body: ScreenerUpdate, _: None = Depends(require_api_key)):
    global screener_config
    async with _screener_config_lock:
        previous_universe = set(screener_config.universe)
        merged = _deep_merge_dict(screener_config.model_dump(), body.config)
        try:
            new_config = ScreenerConfig(**merged)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors())
        screener_config = new_config
        screener_config.save(settings.screener_path)
        reload_strategy_registry(strategy_registry, screener_config)
        _sync_whitelist_with_universe()
        # Tras un cambio manual de config, los filtros pudieron cambiar por
        # completo: se descarta la base de simbolos "pasando" para que el
        # proximo ciclo del scan proactivo no trate la config nueva como
        # transiciones reales (vuelve a ser un primer ciclo, solo establece
        # base). Mismo motivo para la base por estrategia de fondos con
        # strategy_id propio (ver _run_fund_strategy_auto_trade_scan).
        _signal_state["previously_passing"] = None
        _signal_state["previously_passing_by_strategy"] = {}
        _store.save_signal_state(None, {})

        # Simbolos nuevos en el universo (agregados en este PUT) que todavia
        # no tienen sector conocido: se resuelven en background, en un hilo
        # aparte (ver _refresh_missing_sectors_in_background), sin demorar la
        # respuesta de este endpoint sincrono ni importar cuantos se hayan
        # agregado de una (ej. un reemplazo masivo del universo entero).
        new_symbols = set(new_config.universe) - previous_universe
        unclassified_new = [s for s in new_symbols if get_sector(s) is None]
        if unclassified_new:
            threading.Thread(
                target=_refresh_missing_sectors_in_background,
                args=(unclassified_new,),
                daemon=True,
                name="sector-auto-refresh",
            ).start()
    audit.record("screener_config_updated", body.config, {})
    return screener_config.model_dump()


async def _get_or_scan(strategy_id: str, force: bool) -> tuple[datetime, bool, list[dict]]:
    """Resultados de strategy.scan() para `strategy_id`: desde signal_cache si
    esta fresco, corriendo el scan si no. Comun a /api/signals/scan y
    /api/signals/scan/all para que ambos compartan el mismo cache por
    estrategia (ver SIGNAL_CACHE_TTL_SECONDS) en vez de pagar la cuota de la
    API de datos dos veces por lo mismo.

    force=True: refresca datos de red para esta estrategia bajo _market_scan_lock.
    force=False con cache miss: intenta primero cache_only (barato, sin red); si
    el cache esta frio (justo tras restart), cae al camino lento bajo lock."""
    now = datetime.now(timezone.utc)
    cached = signal_cache.get(strategy_id)
    if not force and cached and (now - cached["as_of"]).total_seconds() < SIGNAL_CACHE_TTL_SECONDS:
        return cached["as_of"], True, cached["results"]
    strategy = strategy_registry[strategy_id]
    if force:
        async with _market_scan_lock:
            results = await asyncio.to_thread(strategy.scan, force=True)
    else:
        try:
            results = await asyncio.to_thread(strategy.scan, cache_only=True)
        except MarketDataError:
            async with _market_scan_lock:
                results = await asyncio.to_thread(strategy.scan)
    signal_cache[strategy_id] = {"as_of": now, "results": [r.model_dump() for r in results]}
    return now, False, signal_cache[strategy_id]["results"]


async def _scan_general(force: bool) -> tuple[datetime, bool, list[dict]]:
    """Vista "General" del radar: por cada simbolo, el resultado de la
    estrategia que le dio el score MAS ALTO entre las 4 (comparables gracias a
    apply_cross_sectional_normalization en scoring.py, ver _run_hot_set_cycle).
    Es la misma logica que determina el hot-set del radar en vivo, asi que el
    orden de esta vista coincide con cuales simbolos estan en streaming.

    Reusa _get_or_scan por estrategia, asi que comparte signal_cache con
    /api/signals/scan y /api/signals/scan/all en vez de volver a escanear."""
    now = datetime.now(timezone.utc)
    cached_flags = []
    best_by_symbol: dict[str, dict] = {}
    for strategy_id in strategy_registry:
        _, cached, results = await _get_or_scan(strategy_id, force)
        cached_flags.append(cached)
        for r in results:
            current = best_by_symbol.get(r["symbol"])
            if current is None or r["score"] > current["score"]:
                best_by_symbol[r["symbol"]] = {**r, "winning_strategy_id": strategy_id}
    ranked = sorted(best_by_symbol.values(), key=lambda r: r["score"], reverse=True)
    return now, all(cached_flags), ranked


def _live_overlay_for_symbol(symbol: str, fallback_as_of: datetime | None = None) -> dict:
    """is_hot/last_price/price_as_of en vivo para un simbolo, leyendo solo
    estado en memoria del radar (_hot_symbols/broker.get_live_price/
    _live_prices) -- no escanea ni golpea la API de datos. is_hot solo es
    true cuando ademas hay un precio en vivo real disponible (no alcanza con
    que el simbolo este en el hot-set: justo despues de un reconnect del
    broker, por ejemplo, todavia no hay un primer precio cacheado).
    fallback_as_of se usa como price_as_of de un simbolo frio que todavia no
    paso por ninguna rotacion (ver _overlay_live_data)."""
    if symbol in _hot_symbols:
        live_price = broker.get_live_price(symbol)
        if live_price is not None:
            return {"is_hot": True, "last_price": live_price, "price_as_of": datetime.now(timezone.utc)}
        return {"is_hot": False}
    live_price = _live_prices.get(symbol)
    if live_price is not None:
        return {
            "is_hot": False,
            "last_price": live_price,
            "price_as_of": _live_prices_as_of.get(symbol, fallback_as_of),
        }
    return {"is_hot": False}


def _overlay_live_data(results: list[dict]) -> list[dict]:
    """Pisa el precio mostrado (y marca is_hot) con datos del radar en vivo
    (ver _hot_set_loop / _price_rotation_loop), sin tocar score/RSI/etc, que
    siguen siendo los del scan cacheado."""
    out = []
    for r in results:
        overlay = _live_overlay_for_symbol(r["symbol"], fallback_as_of=r["as_of"])
        out.append({**r, "price_as_of": r["as_of"], **overlay})
    return out


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
    en background, que pegan a la misma API de datos.

    strategy_id="general" es una vista sintetica (ver _scan_general): no
    corresponde a ninguna estrategia de strategy_registry, asi que se maneja
    aparte antes de validar contra ese registro."""
    resolved_id = strategy_id or screener_config.strategy_id
    if resolved_id != GENERAL_VIEW_ID and resolved_id not in strategy_registry:
        raise HTTPException(status_code=422, detail=f"strategy_id desconocido: {resolved_id}")
    try:
        if resolved_id == GENERAL_VIEW_ID:
            as_of, cached, results = await _scan_general(force)
        else:
            as_of, cached, results = await _get_or_scan(resolved_id, force)
    except Exception as exc:
        logger.warning("Error al escanear el mercado (%s): %s", resolved_id, exc)
        raise HTTPException(status_code=502, detail="Error al escanear el mercado. Revisa los logs del servidor.")
    return {
        "as_of": as_of,
        "cached": cached,
        "results": _overlay_live_data(results),
        "live_hot_count": len(_hot_symbols),
        "live_hot_cap": screener_config.live_hot_symbols_cap,
        "live_radar_strategy_filter": screener_config.live_radar_strategy_filter,
    }


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
            logger.warning("Error al escanear el mercado (%s): %s", strategy_id, exc)
            raise HTTPException(
                status_code=502, detail=f"Error al escanear el mercado ({strategy_id}). Revisa los logs del servidor."
            )
        for r in results:
            entry = merged.setdefault(r["symbol"], {"symbol": r["symbol"], "sector": r.get("sector"), "scores": {}})
            entry["scores"][strategy_id] = r["score"]
            if entry["sector"] is None and r.get("sector") is not None:
                entry["sector"] = r["sector"]
    return {"as_of": now, "results": list(merged.values())}


@app.get("/api/signals/live-prices")
def get_live_signal_prices(symbols: str = Query(..., max_length=3000), _: None = Depends(require_api_key)):
    """Precios en vivo del radar para los simbolos ya visibles en la tabla de
    senales (`symbols` separados por coma), sin re-escanear: solo lee el
    estado en memoria que ya mantienen _hot_set_loop/_price_rotation_loop
    (ver _live_overlay_for_symbol). Pensado para que el frontend lo polleé
    cada pocos segundos -- a diferencia de /api/signals/scan, no toca
    _market_scan_lock ni la API de datos de terceros, asi que no hay costo
    ni rate-limit en pedirlo seguido."""
    try:
        symbol_list = [validate_symbol(s) for s in symbols.split(",") if s.strip()][:200]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    out: dict[str, dict] = {}
    for symbol in symbol_list:
        overlay = _live_overlay_for_symbol(symbol)
        if "last_price" in overlay:
            out[symbol] = overlay
    return out


class SectorRefreshRequest(BaseModel):
    symbols: Optional[list[str]] = Field(default=None, max_length=200)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        return [validate_symbol(s) for s in v]


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

_WALK_FORWARD_RUNNERS = {
    "momentum": run_backtest_walk_forward,
    "opportunistic": run_opportunistic_backtest_walk_forward,
}


def _resolve_backtestable_strategy(strategy_id: str | None):
    """Resuelve y valida un strategy_id para los dos endpoints de backtest
    (resumen y walk-forward): mismas reglas en ambos (default a la estrategia
    persistida, 422 si no existe o no es backtesteable)."""
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
    return resolved_id


@app.get("/api/signals/backtest")
async def backtest_strategy(strategy_id: str | None = None, _: None = Depends(require_api_key)):
    """Backtest simplificado sobre el universo configurado, de la estrategia
    indicada (default la persistida en screener_config). Ver docstring de
    run_backtest()/run_opportunistic_backtest() para las simplificaciones
    asumidas (curva de equity diaria real con cupo top_n, comision/slippage
    estimados, sin supervivencia historica del universo).

    Largo plazo y Dividendos NO son backtesteables (supports_backtest=False):
    sin historia point-in-time de fundamentales en yfinance gratuito no hay
    forma de simular sus filtros en el pasado sin inventar datos.

    Requiere API key: es la operacion mas pesada del backend (descarga anos de
    historia de todo el universo), dejarla abierta seria un vector de DoS.
    async + asyncio.to_thread + lock por el mismo motivo que scan_signals."""
    resolved_id = _resolve_backtestable_strategy(strategy_id)
    runner = _BACKTEST_RUNNERS[resolved_id]
    try:
        async with _market_scan_lock:
            return await asyncio.to_thread(runner, screener_config, rules_config)
    except BacktestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/signals/backtest/walk-forward")
async def backtest_strategy_walk_forward(
    strategy_id: str | None = None,
    n_folds: int = Query(default=3, ge=2, le=12),
    _: None = Depends(require_api_key),
):
    """Validacion out-of-sample del backtest: corre la misma simulacion que
    /api/signals/backtest pero particiona el periodo en n_folds tramos de
    igual duracion calendario y devuelve las metricas resumen de cada tramo
    por separado. Ver docstring de run_backtest_walk_forward() /
    run_opportunistic_backtest_walk_forward() para el alcance -- en
    particular, esto NO es walk-forward optimization (no hay refitting de
    parametros por ventana, los thresholds configurados son siempre los
    mismos en todos los folds).

    Mismas reglas de strategy_id y mismo costo/lock que /api/signals/backtest
    (la simulacion subyacente es la misma, solo se la corta en tramos)."""
    resolved_id = _resolve_backtestable_strategy(strategy_id)
    runner = _WALK_FORWARD_RUNNERS[resolved_id]
    try:
        async with _market_scan_lock:
            return await asyncio.to_thread(runner, screener_config, n_folds, rules_config)
    except BacktestError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/orders/size-suggestion")
async def order_size_suggestion(
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
        equity = (await broker.get_account_summary()).net_liquidation
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
    if fund.closed:
        raise HTTPException(
            status_code=422, detail=f"El fondo '{fund.name}' esta cerrado: no se pueden enviar ordenes."
        )
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

    async with _funds_order_lock:
        # account_summary/position_qty se leen DENTRO del lock, no antes: si se
        # leyeran antes de adquirirlo, dos submit_order concurrentes sobre el
        # mismo simbolo evaluarian el rules_engine contra el mismo estado
        # pre-orden (el lock solo protegia el cash del fondo a partir de aca,
        # no esta lectura) y juntas podrian violar max_position_pct/daily_loss.
        account_summary = await broker.get_account_summary()
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
        current_positions = await broker.get_positions()

        decision = rules_engine.evaluate(
            order=order,
            account=account_summary,
            current_position_qty=position_qty,
            reference_price=reference_price,
            trades_today=trades_today,
            halted=state["halted"],
            order_sector=get_sector(order.symbol),
            sector_exposure_usd=_compute_sector_exposure(current_positions, order.symbol),
            max_concurrent_positions_per_sector=screener_config.max_concurrent_positions_per_sector,
            sector_position_count=_compute_sector_position_count(current_positions, order.symbol),
            total_position_value_usd=_compute_total_position_value(current_positions, order.symbol),
            open_portfolio_risk_usd=_compute_open_portfolio_risk_usd(order.symbol),
            trades_today_for_fund=(
                audit.count_trades_today(rules_config.trading_hours_timezone, fund_id=order.fund_id)
                if order.fund_id else None
            ),
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

        # Solo se registra en el ledger del fondo lo que el broker confirmo
        # como realmente ejecutado (mismo criterio que _try_auto_trade_entry
        # en vez de asumir que se lleno toda la cantidad pedida). avg_fill_price
        # (precio real del fill) se prioriza sobre reference_price (que era
        # solo una aproximacion para validar/sizear la orden).
        filled_qty = result.get("filled_qty") or 0.0
        if order.fund_id and filled_qty > 0:
            fill_price = result.get("avg_fill_price") or reference_price
            funds_store.record_fill(
                order.fund_id, order.symbol, order.side, filled_qty, fill_price,
                stop_loss_price=order.stop_loss_price,
                stop_order_id=result.get("stop_order_id"),
                commission=screener_config.commission_per_trade_usd,
            )
        status_label = "executed" if filled_qty > 0 else "submitted"
        audit.record(
            "order_executed" if filled_qty > 0 else "order_submitted_unfilled",
            order.model_dump(),
            result,
        )
        # Misma red de seguridad que _try_auto_trade_entry: si no llenó del
        # todo dentro de la ventana sincronica de place_order, se reconcilia
        # cuando el fill tardío llegue (ver _register_fund_fill_reconciliation).
        if order.fund_id:
            _register_fund_fill_reconciliation(
                order_id=result.get("order_id"),
                fund_id=order.fund_id,
                symbol=order.symbol,
                side=order.side,
                requested_qty=order.quantity,
                already_filled_qty=filled_qty,
                already_avg_price=result.get("avg_fill_price") or reference_price,
                stop_loss_price=order.stop_loss_price,
                stop_order_id=result.get("stop_order_id"),
                audit_action="order_fill_late",
            )
        return {"status": status_label, "result": result}


@app.post("/api/orders/{order_id}/approve")
async def approve_order(order_id: str, _: None = Depends(require_api_key)):
    async with _funds_order_lock:
        pending = state["pending_orders"].get(order_id)
        if not pending:
            raise HTTPException(status_code=404, detail="Orden pendiente no encontrada.")

        reference_price = None
        if pending.order.fund_id:
            reference_price = pending.order.limit_price
            if reference_price is None:
                reference_price = await broker.get_reference_price(pending.order.symbol)
            if not reference_price:
                raise HTTPException(
                    status_code=422,
                    detail="No se pudo obtener un precio de referencia para revalidar la orden.",
                )
            # Re-valida contra el estado ACTUAL del fondo (cash/posicion pudo
            # haber cambiado desde que la orden quedo pendiente, por otra
            # orden ejecutada mientras tanto): si ya no es valida, se levanta
            # antes de tocar pending_orders, asi la orden queda en la cola
            # para que el usuario decida con el ledger ya actualizado a la
            # vista, en vez de perderse silenciosamente.
            _validate_fund_order(pending.order, reference_price)

        del state["pending_orders"][order_id]
        _persist_state()

        try:
            result = await broker.place_order(pending.order)
        except StopLossRejectedError as exc:
            audit.record("stop_loss_rejected", pending.order.model_dump(), {"error": str(exc)})
            raise HTTPException(status_code=502, detail=str(exc))

        filled_qty = result.get("filled_qty") or 0.0
        if pending.order.fund_id and filled_qty > 0:
            fill_price = result.get("avg_fill_price") or reference_price
            funds_store.record_fill(
                pending.order.fund_id, pending.order.symbol, pending.order.side, filled_qty, fill_price,
                stop_loss_price=pending.order.stop_loss_price,
                stop_order_id=result.get("stop_order_id"),
                commission=screener_config.commission_per_trade_usd,
            )
        status_label = "executed" if filled_qty > 0 else "submitted"
        audit.record(
            "order_executed_after_approval" if filled_qty > 0 else "order_submitted_unfilled_after_approval",
            pending.order.model_dump(),
            result,
        )
        # Misma red de seguridad que _try_auto_trade_entry/submit_order (ver
        # _register_fund_fill_reconciliation).
        if pending.order.fund_id:
            _register_fund_fill_reconciliation(
                order_id=result.get("order_id"),
                fund_id=pending.order.fund_id,
                symbol=pending.order.symbol,
                side=pending.order.side,
                requested_qty=pending.order.quantity,
                already_filled_qty=filled_qty,
                already_avg_price=result.get("avg_fill_price") or reference_price,
                stop_loss_price=pending.order.stop_loss_price,
                stop_order_id=result.get("stop_order_id"),
                audit_action="order_fill_late",
            )
        return {"status": status_label, "result": result}


@app.post("/api/orders/{order_id}/reject")
async def reject_order(order_id: str, _: None = Depends(require_api_key)):
    async with _funds_order_lock:
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
    strategy_id: Optional[str] = None


def _fund_view(fund) -> dict:
    return {
        **fund.model_dump(),
        "realized_pnl_total": round(fund.realized_pnl_total(), 2),
        "net_contributed_capital": round(fund.net_contributed_capital(), 2),
    }


def _check_capital_allocation(
    net_liquidation: float, amount: float, current_fund_cash: float, already_allocated: float
) -> None:
    """Valida que asignarle `amount` adicional a un fondo no haga que el
    capital total asignado (cash + posiciones a costo) supere el valor neto
    de liquidación de la cuenta de IBKR.

    Usa net_liquidation (cash + posiciones a valor de mercado) en vez de
    TotalCashValue: `already_allocated` ya incluye el costo de posiciones
    abiertas (ver FundsStore.total_allocated_cash), y compararlos contra el
    valor total de la cuenta — en vez de solo el cash disponible — cubre el
    gap de timing entre el fill y el update de TotalCashValue en IBKR.

    No consulta nada por si sola (ni broker ni FundsStore): se pasa como
    `allocation_check` a FundsStore.create()/apply_capital_flow() para que se
    ejecute DENTRO de su lock, sobre `already_allocated` recalculado en ese
    instante exacto -- lo que cierra la carrera entre dos requests
    concurrentes."""
    if already_allocated + current_fund_cash + amount > net_liquidation:
        raise FundValidationError(
            f"La cuenta de IBKR tiene un valor neto de ${net_liquidation:,.2f}, de los cuales "
            f"${already_allocated + current_fund_cash:,.2f} ya están asignados a fondos. "
            f"No se puede asignar ${amount:,.2f} más sin superar el valor neto disponible."
        )


@app.get("/api/funds")
def list_funds(_: None = Depends(require_api_key)):
    return [_fund_view(f) for f in funds_store.list()]


@app.post("/api/funds")
async def create_fund(body: FundCreate, _: None = Depends(require_api_key)):
    """Crea un fondo: una porcion de capital con su propia contabilidad
    (cash_usd, posiciones, PnL realizado), separada de la cuenta consolidada
    de IBKR y de cualquier otro fondo. Ver funds.py para el detalle del
    ledger y de por que una venta atada a un fondo nunca puede tocar
    holdings que no se registraron en el."""
    if body.initial_capital_usd <= 0:
        raise HTTPException(status_code=422, detail="initial_capital_usd debe ser mayor a 0.")
    if body.strategy_id is not None and body.strategy_id not in strategy_registry:
        raise HTTPException(status_code=422, detail=f"strategy_id desconocido: {body.strategy_id}")
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="No conectado a IBKR.")
    real_cash = (await broker.get_account_summary()).net_liquidation

    def allocation_check(already_allocated: float) -> None:
        _check_capital_allocation(real_cash, body.initial_capital_usd, current_fund_cash=0.0, already_allocated=already_allocated)

    try:
        fund = funds_store.create(
            body.name.strip(), body.initial_capital_usd, body.auto_trading_enabled,
            strategy_id=body.strategy_id, allocation_check=allocation_check,
        )
    except FundValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record("fund_created", body.model_dump(), {"id": fund.id})
    return _fund_view(fund)


_EMPTY_ROI_HISTORY = {
    "dates": [],
    "fund_cumulative_return_pct": [],
    "benchmark_cumulative_return_pct": [],
    "per_fund": [],
}
# Cache para el endpoint de ROI history: el cálculo es costoso (descarga
# barras diarias de yfinance por cada símbolo de todos los fondos) y el
# resultado cambia solo cuando hay un fill nuevo o un aporte/retiro. Un TTL
# de 5 minutos reduce la carga sin sacrificar frescura en la práctica.
_roi_history_cache: "tuple[float, dict] | None" = None
_ROI_HISTORY_CACHE_TTL = 300  # segundos


def _twr_series(
    events_by_day: dict,
    calendar_index: "pd.DatetimeIndex",
    symbol_close: "dict[str, pd.Series | None]",
) -> "tuple[list[str], list[float], dict[str, float]]":
    """Calcula la serie TWR (time-weighted return) para un conjunto de eventos
    agrupados por dia. Devuelve (dates, cum_pct_series, last_trade_price_map).
    Reutilizable tanto para el consolidado como para cada fondo individual,
    con el mismo conjunto de precios ya descargado."""
    cash = 0.0
    positions: dict[str, float] = {}
    last_trade_price: dict[str, float] = {}
    dates: list[str] = []
    cum_pct: list[float] = []
    cum = 0.0
    prev_equity = None

    # Pre-aplicar eventos ANTERIORES al primer día del calendario para que el
    # saldo inicial de cash y posiciones sea correcto cuando empiece el loop.
    # Sin esto, un aporte de capital registrado antes de la ventana de precios
    # disponible (ej. primer aporte del fondo hecho semanas antes de la primera
    # barra de SPY en caché) nunca se suma al cash, lo que hace que el equity
    # de arranque sea ≈$0 y divide el TWR por ese denominador microscópico,
    # produciendo retornos ficticios de ±1000%.
    if len(calendar_index) > 0:
        first_day = calendar_index[0].date()
        for ev_day, day_events in sorted(events_by_day.items()):
            if ev_day >= first_day:
                break
            for event in day_events:
                if event[0] == "flow":
                    cash += event[1]
                else:
                    _, side, symbol, quantity, price = event
                    if side == Side.BUY:
                        cash -= quantity * price
                        positions[symbol] = positions.get(symbol, 0.0) + quantity
                    else:
                        cash += quantity * price
                        positions[symbol] = positions.get(symbol, 0.0) - quantity
                    last_trade_price[symbol] = price

    for day_ts in calendar_index:
        day = day_ts.date()
        net_flow = 0.0
        for event in events_by_day.get(day, []):
            if event[0] == "flow":
                cash += event[1]
                net_flow += event[1]
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
                continue
        else:
            r_t = (equity - net_flow - prev_equity) / prev_equity if prev_equity > 0 else 0.0
            cum = (1 + cum) * (1 + r_t) - 1
            prev_equity = equity

        dates.append(day.isoformat())
        cum_pct.append(round(cum * 100, 2))

    return dates, cum_pct, last_trade_price


def _compute_roi_history(funds: list) -> dict:
    """Retorno acumulado time-weighted (TWR) del capital combinado de TODOS los
    fondos contra el S&P 500 (SPY), mas el TWR de cada fondo individual.

    TWR en vez de dollar-weighted para que aportes/retiros no inflen ni
    desinflen la curva: mide habilidad de inversion, no timing de cash-flow.
    El benchmark se alinea al mismo calendario de trading que las posiciones.
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

    # Descarga de precios compartida entre combined y per-fund (evita llamadas
    # duplicadas a yfinance para el mismo simbolo).
    symbols = {t.symbol for fund in funds for t in fund.trades}
    symbol_close: dict[str, "pd.Series | None"] = {}
    for symbol in symbols:
        try:
            bars = get_daily_bars(symbol, lookback_days)
            symbol_close[symbol] = bars["Close"].reindex(calendar_index).ffill()
        except MarketDataError:
            symbol_close[symbol] = None

    # Benchmark acumulado (misma referencia de inicio para todos).
    bench_start_close: float | None = None
    bench_cum_by_date: dict[str, float] = {}
    for day_ts in calendar_index:
        v = bench_close.get(day_ts)
        if v is None or pd.isna(v):
            continue
        if bench_start_close is None:
            bench_start_close = float(v)
        bench_cum_by_date[day_ts.date().isoformat()] = round(float(v) / bench_start_close - 1, 4) * 100

    # TWR combinado (todos los fondos como si fueran uno solo).
    combined_events: dict = {}
    for fund in funds:
        for flow in fund.capital_flows:
            combined_events.setdefault(flow.created_at.date(), []).append(("flow", flow.amount))
        for trade in fund.trades:
            combined_events.setdefault(trade.executed_at.date(), []).append(
                ("trade", trade.side, trade.symbol, trade.quantity, trade.price)
            )
    combined_dates, combined_pct, _ = _twr_series(combined_events, calendar_index, symbol_close)

    # Alinear benchmark con las fechas que el TWR combinado produjo.
    bench_cum_pct = [round(bench_cum_by_date.get(d, 0.0), 2) for d in combined_dates]

    # TWR por fondo individual, mismo calendario y precios.
    per_fund = []
    for fund in funds:
        fund_events: dict = {}
        for flow in fund.capital_flows:
            fund_events.setdefault(flow.created_at.date(), []).append(("flow", flow.amount))
        for trade in fund.trades:
            fund_events.setdefault(trade.executed_at.date(), []).append(
                ("trade", trade.side, trade.symbol, trade.quantity, trade.price)
            )
        f_dates, f_pct, _ = _twr_series(fund_events, calendar_index, symbol_close)
        if f_dates:
            per_fund.append({
                "id": fund.id,
                "name": fund.name,
                "dates": f_dates,
                "cumulative_return_pct": f_pct,
                "benchmark_cumulative_return_pct": [round(bench_cum_by_date.get(d, 0.0), 2) for d in f_dates],
            })

    if not combined_dates:
        return dict(_EMPTY_ROI_HISTORY)

    return {
        "dates": combined_dates,
        "fund_cumulative_return_pct": combined_pct,
        "benchmark_cumulative_return_pct": bench_cum_pct,
        "per_fund": per_fund,
    }


@app.get("/api/funds/roi-history")
async def get_funds_roi_history(_: None = Depends(require_api_key)):
    """Retorno acumulado time-weighted del capital combinado de todos los
    fondos vs. el S&P 500 (SPY) en la misma ventana -- ver _compute_roi_history
    para el detalle de por que es time-weighted y no dollar-weighted como el
    ROI por fondo de `_fund_view`. Es aparte del bar chart de ROI actual por
    fondo (ese es un snapshot del momento, este es una serie historica).

    El resultado se cachea 5 minutos: el cálculo descarga barras diarias de
    yfinance por cada símbolo y ocupaba el threadpool de FastAPI bloqueando
    otros endpoints síncronos (reject_order, update_rules, etc.)."""
    global _roi_history_cache
    now = time.monotonic()
    if _roi_history_cache is not None:
        cached_at, cached_result = _roi_history_cache
        if now - cached_at < _ROI_HISTORY_CACHE_TTL:
            return cached_result
    result = await asyncio.to_thread(_compute_roi_history, funds_store.list())
    _roi_history_cache = (now, result)
    return result


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
async def create_capital_flow(fund_id: str, body: CapitalFlowCreate, _: None = Depends(require_api_key)):
    """Aporta (amount > 0) o retira (amount < 0) capital virtual de un fondo
    ya existente -- mismas validaciones que la creacion (ver
    _check_capital_allocation), mas el chequeo de que un retiro no deje
    cash_usd negativo (no se puede retirar plata que esta en posiciones
    abiertas; hay que vender primero). Ambos chequeos corren como
    allocation_check DENTRO del lock de FundsStore, sobre el cash_usd del
    fondo leido en ese instante: sin esto, dos retiros (o un retiro y una
    compra) concurrentes sobre el mismo fondo podian leer el mismo cash_usd
    desactualizado y, combinados, dejarlo negativo."""
    existing_fund = funds_store.get(fund_id)
    if existing_fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    if existing_fund.closed:
        raise HTTPException(
            status_code=422, detail="Fondo cerrado: no se pueden registrar aportes ni retiros."
        )
    if body.amount == 0:
        raise HTTPException(status_code=422, detail="El monto no puede ser cero.")

    real_cash = None
    if body.amount > 0:
        if not state["connected"]:
            raise HTTPException(status_code=503, detail="No conectado a IBKR.")
        real_cash = (await broker.get_account_summary()).net_liquidation

    def allocation_check(fund, already_allocated: float) -> None:
        if body.amount < 0:
            if -body.amount > fund.cash_usd:
                raise FundValidationError(
                    f"El fondo '{fund.name}' solo tiene ${fund.cash_usd:,.2f} de cash "
                    "disponibles para retirar (no se puede retirar plata que esta en "
                    "posiciones abiertas; vende primero)."
                )
        else:
            _check_capital_allocation(
                real_cash, body.amount, current_fund_cash=fund.cash_usd, already_allocated=already_allocated
            )

    try:
        flow = funds_store.apply_capital_flow(fund_id, body.amount, body.note, allocation_check=allocation_check)
    except FundValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if flow is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
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


class FundStrategyUpdate(BaseModel):
    strategy_id: Optional[str] = None


@app.put("/api/funds/{fund_id}/strategy")
def set_fund_strategy(fund_id: str, body: FundStrategyUpdate, _: None = Depends(require_api_key)):
    """Define que estrategia debe usar el motor de auto-trading para elegir
    señales en este fondo (ver _try_auto_trade_entry). strategy_id=None
    revierte al comportamiento previo: el fondo sigue la estrategia activa
    global (screener_config.strategy_id)."""
    if body.strategy_id is not None and body.strategy_id not in strategy_registry:
        raise HTTPException(status_code=422, detail=f"strategy_id desconocido: {body.strategy_id}")
    fund = funds_store.set_strategy(fund_id, body.strategy_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    audit.record("fund_strategy_changed", {"fund_id": fund_id, "strategy_id": body.strategy_id}, {})
    return _fund_view(fund)


@app.post("/api/funds/{fund_id}/close")
async def close_fund(fund_id: str, _: None = Depends(require_api_key)):
    """Cierra un fondo de forma definitiva (ver FundsStore.close()): exige
    cash_usd en 0 y ninguna posicion abierta. No hay endpoint para reabrirlo.

    Toma _funds_order_lock (igual que submit_order/approve_order/el
    auto-trading) para no poder cerrarse mientras una orden de ESE fondo esta
    en pleno vuelo: sin el lock, close() podia validar "sin posiciones
    abiertas" justo antes de que un fill en curso (que no chequea fund.closed)
    le agregara una posicion al fondo ya cerrado."""
    async with _funds_order_lock:
        try:
            fund = funds_store.close(fund_id)
        except FundValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if fund is None:
        raise HTTPException(status_code=404, detail="Fondo no encontrado.")
    audit.record("fund_closed", {"fund_id": fund_id}, {})
    return _fund_view(fund)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, api_key: str = ""):
    # Un navegador no puede mandar headers personalizados en el handshake de
    # un WebSocket, asi que la API key viaja como query param (?api_key=...)
    # en vez del header X-API-Key que usa el resto de los endpoints. La cookie
    # de sesion (ver /api/login), en cambio, el navegador la manda sola en el
    # handshake porque es same-origin.
    session = websocket.cookies.get(SESSION_COOKIE_NAME)
    authed = (api_key and secrets.compare_digest(api_key, settings.api_key)) or (
        session and _session_valid(session)
    )
    if not authed:
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
