"""Shared strategy price rules for backtest, paper, and live maker orders."""
from __future__ import annotations

import math


BP = 1e-4

# --- tick quantization (SINGLE source of truth; order_recon imports these) ---
_ROUND_GUARD = 9       # decimals to absorb x/tick float noise BEFORE floor/ceil/round
_OUT_DP = 12           # decimals to clean the floor/ceil/round * tick product


def floor_to_tick(x: float, tick: float) -> float:
    """Round DOWN to the tick grid (float-noise safe)."""
    return round(math.floor(round(x / tick, _ROUND_GUARD)) * tick, _OUT_DP)


def ceil_to_tick(x: float, tick: float) -> float:
    """Round UP to the tick grid (float-noise safe)."""
    return round(math.ceil(round(x / tick, _ROUND_GUARD)) * tick, _OUT_DP)


def round_to_tick(x: float, tick: float, mode: str = "round") -> float:
    """Quantize ``x`` to the ``tick`` grid. ``mode`` in {"floor","ceil","round"}.

    "round" reproduces ``round(x, ndigits)`` for a power-of-ten tick — the legacy
    backtest/paper口径. The two-stage rounding (guard then out) keeps float noise off
    the grid boundary (e.g. ``3*tick == 0.00030000000000000003``)."""
    if mode == "round":
        # legacy backtest/paper口径 == round(x, ndigits); reproduce it EXACTLY. Going
        # through x/tick re-scales the float 1.001349… into an exact 10013.5 and
        # banker's-rounds UP to 1.0014, drifting from round(1.00135,4)==1.0013. ndigits
        # is recovered from a power-of-ten tick (the only tick this project uses).
        ndigits = max(0, round(-math.log10(tick)))
        if abs(10.0 ** -ndigits - tick) > tick * 1e-9:
            # round==round(x,ndigits) only equals round-to-tick for a power-of-ten tick;
            # a non-10^k tick (e.g. 0.0025) would silently drift off the grid -> fail loud.
            raise ValueError(f"round mode needs a power-of-ten tick, got {tick}")
        return round(x, ndigits)
    q = round(x / tick, _ROUND_GUARD)
    if mode == "floor":
        n = math.floor(q)
    elif mode == "ceil":
        n = math.ceil(q)
    else:
        raise ValueError(f"unknown round mode {mode!r}")
    return round(n * tick, _OUT_DP)


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


def final_sell_price(anchor: float, rung_bp: float, entry: float | None,
                     min_profit_bp: float, rest_bps: float, tick: float,
                     *, sell_round: str = "ceil", min_sell_margin_bp: float = 0.0) -> float:
    """SINGLE source for the final tick-quantized sell limit — shared by live order
    reconciliation, the dashboard, paper-fill simulation AND the backtest, so the four
    can never drift (backtest == live == paper == dashboard).

    ``sell_round`` selects the tick rounding (legacy: live=ceil, backtest/paper=round).
    ``min_sell_margin_bp`` (> 0) enforces a non-surrender floor: the sell rests at least
    that many bp above ``entry`` cost. A surrender (anchor broken ``rest_bps`` below cost)
    WAIVES the floor so a losing slice can still reset; ``entry`` None also skips it. The
    margin floor itself FLOORs to the grid (the highest tick <= entry*(1+margin); for a
    sub-peg entry < 1 the floored tick can rest ~1 tick BELOW the nominal margin —
    deliberate, to stay consistent with the floor口径 rather than ceil the sell up. The
    peg band entry≈1.000-1.001 lands exactly on +2bp), independent of ``sell_round``.
    With ``min_sell_margin_bp == 0`` and the matching
    legacy ``sell_round`` this is byte-identical to the old per-call-site rounding."""
    raw = sell_price_raw(anchor, rung_bp, entry, min_profit_bp, rest_bps)
    px = round_to_tick(raw, tick, sell_round)
    e = _finite(entry)
    if min_sell_margin_bp > 0 and e is not None and not surrender_sell(anchor, e, rest_bps):
        px = max(px, floor_to_tick(e * (1 + min_sell_margin_bp * BP), tick))
    return px


def rung_for(rungs, i: int) -> float:
    """The sell rung (bp) for slice index ``i``, clamped to the LAST configured rung when
    slices outnumber rungs (a single-rung ladder topped up past 1 slice). For ``i < len(rungs)``
    this is exactly ``rungs[i]`` (zero change for USD1 N5, slices <= rungs). Single-rung USDC
    => every slice uses ``rungs[0]``."""
    return rungs[i] if i < len(rungs) else rungs[-1]


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
