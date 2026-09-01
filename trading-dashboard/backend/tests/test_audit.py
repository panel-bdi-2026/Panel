import json
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.audit import AuditLog


def make_audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.db")


def _insert_at(audit: AuditLog, action: str, ts: datetime, fund_id: str | None = None) -> None:
    payload = json.dumps({"fund_id": fund_id} if fund_id is not None else {})
    audit._conn.execute(
        "INSERT INTO audit_log (ts, action, payload, result) VALUES (?, ?, ?, '{}')",
        (ts.isoformat(), action, payload),
    )
    audit._conn.commit()


def _insert_full(
    audit: AuditLog, action: str, ts: datetime, payload: dict, result: dict,
) -> None:
    audit._conn.execute(
        "INSERT INTO audit_log (ts, action, payload, result) VALUES (?, ?, ?, ?)",
        (ts.isoformat(), action, json.dumps(payload), json.dumps(result)),
    )
    audit._conn.commit()


def test_uses_wal_journal_mode(tmp_path):
    """Sin WAL, el journal_mode "DELETE" por default toma un lock exclusivo
    de todo el archivo en cada commit, bloqueando a cualquier lector externo
    (ej. abrir audit.db a mano, o un backup en caliente) durante cada
    record(). WAL permite lectores concurrentes mientras se escribe."""
    audit = make_audit(tmp_path)
    mode = audit._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_counts_trade_executed_earlier_today_ny(tmp_path):
    audit = make_audit(tmp_path)
    # Se ancla a mediodia de NY de HOY (no a "now - 5 min"): si el test corre en
    # los primeros minutos despues de la medianoche de NY, "5 minutos atras"
    # caeria en el dia de trading de AYER y el conteo daria 0. Mediodia de NY de
    # hoy siempre cae dentro de la ventana del dia de trading actual, corra a la
    # hora que corra (antes el test era flaky alrededor de la medianoche de NY).
    noon_ny = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    _insert_at(audit, "order_executed", noon_ny.astimezone(timezone.utc))
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


def test_counts_auto_trade_executed_and_auto_trade_exit(tmp_path):
    # Sin esto, max_trades_per_day no limita en absoluto al motor de
    # auto-trading por fondo: cada entrada/salida automatica quedaria fuera
    # del conteo que usa RulesEngine.evaluate() para aplicar el cap diario.
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "auto_trade_executed", now_utc)
    _insert_at(audit, "auto_trade_exit", now_utc)
    assert audit.count_trades_today("America/New_York") == 2


def test_count_trades_today_filters_by_fund_id(tmp_path):
    # RulesConfig.max_trades_per_day_per_fund necesita poder contar solo las
    # operaciones de UN fondo, distinto del cupo global (sin filtrar).
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now_utc, fund_id="fund-a")
    _insert_at(audit, "auto_trade_executed", now_utc, fund_id="fund-a")
    _insert_at(audit, "order_executed", now_utc, fund_id="fund-b")

    assert audit.count_trades_today("America/New_York") == 3
    assert audit.count_trades_today("America/New_York", fund_id="fund-a") == 2
    assert audit.count_trades_today("America/New_York", fund_id="fund-b") == 1
    assert audit.count_trades_today("America/New_York", fund_id="fund-c") == 0


def test_count_trades_today_fund_filter_ignores_orders_without_fund_id(tmp_path):
    # Una orden sin fund_id (cuenta general, no atada a ningun fondo) no debe
    # contarse para ningun fondo especifico.
    audit = make_audit(tmp_path)
    now_utc = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now_utc)  # sin fund_id

    assert audit.count_trades_today("America/New_York") == 1
    assert audit.count_trades_today("America/New_York", fund_id="fund-a") == 0


def test_record_is_serialized_by_internal_lock(tmp_path):
    """check_same_thread=False permite compartir la conexion entre hilos,
    pero no hace que execute()+commit() desde hilos distintos sea atomico
    por si solo: el lock interno de AuditLog es lo que evita que dos
    llamadas a record() interleaveen y corrompan el conteo del dia."""
    audit = make_audit(tmp_path)
    audit._lock.acquire()
    started = threading.Event()
    finished = threading.Event()

    def worker():
        started.set()
        audit.record("order_submitted", {"symbol": "AAPL"}, {"approved": True})
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        started.wait(timeout=1)
        assert not finished.wait(timeout=0.2)  # sigue bloqueado: el lock esta tomado
    finally:
        audit._lock.release()
    thread.join(timeout=1)
    assert finished.is_set()
    assert len(audit.recent()) == 1


def test_find_trade_context_matches_closest_entry_within_window(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "auto_trade_executed", now,
        {"fund_id": "f1", "symbol": "AAPL", "side": "BUY"},
        {"signal": {"score": 72, "strategy_id": "momentum"}},
    )
    # Otra entrada del mismo símbolo/fondo pero mucho más lejos en el tiempo:
    # no debe ganarle a la más cercana.
    _insert_full(
        audit, "auto_trade_executed", now - timedelta(days=5),
        {"fund_id": "f1", "symbol": "AAPL", "side": "BUY"},
        {"signal": {"score": 10, "strategy_id": "momentum"}},
    )
    entry = audit.find_trade_context(
        "f1", "AAPL", ("auto_trade_executed", "order_executed"), now.isoformat(),
    )
    assert entry is not None
    assert entry["result"]["signal"]["score"] == 72


def test_find_trade_context_ignores_entries_outside_window(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "auto_trade_executed", now - timedelta(days=1),
        {"fund_id": "f1", "symbol": "AAPL", "side": "BUY"}, {"signal": {"score": 50}},
    )
    entry = audit.find_trade_context(
        "f1", "AAPL", ("auto_trade_executed",), now.isoformat(), window_seconds=300,
    )
    assert entry is None


def test_find_trade_context_matches_action_without_side_field(tmp_path):
    # auto_trade_stop_loss_reconciled no guarda "side" en el payload (ver
    # main.py): el matching no puede depender de ese campo.
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "auto_trade_stop_loss_reconciled", now,
        {"fund_id": "f1", "symbol": "AAPL"},
        {"quantity": 10, "price": 95.0, "approximate": True},
    )
    entry = audit.find_trade_context(
        "f1", "AAPL", ("auto_trade_stop_loss_reconciled",), now.isoformat(),
    )
    assert entry is not None
    assert entry["result"]["approximate"] is True


def test_find_trade_context_filters_by_fund_and_symbol(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "auto_trade_exit", now,
        {"fund_id": "OTHER_FUND", "symbol": "AAPL"}, {"reason": "trend_break"},
    )
    _insert_full(
        audit, "auto_trade_exit", now,
        {"fund_id": "f1", "symbol": "OTHER_SYMBOL"}, {"reason": "take_profit"},
    )
    entry = audit.find_trade_context("f1", "AAPL", ("auto_trade_exit",), now.isoformat())
    assert entry is None


def test_find_latest_before_returns_most_recent_draft_before_cutoff(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "signal_order_drafted", now - timedelta(hours=3),
        {"fund_id": "f1", "symbol": "AAPL"}, {"signal": {"score": 60}},
    )
    _insert_full(
        audit, "signal_order_drafted", now - timedelta(hours=1),
        {"fund_id": "f1", "symbol": "AAPL"}, {"signal": {"score": 80}},
    )
    # Un draft DESPUES de before_ts (la aprobación) no debe poder "explicar"
    # una compra que ya sucedió antes que él.
    _insert_full(
        audit, "signal_order_drafted", now + timedelta(hours=1),
        {"fund_id": "f1", "symbol": "AAPL"}, {"signal": {"score": 99}},
    )
    entry = audit.find_latest_before("f1", "AAPL", "signal_order_drafted", now.isoformat())
    assert entry is not None
    assert entry["result"]["signal"]["score"] == 80


def test_find_latest_before_respects_since_days_cutoff(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_full(
        audit, "signal_order_drafted", now - timedelta(days=40),
        {"fund_id": "f1", "symbol": "AAPL"}, {"signal": {"score": 60}},
    )
    entry = audit.find_latest_before(
        "f1", "AAPL", "signal_order_drafted", now.isoformat(), since_days=30,
    )
    assert entry is None


def test_prune_deletes_old_entries_and_keeps_recent(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now - timedelta(days=400))
    _insert_at(audit, "order_executed", now - timedelta(days=200))
    _insert_at(audit, "order_executed", now - timedelta(days=10))
    deleted = audit.prune(older_than_days=365)
    assert deleted == 1
    remaining = audit._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert remaining == 2


def test_prune_returns_zero_when_nothing_to_delete(tmp_path):
    audit = make_audit(tmp_path)
    now = datetime.now(timezone.utc)
    _insert_at(audit, "order_executed", now - timedelta(days=10))
    deleted = audit.prune(older_than_days=365)
    assert deleted == 0


def test_close_does_not_raise_on_fresh_db(tmp_path):
    audit = make_audit(tmp_path)
    audit.close()  # no debe lanzar excepcion
