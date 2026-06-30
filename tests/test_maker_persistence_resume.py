"""Task 5 — persistence v2 + crash-resume reconciliation + fail-closed.

Covers (plan A5 / A9, Part B "Task 5"):
  * schema v=2 round-trip of the order/accounting fields + v1->v2 migration (inject
    defaults incl. sell_proceeds/qty_sold; migration idempotent on reload) + a
    resume type-check that rejects a wrong-typed order field (atomic fresh start);
  * persist-intent-before-place (link_id+gen on disk BEFORE the network call);
  * fail-CLOSED maker persistence: an OSError on a maker fill retries then
    cancels-all + halts; a maker fill persists through a SINGLE durable point;
  * restart reconciliation against the gate-fetched open-orders list (no refetch):
    re-link matched orders, cancel orphan ``sca-*``, refuse a foreign order, and
    recover a fill that completed while the engine was down (uncertain-retry never
    places a second live order).

ISOLATION (iron rule): every test writes ONLY under pytest ``tmp_path``. No network
(the ctor's _maybe_resume is the only disk path exercised; bootstrap/WS untouched).
A ``FakeOrderClient`` supplies exchange truth + records calls; real ``order_recon``
matcher/diff and real engine transition math run (real code over mocks).

Run: PYTHONPATH=src python3 -m pytest tests/test_maker_persistence_resume.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import sca.live.engine as engine  # noqa: E402
from sca.live.engine import PaperEngine, OperatorReconcileHalt  # noqa: E402
from sca.interest import DailyMinInterest  # noqa: E402
from sca.live.persistence import load_state, save_state  # noqa: E402

SYMBOL = "USD1USDT"

_ORDER_DEFAULTS = dict(order_id=None, order_link_id=None, order_px=None,
                       order_side=None, order_qty=None, filled_qty=0.0,
                       order_gen=0, reject_streak=0, sell_proceeds=0.0, qty_sold=0.0)


def _sl(state, qty=0.0, cash=0.0, sell_px=0.0, entry=None, **over):
    s = {"state": state, "qty": qty, "cash": cash, "sell_px": sell_px, "entry": entry}
    s.update(_ORDER_DEFAULTS)
    s.update(over)
    return s


def _base_sl(state, qty=0.0, cash=0.0, sell_px=0.0, entry=None):
    """A PRE-MAKER (v1) slice — exactly the 5 fields a v1 snapshot held, NO order
    fields. Used to build literal v1 state files for the migration tests."""
    return {"state": state, "qty": qty, "cash": cash, "sell_px": sell_px, "entry": entry}


def _bal(usd1=0.0, usdt=0.0):
    total = usd1 + usdt
    return {
        "account_type": "UNIFIED",
        "totals": {"equity_usd": total, "available_usd": total, "wallet_usd": total,
                   "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
        "coins": {
            "USD1": {"wallet": usd1, "locked": 0.0, "free": usd1, "usd": usd1, "borrow": 0.0},
            "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": usdt, "borrow": 0.0},
        },
    }


def _state(status_class="open", *, oid=None, link=None, side=None, filled=0.0,
           remaining=0.0, avg=None, price=None):
    return {"id": oid, "link_id": link, "side": side, "status": status_class,
            "status_class": status_class, "filled": filled, "remaining": remaining,
            "avg": avg, "price": price, "reject_reason": None, "raw": None}


def _open_order(oid, link, side, price, remaining, filled=0.0):
    return {"id": oid, "link_id": link, "clientOrderId": link, "side": side,
            "price": price, "qty": remaining, "remaining": remaining,
            "filled": filled, "filled_qty": filled}


class FakeOrderClient:
    """Records every call; returns canned market meta / balance / order state."""

    def __init__(self, *, balance=None, meta=None, open_orders=None,
                 place_result=None, state_results=None, max_order_usd=10_000.0):
        self._balance = balance if balance is not None else _bal()
        self._meta = meta or {"tick": 0.0001, "lot": 0.000001,
                              "min_qty": 0.0, "min_cost": 0.0}
        self._open_orders = open_orders or []
        self._place_result = place_result
        self._state_results = state_results or {}
        self.max_order_usd = max_order_usd
        self.calls = []

    def market_meta(self, symbol):
        self.calls.append(("market_meta", symbol))
        return self._meta

    def fetch_open(self, symbol):
        self.calls.append(("fetch_open", symbol))
        return list(self._open_orders)

    def balance(self):
        self.calls.append(("balance",))
        return self._balance

    def place_postonly(self, symbol, side, price, qty, link_id):
        self.calls.append(("place", side, price, qty, link_id))
        r = self._place_result
        if callable(r):
            r = r(link_id)
        if r is None:
            r = _state("open", oid=f"oid-{link_id}", link=link_id, side=side,
                       filled=0.0, remaining=qty, price=price)
        return r

    def amend(self, symbol, order_id, *, link_id=None, qty=None):
        self.calls.append(("amend", order_id, link_id, qty))
        return _state("open", oid=order_id, link=link_id, filled=0.0, remaining=qty)

    def cancel(self, symbol, order_id, *, link_id=None):
        self.calls.append(("cancel", order_id, link_id))
        return _state("open", oid=order_id, link=link_id)

    def fetch_order_state(self, symbol, order_id=None, *, link_id=None):
        self.calls.append(("fetch_state", order_id, link_id))
        key = link_id if link_id is not None else order_id
        seq = self._state_results.get(key)
        if seq is None:
            return _state("cancelled", oid=order_id, link=link_id, filled=0.0,
                          remaining=0.0)
        if isinstance(seq, list):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return seq

    def kinds(self):
        return [c[0] for c in self.calls]


def _mk_engine(tmp_path, *, persist=False, maker_enabled=True, slices=None,
               anchor=1.0, rungs=None, order_client=None):
    """A PaperEngine whose out_dir == tmp_path, maker live-fields set directly."""
    eng = PaperEngine(symbol=SYMBOL, mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.persist = persist
    eng.maker_enabled = maker_enabled
    eng._auto_cancel_orphans = False     # test default = strict legacy (shipped config ships True)
    eng._r1_ok = True
    eng._sleep = lambda *a, **k: None
    eng.anchor = anchor
    eng.deployed = True
    if rungs is not None:
        eng.rungs = list(rungs)
    if order_client is not None:
        eng.order_client = order_client
    if slices is not None:
        eng.slices = slices
    return eng


def _v1_doc(slices, **over):
    """A complete, well-formed v=1 snapshot (pre-maker)."""
    doc = {
        "v": 1, "symbol": SYMBOL, "mode": "paper",
        "start": 1_700_000_000.0, "deployed": True, "realized_capture": 0.25,
        "slices": slices,
        "interest": DailyMinInterest(0.10 / 365.0).to_dict(),
        "anchor": 1.0, "ema": 1.0, "last_1h_start": 0, "history": [],
    }
    doc.update(over)
    return doc


# === schema v2 + v1->v2 migration ===========================================

def test_order_fields_roundtrip_v2(tmp_path):
    """The order/accounting fields (incl. sell_proceeds/qty_sold) survive a
    save->resume round trip, and the snapshot is written at schema v=2."""
    a = _mk_engine(tmp_path)
    a.slices = [_sl("usdt", qty=0.0, cash=10.0, order_id="A0", order_link_id="sca-0-3",
                    order_px=1.0, order_side="buy", order_qty=10.0, filled_qty=2.0,
                    order_gen=3, reject_streak=1, sell_proceeds=5.0, qty_sold=4.0)]
    a.realized_capture = 0.42
    save_state(str(tmp_path), SYMBOL, a._state_dict(), "dryrun")

    on_disk = load_state(str(tmp_path), SYMBOL, "dryrun")
    assert on_disk["v"] == 2

    b = PaperEngine(symbol=SYMBOL, mode="paper", seconds=1,
                    csv_path=str(tmp_path / "out.csv"))
    assert b._resumed is True
    s = b.slices[0]
    assert s["order_id"] == "A0"
    assert s["order_link_id"] == "sca-0-3"
    assert s["order_gen"] == 3
    assert s["filled_qty"] == 2.0
    assert s["sell_proceeds"] == 5.0
    assert s["qty_sold"] == 4.0
    assert b.realized_capture == 0.42


def test_v1_state_migrates_with_default_order_fields(tmp_path):
    """A v=1 snapshot (no order fields) migrates: every slice gains the default order
    fields incl. sell_proceeds=0.0 / qty_sold=0.0 (C-P1#8). Migration is idempotent
    on reload (re-saving as v2 + resuming does not change the migrated slices)."""
    save_state(str(tmp_path), SYMBOL, _v1_doc(
        [_base_sl("usd1", qty=100.0, entry=1.0),
         _base_sl("usdt", cash=50.0, sell_px=1.0005)]), "dryrun")

    a = PaperEngine(symbol=SYMBOL, mode="paper", seconds=1,
                    csv_path=str(tmp_path / "out.csv"))
    assert a._resumed is True
    for s in a.slices:
        for k, default in _ORDER_DEFAULTS.items():
            assert s[k] == default, f"slice missing migrated default {k}"
    # base fields preserved through migration
    assert a.slices[0]["qty"] == 100.0 and a.slices[1]["cash"] == 50.0

    # IDEMPOTENT on reload: persist the migrated (now-v2) state, resume again, no drift.
    save_state(str(tmp_path), SYMBOL, a._state_dict(), "dryrun")
    assert load_state(str(tmp_path), SYMBOL, "dryrun")["v"] == 2
    b = PaperEngine(symbol=SYMBOL, mode="paper", seconds=1,
                    csv_path=str(tmp_path / "out.csv"))
    assert b._resumed is True
    assert b.slices == a.slices                 # migration did not double-apply / drift


def test_resume_typecheck_rejects_bad_order_fields_fresh_start(tmp_path, capsys):
    """A v=2 snapshot with a wrong-typed order field falls back to a FULLY fresh
    start (atomic — no half-restored hybrid), never a crash."""
    bad_slice = _sl("usd1", qty=10.0)
    bad_slice["filled_qty"] = "not-a-number"     # wrong type -> reject
    save_state(str(tmp_path), SYMBOL, _v1_doc([bad_slice], v=2), "dryrun")

    eng = PaperEngine(symbol=SYMBOL, mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng._resumed is False
    assert eng.slices == []                       # fresh defaults intact (atomic)
    assert eng.deployed is False
    assert "invalid" in capsys.readouterr().err.lower()


# === persist-intent-before-place + fail-closed single point =================

def test_persist_intent_before_place(tmp_path):
    """The order_link_id + bumped order_gen are durable on disk BEFORE the network
    place call fires — a crash in the place call never orphans an unknown order."""
    captured = {}

    def _capture_place(link_id):
        st = load_state(str(tmp_path), SYMBOL, "dryrun")
        captured["link_on_disk"] = st["slices"][0]["order_link_id"]
        captured["gen_on_disk"] = st["slices"][0]["order_gen"]
        return _state("open", oid="OID", link=link_id, side="sell", remaining=100.0)

    fake = FakeOrderClient(balance=_bal(usd1=100.0), place_result=_capture_place)
    eng = _mk_engine(tmp_path, persist=True, slices=[_sl("usd1", qty=100.0)],
                     rungs=[10.0])
    eng.reconcile_orders(0.0, client=fake)

    assert captured["link_on_disk"] == "sca-0-1"   # intent persisted before the call
    assert captured["gen_on_disk"] == 1
    assert eng.slices[0]["order_id"] == "OID"       # acked id persisted after


def test_maker_fill_persist_oserror_halts_cancel_all(tmp_path, monkeypatch):
    """On the maker path a persistence OSError is fail-CLOSED: bounded retries, then
    cancel ALL resting orders and HALT — never continue with an in-memory-only fill."""
    fake = FakeOrderClient()
    eng = _mk_engine(tmp_path, persist=True, order_client=fake,
                     slices=[_sl("usd1", qty=10.0, order_id="A0",
                                 order_link_id="sca-0-0", order_side="sell",
                                 order_px=1.0005, order_qty=10.0)])

    def _boom(*a, **k):
        raise OSError("ENOSPC: disk full")
    monkeypatch.setattr(engine, "save_state", _boom)

    with pytest.raises(OperatorReconcileHalt):
        eng._persist_durable_or_halt()
    # the resting order was cancelled on the way out (no dangling live order)
    assert ("cancel", "A0", "sca-0-0") in fake.calls
    assert eng._halted is True


def test_maker_fill_single_persist_point(tmp_path, monkeypatch):
    """A maker fill is durably snapshotted through a SINGLE point — _log_event is
    fail-closed (no fail-open save_state) on the maker path, so a fill is not
    double-written from both the fill path and the durable-persist path (F10)."""
    n = {"saves": 0}
    real_save = engine.save_state

    def _counting_save(out_dir, symbol, state, tag=""):
        n["saves"] += 1
        return real_save(out_dir, symbol, state, tag)
    monkeypatch.setattr(engine, "save_state", _counting_save)

    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("filled", oid="A0", link="sca-0-0", side="sell",
                          filled=10.0, remaining=0.0, avg=1.0005)})
    eng = _mk_engine(tmp_path, persist=True,
                     slices=[_sl("usd1", qty=10.0, order_id="A0",
                                 order_link_id="sca-0-0", order_side="sell",
                                 order_px=1.0005, order_qty=10.0)])
    eng.poll_fills(0.0, client=fake)

    assert eng.slices[0]["state"] == "usdt"       # the fill was booked + flipped
    assert n["saves"] == 1                         # exactly one durable snapshot


def test_maker_audit_append_failure_is_best_effort_not_fail_closed(tmp_path, monkeypatch):
    """On the maker path the AUDIT ledger append is best-effort: an OSError there is
    logged and swallowed (the durable SNAPSHOT is the authority and is fail-closed
    separately). A failed audit append must NOT halt nor lose the booked fill."""
    monkeypatch.setattr(engine, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("audit disk full")))
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("filled", oid="A0", link="sca-0-0", side="sell",
                          filled=10.0, remaining=0.0, avg=1.0005)})
    eng = _mk_engine(tmp_path, persist=True,
                     slices=[_sl("usd1", qty=10.0, order_id="A0",
                                 order_link_id="sca-0-0", order_side="sell",
                                 order_px=1.0005, order_qty=10.0)])
    eng.poll_fills(0.0, client=fake)              # must NOT raise

    assert eng.slices[0]["state"] == "usdt"        # fill booked despite audit failure
    assert eng._halted is False                    # audit append is NOT fail-closed
    assert load_state(str(tmp_path), SYMBOL, "dryrun")["slices"][0]["state"] == "usdt"  # snapshot durable


# === restart reconciliation (gate-fetched list, no refetch) =================

def test_restart_matches_open_orders_relinks(tmp_path):
    """resume_reconcile_orders re-links a slice to its still-open order by link_id,
    recovering the exchange order_id without placing anything new."""
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("open", oid="X1", link="sca-0-0", side="sell",
                          filled=0.0, remaining=10.0, avg=None)})
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_id=None,
                                           order_link_id="sca-0-0", order_side="sell",
                                           order_px=1.0005, order_qty=10.0)])
    open_orders = [_open_order("X1", "sca-0-0", "sell", 1.0005, 10.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)

    assert eng.slices[0]["order_id"] == "X1"      # re-linked from exchange truth
    assert "place" not in fake.kinds()
    assert "cancel" not in fake.kinds()


def test_restart_cancels_orphan_sca_order(tmp_path):
    """An ``sca-*`` open order that maps to NO current slice (stale/orphan) is
    cancelled (clean, no fill) and logged — not forced onto a guessed slice."""
    fake = FakeOrderClient()      # default fetch_order_state -> cancelled, filled 0
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=5.0)])    # no order on the slice
    open_orders = [_open_order("Y9", "sca-9-9", "buy", 0.9999, 5.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)

    assert ("cancel", "Y9", "sca-9-9") in fake.calls
    # no slice was touched (the orphan has no slice identity)
    assert eng.slices[0]["order_id"] is None


def test_restart_orphan_sca_order_with_executed_qty_halts(tmp_path):
    """An orphan ``sca-*`` order that turns out to have EXECUTED qty cannot be safely
    attributed to a slice -> HALT for operator reconciliation (R2-P1), never guessed."""
    fake = FakeOrderClient(state_results={
        "sca-9-9": _state("filled", oid="Y9", link="sca-9-9", side="buy",
                          filled=5.0, remaining=0.0, avg=0.9999)})
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=5.0)])
    open_orders = [_open_order("Y9", "sca-9-9", "buy", 0.9999, 0.0, filled=5.0)]
    with pytest.raises(OperatorReconcileHalt):
        eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)
    assert ("cancel", "Y9", "sca-9-9") in fake.calls   # cancelled to terminal first


def test_restart_postonly_rejected_clears_slice_order(tmp_path):
    """A slice whose resting order is found PostOnly-rejected on resume clears its
    order identity (and arms the reject cooldown), not booking a phantom fill."""
    fake = FakeOrderClient(state_results={
        "sca-0-1": _state("postonly_rejected", oid="P1", link="sca-0-1", side="sell",
                          filled=0.0, remaining=10.0)})
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_id="P1",
                                           order_link_id="sca-0-1", order_side="sell",
                                           order_px=1.0005, order_qty=10.0)])
    eng.resume_reconcile_orders([], client=fake, now=0.0)

    s = eng.slices[0]
    assert s["order_id"] is None and s["order_link_id"] is None   # order identity cleared
    assert s["state"] == "usd1"                                    # no phantom transition
    assert s["reject_streak"] == 1


def test_resume_noop_when_maker_disabled(tmp_path):
    """resume_reconcile_orders is a no-op on the paper path (maker_enabled False) —
    it never touches the client, mirroring reconcile_orders/poll_fills."""
    fake = FakeOrderClient()
    eng = _mk_engine(tmp_path, maker_enabled=False,
                     slices=[_sl("usd1", qty=10.0, order_link_id="sca-0-0")])
    eng.resume_reconcile_orders([_open_order("X", "sca-0-0", "sell", 1.0, 10.0)],
                                client=fake, now=0.0)
    assert fake.calls == []


def test_restart_refuses_foreign_order_in_dedicated_account(tmp_path):
    """A non-``sca`` (foreign) open order in the dedicated account is a hard refusal
    (SystemExit) — the subaccount must be dedicated; we never trade around it.
    (Strict default: auto_cancel_orphans OFF.)"""
    fake = FakeOrderClient()
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=5.0)])
    open_orders = [_open_order("F1", "manual-deadbeef", "buy", 0.9999, 5.0)]
    with pytest.raises(SystemExit):
        eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)


def test_restart_ignores_foreign_order_when_auto_cancel_on(tmp_path):
    """auto_cancel_orphans ON (boss 2026-06-30): a foreign (non-sca) order is IGNORED — NO
    SystemExit, left untouched (NOT cancelled), bot proceeds and trades around it."""
    fake = FakeOrderClient()
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=5.0)])
    eng._auto_cancel_orphans = True
    open_orders = [_open_order("F1", "manual-deadbeef", "buy", 0.9999, 5.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)    # must NOT raise
    assert "cancel" not in fake.kinds()                              # foreign left untouched


def test_restart_cancels_own_orphan_when_auto_cancel_on(tmp_path):
    """auto_cancel_orphans ON: our OWN sca-* orphan is still auto-cancelled (clean, no fill);
    bot proceeds. This is the boss's 'just clean up my own mess and start' case."""
    fake = FakeOrderClient()      # default fetch_order_state -> cancelled, filled 0
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=5.0)])
    eng._auto_cancel_orphans = True
    open_orders = [_open_order("Y9", "sca-9-9", "buy", 0.9999, 5.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)
    assert ("cancel", "Y9", "sca-9-9") in fake.calls                 # own orphan cancelled


def test_crash_after_place_before_id_persist_recovers_fill_while_down(tmp_path):
    """A slice with order_link_id but order_id=None (crash after intent-persist,
    before id-persist) whose order FILLED while the engine was down is recovered:
    fetch_order_state(link_id) books the fill and flips the slice (F14)."""
    fake = FakeOrderClient(state_results={
        "sca-0-1": _state("filled", oid="Z1", link="sca-0-1", side="sell",
                          filled=10.0, remaining=0.0, avg=1.0005)})
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_id=None,
                                           order_link_id="sca-0-1", order_side="sell",
                                           order_px=1.0005, order_qty=10.0)])
    eng.resume_reconcile_orders([], client=fake, now=0.0)    # empty open list (terminal)

    s = eng.slices[0]
    assert s["state"] == "usdt"                   # the down-time fill was recovered
    assert s["qty"] == 0.0
    assert s["cash"] == pytest.approx(10.0 * 1.0005)
    assert s["order_id"] is None and s["order_link_id"] is None
    # it was learned via the link_id (id was never persisted)
    assert ("fetch_state", None, "sca-0-1") in fake.calls


def test_uncertain_retry_fetches_state_never_two_live_orders(tmp_path):
    """A slice with order_link_id but order_id=None whose order is STILL open is
    re-linked (id recovered) — resume NEVER places a second live order for it."""
    fake = FakeOrderClient(state_results={
        "sca-0-2": _state("open", oid="W2", link="sca-0-2", side="buy",
                          filled=0.0, remaining=10.0, avg=None)})
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=10.0, order_id=None,
                                           order_link_id="sca-0-2", order_side="buy",
                                           order_px=1.0000, order_qty=10.0)])
    open_orders = [_open_order("W2", "sca-0-2", "buy", 1.0000, 10.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)

    assert eng.slices[0]["order_id"] == "W2"      # recovered, not duplicated
    assert "place" not in fake.kinds()            # never a second live order


def test_resume_uses_passed_open_orders_no_refetch(tmp_path):
    """resume_reconcile_orders consumes the gate-stored open-orders list and never
    refetches it from the exchange (F23/C-P0#5)."""
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("open", oid="X1", link="sca-0-0", side="sell",
                          filled=0.0, remaining=10.0, avg=None)})
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_id="X1",
                                           order_link_id="sca-0-0", order_side="sell",
                                           order_px=1.0005, order_qty=10.0)])
    open_orders = [_open_order("X1", "sca-0-0", "sell", 1.0005, 10.0)]
    eng.resume_reconcile_orders(open_orders, client=fake, now=0.0)

    assert "fetch_open" not in fake.kinds()       # used the passed list, no refetch
