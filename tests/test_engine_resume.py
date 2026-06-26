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


def make_engine(tmp_path, symbol=SYMBOL, mode="dryrun", seconds=600):
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

    state_path = tmp_path / f"{SYMBOL}_dryrun_state.json"
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
    status = json.loads((tmp_path / f"status_{SYMBOL}_dryrun.json").read_text())
    assert len(status["position"]["slices"]) == 2


# ---------------------------------------------------------------------------
# 2. bootstrap() deploy guard: resumed engines must NOT re-deploy (which would
#    wipe the restored slices); fresh engines deploy exactly once.
# ---------------------------------------------------------------------------

def test_bootstrap_skips_deploy_when_resumed(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    # bootstrap() now routes klines through the per-symbol adapter; patch its
    # rest_kline so bootstrap stays offline (same intent as the old module shim).
    monkeypatch.setattr(eng.adapter, "rest_kline", _fake_rest_kline)
    eng._resumed = True
    sentinel = [{"state": "usd1", "qty": 1.23, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng.slices = sentinel
    deploy_calls = []
    monkeypatch.setattr(eng, "_deploy", lambda px: deploy_calls.append(px))

    eng.bootstrap()

    assert deploy_calls == []          # guard fired: resumed => no re-deploy
    assert eng.slices is sentinel      # restored slices untouched


def test_bootstrap_deploys_when_not_resumed(tmp_path, monkeypatch):
    eng = make_engine(tmp_path)
    monkeypatch.setattr(eng.adapter, "rest_kline", _fake_rest_kline)
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
    assert not (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()


def test_persist_false_writes_no_state_files(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_LIVE", {"persist": False})
    eng = make_engine(tmp_path)
    assert eng.persist is False

    eng.deployed = True
    eng.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng._log_event(1_700_000_000.0, "sell", 0, 1.0, 1.0)
    eng.write_status(1_700_000_050.0)

    # persistence OFF: no state / events files at all (byte-identical to old behavior)
    assert not (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()
    assert not (tmp_path / f"{SYMBOL}_dryrun_events.jsonl").exists()
    # the legacy status_<symbol>.json IS still written (unchanged contract)
    assert (tmp_path / f"status_{SYMBOL}_dryrun.json").exists()


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
# Write order in _log_event is save_state() FIRST, then append_event(). If the
# ledger append fails (disk full mid-write), the position must be fully captured
# in the snapshot and at most one append-only audit line is missing — NEVER the
# reverse (a ledgered fill the snapshot never saw, which on live would re-execute).
# We inject the failure by making append_event raise; the snapshot must already
# reflect the post-fill slice, and a fresh engine must resume that position from
# the snapshot ALONE (the ledger line never made it to disk).
#
# NOTE (post FIX 3): the persistence OSError is now CAUGHT inside _log_event
# (logged as [PERSISTENCE ERROR], execution continues) rather than propagating —
# a disk fault must not masquerade as a network reconnect. So evaluate_fills must
# NOT raise here; the snapshot>=ledger invariant this test protects is unchanged
# and is still proven by the snapshot/ledger assertions below.
# ---------------------------------------------------------------------------

def test_snapshot_persists_before_ledger_append_crash_safety(tmp_path, monkeypatch, capsys):
    eng = make_engine(tmp_path)
    eng.deployed = True
    eng.anchor = 1.0
    eng.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    # Derive the active sell target from the engine so the persistence invariant is
    # robust to both rung values and the floor/rest pricing rule.
    R = eng.status_doc(1_700_000_000.0)["position"]["slices"][0]["sell_target"]

    # Failure injected exactly at the audit-append step (after the snapshot write).
    def boom(*a, **k):
        raise OSError("simulated disk-full during audit append")
    monkeypatch.setattr(engine, "append_event", boom)

    eng.bid = R                                       # bid >= R => slice 0 sells
    # FIX 3: the OSError is swallowed + logged, NOT propagated (no fake reconnect).
    eng.evaluate_fills(1_700_000_000.0)               # must NOT raise
    assert "[PERSISTENCE ERROR]" in capsys.readouterr().err

    # snapshot was written BEFORE the (failing) append => the fill IS captured
    state_path = tmp_path / f"{SYMBOL}_dryrun_state.json"
    assert state_path.exists()
    snap = json.loads(state_path.read_text())
    assert snap["slices"][0]["state"] == "usdt"       # post-fill state persisted
    assert snap["slices"][0]["qty"] == 0.0
    assert snap["slices"][0]["sell_px"] == R
    # the ledger never got the line (append failed) => snapshot >= ledger, never the reverse
    assert not (tmp_path / f"{SYMBOL}_dryrun_events.jsonl").exists()

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
    assert len(read_events(str(tmp_path), SYMBOL, "dryrun")) == 3

    # --- restart ---
    b = make_engine(tmp_path)
    assert b._resumed is True
    # M = 2 more fills in generation B
    b._log_event(1_700_000_003.0, "buy", 1, 1.0000, 10.0)
    b._log_event(1_700_000_004.0, "sell", 2, 1.0003, 10.0)

    # the FILE must hold N+M = 5 lines, A's earliest fills NOT truncated
    all_ev = read_events(str(tmp_path), SYMBOL, "dryrun")
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
    assert len(read_events(str(tmp_path), SYMBOL, "dryrun")) == 2


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
    save_state(str(tmp_path), SYMBOL, {"v": 2, "future_field": "ignored"}, "dryrun")
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
    ev_path = tmp_path / f"{SYMBOL}_dryrun_events.jsonl"
    with open(ev_path, "a") as f:
        f.write('{"ts": 1700000001000, "side": "bu')

    b = make_engine(tmp_path)                           # must resume, not raise
    assert b._resumed is True
    assert len(b.events) == 1                           # broken tail skipped
    assert b.events[0]["side"] == "sell"
    assert b.slices == a.slices                         # snapshot intact / authoritative


# ---------------------------------------------------------------------------
# FIX (Codex P1) — a VALID snapshot + a NON-UTF8 (bit-rotted) events ledger must
# still resume cleanly. read_events runs AFTER the atomic resume guard commits
# the snapshot, so an unhandled UnicodeDecodeError there crashes boot despite a
# perfectly good snapshot. The engine must construct WITHOUT raising, _resumed
# True, slices restored from the snapshot, and events fall back to []. (The
# broken-tail test above covers a partially-valid ledger; this covers a wholly
# unreadable one.)
# ---------------------------------------------------------------------------

def test_engine_resumes_with_non_utf8_events_ledger(tmp_path):
    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a.realized_capture = 0.55
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 100.0)  # 1 good event + snapshot

    # external corruption: overwrite the ledger with raw non-UTF8 bytes
    ev_path = tmp_path / f"{SYMBOL}_dryrun_events.jsonl"
    with open(ev_path, "wb") as f:
        f.write(b"\xff\xfe corrupted ledger \x80\x81")
    # snapshot (state.json) is untouched and valid
    assert (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()

    b = make_engine(tmp_path)                            # must NOT raise on boot
    assert b._resumed is True                            # snapshot still authoritative
    assert b.slices == a.slices                          # position restored from snapshot
    assert b.realized_capture == 0.55
    assert b.events == []                                # unreadable ledger -> empty, not a crash


# ---------------------------------------------------------------------------
# INVARIANT #7 (SAFETY) — resume must NEVER restore the live-trading gate from
# disk. A stale/forged snapshot claiming mode="live" must not arm a DRYRUN engine
# nor flip its reported mode; arming derives ONLY from live_authorization
# (mode == "live", D14), never from a file an attacker/old-run could have written.
# ---------------------------------------------------------------------------

def test_resume_does_not_restore_safety_gate_from_snapshot(tmp_path):
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "live",        # snapshot CLAIMS live
        "start": 1.0, "deployed": True, "realized_capture": 0.0,
        "slices": [], "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    }, "dryrun")
    eng = make_engine(tmp_path, mode="dryrun")
    assert eng._resumed is True                          # position state DID resume
    assert eng.armed is False                            # but the gate stays CLOSED (dryrun)
    assert eng.mode == "dryrun"                          # mode NOT restored from snapshot


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


def test_daily_notification_day_persists_across_resume(tmp_path):
    a = make_engine(tmp_path)
    a._last_daily_notify_day = "2026-06-20"
    a.write_status(1_700_000_000.0)

    b = make_engine(tmp_path)

    assert b._resumed is True
    assert b._last_daily_notify_day == "2026-06-20"


# ===========================================================================
# CE multi-persona review hardening (report-only findings -> fixes).
# Each pins ONE hardening item from the review. Numbers refer to that brief.
# ===========================================================================


# ---------------------------------------------------------------------------
# FIX 1 (P1, cross-confirm) — a v==1 snapshot that is MISSING a required key
# (hand-edited / truncated / future-schema drift) must NOT KeyError in __init__
# and crash the process at boot. _maybe_resume must guard the whole field-
# restore block (including DailyMinInterest.from_dict) and, on any KeyError/
# TypeError, log to stderr and fall back to a FULLY FRESH start (NOT a half-
# restored hybrid). _resumed stays False and every restored field is back at its
# __init__ default.
#
# (a) missing top-level "slices" — the field-restore block dies before it ever
#     reaches from_dict; defaults must remain pristine.
# ---------------------------------------------------------------------------

def test_v1_missing_slices_key_starts_fresh_no_crash(tmp_path, capsys):
    # v==1 (so the version guard does NOT early-return) but "slices" absent.
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "paper",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 9.9,
        # "slices" deliberately omitted
        "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [{"t": 1, "equity": 1.0}],
    }, "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise

    assert eng._resumed is False                         # fell back to fresh
    # every field is back at __init__ default — no half-restored hybrid
    assert eng.slices == []
    assert eng.deployed is False
    assert eng.realized_capture == 0.0
    assert eng.anchor is None
    assert eng.ema is None
    assert eng.last_1h_start is None
    assert eng.history == []
    assert eng.interest.settled == 0.0
    err = capsys.readouterr().err
    assert "missing/invalid key" in err


# ---------------------------------------------------------------------------
# FIX 1 (b) — missing "interest": the failure surfaces INSIDE
# DailyMinInterest.from_dict (it does d["daily_rate"]). The atomic-restore must
# still leave a pristine fresh engine. This is the trickier case: several fields
# (start/deployed/realized/slices/...) precede the from_dict call, so a non-atomic
# implementation that assigns to self as it goes WOULD leave a half-restored
# engine here. Asserting the defaults proves the restore is atomic.
# ---------------------------------------------------------------------------

def test_v1_missing_interest_key_starts_fresh_atomically(tmp_path, capsys):
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "paper",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 7.7,
        "slices": [{"state": "usd1", "qty": 5.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}],
        # "interest" deliberately omitted -> from_dict KeyErrors on d["daily_rate"]
        "anchor": 1.2345, "ema": 1.2345, "last_1h_start": 999, "history": [{"t": 1}],
    }, "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise

    assert eng._resumed is False
    # ATOMICITY: none of the pre-from_dict fields may have leaked onto self
    assert eng.slices == []                              # NOT the 1-slice list
    assert eng.deployed is False                         # NOT True
    assert eng.realized_capture == 0.0                   # NOT 7.7
    assert eng.anchor is None                            # NOT 1.2345
    assert eng.ema is None
    assert eng.last_1h_start is None                     # NOT 999
    assert eng.history == []
    assert eng.interest.settled == 0.0
    assert "missing/invalid key" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# FIX 1 (c) — a wrong-TYPE field (TypeError, not KeyError) is also caught and
# triggers the same fresh fallback. e.g. interest is a list, not a dict, so
# from_dict's d["daily_rate"] raises TypeError (list indices must be integers).
# ---------------------------------------------------------------------------

def test_v1_wrong_type_interest_starts_fresh_no_crash(tmp_path):
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "paper",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 1.0,
        "slices": [], "interest": ["not", "a", "dict"],   # TypeError in from_dict
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    }, "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise
    assert eng._resumed is False
    assert eng.deployed is False
    assert eng.realized_capture == 0.0
    assert eng.interest.settled == 0.0


# ---------------------------------------------------------------------------
# FIX 1 (d) — a COMPLETE, valid v==1 snapshot still resumes (the guard must not
# break the happy path). Regression that the try/except didn't swallow a good
# restore.
# ---------------------------------------------------------------------------

def test_v1_complete_state_still_resumes_after_guard(tmp_path):
    a = make_engine(tmp_path)
    a.deployed = True
    a.anchor = 1.0
    a.realized_capture = 0.42
    a.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a._log_event(1_700_000_000.0, "sell", 0, 1.0001, 100.0)

    b = make_engine(tmp_path)
    assert b._resumed is True                            # guard did NOT block a valid restore
    assert b.realized_capture == 0.42
    assert b.slices == a.slices
    assert b.deployed is True


# ---------------------------------------------------------------------------
# FIX (Codex P1) — a v==1 snapshot whose keys are ALL PRESENT but WRONG-TYPED
# must be treated as malformed => FULLY fresh start, never a hybrid that commits
# a bad-typed field and then crashes downstream. The pre-existing guard only
# caught KeyError/TypeError (missing keys / from_dict failures); a string-typed
# `start`, a dict-typed `slices`, or a string-typed `deployed` assign cleanly to
# self and only blow up LATER in _t_end()/accrue()/status_doc/evaluate_fills,
# violating the "malformed v1 -> fresh start" contract. _maybe_resume must run a
# lightweight type check on the LOCALS before committing.
#
# Each case asserts: construction does NOT raise, _resumed is False, every field
# is at its __init__ default (atomic — no half-commit), the downstream
# _t_end() call (which would TypeError on a str `start`) works, and the log names
# the invalid-type path (distinct from the missing-key message).
# ---------------------------------------------------------------------------

def _valid_v1_state(**overrides):
    """A complete, well-typed v==1 snapshot dict; override one field to malform it."""
    st = {
        "v": 1, "symbol": SYMBOL, "mode": "paper",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 1.5,
        "slices": [{"state": "usd1", "qty": 5.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}],
        "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [{"t": 1, "equity": 1.0}],
    }
    st.update(overrides)
    return st


def _assert_fresh_defaults(eng):
    assert eng._resumed is False
    assert eng.slices == []
    assert eng.deployed is False
    assert eng.realized_capture == 0.0
    assert eng.anchor is None
    assert eng.ema is None
    assert eng.last_1h_start is None
    assert eng.history == []
    assert eng.interest.settled == 0.0


def test_v1_start_wrong_type_starts_fresh_no_crash(tmp_path, capsys):
    # start is a STRING -> assigning it to self.start would TypeError later in
    # _t_end() (self.start + self.seconds). Must be rejected up front.
    save_state(str(tmp_path), SYMBOL, _valid_v1_state(start="bad"), "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise
    _assert_fresh_defaults(eng)
    # downstream call that a committed str `start` would have crashed:
    eng.seconds = 600
    assert eng._t_end() == eng.start + 600               # start is a real number now
    assert "invalid field type" in capsys.readouterr().err


def test_v1_slices_wrong_type_starts_fresh_no_crash(tmp_path, capsys):
    # slices is a DICT, not a list -> would break evaluate_fills' `for s in slices`
    # enumerate semantics / status_doc. Rejected as malformed.
    save_state(str(tmp_path), SYMBOL, _valid_v1_state(slices={}), "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise
    _assert_fresh_defaults(eng)
    assert "invalid field type" in capsys.readouterr().err


def test_v1_deployed_wrong_type_starts_fresh_no_crash(tmp_path, capsys):
    # deployed is a STRING "yes", not a bool. Truthy strings would silently flip
    # the engine into a deployed state with no real slices. Rejected.
    save_state(str(tmp_path), SYMBOL, _valid_v1_state(deployed="yes"), "dryrun")
    eng = make_engine(tmp_path)                          # must NOT raise
    _assert_fresh_defaults(eng)
    assert "invalid field type" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# FIX 3 (P1, reliability) — a persistence DISK error (ENOSPC / EACCES) inside
# _log_event must NOT propagate to run()'s outer `except Exception` (which would
# misread it as a network drop and spin a 2s reconnect loop, hiding a fatal disk
# fault). It must be caught, logged with a CLEAR [PERSISTENCE ERROR] marker to
# stderr, and execution must CONTINUE — and the in-memory event is still recorded
# (self.events grew) so nothing is lost from RAM and the next snapshot retries.
# ---------------------------------------------------------------------------

def test_log_event_disk_error_does_not_propagate(tmp_path, monkeypatch, capsys):
    eng = make_engine(tmp_path)
    eng.deployed = True
    eng.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]

    def boom_save(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(engine, "save_state", boom_save)

    # must NOT raise despite save_state failing
    eng._log_event(1_700_000_000.0, "sell", 0, 1.0005, 1.0)

    err = capsys.readouterr().err
    assert "[PERSISTENCE ERROR]" in err                  # clearly visible, not a fake reconnect
    # in-memory record survives -> nothing lost from RAM, next snapshot retries
    assert len(eng.events) == 1
    assert eng.events[0]["side"] == "sell"


def test_log_event_disk_error_on_append_does_not_propagate(tmp_path, monkeypatch, capsys):
    # append_event failing (snapshot ok, ledger write fails) must also be caught.
    eng = make_engine(tmp_path)
    eng.deployed = True
    eng.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]

    def boom_append(*a, **k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(engine, "append_event", boom_append)

    eng._log_event(1_700_000_000.0, "buy", 0, 1.0000, 1.0)   # must NOT raise
    assert "[PERSISTENCE ERROR]" in capsys.readouterr().err
    assert len(eng.events) == 1


# ---------------------------------------------------------------------------
# FIX 3 (cont.) — same guarantee for write_status: a save_state OSError there
# must not bubble to run()'s reconnect path. The status_<symbol>.json itself is
# still written (it precedes the snapshot persist), and write_status returns its
# path normally.
# ---------------------------------------------------------------------------

def test_write_status_disk_error_does_not_propagate(tmp_path, monkeypatch, capsys):
    eng = make_engine(tmp_path)
    eng.deployed = True
    eng.anchor = 1.0
    eng.slices = [{"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]

    def boom_save(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(engine, "save_state", boom_save)

    path = eng.write_status(1_700_000_000.0)             # must NOT raise

    err = capsys.readouterr().err
    assert "[PERSISTENCE ERROR]" in err
    # the dashboard status file (written before the snapshot) still landed
    assert os.path.exists(path)
    assert path == str(tmp_path / f"status_{SYMBOL}_dryrun.json")


def test_status_file_is_mode_tagged(tmp_path):
    # D17: the status snapshot (dashboard data: positions/history/markout) is mode-tagged
    # like D15's state/events, so a dryrun and a live run on the SAME out_dir never
    # overwrite each other. No untagged status_<symbol>.json collision.
    SL = {"state": "usd1", "qty": 1.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}
    for mode, tag in (("dryrun", "dryrun"), ("live", "live")):
        eng = make_engine(tmp_path, mode=mode)
        eng.anchor = 1.0
        eng.deployed = True
        eng.slices = [dict(SL)]
        p = eng.write_status(1_700_000_000.0)
        assert eng.mode == tag                                  # mode resolved as expected
        assert p == str(tmp_path / f"status_{SYMBOL}_{tag}.json")
        assert (tmp_path / f"status_{SYMBOL}_{tag}.json").exists()
    # both modes wrote their OWN file; no shared/untagged file
    assert (tmp_path / f"status_{SYMBOL}_dryrun.json").exists()
    assert (tmp_path / f"status_{SYMBOL}_live.json").exists()
    assert not (tmp_path / f"status_{SYMBOL}.json").exists()


# ---------------------------------------------------------------------------
# FIX 4 (P3 -> fix, security) — regression nailing the mode footgun, D14 model.
# A snapshot claiming mode="live" must NEVER arm a DRYRUN engine: arming derives
# PURELY from the requested mode (req_mode == "live"), never from the snapshot's
# mode field, which _maybe_resume deliberately ignores.
# ---------------------------------------------------------------------------

def test_snapshot_mode_live_does_not_arm_dryrun_engine(tmp_path):
    # a forged/stale snapshot claims live, but a DRYRUN-constructed engine stays disarmed
    # (the snapshot's mode field is never the arming signal).
    save_state(str(tmp_path), SYMBOL, {
        "v": 1, "symbol": SYMBOL, "mode": "live",         # forged/stale: claims live
        "start": 1.0, "deployed": True, "realized_capture": 0.0,
        "slices": [], "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    }, "dryrun")
    eng = make_engine(tmp_path, mode="dryrun")
    assert eng._resumed is True                           # position state DID resume
    assert eng.armed is False                             # mode NOT restored -> stays disarmed
    assert eng.mode == "dryrun"


# ===========================================================================
# D15 — state files are segregated by the engine's RESOLVED mode, so a dryrun
# simulation can never be loaded by a (mainnet, real-money) live run. The engine
# threads ``self.mode`` (dryrun|live) as the persistence tag; the legacy untagged
# path is never used by a mode-resolved engine.
# ===========================================================================


def test_engine_state_path_mode_qualified(tmp_path):
    # a dryrun engine persists to <symbol>_dryrun_state.json (+ _dryrun_events.jsonl),
    # NEVER the legacy untagged <symbol>_state.json / <symbol>_events.jsonl.
    eng = make_engine(tmp_path, mode="dryrun")
    assert eng.mode == "dryrun"
    eng.deployed = True
    eng.anchor = 1.0
    eng.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng._log_event(1_700_000_000.0, "sell", 0, 1.0005, 100.0)   # paper path: snapshot + ledger

    assert (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()
    assert (tmp_path / f"{SYMBOL}_dryrun_events.jsonl").exists()
    assert not (tmp_path / f"{SYMBOL}_state.json").exists()      # legacy path NOT used
    assert not (tmp_path / f"{SYMBOL}_events.jsonl").exists()


def test_dryrun_and_live_use_separate_state_files(tmp_path):
    # THE core D15 invariant: a dryrun run persists real position state; a FRESH live
    # engine over the SAME out_dir + symbol must NOT load that dryrun snapshot (it reads
    # <symbol>_live_state.json) — it starts fresh and seeds from scratch. This is the
    # dryrun->live pollution the segregation prevents.
    dry = make_engine(tmp_path, mode="dryrun")
    dry.deployed = True
    dry.anchor = 1.0
    dry.realized_capture = 0.99
    dry.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    dry._log_event(1_700_000_000.0, "sell", 0, 1.0005, 100.0)
    assert (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()   # dryrun state is on disk

    # a fresh LIVE engine, SAME out_dir + symbol (csv dirname == tmp_path)
    live = engine.PaperEngine(symbol=SYMBOL, mode="live", seconds=600,
                              csv_path=str(tmp_path / f"{SYMBOL}_adv.csv"))
    assert live.mode == "live"                                  # armed -> resolves to live tag
    assert live._resumed is False                               # did NOT load the dryrun snapshot
    assert live.slices == []                                    # no leaked position
    assert live.deployed is False
    assert live.realized_capture == 0.0
    # construction reads only; with no live fill the live file is never written, and the
    # dryrun snapshot is left wholly intact (no cross-write between the two modes).
    assert not (tmp_path / f"{SYMBOL}_live_state.json").exists()
    assert (tmp_path / f"{SYMBOL}_dryrun_state.json").exists()


def test_same_mode_restart_recovers_own_state(tmp_path):
    # the flip side of segregation: a dryrun restart MUST still find its own dryrun file
    # (segregation isolates ACROSS modes, not WITHIN a mode).
    a = make_engine(tmp_path, mode="dryrun")
    a.deployed = True
    a.anchor = 1.0
    a.realized_capture = 0.42
    a.slices = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    a._log_event(1_700_000_000.0, "sell", 0, 1.0005, 100.0)

    b = make_engine(tmp_path, mode="dryrun")                    # same mode, same dir/symbol
    assert b._resumed is True                                   # found its own dryrun snapshot
    assert b.realized_capture == 0.42
    assert b.slices == a.slices
