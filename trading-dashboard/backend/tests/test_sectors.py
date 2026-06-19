from app.sectors import TICKER_SECTOR, _VALID_SECTORS, get_sector


def test_get_sector_returns_known_sector():
    assert get_sector("AAPL") == "Information Technology"


def test_get_sector_is_case_insensitive():
    assert get_sector("aapl") == get_sector("AAPL")


def test_get_sector_returns_none_for_unknown_symbol():
    assert get_sector("ZZZZNOTASYMBOL") is None


def test_all_mapped_sectors_are_valid_gics_sectors():
    assert set(TICKER_SECTOR.values()) <= _VALID_SECTORS
