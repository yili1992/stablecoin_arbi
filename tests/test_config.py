"""Tests for sca.config — strategy_for defaults and overrides."""
from sca.config import strategy_for


def test_rebuy_hold_and_floor_defaults_all_symbols():
    for sym in ("USD1USDT", "USDCUSDT"):
        sp = strategy_for(sym)
        assert sp["rebuy_min_hold_sec"] == 86400
        assert sp["rebuy_floor_px"] == 0.9990
