"""Tests for the PURE order-reconciliation core (sca.live.order_recon) — Phase 3a, Task 1.

This module has NO ccxt, NO I/O: deterministic precision helpers, the desired-order
set (aggregate-avail bound + notional cap + min-size drop), the pure order<->slice
matcher (link_id -> id -> unambiguous-approx; ambiguity -> unattributed), and the
queue-preserving diff. Everything is unit-testable with hand-built dicts.

Run: PYTHONPATH=src python3 -m pytest tests/test_order_recon.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pytest  # noqa: E402

from sca.live.order_recon import (  # noqa: E402
    Action,
    Desired,
    Live,
    ceil_to_tick,
    desired_orders,
    diff_orders,
    floor_to_tick,
    match_live_orders,
    quantize_price,
    quantize_qty,
    qty_tol_for,
)

TICK = 0.0001
LOT = 0.001


# --- helpers ---------------------------------------------------------------
def _slice(state, qty=0.0, cash=0.0, order_id=None, order_link_id=None,
           order_px=None, order_side=None, order_qty=None, order_gen=0):
    return {"state": state, "qty": qty, "cash": cash, "sell_px": 0.0, "entry": None,
            "order_id": order_id, "order_link_id": order_link_id, "order_px": order_px,
            "order_side": order_side, "order_qty": order_qty, "order_gen": order_gen,
            "filled_qty": 0.0}


def _oo(client_order_id=None, oid=None, side="buy", price=1.0, qty=0.0, filled_qty=0.0):
    """A normalized OPEN order row as fetch_open/get_open_orders would expose it
    (each carrying clientOrderId; qty = leaves/remaining; filled_qty = cumExecQty)."""
    return {"clientOrderId": client_order_id, "id": oid, "side": side,
            "price": price, "qty": qty, "filled_qty": filled_qty}


def _live(i, d, *, filled_qty=0.0, qty=None, matched_by="link_id"):
    """Build a matched Live mirroring a Desired (for diff tests)."""
    return Live(order_id=f"o{i}", link_id=f"sca-{i}-0", side=d.side, price=d.price,
                qty=d.qty if qty is None else qty, filled_qty=filled_qty,
                matched_by=matched_by)


# --- precision: floor BUY / ceil SELL never cross --------------------------
def test_quantize_buy_floors_never_crosses_up():
    out = quantize_price("buy", 1.00018, TICK)
    assert out == pytest.approx(1.0001)
    assert out <= 1.00018          # never crosses UP into asks


def test_quantize_sell_ceils_never_crosses_down():
    out = quantize_price("sell", 1.00012, TICK)
    assert out == pytest.approx(1.0002)
    assert out >= 1.00012          # never crosses DOWN into bids


def test_floor_and_ceil_to_tick_on_grid_point_are_idempotent():
    assert floor_to_tick(1.0005, TICK) == pytest.approx(1.0005)
    assert ceil_to_tick(1.0005, TICK) == pytest.approx(1.0005)


def test_quantize_qty_floors_to_lot():
    assert quantize_qty(123.456, 0.01) == pytest.approx(123.45)


def test_qty_tol_defaults_half_lot():            # (F17)
    assert qty_tol_for(0.01) == pytest.approx(0.005)
    assert qty_tol_for(LOT) == pytest.approx(LOT / 2)


# --- desired set: formula, lot param, aggregate bound, cap, min-size -------
def test_desired_usd1_is_sell_at_rung_qty_min_avail():
    slices = [_slice("usd1", qty=10.0)]
    out = desired_orders(1.0000, slices, rungs=[5], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=8.0, avail_quote=0.0, min_qty=LOT, min_cost=1.0)
    d = out[0]
    assert d.side == "sell"
    assert d.price == pytest.approx(1.0005)          # ceil(anchor + 5bp)
    assert d.qty == pytest.approx(8.0)               # min(qty=10, avail_base=8) floored to lot


def test_desired_usd1_sell_uses_entry_min_profit_floor():
    slices = [_slice("usd1", qty=10.0)]
    slices[0]["entry"] = 1.0
    out = desired_orders(0.9990, slices, rungs=[1], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=10.0, avail_quote=0.0, min_qty=LOT, min_cost=1.0,
                         min_profit_bp=1.0, rest_bps=0.0)
    d = out[0]
    assert d.side == "sell"
    assert d.price == pytest.approx(1.0002)


def test_desired_usd1_rest_bps_surrenders_to_anchor_rung():
    slices = [_slice("usd1", qty=10.0)]
    slices[0]["entry"] = 1.0
    out = desired_orders(0.9984, slices, rungs=[1], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=10.0, avail_quote=0.0, min_qty=LOT, min_cost=1.0,
                         min_profit_bp=1.0, rest_bps=15.0)
    d = out[0]
    assert d.price == pytest.approx(0.9985)


def test_desired_usdt_is_buy_at_rebuy_qty_cash_over_price():
    slices = [_slice("usdt", cash=8.0)]
    out = desired_orders(1.0000, slices, rungs=[5], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=0.0, avail_quote=8.0, min_qty=LOT, min_cost=1.0)
    d = out[0]
    assert d.side == "buy"
    assert d.price == pytest.approx(0.9999)          # floor(anchor - 1bp)
    assert d.qty == pytest.approx(8.0)               # floor(min(cash/px, pool/px))


def test_desired_usdt_rebuy_uses_bid_when_bid_is_below_anchor():
    slices = [_slice("usdt", cash=8.0)]
    out = desired_orders(1.0009, slices, rungs=[5], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=0.0, avail_quote=8.0, min_qty=LOT, min_cost=1.0,
                         bid=1.0002)
    d = out[0]
    assert d.side == "buy"
    assert d.price == pytest.approx(1.0001)          # floor(min(anchor, bid) - 1bp)


def test_desired_quantizes_qty_with_lot_param():     # (F17)
    slices = [_slice("usd1", qty=10.0)]
    out = desired_orders(1.0000, slices, rungs=[5], rebuy_off_bp=-1, tick=TICK, lot=0.5,
                         avail_base=9.7, avail_quote=0.0, min_qty=0.5, min_cost=1.0)
    assert out[0].qty == pytest.approx(9.5)          # floor(9.7 / 0.5) * 0.5


def test_desired_aggregate_avail_pool_decrements_bounded():   # (F16)
    slices = [_slice("usd1", qty=6.0), _slice("usd1", qty=6.0)]
    out = desired_orders(1.0000, slices, rungs=[5, 5], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                         avail_base=8.0, avail_quote=0.0, min_qty=LOT, min_cost=1.0)
    assert out[0].qty == pytest.approx(6.0)          # first slice gets its full want
    assert out[1].qty == pytest.approx(2.0)          # pool now 2 -> bounded, not InsufficientFunds
    assert out[0].qty + out[1].qty == pytest.approx(8.0)


def test_desired_drops_below_min_qty_and_min_cost():          # (F19)
    slices = [_slice("usd1", qty=0.0005),    # below min_qty
              _slice("usd1", qty=2.0)]        # qty ok but notional below min_cost
    out = desired_orders(1.0000, slices, rungs=[5, 5], rebuy_off_bp=-1, tick=TICK,
                         lot=0.0001, avail_base=100.0, avail_quote=0.0,
                         min_qty=0.001, min_cost=5.0)
    assert out == {}


# --- hysteresis: <1 tick bucket => zero touch; >=1 tick re-prices (F15) ----
def _one_slice_desired(anchor, rung=4):
    slices = [_slice("usd1", qty=8.0)]
    return desired_orders(anchor, slices, rungs=[rung], rebuy_off_bp=-1, tick=TICK, lot=LOT,
                          avail_base=8.0, avail_quote=0.0, min_qty=LOT, min_cost=1.0)


def test_within_one_tick_bucket_zero_touch():        # (F15)
    d1 = _one_slice_desired(1.00001)                 # raw 1.00041 -> ceil 1.0005
    d2 = _one_slice_desired(1.00003)                 # raw 1.00043 -> ceil 1.0005 (same bucket)
    assert d1[0].price == pytest.approx(d2[0].price)
    matched = {0: _live(0, d1[0])}
    actions = diff_orders(d2, matched, price_tol=TICK, qty_tol=qty_tol_for(LOT))
    assert [a.kind for a in actions] == ["leave"]


def test_one_bp_anchor_move_reprices_affected_rungs():   # (F15)
    d1 = _one_slice_desired(1.0000)                  # ceil(1.0004) -> 1.0005
    d2 = _one_slice_desired(1.0001)                  # ceil(1.0005) -> 1.0005? no: +1 tick -> 1.0006
    assert d2[0].price == pytest.approx(d1[0].price + TICK)
    matched = {0: _live(0, d1[0])}
    actions = diff_orders(d2, matched, price_tol=TICK, qty_tol=qty_tol_for(LOT))
    assert [a.kind for a in actions] == ["cancel", "place"]


# --- diff: leave / cancel+place / amend ------------------------------------
def test_diff_unchanged_prices_all_leave_zero_touch():
    desired = {0: Desired("sell", 1.0005, 8.0), 1: Desired("buy", 0.9999, 5.0)}
    matched = {i: _live(i, d) for i, d in desired.items()}
    actions = diff_orders(desired, matched, price_tol=TICK, qty_tol=qty_tol_for(LOT))
    assert all(a.kind == "leave" for a in actions)
    assert len(actions) == 2


def test_diff_price_move_is_cancel_then_place():
    desired = {0: Desired("sell", 1.0006, 8.0)}
    matched = {0: _live(0, Desired("sell", 1.0005, 8.0))}   # 1 tick lower
    actions = diff_orders(desired, matched, price_tol=TICK, qty_tol=qty_tol_for(LOT))
    assert [a.kind for a in actions] == ["cancel", "place"]


def test_diff_place_when_no_live_and_cancel_when_no_desired():
    desired = {0: Desired("sell", 1.0005, 8.0)}
    matched = {1: _live(1, Desired("buy", 0.9999, 5.0))}
    actions = diff_orders(desired, matched, price_tol=TICK, qty_tol=qty_tol_for(LOT))
    kinds = {a.slice_idx: a.kind for a in actions}
    assert kinds == {0: "place", 1: "cancel"}


def test_diff_qty_down_amends_unfilled_qty_up_or_partial_cancel_recreate():   # (F8)
    desired = {0: Desired("sell", 1.0005, 8.0),     # qty DOWN, unfilled -> amend
               1: Desired("sell", 1.0005, 10.0),    # qty UP -> cancel + place
               2: Desired("sell", 1.0005, 8.0)}     # qty down but PARTIALLY filled -> cancel + place
    matched = {0: _live(0, Desired("sell", 1.0005, 10.0)),
               1: _live(1, Desired("sell", 1.0005, 8.0)),
               2: _live(2, Desired("sell", 1.0005, 10.0), filled_qty=3.0)}
    actions = diff_orders(desired, matched, price_tol=TICK, qty_tol=qty_tol_for(1.0))
    by_slice = {}
    for a in actions:
        by_slice.setdefault(a.slice_idx, []).append(a.kind)
    assert by_slice[0] == ["amend"]
    assert by_slice[1] == ["cancel", "place"]
    assert by_slice[2] == ["cancel", "place"]


def test_diff_compares_remaining_to_remaining():    # (F8 — Live.qty = leaves)
    # original order was 10, 5 already filled -> 5 leaves; desired now wants 5 -> LEAVE.
    desired = {0: Desired("sell", 1.0005, 5.0)}
    matched = {0: _live(0, Desired("sell", 1.0005, 5.0), filled_qty=5.0, qty=5.0)}
    actions = diff_orders(desired, matched, price_tol=TICK, qty_tol=qty_tol_for(1.0))
    assert [a.kind for a in actions] == ["leave"]


# --- pure matcher: link_id -> id -> unambiguous-approx; ambiguity -> unattributed
def test_live_has_filled_qty():                     # (C-P1#9)
    slices = [_slice("usd1", qty=8.0, order_link_id="sca-0-0",
                     order_side="sell", order_px=1.0005)]
    matched, unattributed = match_live_orders(
        slices, [_oo(client_order_id="sca-0-0", oid="A0", side="sell",
                     price=1.0005, qty=7.0, filled_qty=3.0)])
    assert matched[0].filled_qty == pytest.approx(3.0)   # cumExecQty
    assert matched[0].qty == pytest.approx(7.0)          # leavesQty
    assert matched[0].matched_by == "link_id"
    assert unattributed == []


def test_match_live_orders_returns_ambiguous_in_unattributed():   # (C-P1#10, R2-P1)
    slices = [
        _slice("usd1", order_link_id="sca-0-0", order_id="A0", order_side="sell", order_px=1.0005),
        _slice("usdt", order_link_id="sca-1-0", order_id="A1", order_side="buy", order_px=0.9999),
        _slice("usdt", order_link_id="sca-2-0", order_id="A2", order_side="buy", order_px=0.9999),
        _slice("usd1", order_link_id=None, order_id=None, order_side="sell", order_px=1.0010),
    ]
    open_orders = [
        _oo(client_order_id="sca-0-0", oid="A0", side="sell", price=1.0005, qty=8.0),  # -> link_id
        _oo(client_order_id=None, oid="A1", side="buy", price=0.9999, qty=5.0),         # -> order_id
        _oo(client_order_id=None, oid=None, side="sell", price=1.0010, qty=4.0),        # -> approx (1 cand)
        _oo(client_order_id=None, oid=None, side="buy", price=0.9999, qty=5.0),         # >1 cand -> unattributed
        _oo(client_order_id=None, oid=None, side="sell", price=2.0000, qty=1.0),        # no slice -> unattributed
        _oo(client_order_id="sca-9-9", oid=None, side="buy", price=0.9999, qty=5.0),    # stale sca -> unattributed
    ]
    matched, unattributed = match_live_orders(slices, open_orders)
    assert set(matched.keys()) == {0, 1, 3}
    assert matched[0].matched_by == "link_id"
    assert matched[1].matched_by == "order_id"
    assert matched[3].matched_by == "approx"
    # three orders cannot be safely attributed -> never forced onto a guessed slice
    assert len(unattributed) == 3
    stale_links = {u.link_id for u in unattributed}
    assert "sca-9-9" in stale_links                  # stale ours NOT approx-mapped despite 2 buy@0.9999
    assert all(u.matched_by is None for u in unattributed)


# --- defensive paths (a wrong order = P0 -> ~100% branch coverage) ---------
def test_quantize_price_rejects_unknown_side():
    with pytest.raises(ValueError):
        quantize_price("hodl", 1.0, TICK)


def test_desired_skips_nonpositive_buy_price():
    # anchor so low that floor(anchor - 1bp) <= 0 -> emit nothing (no div-by-zero / bad order)
    out = desired_orders(0.00005, [_slice("usdt", cash=8.0)], rungs=[5], rebuy_off_bp=-1,
                         tick=TICK, lot=LOT, avail_base=0.0, avail_quote=8.0,
                         min_qty=LOT, min_cost=1.0)
    assert out == {}


def test_match_reads_alternate_open_order_key_spellings():
    # orders.py exposes link_id / remaining / filled (vs the read-client's clientOrderId / qty).
    slices = [_slice("usd1", order_link_id="sca-0-0", order_side="sell", order_px=1.0005)]
    matched, unattributed = match_live_orders(slices, [
        {"link_id": "sca-0-0", "id": "A0", "side": "sell", "price": 1.0005,
         "remaining": 7.0, "filled": 3.0}])
    assert matched[0].matched_by == "link_id"
    assert matched[0].qty == pytest.approx(7.0)      # from "remaining"
    assert matched[0].filled_qty == pytest.approx(3.0)   # from "filled"
    assert unattributed == []


def test_match_foreign_nonsca_link_falls_through_to_approx():
    # a non-sca link that owns no slice must NOT short-circuit to unattributed; it falls
    # through to id -> approx (a venue-native order we can still attribute by price).
    slices = [_slice("usd1", order_link_id=None, order_id=None, order_side="sell", order_px=1.0010)]
    matched, _ = match_live_orders(slices, [
        _oo(client_order_id="binance-xyz", oid=None, side="sell", price=1.0010, qty=4.0)])
    assert matched[0].matched_by == "approx"


def test_match_unmatched_order_id_falls_through_to_approx():
    # oid present but owns no slice -> id loop finds nothing -> falls through to approx.
    slices = [_slice("usd1", order_link_id=None, order_id="A0", order_side="sell", order_px=1.0010)]
    matched, _ = match_live_orders(slices, [
        _oo(client_order_id=None, oid="ghost", side="sell", price=1.0010, qty=4.0)])
    assert matched[0].matched_by == "approx"


def test_match_open_order_without_qty_keys_defaults_zero_leaves():
    slices = [_slice("usd1", order_link_id="sca-0-0", order_side="sell", order_px=1.0005)]
    matched, _ = match_live_orders(slices, [
        {"clientOrderId": "sca-0-0", "id": "A0", "side": "sell", "price": 1.0005}])
    assert matched[0].qty == pytest.approx(0.0)
    assert matched[0].filled_qty == pytest.approx(0.0)


def test_unattributed_order_with_fill_halts_operator_reconcile():   # (R2-P1)
    # PURE-LAYER half of R2-P1: an unattributable order carries its cumExecQty into the
    # unattributed list so the engine's _halt_operator_reconcile (Task 4) can trip on it.
    # A clean stray (filled 0) is also unattributed but with filled_qty 0 (engine logs only).
    slices = [_slice("usd1", order_link_id="sca-0-0", order_side="sell", order_px=1.0005)]
    matched, unattributed = match_live_orders(slices, [
        _oo(client_order_id=None, oid=None, side="buy", price=2.0000, qty=3.0, filled_qty=2.0),
        _oo(client_order_id=None, oid=None, side="buy", price=3.0000, qty=1.0, filled_qty=0.0),
    ])
    assert matched == {}
    assert len(unattributed) == 2
    with_fill = [u for u in unattributed if u.filled_qty > 0]
    clean = [u for u in unattributed if u.filled_qty == 0]
    assert len(with_fill) == 1 and with_fill[0].filled_qty == pytest.approx(2.0)
    assert len(clean) == 1
