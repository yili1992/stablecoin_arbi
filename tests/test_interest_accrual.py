"""USD1 interest accrual — pins Bybit's REAL rule (not continuous accrual).

Rule (user-confirmed 2026-06-15):
  - window = UTC natural day (00:00–24:00 UTC)
  - 24 integer-hour snapshots of the USD1 holding QUANTITY
  - each UTC day credits  min(24 snapshots) * APR/365
  - a day that did not capture all 24 hourly snapshots (engine started mid-day /
    a boundary that passed before we held) credits 0  => the first partial UTC
    day is naturally $0 ("持有满一天").

The min-not-average rule means a slice in USDT at even ONE hourly snapshot drops
that day's interest base for the WHOLE day. A "full" day therefore requires being
alive+holding BEFORE that day's 00:00 boundary (so the hour-0 snapshot is observed
by crossing midnight). These tests stop a silent regress to continuous accrual and
pin the start-boundary + downtime-gap edges raised in heterogeneous review.

Run:  PYTHONPATH=src python -m pytest tests/test_interest_accrual.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import engine as E   # noqa: E402

DAY = 86400
HOUR = 3600


def _new_engine():
    return E.PaperEngine(symbol="USD1USDT", mode="paper", seconds=1)


def _deploy_full(eng, price=1.0):
    """Deploy all slices long USD1; return total USD1 qty (== alloc/price)."""
    eng._deploy(price)
    return _usd1_qty(eng)


def _usd1_qty(eng):
    return sum(s["qty"] for s in eng.slices if s["state"] == "usd1")


def _warm_to_day_hour0(eng, D):
    """Make the engine alive+holding before day D's 00:00 so day D is full-eligible
    (its hour-0 snapshot is observed by CROSSING midnight, not by starting in it)."""
    eng.accrue((D - 1) * DAY + 23 * HOUR + 1800)   # 23:30 of D-1 (lazy init, uncounted)
    eng.accrue(D * DAY + 60)                        # cross into D -> settles D-1(0), snaps hour 0


def _daily(qty):
    return qty * (E.APR / 365.0)


# ---- first-day / start-boundary handling (the "持有满一天" requirement) --------

def test_first_partial_day_credits_zero():
    """Start at hour 3 -> day D never observes hours 0,1,2 -> settles 0."""
    eng = _new_engine()
    _deploy_full(eng)
    D = 20000
    eng.accrue(D * DAY + 3 * HOUR + 60)
    for h in range(4, 24):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 1) * DAY + 60)
    assert eng.settled_interest == 0.0


def test_mid_hour0_start_credits_zero():
    """Start at 00:30 — the 00:00 snapshot already passed before we held -> day 0."""
    eng = _new_engine()
    _deploy_full(eng)
    D = 20010
    eng.accrue(D * DAY + 1800)                       # 00:30
    for h in range(1, 24):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 1) * DAY + 60)
    assert eng.settled_interest == 0.0


def test_exact_midnight_start_credits_zero_that_day():
    """Exact 00:00:00 start: boundary coincident with start is NOT counted
    (conservative/honest) -> that day still credits 0."""
    eng = _new_engine()
    _deploy_full(eng)
    D = 20011
    eng.accrue(D * DAY)                              # 00:00:00 exactly
    for h in range(1, 24):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 1) * DAY + 60)
    assert eng.settled_interest == 0.0


# ---- full-day crediting (requires holding across the prior midnight) ----------

def test_full_day_credits_min_qty_times_daily_rate():
    eng = _new_engine()
    qty = _deploy_full(eng)
    D = 20001
    _warm_to_day_hour0(eng, D)
    for h in range(1, 24):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 1) * DAY + 60)                   # settle day D (full)
    assert abs(eng.settled_interest - _daily(qty)) < 1e-9


def test_sell_across_snapshot_lowers_whole_day_base():
    """A slice in USDT at the hour-14/15 snapshots -> day's min = reduced qty for
    the WHOLE day (min-not-average), not a 2/24 proration."""
    eng = _new_engine()
    full = _deploy_full(eng)
    D = 20002
    _warm_to_day_hour0(eng, D)
    for h in range(1, 14):
        eng.accrue(D * DAY + h * HOUR + 60)
    s = eng.slices[0]
    sold_qty = s["qty"]
    s["state"], s["cash"], s["qty"] = "usdt", sold_qty * 1.0, 0.0
    reduced = _usd1_qty(eng)
    assert reduced < full
    eng.accrue(D * DAY + 14 * HOUR + 60)
    eng.accrue(D * DAY + 15 * HOUR + 60)
    s["state"], s["qty"], s["cash"] = "usd1", sold_qty, 0.0
    for h in range(16, 24):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 1) * DAY + 60)
    assert abs(eng.settled_interest - _daily(reduced)) < 1e-9


def test_multiple_full_days_accumulate():
    eng = _new_engine()
    qty = _deploy_full(eng)
    D = 20003
    _warm_to_day_hour0(eng, D)
    for h in range(1, 48):
        eng.accrue(D * DAY + h * HOUR + 60)
    eng.accrue((D + 2) * DAY + 60)                   # settle days D and D+1
    assert abs(eng.settled_interest - 2 * _daily(qty)) < 1e-9


# ---- downtime gaps (heterogeneous-review P1/P2): backfill is faithful in paper -

def test_downtime_gap_with_held_position_still_credits():
    """accrue() skipped for hours 1..22 (engine stalled) while the position is
    held. In paper, no fills occur during a stall, so the holding was static —
    the day is correctly credited in full (NOT zeroed)."""
    eng = _new_engine()
    qty = _deploy_full(eng)
    D = 20005
    _warm_to_day_hour0(eng, D)                       # hour 0 observed
    eng.accrue(D * DAY + 23 * HOUR + 60)             # JUMP hour 0 -> 23, static position
    eng.accrue((D + 1) * DAY + 60)
    assert abs(eng.settled_interest - _daily(qty)) < 1e-9


def test_gap_cannot_rescue_partial_first_day():
    """A gap cannot fabricate the early hours a mid-day start never entered."""
    eng = _new_engine()
    _deploy_full(eng)
    D = 20006
    eng.accrue(D * DAY + 3 * HOUR + 60)              # start hour 3
    eng.accrue(D * DAY + 23 * HOUR + 60)             # big jump; hours 0,1,2 still unobserved
    eng.accrue((D + 1) * DAY + 60)
    assert eng.settled_interest == 0.0


# ---- status doc contract ------------------------------------------------------

def test_status_doc_exposes_settled_and_pending():
    eng = _new_engine()
    qty = _deploy_full(eng)
    D = 20004
    _warm_to_day_hour0(eng, D)
    eng.start = (D - 1) * DAY                         # keep apr_est elapsed sane
    for h in range(1, 6):
        eng.accrue(D * DAY + h * HOUR + 60)
    doc = eng.status_doc(D * DAY + 6 * HOUR + 60)
    pnl = doc["pnl"]
    assert "accrued_interest" in pnl and "pending_interest" in pnl
    assert pnl["accrued_interest"] == 0.0            # nothing settled yet (day incomplete)
    assert abs(pnl["pending_interest"] - _daily(qty)) < 1e-6   # today is accruing
