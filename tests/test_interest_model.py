"""Direct unit tests for the SHARED carry model (sca.interest.DailyMinInterest).

The live engine and the backtest both delegate to this one class, so these tests
pin the canonical contract. It is also covered through the live engine
(test_interest_accrual.py, integration) and through the backtest (test_smoke.py).

Run:  PYTHONPATH=src python -m pytest tests/test_interest_model.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.interest import DailyMinInterest   # noqa: E402

DAY = 86400
HOUR = 3600
RATE = 0.10 / 365.0


def _observe_full_day(di, D, qty=10000.0):
    """Observe a complete UTC day D (alive before its 00:00) and roll into D+1."""
    di.observe((D - 1) * DAY + 23 * HOUR + 1800, qty)    # 23:30 prev day (lazy init)
    for h in range(24):
        di.observe(D * DAY + h * HOUR + 60, qty)
    di.observe((D + 1) * DAY + 60, qty)                  # cross -> settle day D


def test_first_partial_day_credits_zero():
    di = DailyMinInterest(RATE)
    D = 100
    di.observe(D * DAY + 3 * HOUR, 10000.0)              # start at hour 3 (lazy init)
    for h in range(4, 24):
        di.observe(D * DAY + h * HOUR, 10000.0)
    di.observe((D + 1) * DAY + 60, 10000.0)
    assert di.settled == 0.0                             # never saw hours 0,1,2


def test_full_day_credits_min_qty_times_rate():
    di = DailyMinInterest(RATE)
    _observe_full_day(di, 200, qty=10000.0)
    assert abs(di.settled - 10000.0 * RATE) < 1e-9


def test_dip_at_one_snapshot_lowers_whole_day():
    """min-not-average: a single low hourly snapshot sets the whole day's base."""
    di = DailyMinInterest(RATE)
    D = 300
    di.observe((D - 1) * DAY + 23 * HOUR + 1800, 10000.0)
    di.observe(D * DAY + 60, 10000.0)                    # hour 0 full
    for h in range(1, 14):
        di.observe(D * DAY + h * HOUR + 60, 10000.0)
    di.observe(D * DAY + 14 * HOUR + 60, 7500.0)         # one low snapshot
    for h in range(15, 24):
        di.observe(D * DAY + h * HOUR + 60, 10000.0)
    di.observe((D + 1) * DAY + 60, 10000.0)
    assert abs(di.settled - 7500.0 * RATE) < 1e-9        # min, not average


def test_seconds_vs_ms_over_1000_parity():
    """The engine feeds time.time() (seconds); the backtest feeds ts_ms/1000.
    Both must land on the identical UTC hour/day grid -> identical settlement."""
    a = DailyMinInterest(RATE)
    b = DailyMinInterest(RATE)
    D = 400
    secs = ([(D - 1) * DAY + 23 * HOUR + 1800]
            + [D * DAY + h * HOUR + 60 for h in range(24)]
            + [(D + 1) * DAY + 60])
    for t in secs:
        a.observe(t, 10000.0)
        b.observe((t * 1000) / 1000.0, 10000.0)          # round-trip through ms
    assert a.settled == b.settled
    assert a.settled > 0.0


def test_pending_zero_on_partial_day_positive_on_full_in_progress():
    di = DailyMinInterest(RATE)
    D = 500
    di.observe(D * DAY + 5 * HOUR, 10000.0)              # started mid-day -> can't complete
    for h in range(6, 10):
        di.observe(D * DAY + h * HOUR, 10000.0)
    assert di.pending() == 0.0

    di2 = DailyMinInterest(RATE)
    E = 600
    di2.observe((E - 1) * DAY + 23 * HOUR + 1800, 10000.0)  # alive before midnight
    for h in range(6):
        di2.observe(E * DAY + h * HOUR + 60, 10000.0)
    assert abs(di2.pending() - 10000.0 * RATE) < 1e-9    # caught hour 0 -> estimate
