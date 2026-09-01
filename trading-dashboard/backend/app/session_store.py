"""Persistencia de sesiones de login y signal_state en SQLite.

Reemplaza los dicts en memoria de main.py (_sessions, _signal_state) con
tablas SQLite en state.db, para que sobrevivan reinicios del servicio:
  - sessions: tokens de login (TTL 7 días)
  - signal_state: previously_passing por estrategia (evita señales perdidas
    en el primer scan post-restart)

Mismo patrón que audit.py: una conexión compartida con WAL + threading.Lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 días


class SessionStore:
    """Almacén SQLite de sesiones de login."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.commit()

    # ── Sesiones ─────────────────────────────────────────────────────────────

    def create(self, token: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token, created_at) VALUES (?, ?)",
                (token, now),
            )
            self._conn.commit()

    def valid(self, token: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM sessions WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            return False
        created = datetime.fromisoformat(row[0])
        if (datetime.now(timezone.utc) - created).total_seconds() > SESSION_TTL_SECONDS:
            with self._lock:
                self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                self._conn.commit()
            return False
        return True

    def delete(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._conn.commit()

    def clear_sessions(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions")
            self._conn.commit()

    def _insert_session_at(self, token: str, created_at: datetime) -> None:
        """Solo para tests: inserta una sesión con timestamp arbitrario."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token, created_at) VALUES (?, ?)",
                (token, created_at.isoformat()),
            )
            self._conn.commit()

    def cleanup_expired(self) -> int:
        cutoff = datetime.now(timezone.utc)
        with self._lock:
            rows = self._conn.execute("SELECT token, created_at FROM sessions").fetchall()
            expired = [
                r[0] for r in rows
                if (cutoff - datetime.fromisoformat(r[1])).total_seconds() > SESSION_TTL_SECONDS
            ]
            if expired:
                self._conn.executemany(
                    "DELETE FROM sessions WHERE token = ?", [(t,) for t in expired]
                )
                self._conn.commit()
        return len(expired)

    # ── Signal state ─────────────────────────────────────────────────────────

    def save_signal_state(self, previously_passing: set | None, by_strategy: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        pp_json = json.dumps(list(previously_passing) if previously_passing is not None else None)
        by_json = json.dumps({k: list(v) for k, v in by_strategy.items()})
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO signal_state (key, value, updated_at) VALUES (?, ?, ?)",
                [
                    ("previously_passing", pp_json, now),
                    ("previously_passing_by_strategy", by_json, now),
                ],
            )
            self._conn.commit()

    def load_signal_state(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM signal_state"
            ).fetchall()
        data = {r[0]: json.loads(r[1]) for r in rows}
        pp_raw = data.get("previously_passing")
        by_raw = data.get("previously_passing_by_strategy", {})
        return {
            "previously_passing": set(pp_raw) if pp_raw is not None else None,
            "previously_passing_by_strategy": {k: set(v) for k, v in by_raw.items()},
        }
