"""Unit tests for shared strategy price rules.

Run: PYTHONPATH=src python -m pytest tests/test_strategy_rules.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pytest  # noqa: E402

from sca.strategy_rules import (  # noqa: E402
    ceil_to_tick,
    final_sell_price,
    floor_to_tick,
    rebuy_price_raw,
    round_to_tick,
    sell_price_raw,
    surrender_sell,
)

TICK = 1e-4
COST = 1.0010  # entry cost shared by the final_sell_price scenarios (plan §4 matrix)


# --- rebuy (unchanged) -----------------------------------------------------
def test_rebuy_uses_anchor_when_bid_is_above_anchor():
    assert rebuy_price_raw(1.0009, -1, bid=1.0012) == pytest.approx(1.0008)


def test_rebuy_uses_bid_when_bid_is_below_anchor():
    assert rebuy_price_raw(1.0009, -1, bid=1.0002) == pytest.approx(1.0001)


# --- round_to_tick (mode dispatcher; single source of tick math) -----------
def test_round_to_tick_floor_rounds_down():
    assert round_to_tick(1.00126, TICK, "floor") == pytest.approx(1.0012)


def test_round_to_tick_ceil_rounds_up():
    assert round_to_tick(1.00121, TICK, "ceil") == pytest.approx(1.0013)


def test_round_to_tick_round_matches_decimal_round():
    # backtest legacy口径 = round(raw, 4); the "round" mode must reproduce it bit-for-bit
    for x in (1.00125, 1.00135, 1.000949, 1.001051, 0.99955):
        assert round_to_tick(x, TICK, "round") == pytest.approx(round(x, 4))


def test_round_to_tick_floor_on_grid_is_stable():
    # 场景9: float noise must never push an on-grid / just-above value off by a tick
    assert round_to_tick(1.0012, TICK, "floor") == pytest.approx(1.0012)
    assert round_to_tick(1.00126, TICK, "floor") == pytest.approx(1.0012)
    assert round_to_tick(0.0001 * 3, TICK, "floor") == pytest.approx(0.0003)


def test_round_to_tick_rejects_unknown_mode():
    with pytest.raises(ValueError):
        round_to_tick(1.0, TICK, "nope")


# --- final_sell_price scenarios (plan §4) ----------------------------------
def test_final_sell_s1_anchor_bound_floor_lands_at_2bp():
    # anchor binds, floor naturally lands at +2bp
    assert final_sell_price(1.00116, 1, COST, 1, 0, TICK,
                            sell_round="floor", min_sell_margin_bp=2) == pytest.approx(1.0012)


def test_final_sell_s2_floor_gives_1bp_lifted_to_2bp_margin():
    # floor would give +1bp (1.0011); the ≥2bp margin lifts it to 1.0012
    assert final_sell_price(1.00115, 0, COST, 1, 0, TICK,
                            sell_round="floor", min_sell_margin_bp=2) == pytest.approx(1.0012)


def test_final_sell_s3_floor_gives_3bp_margin_does_not_lower():
    # floor gives +3bp (1.0013); the 2bp margin must NOT pull it down
    assert final_sell_price(1.00125, 1, COST, 1, 0, TICK,
                            sell_round="floor", min_sell_margin_bp=2) == pytest.approx(1.0013)


def test_final_sell_s4_surrender_exempts_margin_allows_loss_sale():
    # anchor broken >14bp below cost -> surrender: margin floor is waived, sells at a loss
    px = final_sell_price(0.9994, 1, COST, 1, 14, TICK,
                          sell_round="floor", min_sell_margin_bp=2)
    assert px == pytest.approx(0.9995)
    assert px < COST  # the margin did NOT clamp a surrender sale up to break-even


def test_final_sell_s5_ceil_zero_margin_equals_legacy_ceil():
    # sell_round=ceil + margin=0 must equal the legacy live quantize ("sell"=ceil)
    raw = sell_price_raw(1.00116, 1, COST, 1, 0)
    assert final_sell_price(1.00116, 1, COST, 1, 0, TICK,
                            sell_round="ceil", min_sell_margin_bp=0) == pytest.approx(ceil_to_tick(raw, TICK))


def test_final_sell_s6_zero_margin_is_pure_floor():
    raw = sell_price_raw(1.00115, 0, COST, 1, 0)
    assert final_sell_price(1.00115, 0, COST, 1, 0, TICK,
                            sell_round="floor", min_sell_margin_bp=0) == pytest.approx(floor_to_tick(raw, TICK))


def test_final_sell_s10_entry_none_skips_margin_floor():
    # entry=None -> margin floor skipped, no crash, pure rounding of raw
    raw = sell_price_raw(1.00116, 1, None, 1, 0)
    px = final_sell_price(1.00116, 1, None, 1, 0, TICK,
                          sell_round="floor", min_sell_margin_bp=2)
    assert px == pytest.approx(floor_to_tick(raw, TICK))


def test_final_sell_round_default_is_ceil_for_legacy_live():
    # default sell_round (no kwarg) must be ceil so a caller that forgets it keeps live口径
    raw = sell_price_raw(1.00116, 1, COST, 1, 0)
    assert final_sell_price(1.00116, 1, COST, 1, 0, TICK) == pytest.approx(ceil_to_tick(raw, TICK))


# --- surrender boundary: STRICT below (locks the < vs <= gate) --------------
def test_surrender_is_strict_below_threshold():
    # The surrender/waiver gate is the hinge for BOTH floors (min_profit + margin):
    # it must trigger only when the anchor breaks STRICTLY below entry*(1-rest*bp).
    # At the EXACT threshold the slice still holds (no surrender); one ulp below it
    # surrenders. Kills the `<` -> `<=` boundary mutation.
    entry, rest = 1.0, 14.0
    thr = entry * (1 - rest * 1e-4)            # the exact internal threshold value
    assert surrender_sell(thr, entry, rest) is False           # on boundary: hold
    assert surrender_sell(thr * (1 - 1e-9), entry, rest) is True  # just below: surrender


def test_surrender_disabled_when_rest_non_positive():
    # rest_bps <= 0 disables surrender entirely (the floor is never waived).
    assert surrender_sell(0.5, 1.0, 0.0) is False
    assert surrender_sell(0.5, 1.0, -3.0) is False


def test_round_to_tick_round_rejects_non_power_of_ten_tick():
    # round mode 复刻 round(x,ndigits) 只对 10 幂 tick 等价; 非 10 幂 tick 明确报错而非静默偏 grid
    with pytest.raises(ValueError):
        round_to_tick(1.00126, 0.0025, "round")
    # floor/ceil 对任意 tick 仍走 grid, 不报错
    assert round_to_tick(1.00126, 0.0025, "floor") == pytest.approx(1.0)
    assert round_to_tick(1.00126, 0.0025, "ceil") == pytest.approx(1.0025)
