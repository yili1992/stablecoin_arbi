"""per-symbol exchange resolver — config.exchange_for(symbol).

Reads ``universe[symbol].exchange``; defaults to ``"bybit"`` for any symbol
without the field (every current symbol is still bybit). ``cfg`` injectable.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from sca.config import exchange_for

CFG = {
    "universe": [
        {"symbol": "USD1USDT", "apr": 0.08, "kind": "reserve"},          # no exchange field
        {"symbol": "USDCUSDT", "apr": 0.0, "kind": "reserve",
         "exchange": "bitget"},
    ],
}


def test_default_is_bybit_when_field_absent():
    assert exchange_for("USD1USDT", cfg=CFG) == "bybit"


def test_reads_explicit_exchange_field():
    assert exchange_for("USDCUSDT", cfg=CFG) == "bitget"


def test_unknown_symbol_defaults_to_bybit():
    assert exchange_for("NOSUCHUSDT", cfg=CFG) == "bybit"


def test_empty_universe_defaults_to_bybit():
    assert exchange_for("USD1USDT", cfg={}) == "bybit"
