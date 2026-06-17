"""Tests for DailyMinInterest serialization (to_dict / from_dict).

Run: PYTHONPATH=src python -m pytest tests/test_interest_persistence.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.interest import DailyMinInterest  # noqa: E402

DAY = 86400
HOUR = 3600
RATE = 0.10 / 365.0


def test_round_trip_all_fields_equal():
    """from_dict(x.to_dict()) produces an instance with all six fields equal to x."""
    di = DailyMinInterest(RATE)
    D = 200
    # lazy-init then observe a few hours to populate all fields
    di.observe((D - 1) * DAY + 23 * HOUR + 1800, 5000.0)
    di.observe(D * DAY + 0 * HOUR + 60, 5000.0)
    di.observe(D * DAY + 5 * HOUR + 60, 4000.0)  # dip: drives _day_min down

    restored = DailyMinInterest.from_dict(di.to_dict())

    assert restored.daily_rate == di.daily_rate
    assert restored.settled == di.settled
    assert restored._snap_hour == di._snap_hour
    assert restored._day_idx == di._day_idx
    assert restored._day_hours == di._day_hours
    assert restored._day_min == di._day_min
    assert isinstance(restored._day_hours, set), (
        "_day_hours must be restored as set, not list"
    )


def test_round_trip_initial_state():
    """A fresh (never-observed) instance round-trips correctly — Nones preserved."""
    di = DailyMinInterest(RATE)

    restored = DailyMinInterest.from_dict(di.to_dict())

    assert restored.daily_rate == RATE
    assert restored.settled == 0.0
    assert restored._snap_hour is None
    assert restored._day_idx is None
    assert restored._day_hours == set()
    assert restored._day_min is None
    assert isinstance(restored._day_hours, set)


def test_observe_continuity_across_serialization_boundary():
    """Interrupting observations with a serialize→restore must not change settled
    interest or internal state vs. uninterrupted observation.

    S1 fills all 24 snapshots of day D without yet crossing into D+1 (no settlement
    yet). Serialization happens here. S2 crosses midnight into D+1, triggering the
    settlement. This exercises the exact failure mode described in the task: if
    _day_hours were lost/reset, the 24-snapshot gate would never fire and day D
    would credit 0.
    """
    D = 300

    def feed_s1(model):
        # alive before midnight of day D so hour-0 is valid
        model.observe((D - 1) * DAY + 23 * HOUR + 1800, 10000.0)  # lazy-init
        for h in range(24):
            model.observe(D * DAY + h * HOUR + 60, 10000.0)
        # NOT crossing into D+1 yet — settlement deferred to S2

    def feed_s2(model):
        # crosses midnight -> triggers _settle() for day D
        model.observe((D + 1) * DAY + 60, 10000.0)
        for h in range(1, 6):
            model.observe((D + 1) * DAY + h * HOUR + 60, 10000.0)

    # Instance A: interrupted by serialization between S1 and S2
    a = DailyMinInterest(RATE)
    feed_s1(a)
    a_prime = DailyMinInterest.from_dict(a.to_dict())
    feed_s2(a_prime)

    # Instance B: uninterrupted baseline
    b = DailyMinInterest(RATE)
    feed_s1(b)
    feed_s2(b)

    # Settlement must match
    assert a_prime.settled == b.settled
    # Internal state must also match (proves no drift)
    assert a_prime._snap_hour == b._snap_hour
    assert a_prime._day_idx == b._day_idx
    assert a_prime._day_hours == b._day_hours
    assert a_prime._day_min == b._day_min
    # Sanity: day D actually settled (not vacuously equal at 0)
    assert a_prime.settled > 0.0


def test_to_dict_is_json_serializable():
    """to_dict() output passes json.dumps; _day_hours is a sorted list in the dict."""
    di = DailyMinInterest(RATE)
    D = 400
    di.observe((D - 1) * DAY + 23 * HOUR + 1800, 8000.0)
    di.observe(D * DAY + 0 * HOUR + 60, 8000.0)
    di.observe(D * DAY + 3 * HOUR + 60, 7000.0)

    d = di.to_dict()

    # Must not raise
    serialized = json.dumps(d)
    assert serialized  # non-empty string

    # _day_hours stored as list (not set) in the dict
    assert isinstance(d["_day_hours"], list)
    # Must be sorted (deterministic round-trip)
    assert d["_day_hours"] == sorted(d["_day_hours"])

    # None fields must survive json round-trip (JSON null)
    di_fresh = DailyMinInterest(RATE)
    d_fresh = di_fresh.to_dict()
    reloaded = json.loads(json.dumps(d_fresh))
    assert reloaded["_snap_hour"] is None
    assert reloaded["_day_min"] is None


def test_multi_hour_gap_backfilled_across_restart_settles_day():
    """R2: a MULTI-HOUR downtime gap across the serialize boundary is backfilled.

    S1 observes only hours 0..10 of day D, then serializes (engine goes DOWN).
    On restore, a single observe at day D+1 must backfill the skipped hours 11..23
    with the current qty — completing day D's 24-snapshot gate and settling it.
    Result must equal the uninterrupted run; if from_dict had dropped _snap_hour /
    _day_hours / _day_min, the resumed model would re-lazy-init and credit 0 for
    day D (the forfeited-day bug this whole feature exists to prevent).
    """
    D = 500
    QTY = 10000.0

    # uninterrupted baseline: every hour 0..23 observed, then cross
    base = DailyMinInterest(RATE)
    base.observe((D - 1) * DAY + 23 * HOUR + 1800, QTY)   # lazy-init pre-midnight
    for h in range(24):
        base.observe(D * DAY + h * HOUR + 60, QTY)
    base.observe((D + 1) * DAY + 60, QTY)                 # cross => settle day D

    # interrupted: observe hours 0..10, serialize, then ONE jump to day D+1
    a = DailyMinInterest(RATE)
    a.observe((D - 1) * DAY + 23 * HOUR + 1800, QTY)
    for h in range(11):                                   # hours 0..10 only
        a.observe(D * DAY + h * HOUR + 60, QTY)
    resumed = DailyMinInterest.from_dict(a.to_dict())     # <-- restart boundary
    resumed.observe((D + 1) * DAY + 60, QTY)              # backfills 11..23 then crosses

    assert resumed.settled == base.settled                # no lost day across the gap
    assert resumed.settled > 0.0                          # day D genuinely settled (not vacuous)
    assert resumed._day_idx == base._day_idx              # internal state converged too
