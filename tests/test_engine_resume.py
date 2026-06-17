"""Tests for engine restart/resume wiring in sca.live.engine.PaperEngine (Task 3).

These exercise the additive persistence layer: synchronous state snapshots on
every fill / status write, and seamless resume on reconstruction.

ISOLATION (iron rule): every test writes ONLY under pytest ``tmp_path`` — out_dir
is dirname(csv_path) which we point at tmp_path. No test touches the real ./out
or ./data. The network is never hit: bootstrap()/WS are exercised only through a
monkeypatched ``_rest_kline`` returning offline klines.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_resume.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sca.live.engine as engine  # noqa: E402
from sca.interest import DailyMinInterest  # noqa: E402
from sca.live.persistence import read_events, save_state  # noqa: E402

DAY = 86400
HOUR = 3600
SYMBOL = "USD1USDT"


def make_engine(tmp_path, symbol=SYMBOL, mode="paper", seconds=600):
    """Construct a PaperEngine whose out_dir == tmp_path (via csv_path dirname).

    persist defaults to True (config has a ``live`` section but no ``persist`` key,
    so ``_LIVE.get('persist', True)`` is True) — i.e. the production default path.
    """
    csv_path = str(tmp_path / f"{symbol}_adv.csv")
    return engine.PaperEngine(symbol=symbol, mode=mode, seconds=seconds, csv_path=csv_path)


def _fake_rest_kline(symbol, interval, limit=200):
    """Deterministic offline klines so bootstrap() never touches the network.

    Oldest-first rows ``[startMs, o, h, l, c, vol, turn]`` with timestamps far in
    the past so every 1h candle counts as CLOSED (start + 1h <= now).
    """
    base = 1_600_000_000_000  # 2020-09, safely in the past
    if interval == "60":
        return [[base + i * engine.ONE_HOUR_MS, "1.0", "1.0", "1.0", "1.0", "0", "0"]
                for i in range(30)]
    return [[base + i * 300_000, "1.0", "1.0", "1.0", "1.0", "0", "0"] for i in range(5)]


# ---------------------------------------------------------------------------
# 1. CORE bug regression: resume restores state; a later write_status must NOT
#    truncate the snapshot back to empty (the 12s-overwrite-with-empty bug).
# ---------------------------------------------------------------------------

def test_resume_restores_state_and_status_not_emptied(tmp_path):
    a = make_engine(tmp_path)
    assert a._resumed is False  # clean dir => fresh start

    a.start = 1_700_000_000.0
    a.deployed = True
    a.slices = [
        {"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": 50.0, "sell_px": 1.0005, "entry": None},
    ]
    a.realized_capture = 0.1234
    a.anchor = 1.0
    a.ema = 1.0
    a.last_1h_start = 1_600_000_000_000
    a.interest.observe(0.0, 100.0)          # lazy-init at hour 0
    a.interest.observe(172800.0, 100.0)     # +48h crosses 2 days => settle 1 full day

    a._log_event(1_700_000_000.0, "sell", 1, 1.0005, 50.0)  # persists snapshot + event
    a._log_event(1_700_000_050.0, "buy", 1, 1.0, 50.0)
    a.write_status(1_700_000_100.0)          # persists snapshot + history

    state_path = tmp_path / f"{SYMBOL}_state.json"
    assert state_path.exists()

    # --- reconstruct over same out_dir/symbol: must RESUME, not start empty ---
    b = make_engine(tmp_path)
    assert b._resumed is True
    assert b.deployed is True
    assert b.slices == a.slices
    assert b.realized_capture == a.realized_capture
    assert b.anchor == a.anchor
    assert b.ema == a.ema
    assert b.last_1h_start == a.last_1h_start
    assert b.start == a.start
    assert b.interest.settled == a.interest.settled
    assert b.interest.settled > 0.0

    doc = b.status_doc(1_700_000_200.0)
    assert len(doc["events"]) == 2                  # recovered from the jsonl log
    assert len(doc["history"]) >= 1                 # recovered from the snapshot
    assert len(doc["position"]["slices"]) == 2

    # --- CORE BUG: B's first write_status must NOT clear the state to empty ---
    before = json.loads(state_path.read_text())
    assert len(before["slices"]) == 2
    b.write_status(1_700_000_300.0)
    after = json.loads(state_path.read_text())
    assert len(after["slices"]) == 2                # was the bug: emptied to []
    assert after["realized_capture"] == a.realized_capture
    # user-visible: status_<symbol>.json keeps the position too
    status = json.loads((tmp_path / f"status_{SYMBOL}.json").read_text())
    assert len(status["position"]["slices"]) == 2


# ---------------------------------------------------------------------------
# 2. bootstrap() deploy guard: resumed engines must NOT re-deploy (which would
#    wipe the restored slices); fresh engines deploy exactly once.
# ---------------------------------------------------------------------------

def test_bootstrap_skips_deploy_when_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_rest_kline", _fake_rest_kline)
    eng = make_engine(tmp_path)
    eng._resumed = True
    sentinel = [{"state": "usd1", "qty": 1.23, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng.slices = sentinel
    deploy_calls = []
    monkeypatch.setattr(eng, "_deploy", lambda px: deploy_calls.append(px))

    eng.bootstrap()

    assert deploy_calls == []          # guard fired: resumed => no re-deploy
    assert eng.slices is sentinel      # restored slices untouched


def test_bootstrap_deploys_when_not_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_rest_kline", _fake_rest_kline)
    eng = make_engine(tmp_path)
    assert eng._resumed is False
    deploy_calls = []
    monkeypatch.setattr(eng, "_deploy", lambda px: deploy_calls.append(px))

    eng.bootstrap()

    assert deploy_calls == [1.0]       # normal first-run deploy at last 5m close


# ---------------------------------------------------------------------------
# 3. Backward compatibility: no state file => fresh; persist=False => no files.
# ---------------------------------------------------------------------------

def test_no_state_file_means_fresh_start(tmp_path):
    eng = make_engine(tmp_path)
    assert eng._resumed is False
    assert eng.slices == []
    assert eng.deployed is False
    assert eng.realized_capture == 0.0
    # construction alone (no fills) writes no snapshot
    assert not (tmp_path / f"{SYMBOL}_state.json").exists()


def test_persist_false_writes_no_state_files(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_LIVE", {"persist": False})
    eng = make_engine(tmp_path)
    assert eng.persist is False

    eng.deployed = True
    eng.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng._log_event(1_700_000_000.0, "sell", 0, 1.0, 1.0)
    eng.write_status(1_700_000_050.0)

    # persistence OFF: no state / events files at all (byte-identical to old behavior)
    assert not (tmp_path / f"{SYMBOL}_state.json").exists()
    assert not (tmp_path / f"{SYMBOL}_events.jsonl").exists()
    # the legacy status_<symbol>.json IS still written (unchanged contract)
    assert (tmp_path / f"status_{SYMBOL}.json").exists()


# ---------------------------------------------------------------------------
# 4. run()'s end-of-run deadline: seconds<=0 => run forever (inf); finite run
#    is measured from the (possibly resumed) start.
# ---------------------------------------------------------------------------

def test_t_end_infinite_when_seconds_nonpositive(tmp_path):
    eng = make_engine(tmp_path)
    eng.start = 1000.0
    eng.seconds = 0
    assert eng._t_end() == float("inf")
    eng.seconds = -5
    assert eng._t_end() == float("inf")


def test_t_end_uses_resumed_start_for_finite_run(tmp_path):
    eng = make_engine(tmp_path)
    eng.start = 1000.0
    eng.seconds = 600
    assert eng._t_end() == 1600.0


# ---------------------------------------------------------------------------
# 5. Interest continuity THROUGH the engine: a settled day survives resume with
#    full internal state, and B can keep observing and settle the next day.
# ---------------------------------------------------------------------------

def test_interest_continuity_through_engine_resume(tmp_path):
    D = 300
    a = make_engine(tmp_path)
    # settle exactly one full UTC day (D) via the engine's interest model
    a.interest.observe((D - 1) * DAY + 23 * HOUR + 1800, 10000.0)  # lazy-init pre-midnight
    for h in range(24):
        a.interest.observe(D * DAY + h * HOUR + 60, 10000.0)
    a.interest.observe((D + 1) * DAY + 60, 10000.0)               # cross => settle day D
    a_settled = a.interest.settled
    assert a_settled > 0.0

    # force a state snapshot through the engine fill path
    a.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a._log_event(1_700_000_000.0, "sell", 0, 1.0, 1.0)

    b = make_engine(tmp_path)
    assert b._resumed is True
    # full interest state restored (not just `settled`)
    assert b.interest.settled == a_settled
    assert b.interest._snap_hour == a.interest._snap_hour
    assert b.interest._day_idx == a.interest._day_idx
    assert b.interest._day_hours == a.interest._day_hours
    assert b.interest._day_min == a.interest._day_min
    assert isinstance(b.interest, DailyMinInterest)

    # B keeps observing and settles the NEXT full day -> total doubles. This only
    # holds if _day_hours/_day_min/_snap_hour were faithfully restored; if they
    # had been lost, day D+1 would miss the 24-snapshot gate and credit 0.
    for h in range(1, 24):
        b.interest.observe((D + 1) * DAY + h * HOUR + 60, 10000.0)
    b.interest.observe((D + 2) * DAY + 60, 10000.0)               # cross => settle day D+1
    assert b.interest.settled == pytest.approx(2 * a_settled)


# ===========================================================================
# QA gap-filling tests (QA-Lee). Each pins ONE high-value business invariant
# that the original 8 tests left uncovered. Invariant numbers refer to the
# review brief.
# ===========================================================================


# ---------------------------------------------------------------------------
# INVARIANT #1 — 快照 >= 流水 (snapshot is always ahead of the ledger).
# Write order in _log_event is save_state() FIRST, then append_event(). If a
# crash lands BETWEEN the two writes, the position must be fully captured in the
# snapshot and at most one append-only audit line is missing — NEVER the reverse
# (a ledgered fill the snapshot never saw, which on live would re-execute).
# We inject the crash by making append_event raise; the snapshot must already
# reflect the post-fill slice, and a fresh engine must resume that position from
# the snapshot ALONE (the ledger line never made it to disk).
# ---------------------------------------------------------------------------

def test_snapshot_persists_before_ledger_append_crash_safety(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    eng.deployed = True
    eng.anchor = 1.0
    eng.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    # rung floats with config; derive R so the test is robust to rungs values
    R = round(eng.anchor + eng.rungs[0] / 1e4, 4)

    # Crash injected exactly at the audit-append step (after the snapshot write).
    def boom(*a, **k):
        raise OSError("simulated crash during audit append")
    monkeypatch.setattr(engine, "append_event", boom)

    eng.bid = R                                       # bid >= R => slice 0 sells
    with pytest.raises(OSError):
        eng.evaluate_fills(1_700_000_000.0)

    # snapshot was written BEFORE the (failing) append => the fill IS captured
    state_path = tmp_path / f"{SYMBOL}_state.json"
    assert state_path.exists()
    snap = json.loads(state_path.read_text())
    assert snap["slices"][0]["state"] == "usdt"       # post-fill state persisted
    assert snap["slices"][0]["qty"] == 0.0
    assert snap["slices"][0]["sell_px"] == R
    # the ledger never got the line (crash) => snapshot >= ledger, never the reverse
    assert not (tmp_path / f"{SYMBOL}_events.jsonl").exists()

    # a fresh engine resumes the post-fill position from the SNAPSHOT ALONE
    b = make_engine(tmp_path)
    assert b._resumed is True
    assert b.slices[0]["state"] == "usdt"
    assert b.slices[0]["qty"] == 0.0


# ---------------------------------------------------------------------------
# INVARIANT #2 — append-only ledger GROWS across restarts, never truncates.
# This is the user's ORIGINAL pain point ("重启后历史没了"). A runs N fills, B
# resumes and runs M more; <symbol>_events.jsonl must contain N+M lines with A's
# early fills intact. Guards against resume reopening the ledger in write/truncate
# mode instead of append.
# ---------------------------------------------------------------------------

def test_events_jsonl_appends_across_restarts_not_truncated(tmp_path):
    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.slices = [
        {"state": "usd1", "qty": 10.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usd1", "qty": 10.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usd1", "qty": 10.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
    ]
    # N = 3 fills in generation A
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 10.0)
    a._log_event(1_700_000_001.0, "buy", 0, 1.0000, 10.0)
    a._log_event(1_700_000_002.0, "sell", 1, 1.0002, 10.0)
    assert len(read_events(str(tmp_path), SYMBOL)) == 3

    # --- restart ---
    b = make_engine(tmp_path)
    assert b._resumed is True
    # M = 2 more fills in generation B
    b._log_event(1_700_000_003.0, "buy", 1, 1.0000, 10.0)
    b._log_event(1_700_000_004.0, "sell", 2, 1.0003, 10.0)

    # the FILE must hold N+M = 5 lines, A's earliest fills NOT truncated
    all_ev = read_events(str(tmp_path), SYMBOL)
    assert len(all_ev) == 5
    assert all_ev[0]["side"] == "sell" and all_ev[0]["slice"] == 0   # A's first survived
    assert all_ev[2]["side"] == "sell" and all_ev[2]["slice"] == 1   # A's third survived
    assert all_ev[4]["side"] == "sell" and all_ev[4]["slice"] == 2   # B's last appended
    # timestamps strictly increasing => true append order, no rewrite
    assert [e["ts"] for e in all_ev] == sorted(e["ts"] for e in all_ev)


# ---------------------------------------------------------------------------
# INVARIANT #5 — multiple consecutive restarts: position / realized / settled
# interest do NOT drift and completed work is NOT re-executed or double-credited.
# A -> B -> C, each generation persists; C must see exactly B's realized (no
# double-count) and the SAME settled interest (a completed day is never re-settled).
# ---------------------------------------------------------------------------

def test_three_consecutive_restarts_no_drift(tmp_path):
    D = 600
    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    # settle exactly one full UTC day through the engine accrue path
    a.accrue((D - 1) * DAY + 23 * HOUR + 1800)
    for h in range(24):
        a.accrue(D * DAY + h * HOUR + 60)
    a.accrue((D + 1) * DAY + 60)                       # cross => settle day D
    settled = a.interest.settled
    assert settled > 0.0
    a.realized_capture = 0.5
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 0.0)   # persist generation A

    # --- restart B: no drift, then book +0.3 realized ---
    b = make_engine(tmp_path)
    assert b._resumed is True
    assert b.realized_capture == 0.5
    assert b.interest.settled == settled
    assert b.slices == a.slices
    b.realized_capture += 0.3                          # -> 0.8
    b._log_event(1_700_000_001.0, "buy", 0, 1.0000, 0.0)    # persist generation B

    # --- restart C: realized reflects B exactly (no double-count); settled stable ---
    c = make_engine(tmp_path)
    assert c._resumed is True
    assert c.realized_capture == pytest.approx(0.8)     # NOT 0.5+0.3+0.3 etc.
    assert c.interest.settled == settled                # day D never re-settled
    assert c.slices == a.slices                         # position stable across 3 gens
    # ledger accumulated both generations' audit lines
    assert len(read_events(str(tmp_path), SYMBOL)) == 2


# ---------------------------------------------------------------------------
# INVARIANT #3 (R2) — interest is continuous across a MULTI-HOUR restart gap:
# observe() backfills the skipped integer hours with the current qty, so a day
# whose tail hours were never explicitly observed (engine was DOWN) still reaches
# the 24-snapshot gate and settles — no forfeited day. Driven through the engine
# (accrue) across a real resume.
# ---------------------------------------------------------------------------

def test_engine_interest_no_lost_day_across_restart_gap(tmp_path):
    D = 500
    QTY = 10000.0
    a = make_engine(tmp_path)
    a.deployed = True
    a.slices = [{"state": "usd1", "qty": QTY, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a.accrue((D - 1) * DAY + 23 * HOUR + 1800)        # lazy-init pre-midnight
    for h in range(11):                                # observe only hours 0..10 of day D
        a.accrue(D * DAY + h * HOUR + 60)
    a._log_event(D * DAY + 10 * HOUR + 60, "sell", 0, 1.0, 0.0)  # persist mid-day snapshot
    assert a.interest.settled == 0.0                   # day D not yet settled

    # DOWNTIME spanning hours 11..23 — those snapshots are NEVER explicitly fed.
    b = make_engine(tmp_path)
    assert b._resumed is True
    b.accrue((D + 1) * DAY + 60)                       # single jump backfills 11..23 + crosses

    # day D settled at the uninterrupted value despite the gap (qty constant => min=QTY)
    assert b.interest.settled == pytest.approx(QTY * b.interest.daily_rate)
    assert b.interest.settled > 0.0


# ---------------------------------------------------------------------------
# INVARIANT #4 — corrupt / unknown state must NOT crash startup.
# (a) Unknown schema version (v != 1) => ignore snapshot, start fresh, no raise.
#     The v=2 doc deliberately omits "start"/"slices" so a removed version guard
#     would KeyError — proving the guard is load-bearing.
# ---------------------------------------------------------------------------

def test_unknown_schema_version_starts_fresh_no_crash(tmp_path):
    save_state(str(tmp_path), SYMBOL, {"v": 2, "future_field": "ignored"})
    eng = make_engine(tmp_path)                        # must NOT raise
    assert eng._resumed is False                       # unknown schema ignored
    assert eng.slices == []                            # fresh defaults intact
    assert eng.deployed is False
    assert eng.realized_capture == 0.0


# ---------------------------------------------------------------------------
# INVARIANT #4 (cont.) — a valid snapshot + a half-written (crash-tail) ledger
# must still resume: read_events skips the broken trailing line, the snapshot is
# authoritative for the position. This is the realistic crash shape (snapshot ok,
# last append interrupted).
# ---------------------------------------------------------------------------

def test_engine_resumes_with_broken_events_tail(tmp_path):
    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.slices = [{"state": "usdt", "qty": 0.0, "cash": 50.0, "sell_px": 1.0001, "entry": None}]
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 50.0)   # 1 good event + snapshot

    # simulate a crash mid-append: half-written JSON, no trailing newline
    ev_path = tmp_path / f"{SYMBOL}_events.jsonl"
    with open(ev_path, "a") as f:
        f.write('{"ts": 1700000001000, "side": "bu')

    b = make_engine(tmp_path)                           # must resume, not raise
    assert b._resumed is True
    assert len(b.events) == 1                           # broken tail skipped
    assert b.events[0]["side"] == "sell"
    assert b.slices == a.slices                         # snapshot intact / authoritative


# ---------------------------------------------------------------------------
# INVARIANT #7 (SAFETY) — resume must NEVER restore the live-trading gate from
# disk. A stale/forged snapshot claiming mode="live" must not arm a paper engine
# nor flip its reported mode; arming derives ONLY from live_authorization
# (mode + env + keys), never from a file an attacker/old-run could have written.
# ---------------------------------------------------------------------------

def test_resume_does_not_restore_safety_gate_from_snapshot(tmp_path):
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "live",        # snapshot CLAIMS live
        "start": 1.0, "deployed": True, "realized_capture": 0.0,
        "slices": [], "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    })
    eng = make_engine(tmp_path, mode="paper")
    assert eng._resumed is True                          # position state DID resume
    assert eng.armed is False                            # but the gate stays CLOSED
    assert eng.mode == "paper"                           # mode NOT restored from snapshot


# ---------------------------------------------------------------------------
# INVARIANT #6 — dashboard contract: a RESUMED engine emits a status_doc with the
# exact same top-level key set as a fresh engine (no schema drift introduced by
# the resume path). status_*.json is the dashboard's read contract.
# ---------------------------------------------------------------------------

def test_status_doc_contract_keys_stable_across_resume(tmp_path):
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    fresh = engine.PaperEngine(symbol=SYMBOL, mode="paper", seconds=600,
                               csv_path=str(fresh_dir / f"{SYMBOL}_adv.csv"))
    fresh_keys = set(fresh.status_doc(1_700_000_000.0).keys())

    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 100.0)
    a.write_status(1_700_000_100.0)

    b = make_engine(tmp_path)
    assert b._resumed is True
    assert set(b.status_doc(1_700_000_200.0).keys()) == fresh_keys
