from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class AuditLog:
    """Bitacora de solo-agregado de todo lo que toca dinero: ordenes enviadas,
    aprobadas, rechazadas, ejecutadas, y cambios de reglas o de modo halt."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # check_same_thread=False habilita compartir esta conexion entre el
        # event loop y el threadpool de endpoints sincronos de FastAPI, pero
        # sqlite3 no serializa por si solo un execute+commit hecho desde
        # threads distintos como una unidad atomica: sin este lock, dos
        # llamadas concurrentes a record()/count_trades_today() pueden
        # interleavear y devolver "database is locked", o un conteo de
        # trades del dia inconsistente justo cuando RulesEngine.evaluate() lo
        # usa para el cap de ordenes diarias.
        self._lock = threading.Lock()
        # WAL en vez del journal_mode "DELETE" por default: este ultimo toma
        # un lock exclusivo de TODO el archivo durante cada commit, lo que
        # bloquea a cualquier lector externo (ej. abrir audit.db a mano con
        # el cliente sqlite3 para auditar, o un backup en caliente) mientras
        # el backend esta escribiendo -- con la frecuencia de record() en
        # produccion (cada orden, cada cambio de reglas/halt), esa ventana de
        # bloqueo es practicamente constante. WAL permite lectores
        # concurrentes mientras se escribe, y persiste en el archivo (no es
        # un PRAGMA por-conexion que haya que repetir en cada conexion nueva
        # una vez seteado en el archivo .db).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
        # Indice en action+ts: acelera count_trades_today que filtra por action IN (...) AND ts
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_log(action, ts)")
        # Indice en el simbolo del payload: acelera was_submitted_by_system y get_last_stop_price
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_symbol "
            "ON audit_log(json_extract(payload,'$.symbol'))"
        )
        # wal_autocheckpoint=1000: en WAL el checkpoint automatico corre cada N paginas
        # escritas (default: 1000 -- se confirma aqui explicitamente para que quede
        # documentado y no cambie si sqlite sube el default en el futuro).
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        self._conn.commit()

    def record(self, action: str, payload: dict, result: dict) -> None:
        # Perder una entrada de auditoria es malo pero recuperable; matar el
        # loop que sincroniza state["connected"] o el kill switch es mucho peor
        # (mismo criterio que _persist_state en main.py). Sin este try/except,
        # un fallo de sqlite (disco lleno, conexion cerrada) propagaria como
        # excepcion no atrapada y mataria la tarea de asyncio que llamo record().
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_log (ts, action, payload, result) VALUES (?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        action,
                        json.dumps(payload, default=str),
                        json.dumps(result, default=str),
                    ),
                )
                self._conn.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("audit.record fallo para action=%r", action)

    def recent(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, action, payload, result FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "ts": row[1],
                "action": row[2],
                "payload": json.loads(row[3]),
                "result": json.loads(row[4]),
            }
            for row in rows
        ]

    # Acciones que representan una operacion realmente ejecutada (mueve
    # posicion/dinero), manual o autonoma. auto_trade_executed/auto_trade_exit
    # tienen que entrar aca: si no, el cap de max_trades_per_day no limita en
    # absoluto la actividad del motor de auto-trading por fondo, que es
    # justamente el que opera sin que nadie apruebe cada orden a mano.
    _TRADE_ACTIONS = (
        "order_executed",
        "order_executed_after_approval",
        "auto_trade_executed",
        "auto_trade_exit",
    )

    def get_untracked_fills(self, since_days: int = 7) -> list[dict]:
        """Retorna entradas *_submitted_unfilled recientes que pueden haber
        llenado en IBKR sin que el fondo lo registrara (ej. fill llegó tras
        el timeout de _wait_for_fill o tras un reinicio del backend)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        actions = (
            "auto_trade_submitted_unfilled",
            "order_submitted_unfilled",
            "order_submitted_unfilled_after_approval",
        )
        placeholders = ", ".join("?" for _ in actions)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, ts, action, payload, result FROM audit_log "
                f"WHERE action IN ({placeholders}) AND ts >= ? ORDER BY id ASC",
                (*actions, cutoff),
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "ts": row[1],
                "action": row[2],
                "payload": json.loads(row[3]),
                "result": json.loads(row[4]),
            }
            for row in rows
        ]

    def was_submitted_by_system(self, symbol: str, since_days: int = 90) -> bool:
        """Retorna True si el sistema sometió alguna vez una orden de COMPRA para
        este símbolo (ejecutada, sin fill, o borrador aprobado). Se usa como guard
        en la reconciliación de huérfanas para evitar adoptar posiciones colocadas
        manualmente en IBKR fuera del sistema."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        buy_actions = (
            "auto_trade_executed",
            "auto_trade_submitted_unfilled",
            "auto_trade_stop_loss_rejected",  # compra que llenó aunque el stop fallara
            "order_executed",
            "order_submitted_unfilled",
            "order_executed_after_approval",
            "order_submitted_unfilled_after_approval",
        )
        placeholders = ", ".join("?" for _ in buy_actions)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT 1 FROM audit_log "
                f"WHERE action IN ({placeholders}) AND ts >= ? "
                f"AND json_extract(payload,'$.symbol') = ? LIMIT 1",
                (*buy_actions, cutoff, symbol),
            )
            return cur.fetchone() is not None

    def get_last_stop_price(self, symbol: str, since_days: int = 90) -> "float | None":
        """Retorna el stop_loss_price de la entrada de compra más reciente para
        este símbolo. Usado por la reconciliación de arranque para recuperar el
        precio de stop de posiciones huérfanas (ej. stop-loss rechazado por IBKR
        en la sesión anterior) y colocar un stop protector de emergencia."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        buy_actions = (
            "auto_trade_executed",
            "auto_trade_submitted_unfilled",
            "auto_trade_stop_loss_rejected",
            "order_executed",
            "order_submitted_unfilled",
            "order_executed_after_approval",
            "order_submitted_unfilled_after_approval",
        )
        placeholders = ", ".join("?" for _ in buy_actions)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT json_extract(payload,'$.stop_loss_price') FROM audit_log "
                f"WHERE action IN ({placeholders}) AND ts >= ? "
                f"AND json_extract(payload,'$.symbol') = ? "
                f"AND json_extract(payload,'$.stop_loss_price') IS NOT NULL "
                f"ORDER BY id DESC LIMIT 1",
                (*buy_actions, cutoff, symbol),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def count_trades_today(self, tz_name: str = "America/New_York", fund_id: str | None = None) -> int:
        """Cuenta ordenes ejecutadas hoy segun el dia de trading en `tz_name`
        (no el dia calendario UTC): ts se guarda en UTC, asi que filtrar por el
        prefijo de fecha UTC desalinea el conteo del dia real de mercado --
        ej. una orden ejecutada a las 21:00 ET ya es "manana" en UTC.

        `fund_id`, si se pasa, acota el conteo a las ordenes de ESE fondo
        (via json_extract sobre payload.fund_id, presente en el
        model_dump() de OrderRequest que ya se guarda en cada accion de
        _TRADE_ACTIONS) -- para RulesConfig.max_trades_per_day_per_fund, un
        cupo diario adicional POR fondo, distinto del cupo global
        (max_trades_per_day) que sigue contando sin filtrar por fondo."""
        tz = ZoneInfo(tz_name)
        now_local = datetime.now(tz)
        start_local = datetime.combine(now_local.date(), dtime.min, tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        placeholders = ", ".join("?" for _ in self._TRADE_ACTIONS)
        fund_filter = " AND json_extract(payload, '$.fund_id') = ?" if fund_id is not None else ""
        params = (*self._TRADE_ACTIONS, start_utc, end_utc, *((fund_id,) if fund_id is not None else ()))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE action IN ({placeholders}) "
                f"AND ts >= ? AND ts < ?{fund_filter}",
                params,
            )
            return cur.fetchone()[0]

    def find_trade_context(
        self, fund_id: str, symbol: str, actions: tuple[str, ...], near_ts: str, window_seconds: int = 300,
    ) -> "dict | None":
        """Entrada de audit_log más cercana en el tiempo a `near_ts` (ISO-8601
        UTC) entre `actions`, para este fondo/símbolo -- reconstruye "por qué
        se compró/vendió" un trade puntual del ledger de un fondo (FundTrade
        no guarda ningún ID de vuelta hacia el audit log). No filtra por side:
        algunas acciones relevantes (auto_trade_stop_loss_reconciled) no lo
        tienen en el payload, y la ventana de tiempo acotada ya alcanza para
        no confundir una compra con una venta del mismo símbolo (nunca pasan
        en el mismo instante). `actions` ya viene filtrado por el caller a
        las que tienen sentido para el lado (compra/venta) del trade."""
        near = datetime.fromisoformat(near_ts)
        lo = (near - timedelta(seconds=window_seconds)).isoformat()
        hi = (near + timedelta(seconds=window_seconds)).isoformat()
        placeholders = ", ".join("?" for _ in actions)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT id, ts, action, payload, result FROM audit_log "
                f"WHERE action IN ({placeholders}) "
                f"AND json_extract(payload,'$.fund_id') = ? "
                f"AND json_extract(payload,'$.symbol') = ? "
                f"AND ts BETWEEN ? AND ? "
                f"ORDER BY ABS(julianday(ts) - julianday(?)) ASC LIMIT 1",
                (*actions, fund_id, symbol, lo, hi, near_ts),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "ts": row[1], "action": row[2],
            "payload": json.loads(row[3]), "result": json.loads(row[4]),
        }

    def find_latest_before(
        self, fund_id: str, symbol: str, action: str, before_ts: str, since_days: int = 30,
    ) -> "dict | None":
        """Entrada `action` más reciente para (fund_id, symbol) con ts <=
        before_ts, dentro de los últimos `since_days`. Usado para encontrar el
        signal_order_drafted que originó una compra ejecutada bastante después
        de aprobarse a mano (order_executed_after_approval no vuelve a
        adjuntar la señal original -- ver find_trade_context)."""
        cutoff = (datetime.fromisoformat(before_ts) - timedelta(days=since_days)).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, action, payload, result FROM audit_log "
                "WHERE action = ? AND json_extract(payload,'$.fund_id') = ? "
                "AND json_extract(payload,'$.symbol') = ? AND ts <= ? AND ts >= ? "
                "ORDER BY id DESC LIMIT 1",
                (action, fund_id, symbol, before_ts, cutoff),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "ts": row[1], "action": row[2],
            "payload": json.loads(row[3]), "result": json.loads(row[4]),
        }

    def prune(self, older_than_days: int = 365) -> int:
        """Elimina entradas con ts anterior a `older_than_days` dias. Devuelve
        el numero de filas eliminadas. Llamar 1×/dia desde _supervised_loop
        (ver main.py) para evitar que audit.db crezca indefinidamente; no
        es destructivo para auditorias operativas: las posiciones y PnL del
        ledger (funds.py) son la fuente de verdad, el audit_log es bitacora
        de decisiones, no de estado -- raramente se necesitan registros de
        mas de un año."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        """Fuerza un WAL checkpoint completo. Llamar desde lifespan shutdown
        para que el archivo .db quede consolidado (sin el fragmento .wal
        pendiente) antes de que el proceso termine -- los backups que copian
        solo el .db obtendran un snapshot consistente. No cierra la conexion:
        el proceso la cierra al salir, y en tests el singleton de modulo sigue
        siendo valido para el siguiente test."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.commit()
            except Exception:
                pass

    def was_stopped_out_after(self, fund_id: str, symbol: str, since_ts: str) -> bool:
        """True si hay una entrada auto_trade_stop_loss_reconciled para
        (fund_id, symbol) con ts posterior a `since_ts` (ISO-8601 UTC).
        Usado por _reconcile_unfilled_on_startup para no re-reconciliar una
        posicion que ya fue cerrada por stop-loss -- evita el ciclo infinito
        de venta→recompra en extended hours de paper trading."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM audit_log "
                "WHERE action = 'auto_trade_stop_loss_reconciled' "
                "AND ts > ? "
                "AND json_extract(payload, '$.fund_id') = ? "
                "AND json_extract(payload, '$.symbol') = ? LIMIT 1",
                (since_ts, fund_id, symbol),
            )
            return cur.fetchone() is not None
