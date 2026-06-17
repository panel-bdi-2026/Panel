from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class AuditLog:
    """Bitacora de solo-agregado de todo lo que toca dinero: ordenes enviadas,
    aprobadas, rechazadas, ejecutadas, y cambios de reglas o de modo halt."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
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
        cur = self._conn.execute(
            "SELECT id, ts, action, payload, result FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "ts": row[1],
                "action": row[2],
                "payload": json.loads(row[3]),
                "result": json.loads(row[4]),
            }
            for row in cur.fetchall()
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
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action IN "
            "('order_executed', 'order_executed_after_approval') AND ts >= ? AND ts < ?",
            (start_utc, end_utc),
        )
        return cur.fetchone()[0]
