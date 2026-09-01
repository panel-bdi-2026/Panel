import pytest
from pydantic import ValidationError

from app.models import OrderRequest, Side, validate_symbol


def make_order(symbol):
    return OrderRequest(symbol=symbol, side=Side.BUY, quantity=1, stop_loss_price=1)


def test_symbol_is_uppercased_and_trimmed():
    assert make_order("  aapl ").symbol == "AAPL"


def test_symbol_allows_dot_and_dash():
    # Tickers reales con clase de accion: BRK.B, RDS-A.
    assert make_order("brk.b").symbol == "BRK.B"
    assert make_order("rds-a").symbol == "RDS-A"


@pytest.mark.parametrize("payload", [
    "<img src=x onerror=alert(1)>",
    "AAPL<script>",
    "A B",          # espacio interno
    "AAPL;DROP",
    "'\"",
    "",             # vacio
    "TOOLONGSYMBOL123",  # mas de 12 caracteres
])
def test_symbol_rejects_invalid_or_malicious_input(payload):
    with pytest.raises(ValidationError):
        make_order(payload)


# validate_symbol() es el mismo chequeo que usa OrderRequest.symbol, expuesto
# como funcion compartida para otros inputs de simbolo (universo del
# screener, query params) que no son un OrderRequest.

def test_validate_symbol_normalizes_and_validates():
    assert validate_symbol("  aapl ") == "AAPL"
    assert validate_symbol("brk.b") == "BRK.B"


def test_validate_symbol_rejects_invalid_or_malicious_input():
    with pytest.raises(ValueError):
        validate_symbol("<script>alert(1)</script>")
