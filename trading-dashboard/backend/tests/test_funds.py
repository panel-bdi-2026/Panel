import json
import threading

import pytest

from app.funds import FundPosition, FundsStore, FundValidationError
from app.models import Side


@pytest.fixture
def store(tmp_path) -> FundsStore:
    return FundsStore(tmp_path / "funds.json")


def test_create_fund_starts_with_full_cash_and_no_positions(store):
    fund = store.create("Test 1 semana", 5000)
    assert fund.cash_usd == 5000
    assert fund.net_contributed_capital() == 5000
    assert fund.auto_trading_enabled is False
    assert fund.positions == {}
    assert fund.owned_quantity("AAPL") == 0.0


def test_create_fund_registers_initial_capital_as_first_flow(store):
    fund = store.create("Test", 5000)
    assert len(fund.capital_flows) == 1
    assert fund.capital_flows[0].amount == 5000
    assert fund.capital_flows[0].note == "Capital inicial"


def test_buy_reduces_cash_and_opens_position(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    fund = store.get(fund.id)
    assert fund.cash_usd == 4000
    assert fund.owned_quantity("AAPL") == 10
    assert fund.positions["AAPL"].avg_cost == 100


def test_buy_deducts_commission_from_cash_without_changing_avg_cost(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, commission=1.0)
    fund = store.get(fund.id)
    assert fund.cash_usd == 5000 - 10 * 100 - 1.0
    assert fund.positions["AAPL"].avg_cost == 100


def test_buy_twice_averages_cost(store):
    fund = store.create("Test", 10_000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 120)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 20
    assert fund.positions["AAPL"].avg_cost == 110


def test_sell_increases_cash_and_records_realized_pnl(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.SELL, 10, 120)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 5000 + (120 - 100) * 10
    assert fund.realized_pnl_total() == 200.0


def test_sell_deducts_commission_from_cash_and_realized_pnl(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, commission=1.0)
    store.record_fill(fund.id, "AAPL", Side.SELL, 10, 120, commission=1.0)
    fund = store.get(fund.id)
    assert fund.cash_usd == 5000 - 1.0 + (120 - 100) * 10 - 1.0
    # realized_pnl_total() netea AMBAS comisiones (entrada + salida), no solo
    # la de salida: la de entrada ya se habia descontado del cash al comprar,
    # pero antes de este fix nunca se reflejaba en el PnL REALIZADO reportado
    # (ver cost_basis_commission en funds.py).
    assert fund.realized_pnl_total() == 200.0 - 1.0 - 1.0


def test_partial_sell_keeps_remaining_position_and_avg_cost(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.SELL, 4, 150)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 6
    assert fund.positions["AAPL"].avg_cost == 100


def test_partial_sells_prorate_entry_commission_across_both_legs(store):
    # Comision de entrada ($10) se prorratea segun la cantidad vendida en
    # cada venta parcial, no se descuenta entera en la primera: 10 acciones
    # compradas ($1/accion de comision), se venden 4 y despues las 6
    # restantes -- cada venta debe absorber su porcion proporcional (4/10 y
    # 6/10 de los $10 de comision de entrada), y la suma de ambos
    # realized_pnl debe coincidir con el total esperado neteando TODA la
    # comision (entrada + las dos salidas).
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, commission=10.0)
    store.record_fill(fund.id, "AAPL", Side.SELL, 4, 150, commission=1.0)
    fund = store.get(fund.id)
    # PnL bruto de esta porcion: (150-100)*4 = 200; menos comision de salida
    # (1) y la porcion de entrada prorrateada (10 * 4/10 = 4): 200 - 1 - 4 = 195
    assert fund.trades[-1].realized_pnl == pytest.approx(195.0)
    assert fund.positions["AAPL"].cost_basis_commission == pytest.approx(6.0)  # 10 - 4 restante

    store.record_fill(fund.id, "AAPL", Side.SELL, 6, 150, commission=1.0)
    fund = store.get(fund.id)
    # (150-100)*6 = 300; menos comision de salida (1) y el resto de la
    # comision de entrada prorrateada (6): 300 - 1 - 6 = 293
    assert fund.trades[-1].realized_pnl == pytest.approx(293.0)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.positions["AAPL"].cost_basis_commission == 0.0

    # Suma total de ambas ventas == PnL bruto total (200+300=500) menos TODA
    # la comision (10 de entrada + 1 + 1 de las dos salidas) = 488.
    assert fund.realized_pnl_total() == pytest.approx(488.0)


def test_sell_more_than_held_clamps_to_position_quantity(store, caplog):
    """Una venta que pide mas de lo que la posicion tiene (ej. un closed_qty
    mal calculado en una reconciliacion) no debe inflar cash_usd con dinero
    virtual ni registrar un PnL irreal: se acota a lo realmente poseido."""
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    with caplog.at_level("WARNING"):
        store.record_fill(fund.id, "AAPL", Side.SELL, 15, 120)
    fund = store.get(fund.id)
    assert fund.owned_quantity("AAPL") == 0
    assert fund.cash_usd == 5000 + (120 - 100) * 10
    assert fund.realized_pnl_total() == 200.0
    assert fund.trades[-1].quantity == 10
    assert "supera la posicion registrada" in caplog.text


def test_owned_quantity_does_not_see_holdings_never_bought_by_this_fund(store):
    """El guardrail central: un fondo que nunca compro un simbolo no figura
    con cantidad en su ledger, sin importar lo que haya en la cuenta real de
    IBKR (holdings preexistentes o de otro fondo)."""
    fund = store.create("Test", 5000)
    assert fund.owned_quantity("TSLA") == 0.0
    assert fund.can_afford(4999)
    assert not fund.can_afford(5001)


def test_equity_estimate_with_only_cash(store):
    fund = store.create("Test", 5000)
    assert fund.equity_estimate() == 5000


def test_equity_estimate_includes_open_position_at_avg_cost(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    fund = store.get(fund.id)
    assert fund.cash_usd == 4000
    assert fund.equity_estimate() == fund.cash_usd + 10 * fund.positions["AAPL"].avg_cost


def test_equity_estimate_reflects_realized_gains_after_sell(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.record_fill(fund.id, "AAPL", Side.SELL, 10, 150)  # realiza una ganancia de 500
    fund = store.get(fund.id)
    assert fund.equity_estimate() == 5500


def test_set_auto_trading_toggle(store):
    fund = store.create("Test", 5000)
    assert fund.auto_trading_enabled is False
    updated = store.set_auto_trading(fund.id, True)
    assert updated.auto_trading_enabled is True
    assert store.get(fund.id).auto_trading_enabled is True


def test_set_auto_trading_unknown_fund_returns_none(store):
    assert store.set_auto_trading("no-existe", True) is None


def test_record_fill_unknown_fund_returns_none(store):
    assert store.record_fill("no-existe", "AAPL", Side.BUY, 1, 100) is None


def test_persists_across_store_reload(tmp_path):
    path = tmp_path / "funds.json"
    store1 = FundsStore(path)
    fund = store1.create("Test", 5000)
    store1.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store1.apply_capital_flow(fund.id, 1000, note="Aporte extra")

    store2 = FundsStore(path)
    reloaded = store2.get(fund.id)
    assert reloaded is not None
    assert reloaded.cash_usd == 5000
    assert reloaded.owned_quantity("AAPL") == 10
    assert len(reloaded.trades) == 1
    assert reloaded.net_contributed_capital() == 6000
    assert len(reloaded.capital_flows) == 2


def test_apply_capital_flow_increases_cash_and_net_contributed(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, 2000, note="Aporte adicional")
    fund = store.get(fund.id)
    assert fund.cash_usd == 7000
    assert fund.net_contributed_capital() == 7000
    assert len(fund.capital_flows) == 2


def test_apply_capital_flow_negative_amount_withdraws_cash(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, -1000, note="Retiro")
    fund = store.get(fund.id)
    assert fund.cash_usd == 4000
    assert fund.net_contributed_capital() == 4000


def test_apply_capital_flow_unknown_fund_returns_none(store):
    assert store.apply_capital_flow("no-existe", 1000) is None


def test_total_allocated_cash_sums_across_funds(store):
    fund1 = store.create("Fondo 1", 5000)
    fund2 = store.create("Fondo 2", 3000)
    assert store.total_allocated_cash() == 8000
    assert store.total_allocated_cash(exclude_fund_id=fund1.id) == 3000
    assert store.total_allocated_cash(exclude_fund_id=fund2.id) == 5000


def test_buy_opening_position_sets_opened_at_and_stop_loss(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    fund = store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.opened_at is not None
    assert pos.stop_loss_price == 90


def test_buy_topping_up_open_position_keeps_original_opened_at_and_stop_loss(store):
    fund = store.create("Test", 10_000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    fund = store.get(fund.id)
    original_opened_at = fund.positions["AAPL"].opened_at

    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 120, stop_loss_price=110)
    fund = store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.opened_at == original_opened_at
    assert pos.stop_loss_price == 90


def test_sell_closing_position_clears_opened_at_and_stop_loss(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    store.record_fill(fund.id, "AAPL", Side.SELL, 10, 120)
    fund = store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.opened_at is None
    assert pos.stop_loss_price is None


def test_partial_sell_keeps_opened_at_and_stop_loss(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    original_opened_at = store.get(fund.id).positions["AAPL"].opened_at

    store.record_fill(fund.id, "AAPL", Side.SELL, 4, 150)
    pos = store.get(fund.id).positions["AAPL"]
    assert pos.opened_at == original_opened_at
    assert pos.stop_loss_price == 90


def test_update_stop_loss_raises_stop_on_open_position(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    store.update_stop_loss(fund.id, "AAPL", 95)
    fund = store.get(fund.id)
    assert fund.positions["AAPL"].stop_loss_price == 95
    assert fund.owned_quantity("AAPL") == 10  # no toca cash/quantity/trades
    assert fund.cash_usd == 4000
    assert len(fund.trades) == 1


def test_update_stop_loss_noop_when_no_position(store):
    fund = store.create("Test", 5000)
    assert store.update_stop_loss(fund.id, "AAPL", 95) is not None
    fund = store.get(fund.id)
    assert "AAPL" not in fund.positions


def test_update_stop_loss_unknown_fund_returns_none(store):
    assert store.update_stop_loss("no-existe", "AAPL", 95) is None


def test_set_stop_order_id_registers_id_on_open_position(store):
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100, stop_loss_price=90)
    store.set_stop_order_id(fund.id, "AAPL", 12345)
    fund = store.get(fund.id)
    assert fund.positions["AAPL"].stop_order_id == 12345
    assert fund.positions["AAPL"].stop_loss_price == 90  # no toca el precio
    assert fund.owned_quantity("AAPL") == 10  # no toca cash/quantity/trades
    assert fund.cash_usd == 4000
    assert len(fund.trades) == 1


def test_set_stop_order_id_noop_when_no_position(store):
    fund = store.create("Test", 5000)
    assert store.set_stop_order_id(fund.id, "AAPL", 12345) is not None
    fund = store.get(fund.id)
    assert "AAPL" not in fund.positions


def test_set_stop_order_id_unknown_fund_returns_none(store):
    assert store.set_stop_order_id("no-existe", "AAPL", 12345) is None


def test_close_succeeds_when_cash_zero_and_no_positions(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, -5000, note="Retiro total")
    closed = store.close(fund.id)
    assert closed.closed is True
    assert closed.closed_at is not None
    assert store.get(fund.id).closed is True


def test_close_disables_auto_trading(store):
    fund = store.create("Test", 5000, auto_trading_enabled=True)
    store.apply_capital_flow(fund.id, -5000, note="Retiro total")
    closed = store.close(fund.id)
    assert closed.auto_trading_enabled is False


def test_close_rejects_nonzero_cash(store):
    fund = store.create("Test", 5000)
    with pytest.raises(FundValidationError):
        store.close(fund.id)
    assert store.get(fund.id).closed is False


def test_close_rejects_open_positions(store):
    """cash_usd en 0 no alcanza si todavia hay posiciones abiertas: el cash
    pudo haberse ido entero a comprar acciones en vez de quedar libre."""
    fund = store.create("Test", 5000)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
    store.apply_capital_flow(fund.id, -4000, note="Retiro del resto del cash")
    fund = store.get(fund.id)
    assert fund.cash_usd == 0
    with pytest.raises(FundValidationError):
        store.close(fund.id)


def test_close_rejects_already_closed_fund(store):
    fund = store.create("Test", 5000)
    store.apply_capital_flow(fund.id, -5000, note="Retiro total")
    store.close(fund.id)
    with pytest.raises(FundValidationError):
        store.close(fund.id)


def test_close_unknown_fund_returns_none(store):
    assert store.close("no-existe") is None


def test_load_skips_corrupt_fund_entry_but_keeps_valid_ones(tmp_path):
    path = tmp_path / "funds.json"
    good = FundsStore(path).create("Bueno", 1000)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["fondo-roto"] = {"id": "fondo-roto", "name": "Roto"}  # falta cash_usd/created_at
    path.write_text(json.dumps(raw, default=str), encoding="utf-8")

    store2 = FundsStore(path)
    assert store2.get(good.id) is not None
    assert store2.get(good.id).cash_usd == 1000
    assert "fondo-roto" not in store2.funds

    backups = list(tmp_path.glob("funds.corrupt.*.bak"))
    assert len(backups) == 1


def test_load_with_invalid_json_backs_up_and_starts_empty(tmp_path):
    path = tmp_path / "funds.json"
    path.write_text("{esto no es json valido", encoding="utf-8")

    store = FundsStore(path)
    assert store.funds == {}

    backups = list(tmp_path.glob("funds.corrupt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{esto no es json valido"


def test_load_with_non_dict_json_backs_up_and_starts_empty(tmp_path):
    path = tmp_path / "funds.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    store = FundsStore(path)
    assert store.funds == {}

    backups = list(tmp_path.glob("funds.corrupt.*.bak"))
    assert len(backups) == 1


def test_load_does_not_back_up_a_clean_file(tmp_path):
    path = tmp_path / "funds.json"
    FundsStore(path).create("Test", 5000)

    FundsStore(path)

    assert list(tmp_path.glob("funds.corrupt.*.bak")) == []


def test_record_fill_is_serialized_by_internal_lock(store):
    """El lock interno de FundsStore protege la secuencia leer-mutar-guardar:
    mientras un hilo lo tiene tomado, otra llamada a record_fill debe quedar
    bloqueada hasta que se libera, no interleavear su lectura del fondo con la
    escritura del primero."""
    fund = store.create("Test", 5000)
    store._lock.acquire()
    started = threading.Event()
    finished = threading.Event()

    def worker():
        started.set()
        store.record_fill(fund.id, "AAPL", Side.BUY, 10, 100)
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        started.wait(timeout=1)
        assert not finished.wait(timeout=0.2)  # sigue bloqueado: el lock esta tomado
    finally:
        store._lock.release()
    thread.join(timeout=1)
    assert finished.is_set()
    assert store.get(fund.id).owned_quantity("AAPL") == 10


def test_apply_capital_flow_is_serialized_by_internal_lock(store):
    fund = store.create("Test", 5000)
    store._lock.acquire()
    started = threading.Event()
    finished = threading.Event()

    def worker():
        started.set()
        store.apply_capital_flow(fund.id, 1000, note="Aporte concurrente")
        finished.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        started.wait(timeout=1)
        assert not finished.wait(timeout=0.2)
    finally:
        store._lock.release()
    thread.join(timeout=1)
    assert finished.is_set()
    assert store.get(fund.id).cash_usd == 6000


# --- Posiciones cortas (A1 + A2) ---

def _open_short(store, fund_id, symbol, quantity, avg_cost, stop_loss_price=None):
    """Helper: inserta una posicion corta directamente en el store (simula un
    fill de SELL que abrio el short en el broker). No pasa por record_fill
    porque el SELL branch actual no admite abrir cortos (solo cubre largos)."""
    fund = store.funds[fund_id]
    fund.positions[symbol] = FundPosition(
        quantity=-quantity,
        avg_cost=avg_cost,
        stop_loss_price=stop_loss_price,
        initial_stop_loss_price=stop_loss_price,
        cost_basis_commission=0.0,
    )
    store.save()


def test_has_open_positions_true_for_short(store):
    fund = store.create("Test", 10_000)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    assert store.get(fund.id).has_open_positions() is True


def test_buy_covers_short_fully_clears_position(store):
    fund = store.create("Test", 10_000)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 140.0)
    fund = store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.quantity == 0.0
    assert pos.avg_cost == 0.0
    assert pos.stop_loss_price is None
    assert pos.opened_at is None


def test_buy_covers_short_realized_pnl_profit(store):
    """Cubrir a 140 un short abierto a 150 debe generar PnL positivo."""
    fund = store.create("Test", 10_000)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    trade = store.record_fill(fund.id, "AAPL", Side.BUY, 10, 140.0, commission=1.0)
    # PnL = (150 - 140) * 10 - 1.0 commission = 99.0
    assert trade.realized_pnl == pytest.approx(99.0)


def test_buy_covers_short_realized_pnl_loss(store):
    """Cubrir a 160 un short abierto a 150 debe generar PnL negativo."""
    fund = store.create("Test", 10_000)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    trade = store.record_fill(fund.id, "AAPL", Side.BUY, 10, 160.0, commission=0.0)
    # PnL = (150 - 160) * 10 = -100.0
    assert trade.realized_pnl == pytest.approx(-100.0)


def test_buy_covers_short_partially_leaves_residual(store):
    fund = store.create("Test", 10_000)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    store.record_fill(fund.id, "AAPL", Side.BUY, 4, 145.0)
    fund = store.get(fund.id)
    pos = fund.positions["AAPL"]
    assert pos.quantity == pytest.approx(-6.0)
    assert pos.avg_cost == pytest.approx(150.0)
    assert pos.stop_loss_price is None  # heredado del helper que no puso stop


def test_buy_covers_short_cash_reduced_by_cover_cost(store):
    initial_cash = 10_000.0
    fund = store.create("Test", initial_cash)
    _open_short(store, fund.id, "AAPL", 10, 150.0)
    store.record_fill(fund.id, "AAPL", Side.BUY, 10, 140.0, commission=2.0)
    fund = store.get(fund.id)
    # cash se reduce por el precio de recompra + comision
    assert fund.cash_usd == pytest.approx(initial_cash - 10 * 140.0 - 2.0)
