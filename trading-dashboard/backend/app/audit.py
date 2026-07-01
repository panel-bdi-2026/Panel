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
        self._conn.commit()

    def record(self, action: str, payload: dict, result: dict) -> None:
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

    def count_trades_today(self, tz_name: str = "America/New_York") -> int:
        """Cuenta ordenes ejecutadas hoy segun el dia de trading en `tz_name`
        (no el dia calendario UTC): ts se guarda en UTC, asi que filtrar por el
        prefijo de fecha UTC desalinea el conteo del dia real de mercado --
        ej. una orden ejecutada a las 21:00 ET ya es "manana" en UTC."""
        tz = ZoneInfo(tz_name)
        now_local = datetime.now(tz)
        start_local = datetime.combine(now_local.date(), dtime.min, tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).isoformat()
        end_utc = end_local.astimezone(timezone.utc).isoformat()
        placeholders = ", ".join("?" for _ in self._TRADE_ACTIONS)
        with self._lock:
            cur = self._conn.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE action IN ({placeholders}) AND ts >= ? AND ts < ?",
                (*self._TRADE_ACTIONS, start_utc, end_utc),
            )
            return cur.fetchone()[0]
