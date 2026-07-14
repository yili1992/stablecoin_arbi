"""Tests for sca.config — strategy_for defaults and overrides."""
from sca.config import strategy_for


def test_rebuy_hold_and_floor_defaults_all_symbols():
    for sym in ("USD1USDT", "USDCUSDT"):
        sp = strategy_for(sym)
        assert sp["rebuy_min_hold_sec"] == 43200  # 12h (老板 2026-07-14 从 24h 下调)
        assert sp["rebuy_floor_px"] == 0.9990
