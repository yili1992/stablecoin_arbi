"""D16 — docker-deploy safety: operator-reconcile halt is DURABLE across restarts
and REFUSES to auto-continue (production gate: only a human reset resumes).

The boss deploys the live engine via docker with auto-restart. The single remaining
halt (``_halt_operator_reconcile`` — unattributable fill / cancel-never-terminal /
reject-streak / durable-persist failure) must therefore:

  1. PERSIST the halt before raising, so it survives an immediate process exit;
  2. on restart, a resumed-halted engine REFUSES the maker path and exits CLEANLY
     (code 0) so docker ``restart: on-failure`` does NOT loop it back to life;
  3. clear only via an explicit operator action — delete the (mode-tagged) state
     file or set ``LIVE_CLEAR_HALT=yes`` (keeps the position, clears the halt);
  4. when the halt (or any deliberate ``_refuse``) propagates out of ``run()`` it
     becomes a CLEAN exit (0); a real crash still propagates non-zero so on-failure
     recovers transient faults.

This is the D13 mechanism re-added in a MINIMAL form, scoped to the operator-reconcile
halt ONLY — the D14-removed PnL max-loss kill-switch stays removed.

ISOLATION (iron rule): every test writes ONLY under pytest ``tmp_path``; no network,
no real keys (a live-mode engine builds NO client at construction). Heavy ``run()``
internals are monkeypatched so the wiring is exercised without I/O.

Run: PYTHONPATH=src python3 -m pytest tests/test_operator_halt_durable.py -q
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sca.live.engine as engine  # noqa: E402
from sca.live.engine import PaperEngine, OperatorReconcileHalt  # noqa: E402
from sca.interest import DailyMinInterest  # noqa: E402
from sca.live.persistence import load_state, save_state  # noqa: E402

SYMBOL = "USD1USDT"


def _live_engine(tmp_path, *, persist=True):
    """A live-mode PaperEngine (armed; tag == 'live') whose out_dir == tmp_path.

    Construction builds NO order client and needs NO keys (that is lazy in run()),
    so this is a pure, offline engine whose only disk path is _maybe_resume.
    """
    eng = PaperEngine(symbol=SYMBOL, mode="live", seconds=0,
                      csv_path=str(tmp_path / "out.csv"))
    eng.persist = persist
    return eng


def _v2_doc(**over):
    """A complete, well-formed PRE-D16 v=2 snapshot (no `halted`/`halt_reason`)."""
    doc = {
        "v": 2, "symbol": SYMBOL, "mode": "live",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 0.0,
        "slices": [], "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    }
    doc.update(over)
    return doc


# ===========================================================================
# Part 1 — the operator-reconcile halt is PERSISTED before it is raised
# ===========================================================================

def test_operator_halt_persists_halted_to_disk(tmp_path):
    eng = _live_engine(tmp_path, persist=True)
    with pytest.raises(OperatorReconcileHalt):
        eng._halt_operator_reconcile("unattributable fill on order X")
    assert eng._halted is True                       # in-memory contract unchanged

    # DURABLE: the halt is on disk even though the process would exit immediately.
    st = load_state(str(tmp_path), SYMBOL, tag="live")
    assert st is not None
    assert st["halted"] is True
    assert st["halt_reason"] == "unattributable fill on order X"


def test_halt_persisted_and_restored_on_resume(tmp_path):
    a = _live_engine(tmp_path, persist=True)
    with pytest.raises(OperatorReconcileHalt):
        a._halt_operator_reconcile("cancel never reached terminal: sca-2-1")

    # a FRESH engine over the same out_dir + live tag resumes the halt
    b = _live_engine(tmp_path)
    assert b._resumed is True
    assert b._halted is True
    assert b._halt_reason == "cancel never reached terminal: sca-2-1"


def test_old_v2_state_without_halted_resumes_not_halted(tmp_path):
    # A pre-D16 v2 snapshot has NO `halted` key: resume must default _halted False
    # (additive / backward compatible) and still RESUME (never a spurious fresh start).
    save_state(str(tmp_path), SYMBOL, _v2_doc(realized_capture=0.5), tag="live")
    eng = _live_engine(tmp_path)
    assert eng._resumed is True                      # additive field absent != fresh start
    assert eng._halted is False
    assert eng._halt_reason is None
    assert eng.realized_capture == 0.5               # the rest of the snapshot resumed


def test_halt_persist_is_best_effort_still_raises_on_oserror(tmp_path, monkeypatch):
    # The fail-closed RAISE must never be blocked by a disk error while persisting
    # the halt: an OSError on save_state is swallowed, the halt still propagates.
    eng = _live_engine(tmp_path, persist=True)
    monkeypatch.setattr(engine, "save_state",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC")))
    with pytest.raises(OperatorReconcileHalt):
        eng._halt_operator_reconcile("reject streak 5 >= 5")
    assert eng._halted is True


def test_halt_not_persisted_when_persist_off(tmp_path):
    # persist=False -> no state file is written by the halt (byte-identical to the
    # in-memory-only behaviour), but the halt is still raised + flagged in memory.
    eng = _live_engine(tmp_path, persist=False)
    with pytest.raises(OperatorReconcileHalt):
        eng._halt_operator_reconcile("durable persist failed")
    assert eng._halted is True
    assert load_state(str(tmp_path), SYMBOL, tag="live") is None


# ===========================================================================
# Part 2 — a resumed-halted engine REFUSES the maker path + exits cleanly (0);
#          clears only via an explicit operator action
# ===========================================================================

def _resume_halted(tmp_path, *, slices=None, reason="halted reason"):
    """Persist a halted v2 snapshot, then return a FRESH engine resumed from it."""
    save_state(str(tmp_path), SYMBOL,
               _v2_doc(halted=True, halt_reason=reason,
                       slices=slices if slices is not None else []),
               tag="live")
    eng = _live_engine(tmp_path)
    assert eng._resumed is True and eng._halted is True   # precondition for the test
    return eng


def test_resume_guard_refuses_with_clean_exit_zero(tmp_path):
    eng = _resume_halted(tmp_path)
    with pytest.raises(SystemExit) as ei:
        eng._enforce_resume_halt_gate()
    assert ei.value.code == 0                        # clean exit -> docker on-failure won't loop it
    assert eng.order_client is None                  # never entered the maker path


def test_resume_guard_noop_when_not_halted(tmp_path):
    # the common case: a healthy resume (or a fresh start) must pass the gate untouched.
    eng = _live_engine(tmp_path)
    assert eng._halted is False
    eng._enforce_resume_halt_gate()                  # must NOT raise
    assert eng._halted is False


def test_clear_halt_via_env_keeps_position(tmp_path, monkeypatch):
    pos = [{"state": "usd1", "qty": 100.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0}]
    eng = _resume_halted(tmp_path, slices=pos)
    monkeypatch.setenv("LIVE_CLEAR_HALT", "yes")

    eng._enforce_resume_halt_gate()                  # explicit clear -> must NOT raise
    assert eng._halted is False                      # halt cleared
    assert eng._halt_reason is None
    assert eng.slices == pos                         # position is KEPT (clear != wipe)

    # the cleared halt is re-persisted, so a later restart WITHOUT the env stays cleared
    st = load_state(str(tmp_path), SYMBOL, tag="live")
    assert st["halted"] is False


def test_clear_halt_via_delete_state_file(tmp_path):
    a = _live_engine(tmp_path, persist=True)
    with pytest.raises(OperatorReconcileHalt):
        a._halt_operator_reconcile("some halt")
    state_file = tmp_path / f"{SYMBOL}_live_state.json"
    assert state_file.exists()

    state_file.unlink()                              # operator deletes the (mode-tagged) state

    b = _live_engine(tmp_path)
    assert b._resumed is False                       # no file -> fresh start
    assert b._halted is False
    b._enforce_resume_halt_gate()                    # not halted -> proceeds, no raise


def test_clear_halt_env_must_be_exactly_yes(tmp_path, monkeypatch):
    # a non-"yes" value must NOT clear the halt (avoid an accidental/typo'd clear).
    eng = _resume_halted(tmp_path)
    monkeypatch.setenv("LIVE_CLEAR_HALT", "1")
    with pytest.raises(SystemExit) as ei:
        eng._enforce_resume_halt_gate()
    assert ei.value.code == 0
    assert eng._halted is True                        # still halted (not cleared by "1")


# --- run()-level: the gate runs BEFORE the maker path is entered -----------

def _stub_run_min(eng, monkeypatch, *, resume_fn=None):
    """Stub the heavy run() internals so the startup WIRING runs without I/O."""
    monkeypatch.setattr(eng, "_compute_maker_enabled", lambda: True)
    monkeypatch.setattr(eng, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(eng, "_maker_startup_banner", lambda: None)
    monkeypatch.setattr(eng, "_maybe_gate", lambda: None)
    monkeypatch.setattr(eng, "bootstrap", lambda: None)
    monkeypatch.setattr(eng, "flush_markout", lambda *a, **k: None)
    monkeypatch.setattr(eng, "accrue", lambda *a, **k: None)
    monkeypatch.setattr(eng, "print_summary", lambda *a, **k: None)
    monkeypatch.setattr(eng, "write_status", lambda *a, **k: str(eng.out_dir))
    if resume_fn is not None:
        monkeypatch.setattr(eng, "resume_reconcile_orders", resume_fn)
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    eng.csv_path = None
    eng.start = engine.time.time() - 10_000.0        # t_end already in the past
    eng.seconds = 1


def test_run_refuses_resumed_halt_without_entering_maker(tmp_path, monkeypatch):
    import asyncio
    eng = _resume_halted(tmp_path)
    built, resumed = [], []
    monkeypatch.setattr(eng, "_build_order_client", lambda: built.append(1))
    _stub_run_min(eng, monkeypatch, resume_fn=lambda *a, **k: resumed.append(1))

    with pytest.raises(SystemExit) as ei:
        asyncio.run(eng.run())
    assert ei.value.code == 0                        # clean exit on a resumed halt
    assert built == []                               # never built the order client
    assert resumed == []                             # never ran resume reconciliation


# ===========================================================================
# Part 3 — a deliberate stop (operator halt / _refuse) exits CLEANLY (0); a real
#          crash still propagates non-zero so docker on-failure recovers it
# ===========================================================================

def test_refuse_exits_clean(tmp_path):
    # a deliberate refusal (R1 reconcile / liability guard) is an INTENTIONAL stop:
    # exit 0 so docker restart:on-failure does not loop on it.
    eng = _live_engine(tmp_path)
    with pytest.raises(SystemExit) as ei:
        eng._refuse("R1 reconciliation refused: balance mismatch")
    assert ei.value.code == 0


def test_operator_halt_from_run_exits_clean(tmp_path, monkeypatch):
    import asyncio
    eng = _live_engine(tmp_path)                     # not halted at construction
    cancels = []
    monkeypatch.setattr(eng, "_build_order_client", lambda: None)
    monkeypatch.setattr(eng, "_cancel_all_resting", lambda *a, **k: cancels.append(1))

    def boom_resume(*a, **k):
        raise OperatorReconcileHalt("fill on orphan order during resume")
    _stub_run_min(eng, monkeypatch, resume_fn=boom_resume)

    with pytest.raises(SystemExit) as ei:
        asyncio.run(eng.run())
    assert ei.value.code == 0                        # deliberate halt -> clean exit (no on-failure loop)
    assert cancels == [1]                            # resting orders STILL cancelled on the way out


def test_real_crash_from_run_propagates_nonzero(tmp_path, monkeypatch):
    # a genuine bug (NOT an OperatorReconcileHalt) must NOT be masked as a clean exit:
    # it propagates so the process exits non-zero and docker on-failure restarts it
    # (transient-fault recovery). This is the flip side of the clean-halt exit.
    import asyncio
    eng = _live_engine(tmp_path)
    monkeypatch.setattr(eng, "_build_order_client", lambda: None)
    monkeypatch.setattr(eng, "_cancel_all_resting", lambda *a, **k: None)

    def boom_resume(*a, **k):
        raise RuntimeError("unexpected bug")
    _stub_run_min(eng, monkeypatch, resume_fn=boom_resume)

    with pytest.raises(RuntimeError):                # NOT SystemExit -> non-zero exit
        asyncio.run(eng.run())
