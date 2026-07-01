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


# --- rebuy (anchor-led + ask-cap) ------------------------------------------
def test_rebuy_is_anchor_minus_offset_when_ask_high():
    assert rebuy_price_raw(1.0009, -1, ask=1.0020) == pytest.approx(1.0008)


def test_rebuy_capped_at_ask_minus_tick_when_ask_low():
    assert rebuy_price_raw(1.0011, -1, ask=1.0010, tick=1e-4) == pytest.approx(1.0009)


def test_rebuy_no_ask_falls_back_to_anchor_offset():
    assert rebuy_price_raw(1.0009, -1) == pytest.approx(1.0008)


# --- rebuy invariant cluster (Task 1: anchor-led + ask-cap, backtest fidelity) ---
# INVARIANT A — backtest byte-fidelity: ask=None => EXACTLY the old pure-anchor
# formula (anchor + off*BP), for ANY offset, independent of tick. The backtest
# (strategy.py:218) calls rounded_rebuy_price with no ask; this must never move.
@pytest.mark.parametrize("anchor,off", [
    (1.0009, -1), (1.0009, -2), (1.0011, -3), (0.9998, -1), (1.0000, -5),
])
def test_rebuy_fallback_is_pure_anchor_offset_for_any_offset(anchor, off):
    # ask=None branch is the backtest/no-book口径; equals old `anchor + off*BP` byte-for-byte.
    assert rebuy_price_raw(anchor, off, ask=None) == pytest.approx(anchor + off * TICK)
    # tick param must be inert when ask is absent (kills a `tick`-leak into the fallback).
    assert rebuy_price_raw(anchor, off, ask=None, tick=5e-4) == pytest.approx(anchor + off * TICK)


# INVARIANT B — anchor leads when the book is far: a high ask does not touch the
# result (cap not binding). off must actually subtract (kills off*BP -> +off*BP / 0).
def test_rebuy_anchor_leads_when_ask_far():
    # anchor+off*BP = 1.0008; ask-tick = 1.0019 >> base => cap inert, base wins.
    assert rebuy_price_raw(1.0009, -1, ask=1.0020, tick=TICK) == pytest.approx(1.0008)
    # if off were dropped/inverted the result would be >= anchor; assert strictly below.
    assert rebuy_price_raw(1.0009, -1, ask=1.0020, tick=TICK) < 1.0009


# INVARIANT C — cap binds and the `- tick` term is load-bearing. This is the
# maker never-cross guard at the raw (pre-quantization) level: dropping `- tick`
# (ask-tick -> ask) or flipping `min` (min -> max) both change THIS value.
def test_rebuy_cap_binds_and_tick_term_is_load_bearing():
    # anchor+off*BP = 1.0010 (would sit AT the ask); cap forces it to ask - tick = 1.0009.
    got = rebuy_price_raw(1.0011, -1, ask=1.0010, tick=TICK)
    assert got == pytest.approx(1.0009)          # ask - tick, NOT ask (kills drop-tick)
    assert got < 1.0010                          # strictly below ask (never-cross)
    # min (not max): a base ABOVE ask-tick must be pulled DOWN to the cap.
    assert rebuy_price_raw(1.0050, -1, ask=1.0010, tick=TICK) == pytest.approx(1.0009)


# INVARIANT D (property) — passive-buy never crosses: for ANY finite ask, the raw
# rebuy is <= ask - tick < ask. A maker buy at/above the ask would take liquidity.
@pytest.mark.parametrize("anchor", [0.9990, 1.0000, 1.0009, 1.0050])
@pytest.mark.parametrize("off", [-1, -2, 0, 3])           # incl. off>=0 (base at/above anchor)
@pytest.mark.parametrize("ask", [0.9995, 1.0003, 1.0010, 1.0100])
def test_rebuy_never_crosses_ask_property(anchor, off, ask):
    tick = TICK
    got = rebuy_price_raw(anchor, off, ask=ask, tick=tick)
    assert got <= ask - tick + 1e-12            # capped at or below ask - tick
    assert got < ask                            # strictly passive (never taker)


# INVARIANT E — non-finite ask is ignored (falls back to pure anchor), same as
# ask=None. NaN/inf must not corrupt the cap via a min() with a non-finite value.
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "x"])
def test_rebuy_non_finite_ask_falls_back_to_anchor(bad):
    assert rebuy_price_raw(1.0009, -1, ask=bad, tick=TICK) == pytest.approx(1.0008)


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
