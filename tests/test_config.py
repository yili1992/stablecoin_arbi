"""Tests for sca.config — strategy_for defaults and overrides."""
from sca.config import strategy_for


def test_rebuy_hold_and_floor_defaults_all_symbols():
    for sym in ("USD1USDT", "USDCUSDT"):
        sp = strategy_for(sym)
        assert sp["rebuy_min_hold_sec"] == 43200  # 12h (老板 2026-07-14 从 24h 下调)
        assert sp["rebuy_floor_px"] == 0.9990


def test_usd1_sell_ladder_sizes_more_at_cheaper_prices():
    sp = strategy_for("USD1USDT")
    assert sp["rungs"] == sorted(sp["rungs"])
    assert all(lo > hi for lo, hi in zip(sp["fractions"], sp["fractions"][1:]))
