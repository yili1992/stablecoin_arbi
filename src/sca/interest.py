"""Shared USD1 carry model — Bybit's real rule, used by BOTH the live engine
(``sca.live.engine``) and the backtest (``sca.backtest.strategy``) so their
interest口径 cannot drift apart.

RULE (user-confirmed 2026-06-15): interest accrues on the USD1 holding QUANTITY.
Within each UTC natural day, 24 integer-hour snapshots are taken; a COMPLETED day
credits ``min(snapshots) * APR/365``. A day that does not capture all 24 hourly
snapshots (started mid-day, or a boundary that passed before holding) credits 0 —
so the first partial day is naturally $0 ("持有满一天"). Min-not-average: a holding
dip at even one hourly snapshot lowers the WHOLE day's base, so selling
cannibalizes carry.

Feed observations via :meth:`observe(now_sec, usd1_qty)`. ``now_sec`` is UTC
seconds — the live engine passes ``time.time()``; the backtest passes
``bar_ts_ms / 1000`` — so both share the identical integer-hour / day grid and
therefore the identical settlement.
"""
from __future__ import annotations


class DailyMinInterest:
    """Accumulates settled interest from completed UTC days under the min-of-
    hourly-snapshots rule. Stateful; feed time-ordered observations."""

    def __init__(self, daily_rate: float):
        self.daily_rate = daily_rate          # APR / 365
        self.settled = 0.0                    # credited from COMPLETED UTC days
        self._snap_hour: int | None = None    # last integer-hour index snapshotted
        self._day_idx: int | None = None      # UTC day index currently accumulating
        self._day_hours: set[int] = set()     # hours-of-day (0..23) snapshotted this day
        self._day_min: float | None = None    # running min USD1 qty over this day's snaps

    def observe(self, now_sec: float, usd1_qty: float) -> None:
        """Record the USD1 holding at time ``now_sec``; settle on UTC-day rollover."""
        hour_idx = int(now_sec // 3600)
        if self._snap_hour is None:                 # first observation (lazy init)
            # The integer-hour snapshot for the hour we START in already passed
            # BEFORE we held USD1, so it is NOT a valid observation — do not count
            # it. The first valid snapshot is the next integer hour we cross. => a
            # day is "full" only if we were holding before its 00:00 boundary; a
            # mid-day (or exact-boundary) start leaves that day short of 24
            # snapshots and credits 0.
            self._snap_hour = hour_idx
            self._day_idx = hour_idx // 24
            self._day_hours = set()
            self._day_min = None
            return
        while hour_idx > self._snap_hour:           # advance one integer hour at a time
            self._snap_hour += 1
            d = self._snap_hour // 24
            if d != self._day_idx:                  # crossed a UTC-day boundary -> settle
                self._settle()
                self._day_idx = d
                self._day_hours = set()
                self._day_min = None
            # Holding at (≈) this integer hour. Skipped hours (live: WS stall;
            # backtest: ≤1 hour between 5m bars) are backfilled with the CURRENT
            # qty — faithful when the holding is static across the gap. A mid-day
            # START still credits 0 — its early hours are never entered here, so
            # the day stays short of 24 snapshots.
            self._day_hours.add(self._snap_hour % 24)
            self._day_min = usd1_qty if self._day_min is None else min(self._day_min, usd1_qty)

    def _settle(self) -> None:
        if self._day_min is not None and len(self._day_hours) == 24:
            self.settled += self._day_min * self.daily_rate

    def pending(self) -> float:
        """Upper-bound estimate of what the current (incomplete) UTC day will
        credit at rollover (the running min can only fall). 0 when the day cannot
        complete (started mid-day -> never captures hour 0), so it never
        overstates the first partial day."""
        if self._day_min is None or 0 not in self._day_hours:
            return 0.0
        return self._day_min * self.daily_rate
