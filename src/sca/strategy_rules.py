"""Shared strategy price rules for backtest, paper, and live maker orders."""
from __future__ import annotations

import math


BP = 1e-4


def _finite(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def surrender_sell(anchor: float, entry: float | None, rest_bps: float) -> bool:
    """True when the anchor has broken below entry by ``rest_bps``."""
    a = _finite(anchor)
    e = _finite(entry)
    rest = _finite(rest_bps)
    if a is None or e is None or rest is None or rest <= 0:
        return False
    return a < e * (1 - rest * BP)


def sell_price_raw(anchor: float, rung_bp: float, entry: float | None = None,
                   min_profit_bp: float = 0.0, rest_bps: float = 0.0) -> float:
    """Raw sell limit before exchange tick quantization.

    Normal rule Z:
      max(anchor, entry * (1 + min_profit_bp)) + rung_bp

    If ``rest_bps`` is breached, the floor is disabled so the slice can surrender
    at the anchor+rung price. With ``min_profit_bp <= 0`` this degenerates exactly
    to the old anchor+rung behavior.
    """
    a = float(anchor)
    rung = float(rung_bp)
    min_profit = float(min_profit_bp)
    base = a
    e = _finite(entry)
    if min_profit > 0 and e is not None and not surrender_sell(a, e, rest_bps):
        base = max(a, e * (1 + min_profit * BP))
    return base + rung * BP


def rounded_sell_price(anchor: float, rung_bp: float, entry: float | None = None,
                       min_profit_bp: float = 0.0, rest_bps: float = 0.0,
                       ndigits: int = 4) -> float:
    return round(sell_price_raw(anchor, rung_bp, entry, min_profit_bp, rest_bps), ndigits)


def rebuy_price_raw(anchor: float, rebuy_off_bp: float,
                    bid: float | None = None) -> float:
    """Raw rebuy limit before exchange tick quantization.

    The rebuy reference is the lower of the floating anchor and the current best
    bid when a bid is available, so a resting buy stays behind a falling book
    instead of quoting above the current bid.
    """
    base = float(anchor)
    b = _finite(bid)
    if b is not None:
        base = min(base, b)
    return base + float(rebuy_off_bp) * BP


def rounded_rebuy_price(anchor: float, rebuy_off_bp: float,
                        ndigits: int = 4, bid: float | None = None) -> float:
    return round(rebuy_price_raw(anchor, rebuy_off_bp, bid), ndigits)
