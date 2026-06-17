from datetime import datetime, timedelta, timezone

from app.audit import AuditLog


def make_audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.db")


def _insert_at(audit: AuditLog, action: str, ts: datetime) -> None:
    audit._conn.execute(
        "INSERT INTO audit_log (ts, action, payload, result) VALUES (?, ?, '{}', '{}')",
        (ts.isoformat(), action),
    )
    audit._conn.commit()


def test_counts_trade_executed_earlier_today_ny(tmp_path):
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now_utc - timedelta(minutes=5))
    assert audit.count_trades_today("America/New_York") == 1


def test_does_not_count_trade_from_a_different_ny_trading_day(tmp_path):
    # 23:30 UTC == 19:30 ET (EDT, UTC-4): todavia "hoy" en UTC pero la barra de
    # las 00:00 UTC del dia siguiente cae fuera del dia de trading de NY si nos
    # quedamos parados un poco antes de la medianoche UTC. Para que el test no
    # dependa de la hora real al correrlo, anclamos un timestamp claramente en
    # el dia de trading de NY de AYER (24h atras) y verificamos que no cuenta.
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now_utc - timedelta(hours=24))
    assert audit.count_trades_today("America/New_York") == 0


def test_only_counts_executed_actions(tmp_path):
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "order_submitted", now_utc)
    _insert_at(audit, "order_rejected", now_utc)
    assert audit.count_trades_today("America/New_York") == 0
