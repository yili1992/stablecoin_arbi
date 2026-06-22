"""PURE order-reconciliation core for the Phase-3a maker primitive.

No ccxt, no I/O, no engine state — only deterministic functions over hand-built
dicts (mirrors ``reconcile.py``). ``orders.py`` imports the precision helpers from
here (SINGLE source of truth — it must NOT re-derive tick math), and the engine
imports ``desired_orders`` / ``match_live_orders`` / ``diff_orders``.

Design notes baked in:
- BUY prices FLOOR to the grid (never cross up into asks); SELL prices CEIL
  (never cross down into bids).
- ``desired_orders`` bounds aggregate committed base/quote via running pools and
  drops sub-min orders (no per-order notional cap — D14; total deployment is bounded
  upstream by ``max_total_alloc_usd``).
- ``match_live_orders`` attributes each open order by exact ``order_link_id`` ->
  exact ``order_id`` -> UNAMBIGUOUS ``(side, price~)`` approx (exactly one
  candidate). An order matching no slice, >1 candidate, or a stale ``sca-*`` link
  is returned in a separate UNATTRIBUTED list with NO slice identity — never
  forced onto a guessed slice (R2-P1). It preserves ``filled_qty`` so the engine
  can halt on an executed-but-unattributable order.
- ``diff_orders`` compares REMAINING-to-remaining (``Live.qty`` = leaves) and
  re-prices only on a >= 1-tick move (queue-preserving hysteresis).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sca.strategy_rules import sell_price_raw, rebuy_price_raw

TICK = 0.0001          # default 1bp tick (engine TICK_DP=4); real tick comes from market meta

_ROUND_GUARD = 9       # decimals to absorb x/tick float noise BEFORE floor/ceil
_OUT_DP = 12           # decimals to clean the floor/ceil * step product


# --- precision (single source of truth) ------------------------------------
def floor_to_tick(x: float, tick: float) -> float:
    """Round DOWN to the tick grid (float-noise safe)."""
    return round(math.floor(round(x / tick, _ROUND_GUARD)) * tick, _OUT_DP)


def ceil_to_tick(x: float, tick: float) -> float:
    """Round UP to the tick grid (float-noise safe)."""
    return round(math.ceil(round(x / tick, _ROUND_GUARD)) * tick, _OUT_DP)


def quantize_price(side: str, raw: float, tick: float) -> float:
    """BUY -> floor (never cross up into asks); SELL -> ceil (never cross down)."""
    if side == "buy":
        return floor_to_tick(raw, tick)
    if side == "sell":
        return ceil_to_tick(raw, tick)
    raise ValueError(f"unknown side {side!r}")


def quantize_qty(qty: float, lot: float) -> float:
    """Floor to the lot step (float-noise safe)."""
    return round(math.floor(round(qty / lot, _ROUND_GUARD)) * lot, _OUT_DP)


def qty_tol_for(lot: float) -> float:
    """Default qty tolerance for the diff: half a lot (F17)."""
    return lot / 2.0


# --- dataclasses -----------------------------------------------------------
@dataclass(frozen=True)
class Desired:
    side: str            # "buy" | "sell"
    price: float
    qty: float


@dataclass(frozen=True)
class Live:
    order_id: str | None
    link_id: str | None
    side: str
    price: float
    qty: float                       # REMAINING (leaves) resting size
    filled_qty: float = 0.0          # cumExecQty on the resting order (C-P1#9)
    matched_by: str | None = None    # "link_id" | "order_id" | "approx" (None when unattributed)


@dataclass(frozen=True)
class Action:
    kind: str                        # "place" | "cancel" | "amend" | "leave"
    slice_idx: int
    desired: Desired | None
    live: Live | None


# --- desired set -----------------------------------------------------------
def _s_side(s: dict) -> str:
    return "sell" if s["state"] == "usd1" else "buy"


def desired_orders(anchor, slices, rungs, rebuy_off_bp, tick, lot,
                   avail_base, avail_quote, min_qty, min_cost,
                   min_profit_bp=0.0, rest_bps=0.0,
                   bid: float | None = None) -> dict[int, Desired]:
    """Pure desired-order set with aggregate-avail bound (F16) and min-size drop (F19).
    ``avail_base``/``avail_quote`` are the running pools. Per-order size is the ladder's
    (slice want, bounded by the pool) — there is no per-order notional cap (D14 removed
    ``max_order_usd``; the total deployment is bounded by ``max_total_alloc_usd`` upstream)."""
    out: dict[int, Desired] = {}
    pool_base, pool_quote = avail_base, avail_quote
    for i, s in enumerate(slices):
        if s["state"] == "usd1":                       # want resting SELL at rung
            raw = sell_price_raw(anchor, rungs[i], s.get("entry"),
                                 min_profit_bp, rest_bps)
            px = quantize_price("sell", raw, tick)      # CEIL -> never cross down
            qty = quantize_qty(min(s["qty"], pool_base), lot)
        else:                                          # "usdt" -> want resting BUY at rebuy
            raw = rebuy_price_raw(anchor, rebuy_off_bp, bid)
            px = quantize_price("buy", raw, tick)       # FLOOR -> never cross up
            if px <= 0:
                continue
            qty = quantize_qty(min(s["cash"] / px, pool_quote / px), lot)
        if qty < min_qty or qty * px < min_cost:        # min-size drop -> emit NOTHING
            continue
        out[i] = Desired(_s_side(s), px, qty)
        if s["state"] == "usd1":
            pool_base -= qty                            # decrement pools so aggregate
        else:
            pool_quote -= qty * px                      #   committed base/quote is bounded
    return out


# --- pure order<->slice matcher --------------------------------------------
def _oo_link(o: dict):
    v = o.get("clientOrderId")
    return v if v is not None else o.get("link_id")


def _oo_leaves(o: dict) -> float:
    for k in ("remaining", "qty", "leaves", "amount"):
        if o.get(k) is not None:
            return float(o[k])
    return 0.0


def _oo_filled(o: dict) -> float:
    for k in ("filled_qty", "filled"):
        if o.get(k) is not None:
            return float(o[k])
    return 0.0


def _attribute(slices: list[dict], link, oid, side, price) -> tuple[int | None, str | None]:
    # 1. EXACT order_link_id (authoritative on the maker path)
    if link is not None:
        for i, s in enumerate(slices):
            if s.get("order_link_id") == link:
                return i, "link_id"
        # link present but no slice owns it: a stale OURS (sca-*) is NEVER re-mapped
        # by approx (it could grab a same-price sibling) -> route to unattributed.
        if str(link).startswith("sca-"):
            return None, None
    # 2. EXACT order_id
    if oid is not None:
        for i, s in enumerate(slices):
            if s.get("order_id") == oid:
                return i, "order_id"
    # 3. UNAMBIGUOUS approx: exactly one (side, price~) candidate
    cands = [i for i, s in enumerate(slices)
             if s.get("order_side") == side and s.get("order_px") is not None
             and abs(float(s["order_px"]) - float(price)) < TICK]
    if len(cands) == 1:
        return cands[0], "approx"
    return None, None


def match_live_orders(persisted_slices, open_orders) -> tuple[dict[int, Live], list[Live]]:
    """Attribute each open order to a slice by link_id -> id -> unambiguous-approx.
    Returns ``(matched_by_slice, unattributed)``. Ambiguous / no-slice / stale-``sca-*``
    orders go to ``unattributed`` with ``matched_by=None`` and NO slice identity —
    they are never forced onto a guessed ``slice_idx`` (R2-P1)."""
    matched: dict[int, Live] = {}
    unattributed: list[Live] = []
    for o in open_orders:
        link = _oo_link(o)
        oid = o.get("id")
        side = o.get("side")
        price = o.get("price")
        idx, mb = _attribute(persisted_slices, link, oid, side, price)
        live = Live(order_id=oid, link_id=link, side=side,
                    price=float(price) if price is not None else None,
                    qty=_oo_leaves(o), filled_qty=_oo_filled(o),
                    matched_by=mb if idx is not None else None)
        if idx is None:
            unattributed.append(live)
        else:
            matched[idx] = live
    return matched, unattributed


# --- queue-preserving diff -------------------------------------------------
def _same_price(p1: float, p2: float, price_tol: float) -> bool:
    """True when the two prices are in the same tick bucket. ``price_tol`` is the
    tick; integer-tick rounding makes this immune to float noise at the boundary."""
    return round((p1 - p2) / price_tol) == 0


def diff_orders(desired, matched, price_tol, qty_tol) -> list[Action]:
    """Compute place|cancel|amend|leave actions. ``matched`` is the slice-attributed
    Live map ONLY (ambiguity handled out-of-band). Compares REMAINING-to-remaining."""
    actions: list[Action] = []
    for i in sorted(set(desired) | set(matched)):       # union: every i has d or l
        d = desired.get(i)
        l = matched.get(i)
        if d is None:                                   # live exists, no longer wanted
            actions.append(Action("cancel", i, None, l))
            continue
        if l is None:                                   # wanted, nothing resting
            actions.append(Action("place", i, d, None))
            continue
        if l.side == d.side and _same_price(l.price, d.price, price_tol):
            if abs(l.qty - d.qty) <= qty_tol:
                actions.append(Action("leave", i, d, l))          # PRESERVE QUEUE
            elif d.qty < l.qty and l.filled_qty == 0:
                actions.append(Action("amend", i, d, l))          # qty-down, unfilled -> keep queue
            else:                                                 # qty-up OR partial -> recreate
                actions.append(Action("cancel", i, None, l))
                actions.append(Action("place", i, d, None))
        else:                                                     # side or price changed
            actions.append(Action("cancel", i, None, l))
            actions.append(Action("place", i, d, None))
    return actions
