"""Task 4 — engine maker fill driver: real-fill slice transitions, declarative
reconcile-apply, REST poll, cancel-to-terminal, cooldown/halt, A4a leg-valuation.

ISOLATION: no network, no disk. A real ``PaperEngine`` is built in paper mode
(out_dir -> tmp_path), the maker live fields (``maker_enabled``/``_r1_ok``/
``anchor``/``slices``) are set DIRECTLY, ``persist`` is OFF, ``_sleep`` is a no-op,
and a ``FakeOrderClient`` supplies exchange truth + records calls. Real code over
mocks: the pure ``order_recon`` matcher/diff and the real engine transition math run.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_maker_fills.py -q
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import (  # noqa: E402
    CANCEL_POLL_BACKOFFS, OperatorReconcileHalt, PaperEngine,
)
from sca.live.order_recon import Live  # noqa: E402


# --- fixtures / fakes -------------------------------------------------------
_ORDER_DEFAULTS = dict(order_id=None, order_link_id=None, order_px=None,
                       order_side=None, order_qty=None, filled_qty=0.0,
                       order_gen=0, reject_streak=0, sell_proceeds=0.0, qty_sold=0.0)


def _sl(state, qty=0.0, cash=0.0, sell_px=0.0, entry=None, **over):
    s = {"state": state, "qty": qty, "cash": cash, "sell_px": sell_px, "entry": entry}
    s.update(_ORDER_DEFAULTS)
    s.update(over)
    return s


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
                 place_result=None, state_results=None, max_order_usd=2000.0):
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


class FakeNotifier:
    def __init__(self):
        self.orders = []
        self.daily = []
        self.fills = []

    def order_placed(self, **kwargs):
        self.orders.append(kwargs)

    def daily_pnl(self, **kwargs):
        self.daily.append(kwargs)

    def fill_executed(self, **kwargs):
        self.fills.append(kwargs)


def _mk_engine(tmp_path, *, anchor=1.0, slices=None, bid=None, ask=None,
               rungs=None, fracs=None):
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.persist = False
    eng.maker_enabled = True
    eng._auto_cancel_orphans = False     # strict default (shipped config ships True; these pin strict)
    eng._r1_ok = True
    eng._sleep = lambda *a, **k: None
    eng.anchor = anchor
    eng.bid = bid
    eng.ask = ask
    eng.deployed = True
    if rungs is not None:
        eng.rungs = list(rungs)
    if fracs is not None:
        eng.fracs = list(fracs)
        eng.n = len(eng.fracs)
    if slices is not None:
        eng.slices = slices
    return eng


# === transitions: full / partial fills ======================================

def test_full_sell_fill_flips_usd1_to_usdt_clears_order(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_id="A0",
                                           order_link_id="sca-0-0", order_side="sell",
                                           order_px=1.0005, order_qty=10.0)])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("filled", oid="A0", link="sca-0-0", side="sell",
                          filled=10.0, remaining=0.0, avg=1.0005)})
    eng.poll_fills(0.0, client=fake)
    s = eng.slices[0]
    assert s["state"] == "usdt"
    assert s["qty"] == 0.0
    assert s["cash"] == pytest.approx(10.0 * 1.0005)
    assert s["order_id"] is None and s["order_link_id"] is None
    assert s["filled_qty"] == 0.0


def test_full_buy_fill_books_realized_capture(tmp_path):
    # slice already sold 10@1.0005 -> proceeds in cash; resting BUY at 1.0000
    s = _sl("usdt", qty=0.0, cash=10.0 * 1.0005, sell_px=1.0005,
            sell_proceeds=10.0 * 1.0005, qty_sold=10.0,
            order_id="B0", order_link_id="sca-0-1", order_side="buy",
            order_px=1.0000, order_qty=10.0 * 1.0005 / 1.0000)
    eng = _mk_engine(tmp_path, slices=[s])
    nq = 10.0 * 1.0005 / 1.0000
    fake = FakeOrderClient(state_results={
        "sca-0-1": _state("filled", oid="B0", link="sca-0-1", side="buy",
                          filled=nq, remaining=0.0, avg=1.0000)})
    eng.poll_fills(0.0, client=fake)
    assert eng.realized_capture == pytest.approx((1.0005 - 1.0000) * nq)
    assert eng.slices[0]["state"] == "usd1"
    assert eng.slices[0]["cash"] == 0.0


def test_full_buy_keeps_actual_fill_avg_as_entry_cost(tmp_path):
    # The floor uses entry cost. A live maker fill can report an actual avg that differs
    # from the posted limit, so _flip_state must not overwrite _apply_exec's avg entry.
    s = _sl("usdt", qty=0.0, cash=10.0 * 1.0005, sell_px=1.0005,
            sell_proceeds=10.0 * 1.0005, qty_sold=10.0,
            order_id="B0", order_link_id="sca-0-1", order_side="buy",
            order_px=1.0000, order_qty=10.0 * 1.0005 / 1.0000)
    eng = _mk_engine(tmp_path, slices=[s])
    nq = 10.0 * 1.0005 / 0.9999
    fake = FakeOrderClient(state_results={
        "sca-0-1": _state("filled", oid="B0", link="sca-0-1", side="buy",
                          filled=nq, remaining=0.0, avg=0.9999)})

    eng.poll_fills(0.0, client=fake)

    assert eng.slices[0]["state"] == "usd1"
    assert eng.slices[0]["entry"] == pytest.approx(0.9999)


def test_realized_uses_persistent_sell_proceeds(tmp_path):
    # avg_sell = sell_proceeds / qty_sold, booked BEFORE cash reduced
    s = _sl("usdt", cash=20.02, sell_proceeds=20.02, qty_sold=20.0,
            order_id="B0", order_link_id="sca-0-1", order_side="buy",
            order_px=1.0000, order_qty=20.02)
    eng = _mk_engine(tmp_path, slices=[s])
    avg_sell = 20.02 / 20.0  # = 1.001
    fake = FakeOrderClient(state_results={
        "sca-0-1": _state("filled", link="sca-0-1", side="buy", filled=20.02,
                          remaining=0.0, avg=1.0000)})
    eng.poll_fills(0.0, client=fake)
    assert eng.realized_capture == pytest.approx((avg_sell - 1.0000) * 20.02)


def test_realized_capture_exact_under_multi_price_partial_sells(tmp_path):
    # sell 5@1.0010 then 5@1.0006 -> blended avg, then buy back the proceeds
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0006, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    eng._apply_exec(0, "sell", 5.0, 1.0010, 0.0)
    eng._apply_exec(0, "sell", 5.0, 1.0006, 0.0)
    proceeds = 5 * 1.0010 + 5 * 1.0006
    assert s["sell_proceeds"] == pytest.approx(proceeds)
    assert s["qty_sold"] == pytest.approx(10.0)
    avg_sell = proceeds / 10.0
    # now a full rebuy of the proceeds at B
    B = 1.0000
    nq = proceeds / B
    eng._apply_exec(0, "buy", nq, B, 0.0)
    assert eng.realized_capture == pytest.approx((avg_sell - B) * nq)


def test_partial_sell_updates_qty_cash_proceeds_keeps_state_usd1(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("open", oid="A0", link="sca-0-0", side="sell",
                          filled=4.0, remaining=6.0, avg=1.0005)})
    eng.poll_fills(0.0, client=fake)
    assert s["state"] == "usd1"            # NOT flipped
    assert s["qty"] == pytest.approx(6.0)
    assert s["cash"] == pytest.approx(4.0 * 1.0005)
    assert s["sell_proceeds"] == pytest.approx(4.0 * 1.0005)
    assert s["qty_sold"] == pytest.approx(4.0)
    assert s["filled_qty"] == pytest.approx(4.0)
    assert s["order_id"] == "A0"           # order still resting


def test_partial_then_full_completes_transition(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": [_state("open", oid="A0", link="sca-0-0", side="sell",
                           filled=4.0, remaining=6.0, avg=1.0005),
                    _state("filled", oid="A0", link="sca-0-0", side="sell",
                           filled=10.0, remaining=0.0, avg=1.0005)]})
    eng.poll_fills(0.0, client=fake)       # partial
    assert s["state"] == "usd1" and s["qty"] == pytest.approx(6.0)
    eng.poll_fills(0.0, client=fake)       # completes
    assert s["state"] == "usdt"
    assert s["qty"] == 0.0
    assert s["cash"] == pytest.approx(10.0 * 1.0005)
    assert s["order_id"] is None


def test_poll_fills_notifies_once_per_execution_delta(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    eng.strategy_name = "USD1 EMA Slice Ladder"
    notifier = FakeNotifier()
    eng.notifier = notifier
    fake = FakeOrderClient(state_results={
        "sca-0-0": [
            _state("open", oid="A0", link="sca-0-0", side="sell",
                   filled=4.0, remaining=6.0, avg=1.0005),
            _state("open", oid="A0", link="sca-0-0", side="sell",
                   filled=4.0, remaining=6.0, avg=1.0005),
            _state("filled", oid="A0", link="sca-0-0", side="sell",
                   filled=10.0, remaining=0.0, avg=1.0005),
        ],
    })

    eng.poll_fills(100.0, client=fake)
    eng.poll_fills(160.0, client=fake)
    eng.poll_fills(220.0, client=fake)

    assert len(notifier.fills) == 2
    first, second = notifier.fills
    assert first["strategy_name"] == "USD1 EMA Slice Ladder"
    assert first["mode"] == eng.mode
    assert first["symbol"] == "USD1USDT"
    assert first["side"] == "sell"
    assert first["slice_idx"] == 0
    assert first["price"] == pytest.approx(1.0005)
    assert first["qty"] == pytest.approx(4.0)
    assert first["filled"] == pytest.approx(4.0)
    assert first["total"] == pytest.approx(10.0)
    assert first["status_class"] == "open"
    assert first["link_id"] == "sca-0-0"
    assert first["order_id"] == "A0"
    assert second["qty"] == pytest.approx(6.0)
    assert second["filled"] == pytest.approx(10.0)
    assert second["status_class"] == "filled"


def test_fill_notification_failure_does_not_block_fill_booking(tmp_path):
    class RaisingNotifier:
        def fill_executed(self, **_kwargs):
            raise RuntimeError("webhook down")

    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    eng.notifier = RaisingNotifier()
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("open", oid="A0", link="sca-0-0", side="sell",
                          filled=4.0, remaining=6.0, avg=1.0005),
    })

    eng.poll_fills(0.0, client=fake)

    assert s["qty"] == pytest.approx(6.0)
    assert s["cash"] == pytest.approx(4.0 * 1.0005)
    assert s["filled_qty"] == pytest.approx(4.0)


# === cancel-to-terminal: never drop a fill, poll through PendingCancel =======

def test_cancel_books_fill_before_clear_cancel_first(tmp_path):
    # a fill lands during the cancel -> terminal re-poll books it before clearing
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("filled", oid="A0", link="sca-0-0", side="sell",
                          filled=10.0, remaining=0.0, avg=1.0005)})
    st = eng._cancel_to_terminal("A0", "sca-0-0", 0.0, slice_idx=0, client=fake)
    assert fake.kinds()[0] == "cancel"     # cancel FIRST
    assert st["changed"] is True
    assert s["state"] == "usdt"            # the fill was booked
    assert s["cash"] == pytest.approx(10.0 * 1.0005)
    assert s["order_id"] is None           # cleared only after terminal


def test_cancel_polls_through_pending_cancel_until_terminal(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    sleeps = []
    eng._sleep = lambda b: sleeps.append(b)
    fake = FakeOrderClient(state_results={
        "sca-0-0": [_state("open", oid="A0", link="sca-0-0", side="sell",
                           filled=0.0, remaining=10.0),      # PendingCancel
                    _state("open", oid="A0", link="sca-0-0", side="sell",
                           filled=0.0, remaining=10.0),      # still open
                    _state("cancelled", oid="A0", link="sca-0-0", side="sell",
                           filled=0.0, remaining=0.0)]})
    st = eng._cancel_to_terminal("A0", "sca-0-0", 0.0, slice_idx=0, client=fake)
    assert st["status_class"] == "cancelled"
    # 3 fetch_state polls (open, open, terminal) and 2 sleeps between them
    assert fake.kinds().count("fetch_state") == 3
    assert len(sleeps) == 2
    assert s["order_id"] is None           # cleared after terminal


def test_cancel_exhausts_polls_then_halts(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": [_state("open", oid="A0", link="sca-0-0", side="sell",
                           filled=0.0, remaining=10.0)]})   # ALWAYS open
    with pytest.raises(OperatorReconcileHalt):
        eng._cancel_to_terminal("A0", "sca-0-0", 0.0, slice_idx=0, client=fake)
    assert eng._halted is True
    assert s["order_id"] == "A0"           # NEVER cleared on an unknown outcome


def test_cancel_to_terminal_not_found_keeps_polling_then_halts(tmp_path):
    # P0-1: a transient not_found (order absent from BOTH fetch_open_orders and the
    # canceled/closed history due to eventual consistency) is NOT terminal. The poll loop
    # must keep polling (not_found != open, yet not in the terminal set) and, on bounded-
    # poll exhaustion, HALT fail-closed — NEVER treat not_found as terminal and clear
    # the slice order state.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": [_state("not_found", oid="A0", link="sca-0-0", filled=0.0,
                           remaining=0.0)]})   # ALWAYS not_found (never terminal)
    with pytest.raises(OperatorReconcileHalt):
        eng._cancel_to_terminal("A0", "sca-0-0", 0.0, slice_idx=0, client=fake)
    assert eng._halted is True
    # the loop must have polled the full bounded sequence (not bailed on the 1st not_found)
    assert fake.kinds().count("fetch_state") == len(CANCEL_POLL_BACKOFFS)
    assert s["order_id"] == "A0"           # NEVER cleared on an unknown outcome
    assert s["order_link_id"] == "sca-0-0"


# === unattributed orders -> halt only on an executed fill ====================

def test_unattributed_order_with_fill_halts_operator_reconcile(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)],
                     rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[_open_order("X", "foreign-1", "buy", 0.9999, 0.0, filled=5.0)],
        state_results={"foreign-1": _state("filled", oid="X", link="foreign-1",
                                            side="buy", filled=5.0, remaining=0.0,
                                            avg=0.9999)})
    with pytest.raises(OperatorReconcileHalt):
        eng.reconcile_orders(0.0, client=fake)
    assert eng._halted is True


def test_unattributed_clean_cancel_touches_no_slice(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)],
                     rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=0.0),   # zero free -> no desired place to muddy the test
        open_orders=[_open_order("X", "foreign-2", "buy", 0.9999, 8.0, filled=0.0)],
        state_results={"foreign-2": _state("cancelled", oid="X", link="foreign-2",
                                            filled=0.0, remaining=0.0)})
    eng.reconcile_orders(0.0, client=fake)
    assert eng._halted is False
    assert "cancel" in fake.kinds()
    assert eng.slices[0]["state"] == "usd1"   # slice untouched


# === stale-place abort across a cancel-induced state flip ====================

def test_stale_place_aborted_after_cancel_flips_state(tmp_path):
    # resting sell at 1.0005; anchor stepped -> diff wants cancel+place. The cancel's
    # terminal fetch shows the order FILLED -> state flips -> paired place is dropped.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0010, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[_open_order("A0", "sca-0-0", "sell", 1.0005, 10.0)],
        state_results={"sca-0-0": _state("filled", oid="A0", link="sca-0-0",
                                          side="sell", filled=10.0, remaining=0.0,
                                          avg=1.0005)})
    eng.reconcile_orders(0.0, client=fake)
    assert "cancel" in fake.kinds()
    assert "place" not in fake.kinds()        # stale place dropped
    assert s["state"] == "usdt"               # the cancel booked the fill + flipped


# === vanished-order terminal-sync BEFORE place (R3-P0 second clause) =========

def test_reconcile_terminal_syncs_vanished_order_before_place(tmp_path):
    # R3-P0: a slice's persisted resting order has VANISHED from fetch_open (it filled
    # in the sub-tick window between poll_fills and this reconcile). BEFORE computing the
    # desired set, reconcile must terminal-sync that slice (fetch_order_state ->
    # _apply_exec_delta: book the fill, flip state, clear), and must NOT overwrite the
    # link / place a NEW same-side order while the prior order's cumExecQty is unbooked.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[],          # order is GONE from the open book
        state_results={"sca-0-0": _state("filled", oid="A0", link="sca-0-0",
                                          side="sell", filled=10.0, remaining=0.0,
                                          avg=1.0005)})
    eng.reconcile_orders(0.0, client=fake)
    assert s["state"] == "usdt"                      # vanished order's fill was booked
    assert s["cash"] == pytest.approx(10.0 * 1.0005)
    assert s["order_id"] is None and s["order_link_id"] is None   # cleared
    # CRUCIAL: no new order placed while the fill was unbooked (no same-side double-place)
    assert "place" not in fake.kinds()
    # it learned the truth via fetch_order_state (terminal-sync), not by re-placing
    assert ("fetch_state", "A0", "sca-0-0") in fake.calls


def test_reconcile_vanished_not_found_does_not_place(tmp_path):
    # R3-P0: a vanished slice whose state is not_found (eventual consistency: absent from
    # open AND from terminal history) must NOT be placed this tick — wait, re-check next
    # tick — never double-place against an unknown outcome. The slice order state is kept.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[],
        state_results={"sca-0-0": _state("not_found", oid="A0", link="sca-0-0",
                                          filled=0.0, remaining=0.0)})
    eng.reconcile_orders(0.0, client=fake)
    assert "place" not in fake.kinds()               # do not place over an unknown outcome
    assert s["state"] == "usd1"                       # untouched
    assert s["order_link_id"] == "sca-0-0"            # kept for next tick's re-check


# === terminal-state GHOST-ORDER fix: clear on ANY terminal class =============
# A terminal order that is `cancelled` with a PARTIAL fill, or `rejected` /
# `postonly_rejected` with little/zero fill, must book any exec delta AND THEN clear the
# slice order identity — the slice is freed to be re-quoted. Clearing ONLY on a full fill
# left a permanent ghost (reconcile believed a live order still rested -> never re-placed;
# _cancel_all_resting tried to cancel a dead order). State flips ONLY on a genuine FULL
# fill; a non-terminal `open` / `not_found` (incl. PendingCancel, which classify_status
# maps to `open`) is NEVER cleared (R2-P0/P0-1 "never clear on an unknown/live outcome").

def test_cancelled_partial_books_then_clears_order_identity_no_ghost(tmp_path):
    # A resting SELL is `cancelled` after a PARTIAL fill (Bybit PartiallyFilledCanceled ->
    # status_class `cancelled`, filled>0). poll_fills must book the 4-lot delta, then CLEAR
    # the order identity (no ghost) WITHOUT flipping state, leaving the slice re-quotable.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=6.0),               # 6 base remains free after the 4-lot partial
        open_orders=[],
        state_results={"sca-0-0": _state("cancelled", oid="A0", link="sca-0-0",
                                          side="sell", filled=4.0, remaining=6.0,
                                          avg=1.0005)})
    eng.poll_fills(0.0, client=fake)
    # (1) exec delta booked: 4 sold @ 1.0005
    assert s["qty"] == pytest.approx(6.0)
    assert s["cash"] == pytest.approx(4.0 * 1.0005)
    assert s["sell_proceeds"] == pytest.approx(4.0 * 1.0005)
    assert s["qty_sold"] == pytest.approx(4.0)
    # (2) order identity CLEARED -> no ghost (even though NOT fully filled)
    assert s["order_id"] is None
    assert s["order_link_id"] is None
    assert s["order_px"] is None
    assert s["order_qty"] is None
    assert s["filled_qty"] == 0.0
    # (3) state NOT flipped — a cancelled-partial keeps its (delta-updated) partial position
    assert s["state"] == "usd1"
    # (4) re-quotable: a subsequent reconcile places a FRESH order for the now-free slice
    eng.reconcile_orders(0.0, client=fake)
    assert "place" in fake.kinds()
    assert s["order_link_id"] is not None     # a fresh resting order was placed


def test_rejected_terminal_clears_order_identity_no_ghost(tmp_path):
    # An exchange-`rejected` resting order (not postonly) with ~0 fill must clear the order
    # identity (no ghost) without flipping state and WITHOUT touching the postonly reject
    # streak (that cooldown is only for postonly_rejected).
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("rejected", oid="A0", link="sca-0-0", side="sell",
                          filled=0.0, remaining=0.0)})
    eng.poll_fills(0.0, client=fake)
    # no fill, no flip, position unchanged
    assert s["state"] == "usd1"
    assert s["qty"] == pytest.approx(10.0)
    assert s["cash"] == 0.0
    # order identity cleared -> no ghost
    assert s["order_id"] is None and s["order_link_id"] is None
    assert s["order_px"] is None and s["order_qty"] is None
    assert s["filled_qty"] == 0.0
    # plain `rejected` is NOT a postonly reject -> streak untouched
    assert s["reject_streak"] == 0


def test_apply_exec_delta_terminal_clears_live_keeps_full_flips(tmp_path):
    # The single decision point. (a) FULL fill -> flip + clear (regression, unchanged).
    # (b) non-terminal `open`/`not_found` (PendingCancel -> `open`) -> NEVER cleared.
    # (c) terminal-but-NON-full (cancelled/rejected/postonly_rejected) -> clear, no flip.
    # (a) full fill flips state + clears
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    eng._apply_exec_delta(0, _state("filled", oid="A0", link="sca-0-0", side="sell",
                                    filled=10.0, remaining=0.0, avg=1.0005), 0.0)
    assert s["state"] == "usdt"
    assert s["order_id"] is None and s["filled_qty"] == 0.0
    # (b) non-terminal live outcomes are NEVER cleared (preserve R2-P0 invariant)
    for live_class in ("open", "not_found"):
        s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
                order_side="sell", order_px=1.0005, order_qty=10.0)
        eng = _mk_engine(tmp_path, slices=[s])
        eng._apply_exec_delta(0, _state(live_class, oid="A0", link="sca-0-0",
                                        side="sell", filled=0.0, remaining=10.0), 0.0)
        assert s["order_id"] == "A0", f"{live_class} must NOT clear (unknown/live)"
        assert s["order_link_id"] == "sca-0-0"
        assert s["order_qty"] == pytest.approx(10.0)
        assert s["state"] == "usd1"
    # (c) terminal but non-full -> clear the order identity, do NOT flip
    for term_class in ("cancelled", "rejected", "postonly_rejected"):
        s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
                order_side="sell", order_px=1.0005, order_qty=10.0)
        eng = _mk_engine(tmp_path, slices=[s])
        eng._apply_exec_delta(0, _state(term_class, oid="A0", link="sca-0-0",
                                        side="sell", filled=0.0, remaining=10.0), 0.0)
        assert s["order_id"] is None, f"{term_class} must clear (no ghost)"
        assert s["order_link_id"] is None
        assert s["order_qty"] is None
        assert s["state"] == "usd1", f"{term_class} non-full must NOT flip"


def test_reconcile_vanished_cancelled_partial_books_clears_no_ghost(tmp_path):
    # R3-P0 + ghost fix via the VANISHED-SYNC path (defence-in-depth): a slice's resting
    # order VANISHED from fetch_open and its terminal state is `cancelled` with a PARTIAL
    # fill. reconcile's vanished-sync must book the partial AND clear the order identity
    # (no ghost), NOT flip state, and NOT place a stale paired order this tick.
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[],                        # order vanished from the open book
        state_results={"sca-0-0": _state("cancelled", oid="A0", link="sca-0-0",
                                          side="sell", filled=4.0, remaining=6.0,
                                          avg=1.0005)})
    eng.reconcile_orders(0.0, client=fake)
    assert s["qty"] == pytest.approx(6.0)               # partial booked
    assert s["cash"] == pytest.approx(4.0 * 1.0005)
    assert s["state"] == "usd1"                          # NOT flipped
    assert s["order_id"] is None and s["order_link_id"] is None   # cleared -> no ghost
    assert s["order_qty"] is None
    # learned the truth via terminal-sync; the paired place is dropped this tick
    assert ("fetch_state", "A0", "sca-0-0") in fake.calls
    assert "place" not in fake.kinds()


# === _flip_state parity with paper evaluate_fills ============================

def test_evaluate_fills_min_profit_floor_blocks_loss_sale(tmp_path):
    # anchor+rung is below the tracked entry-cost floor, and rest is disabled:
    # the slice must keep holding USD1 instead of selling below cost.
    paper = _mk_engine(tmp_path, anchor=0.9990,
                       slices=[_sl("usd1", qty=10.0, entry=1.0)],
                       rungs=[1], fracs=[1.0])
    paper.maker_enabled = False
    paper.min_profit_bp = 1.0
    paper.rest_bps = 0.0
    paper.bid = 0.9991
    paper.evaluate_fills(0.0)
    assert paper.slices[0]["state"] == "usd1"
    assert paper.slices[0]["qty"] == pytest.approx(10.0)
    assert paper.slices[0]["cash"] == 0.0


def test_evaluate_fills_rest_bps_allows_surrender_sale(tmp_path):
    # When the anchor breaks below entry by rest_bps, the floor is deliberately
    # disabled for this sell: the canary exits, accepts the loss, and will re-anchor
    # on the next rebuy via the normal entry update.
    paper = _mk_engine(tmp_path, anchor=0.9984,
                       slices=[_sl("usd1", qty=10.0, entry=1.0)],
                       rungs=[1], fracs=[1.0])
    paper.maker_enabled = False
    paper.min_profit_bp = 1.0
    paper.rest_bps = 15.0
    paper.bid = 0.9985
    paper.evaluate_fills(0.0)
    assert paper.slices[0]["state"] == "usdt"
    assert paper.slices[0]["sell_px"] == pytest.approx(0.9985)
    assert paper.slices[0]["cash"] == pytest.approx(10.0 * 0.9985)


def test_evaluate_fills_floor_zero_keeps_anchor_rung_behavior(tmp_path):
    paper = _mk_engine(tmp_path, anchor=0.9990,
                       slices=[_sl("usd1", qty=10.0, entry=1.0)],
                       rungs=[1], fracs=[1.0])
    paper.maker_enabled = False
    paper.min_profit_bp = 0.0
    paper.rest_bps = 0.0
    paper.min_sell_margin_bp = 0.0      # isolate min_profit=0 anchor+rung (no margin floor)
    paper.bid = 0.9991
    paper.evaluate_fills(0.0)
    assert paper.slices[0]["state"] == "usdt"
    assert paper.slices[0]["sell_px"] == pytest.approx(0.9991)


def test_evaluate_fills_rebuy_uses_bid_when_bid_is_below_anchor(tmp_path):
    paper = _mk_engine(tmp_path, anchor=1.0009, bid=1.0002, ask=1.0008,
                       slices=[_sl("usdt", cash=10.0, sell_px=1.0005)],
                       rungs=[5], fracs=[1.0])
    paper.maker_enabled = False

    paper.evaluate_fills(0.0)

    assert paper.slices[0]["state"] == "usdt"       # anchor-1bp would have filled

    paper.ask = 1.0001
    paper.evaluate_fills(1.0)

    assert paper.slices[0]["state"] == "usd1"
    assert paper.slices[0]["entry"] == pytest.approx(1.0001)


def test_flip_state_resets_same_fields_as_evaluate_fills(tmp_path):
    # paper: a full SELL then full REBUY at the same prices
    paper = _mk_engine(tmp_path, anchor=1.0,
                       slices=[_sl("usd1", qty=10.0, entry=1.0)], rungs=[5], fracs=[1.0])
    paper.maker_enabled = False
    paper.min_profit_bp = 0.0
    paper.rest_bps = 0.0
    R = round(1.0 + 5 / 1e4, 4)
    B = round(1.0 + (-1) / 1e4, 4)
    paper.bid = R
    paper.evaluate_fills(0.0)                 # sell fills
    paper.ask = B
    paper.evaluate_fills(0.0)                 # rebuy fills
    p = paper.slices[0]

    # maker: same prices via _apply_exec + _flip_state
    maker = _mk_engine(tmp_path, anchor=1.0,
                       slices=[_sl("usd1", qty=10.0, entry=1.0, order_side="sell",
                                   order_px=R, order_qty=10.0)], rungs=[5], fracs=[1.0])
    maker._apply_exec(0, "sell", 10.0, R, 0.0)
    maker._flip_state(0)                      # -> usdt
    m = maker.slices[0]
    nq = m["cash"] / B
    m["order_px"] = B
    maker._apply_exec(0, "buy", nq, B, 0.0)
    maker._flip_state(0)                      # -> usd1
    for f in ("state", "qty", "cash", "entry"):
        assert m[f] == pytest.approx(p[f]) if isinstance(p[f], float) else m[f] == p[f]


def test_full_cycle_maker_realized_capture_parity_with_paper(tmp_path):
    paper = _mk_engine(tmp_path, anchor=1.0,
                       slices=[_sl("usd1", qty=10.0, entry=1.0)], rungs=[5], fracs=[1.0])
    paper.maker_enabled = False
    paper.min_profit_bp = 0.0
    paper.rest_bps = 0.0
    R = round(1.0 + 5 / 1e4, 4)
    B = round(1.0 + (-1) / 1e4, 4)
    paper.bid = R
    paper.evaluate_fills(0.0)
    paper.ask = B
    paper.evaluate_fills(0.0)

    maker = _mk_engine(tmp_path, anchor=1.0,
                       slices=[_sl("usd1", qty=10.0, entry=1.0, order_px=R,
                                   order_qty=10.0)], rungs=[5], fracs=[1.0])
    maker._apply_exec(0, "sell", 10.0, R, 0.0)
    maker._flip_state(0)
    nq = maker.slices[0]["cash"] / B
    maker.slices[0]["order_px"] = B
    maker._apply_exec(0, "buy", nq, B, 0.0)
    maker._flip_state(0)
    assert maker.realized_capture == pytest.approx(paper.realized_capture)


# === guards: None/NaN, overshoot =============================================

def test_none_or_nonfinite_filled_total_guarded_skips(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    for bad in (None, float("nan"), float("inf")):
        changed = eng._apply_exec_delta(0, _state("open", side="sell", filled=bad,
                                                   remaining=0.0, avg=1.0005), 0.0)
        assert changed is False
        assert s["state"] == "usd1" and s["qty"] == 10.0 and s["cash"] == 0.0
    # also a non-finite TOTAL (order_qty) is guarded
    s["order_qty"] = float("nan")
    assert eng._apply_exec_delta(0, _state("open", side="sell", filled=1.0,
                                           remaining=0.0, avg=1.0005), 0.0) is False
    assert s["qty"] == 10.0


def test_exec_delta_without_price_skips(tmp_path):
    # a positive fill but no/inf avg price -> cannot book -> skip (re-poll), no mutation
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    for bad_avg in (None, float("nan")):
        changed = eng._apply_exec_delta(0, _state("open", side="sell", filled=4.0,
                                                  remaining=6.0, avg=bad_avg), 0.0)
        assert changed is False
        assert s["qty"] == 10.0 and s["cash"] == 0.0 and s["filled_qty"] == 0.0


def test_sell_qty_capped_at_available_no_overshoot(tmp_path):
    s = _sl("usd1", qty=3.0, order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    eng._apply_exec(0, "sell", 10.0, 1.0005, 0.0)   # dq exceeds held qty
    assert s["qty"] == 0.0                           # capped, never negative
    assert s["cash"] == pytest.approx(3.0 * 1.0005)  # only what was held
    assert s["qty_sold"] == pytest.approx(3.0)


# === available-balance pool: free + own-locked ==============================

def test_avail_uses_free_plus_own_locked(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=8.0)])
    bal = _bal(usd1=100.0, usdt=50.0)
    live = {0: Live(order_id="A0", link_id="sca-0-0", side="sell", price=1.0005,
                    qty=8.0, filled_qty=0.0, matched_by="link_id"),
            1: Live(order_id="A1", link_id="sca-1-0", side="buy", price=0.9999,
                    qty=20.0, filled_qty=0.0, matched_by="link_id")}
    ab, aq = eng._available_from_balance(bal, live)
    assert ab == pytest.approx(100.0 + 8.0)             # free_base + own resting SELL leaves
    assert aq == pytest.approx(50.0 + 20.0 * 0.9999)    # free_quote + own resting BUY leaves*px


# === A4a readers: carry + valuation value BOTH legs =========================

def test_carry_sums_base_across_all_slices(tmp_path):
    # a mid-partial slice (state usd1, qty residual) + a clean usdt slice +
    # a mid-REBUY slice (state STILL usdt while base qty accrues): that base earns
    # carry, so a state-filtered sum would under-count it. The usdt qty MUST be
    # nonzero or the all-slices sum equals the state-filtered sum (tautology).
    eng = _mk_engine(tmp_path, slices=[
        _sl("usd1", qty=6.0, cash=4.0),    # partial residual base counts
        _sl("usd1", qty=5.0),
        _sl("usdt", qty=3.0, cash=2.0)])   # mid-rebuy base residual under state=='usdt'
    assert eng._usd1_qty() == pytest.approx(6.0 + 5.0 + 3.0)


def test_status_base_quote_value_independent_of_state(tmp_path):
    # ONE mid-partial slice: state usd1 but holds both base (qty) AND proceeds (cash)
    eng = _mk_engine(tmp_path, bid=0.9999, ask=1.0001,
                     slices=[_sl("usd1", qty=4.0, cash=6.0)], rungs=[5], fracs=[1.0])
    doc = eng.status_doc(86401.0)
    pos = doc["position"]
    assert pos["usd1_value"] == pytest.approx(4.0 * 1.0)    # base = Σ qty*mark
    assert pos["usdt_value"] == pytest.approx(6.0)          # quote = Σ cash
    assert pos["total_value"] == pytest.approx(10.0)


def test_live_status_rebuy_price_uses_maker_buy_tick_floor(tmp_path):
    # Raw rebuy is 1.000961..., which rounded display shows as 1.0010.
    # Live maker BUY orders floor to the tick instead, so dashboard must show 1.0009.
    eng = _mk_engine(tmp_path, anchor=1.0010613770166539,
                     slices=[_sl("usdt", cash=100.0)], rungs=[5], fracs=[1.0])
    eng.mode = "live"

    doc = eng.status_doc(86401.0)

    assert doc["indicators"]["rebuy_price"] == pytest.approx(1.0009)


def test_live_status_rebuy_price_uses_bid_when_bid_is_below_anchor(tmp_path):
    eng = _mk_engine(tmp_path, anchor=1.0009, bid=1.0002, ask=1.0003,
                     slices=[_sl("usdt", cash=100.0)], rungs=[5], fracs=[1.0])
    eng.mode = "live"

    doc = eng.status_doc(86401.0)

    assert doc["indicators"]["rebuy_price"] == pytest.approx(1.0001)


def test_dryrun_status_rebuy_price_uses_bid_when_bid_is_below_anchor(tmp_path):
    eng = _mk_engine(tmp_path, anchor=1.0009, bid=1.0002, ask=1.0003,
                     slices=[_sl("usdt", cash=100.0)], rungs=[5], fracs=[1.0])
    eng.mode = "dryrun"

    doc = eng.status_doc(86401.0)

    assert doc["indicators"]["rebuy_price"] == pytest.approx(1.0001)


def test_live_status_sell_prices_use_maker_sell_tick_floor(tmp_path):
    # yaml sell_round=floor: live maker SELL floors to the tick. Raw 1.001141 -> floor 1.0011
    # (entry 1.0 + 2bp margin floor = 1.0002, does not bind here). Dashboard mirrors下单价.
    eng = _mk_engine(tmp_path, anchor=1.001041,
                     slices=[_sl("usd1", qty=10.0, entry=1.0)],
                     rungs=[1], fracs=[1.0])
    eng.mode = "live"

    doc = eng.status_doc(86401.0)

    assert doc["indicators"]["sell_rungs"][0]["price"] == pytest.approx(1.0011)
    assert doc["position"]["slices"][0]["sell_target"] == pytest.approx(1.0011)


def test_status_doc_valuation_under_partial_fill(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=4.0, cash=6.0)])
    val = eng._slice_value(eng.slices[0], 1.0)
    assert val == pytest.approx(4.0 * 1.0 + 6.0)            # both legs


def test_daily_pnl_notification_once_per_utc_day(tmp_path):
    eng = _mk_engine(tmp_path, bid=1.0, ask=1.0,
                     slices=[_sl("usd1", qty=100.0, entry=1.0)])
    eng.start = 1_700_000_000.0 - 2 * 86400
    eng.realized_capture = 1.25
    eng.interest.settled = 0.5
    eng._deployed_capital = 100.0
    eng.strategy_name = "USD1 EMA Slice Ladder"
    notifier = FakeNotifier()
    eng.notifier = notifier

    eng._maybe_notify_daily_pnl(1_700_000_000.0)
    eng._maybe_notify_daily_pnl(1_700_000_100.0)

    assert len(notifier.daily) == 1
    msg = notifier.daily[0]
    assert msg["strategy_name"] == "USD1 EMA Slice Ladder"
    assert msg["mode"] == eng.mode
    assert msg["symbol"] == "USD1USDT"
    assert msg["day"] == "2023-11-14"
    assert msg["pnl"]["total"] == pytest.approx(0.5)
    assert eng._last_daily_notify_day == "2023-11-14"


def test_local_summary_independent_of_state(tmp_path):
    # second slice is mid-REBUY: state=='usdt' yet holds a base residual (qty=3) that
    # the reconcile summary must report; qty>0 here is what makes the all-slices base
    # sum diverge from a state-filtered sum (else the base-leg assertion is a tautology).
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=4.0, cash=6.0),
                                       _sl("usdt", qty=3.0, cash=3.0)])
    summ = eng._local_summary()
    assert summ["base_qty"] == pytest.approx(4.0 + 3.0)
    assert summ["quote_qty"] == pytest.approx(6.0 + 3.0)


# === anchor-None no-op + r1 assertion =======================================

def test_reconcile_orders_noop_when_anchor_none(tmp_path):
    eng = _mk_engine(tmp_path, anchor=None, slices=[_sl("usd1", qty=10.0)])
    fake = FakeOrderClient()
    eng.reconcile_orders(0.0, client=fake)
    assert fake.calls == []


def test_poll_fills_noop_when_anchor_none(tmp_path):
    eng = _mk_engine(tmp_path, anchor=None,
                     slices=[_sl("usd1", qty=10.0, order_link_id="sca-0-0")])
    fake = FakeOrderClient()
    eng.poll_fills(0.0, client=fake)
    assert fake.calls == []


def test_reconcile_orders_asserts_r1_ok(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])
    eng._r1_ok = False
    with pytest.raises(AssertionError):
        eng.reconcile_orders(0.0, client=FakeOrderClient())


def test_poll_fills_asserts_r1_ok(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])
    eng._r1_ok = False
    with pytest.raises(AssertionError):
        eng.poll_fills(0.0, client=FakeOrderClient())


# === poll when only link_id present (crash-after-place recovery) ============

def test_poll_when_only_link_id_present(tmp_path):
    s = _sl("usd1", qty=10.0, order_id=None, order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("open", link="sca-0-0", side="sell", filled=0.0,
                          remaining=10.0)})
    eng.poll_fills(0.0, client=fake)
    assert ("fetch_state", None, "sca-0-0") in fake.calls


def test_poll_skips_when_both_id_and_link_absent(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])  # no order at all
    fake = FakeOrderClient()
    eng.poll_fills(0.0, client=fake)
    assert "fetch_state" not in fake.kinds()


# === PostOnly-reject cooldown + halt threshold ==============================

def test_postonly_reject_slice_cooldown_until_anchor_change(tmp_path):
    s = _sl("usd1", qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    eng._reject_halt_threshold = 100
    reject = _state("postonly_rejected", filled=0.0, remaining=0.0)
    fake = FakeOrderClient(balance=_bal(usd1=10.0), place_result=reject)
    eng.reconcile_orders(0.0, client=fake)          # place -> rejected -> cooldown
    assert fake.kinds().count("place") == 1
    eng.reconcile_orders(0.0, client=fake)          # same anchor -> cooldown skips place
    assert fake.kinds().count("place") == 1
    eng.anchor = 1.0010                              # anchor moved -> cooldown clears
    eng.reconcile_orders(0.0, client=fake)
    assert fake.kinds().count("place") == 2


def test_consecutive_postonly_rejects_trip_halt_threshold(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)], rungs=[5], fracs=[1.0])
    eng._reject_halt_threshold = 3
    eng._note_reject(0)
    eng._note_reject(0)
    with pytest.raises(OperatorReconcileHalt):
        eng._note_reject(0)        # streak hits 3 -> halt
    assert eng.slices[0]["reject_streak"] == 3


def test_cooldown_lifts_when_topofbook_lets_rung_rest(tmp_path):
    from sca.live.order_recon import Desired
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[_sl("usd1", qty=10.0)])
    # SELL rung would now rest (bid below the sell price) -> cooldown lifts
    eng._reject_anchor[0] = 1.0
    eng.bid = 1.0003
    assert eng._in_cooldown(0, Desired("sell", 1.0005, 10.0)) is False
    # BUY rung would now rest (ask above the buy price) -> cooldown lifts
    eng._reject_anchor[0] = 1.0
    eng.ask = 1.0002
    assert eng._in_cooldown(0, Desired("buy", 0.9999, 10.0)) is False
    # still doomed (sell at/below bid) -> stays in cooldown
    eng._reject_anchor[0] = 1.0
    eng.bid = 1.0010
    eng.ask = None
    assert eng._in_cooldown(0, Desired("sell", 1.0005, 10.0)) is True
    # no desired this tick (slice dropped) -> stays in cooldown until anchor moves
    eng._reject_anchor[0] = 1.0
    assert eng._in_cooldown(0, None) is True


def test_reconcile_orders_noop_when_maker_disabled(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])
    eng.maker_enabled = False
    fake = FakeOrderClient()
    eng.reconcile_orders(0.0, client=fake)
    assert fake.calls == []


def test_poll_fills_noop_when_maker_disabled(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0, order_link_id="sca-0-0")])
    eng.maker_enabled = False
    fake = FakeOrderClient()
    eng.poll_fills(0.0, client=fake)
    assert fake.calls == []


def test_reconcile_amends_qty_down_on_unfilled_order(tmp_path):
    # resting sell of 10 at the rung; available shrinks so desired qty drops -> amend
    s = _sl("usd1", qty=4.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=4.0),   # only 4 base available now -> desired qty 4 < resting 10
        open_orders=[_open_order("A0", "sca-0-0", "sell", 1.0005, 10.0, filled=0.0)])
    eng.reconcile_orders(0.0, client=fake)
    assert "amend" in fake.kinds()
    assert "place" not in fake.kinds() and "cancel" not in fake.kinds()
    assert s["order_qty"] == pytest.approx(4.0)


def test_reconcile_amend_then_place_in_one_tick(tmp_path):
    # slice 0 amends (qty down on a resting unfilled sell); slice 1 places a new sell.
    # Exercises the loop continuing PAST an amend to a later action in the same tick.
    s0 = _sl("usd1", qty=4.0, order_id="A0", order_link_id="sca-0-0",
             order_side="sell", order_px=1.0005, order_qty=10.0)
    s1 = _sl("usd1", qty=5.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s0, s1], rungs=[5, 7], fracs=[0.5, 0.5])
    fake = FakeOrderClient(
        balance=_bal(usd1=5.0),
        open_orders=[_open_order("A0", "sca-0-0", "sell", 1.0005, 10.0, filled=0.0)])
    eng.reconcile_orders(0.0, client=fake)
    assert "amend" in fake.kinds()         # slice 0
    assert "place" in fake.kinds()         # slice 1 (loop continued past the amend)
    assert s0["order_qty"] == pytest.approx(4.0)
    assert s1["order_id"] is not None


def test_reconcile_notifies_after_successful_postonly_place(tmp_path):
    s = _sl("usd1", qty=10.0, entry=1.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    eng.strategy_name = "USD1 EMA Slice Ladder"
    notifier = FakeNotifier()
    eng.notifier = notifier
    fake = FakeOrderClient(balance=_bal(usd1=10.0))

    eng.reconcile_orders(0.0, client=fake)

    assert len(notifier.orders) == 1
    msg = notifier.orders[0]
    assert msg["strategy_name"] == "USD1 EMA Slice Ladder"
    assert msg["mode"] == eng.mode
    assert msg["symbol"] == "USD1USDT"
    assert msg["side"] == "sell"
    assert msg["slice_idx"] == 0
    assert msg["price"] == s["order_px"]
    assert msg["qty"] == s["order_qty"]
    assert msg["link_id"] == s["order_link_id"]
    assert msg["order_id"] == s["order_id"]


def test_place_too_small_clears_slice_no_retry(tmp_path):
    s = _sl("usd1", qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    too_small = _state("too_small", filled=0.0, remaining=0.0)
    fake = FakeOrderClient(balance=_bal(usd1=10.0), place_result=too_small)
    eng.reconcile_orders(0.0, client=fake)
    assert fake.kinds().count("place") == 1     # placed once, not hot-retried
    assert s["order_id"] is None and s["order_link_id"] is None   # slice cleared
    assert s["reject_streak"] == 0              # too_small is NOT a reject


def test_place_insufficient_funds_clears_intent_no_ghost(tmp_path):
    # P1-7: place_postonly can return status_class == "insufficient_funds" (order NOT
    # placed). _place must NOT treat it as success — that would leave a ghost order_link_id
    # / order_qty with order_id=None for an order that never existed (poll_fills would then
    # forever poll a non-existent order). It clears the slice order intent and skips.
    s = _sl("usd1", qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    insufficient = _state("insufficient_funds", filled=0.0, remaining=0.0)
    fake = FakeOrderClient(balance=_bal(usd1=10.0), place_result=insufficient)
    eng.reconcile_orders(0.0, client=fake)
    assert fake.kinds().count("place") == 1       # attempted exactly once (not hot-retried)
    assert s["order_id"] is None                   # NOT recorded as a success
    assert s["order_link_id"] is None              # no ghost link
    assert s["order_qty"] is None                  # no ghost intent
    assert s["reject_streak"] == 0                 # insufficient_funds is NOT a postonly reject


def test_poll_fills_postonly_rejected_notes_and_clears(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s])
    fake = FakeOrderClient(state_results={
        "sca-0-0": _state("postonly_rejected", oid="A0", link="sca-0-0")})
    eng.poll_fills(0.0, client=fake)
    assert s["reject_streak"] == 1
    assert s["order_id"] is None and s["order_link_id"] is None


def test_persist_durable_oserror_halts(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])
    eng.persist = True
    import sca.live.engine as E
    orig = E.save_state
    E.save_state = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    try:
        with pytest.raises(OperatorReconcileHalt):
            eng._persist_durable_or_halt()
    finally:
        E.save_state = orig
    assert eng._halted is True


# === hysteresis: zero touch on sub-tick move; replace on >=1bp step =========

def test_reconcile_zero_actions_when_anchor_submove(tmp_path):
    # resting sell at the rung; anchor unchanged -> desired == resting -> all leave
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[_open_order("A0", "sca-0-0", "sell", 1.0005, 10.0)])
    eng.reconcile_orders(0.0, client=fake)
    assert "place" not in fake.kinds()
    assert "cancel" not in fake.kinds()
    assert "amend" not in fake.kinds()


def test_reconcile_anchor_step_replaces_affected_orders(tmp_path):
    s = _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0)
    eng = _mk_engine(tmp_path, anchor=1.0010, slices=[s], rungs=[5], fracs=[1.0])
    fake = FakeOrderClient(
        balance=_bal(usd1=10.0),
        open_orders=[_open_order("A0", "sca-0-0", "sell", 1.0005, 10.0)])
    eng.reconcile_orders(0.0, client=fake)
    assert "cancel" in fake.kinds()        # old rung cancelled
    assert "place" in fake.kinds()         # new rung placed
    assert eng.slices[0]["order_px"] == pytest.approx(1.0015)  # re-priced to new rung


# === sell_round / min_sell_margin_bp wiring (floor口径) ======================

def test_status_sell_price_uses_floor_and_margin_when_configured(tmp_path):
    # sell_round=floor + 2bp margin -> floor(1.0012), NOT round/ceil (1.0013)
    eng = _mk_engine(tmp_path, rungs=[1], fracs=[1.0])
    eng.sell_round = "floor"
    eng.min_sell_margin_bp = 2.0
    eng.min_profit_bp = 1.0
    eng.rest_bps = 14.0
    assert eng._status_sell_price(1.00116, 1, 1.0010) == pytest.approx(1.0012)


def test_status_sell_price_none_round_fallback_paper(tmp_path):
    # sell_round=None (yaml key deleted) + paper mode -> legacy round (rollback guarantee)
    from sca.strategy_rules import rounded_sell_price
    eng = _mk_engine(tmp_path, rungs=[1], fracs=[1.0])
    eng.sell_round = None            # simulate deleted yaml key -> legacy fallback
    eng.min_sell_margin_bp = 0.0
    eng.min_profit_bp = 1.0
    eng.rest_bps = 0.0
    assert eng._status_sell_price(1.00115, 1, 1.0010) == pytest.approx(
        rounded_sell_price(1.00115, 1, 1.0010, 1.0, 0.0, 4))


def test_evaluate_fills_uses_floor_sell_price_when_configured(tmp_path):
    # floor sell (1.0012) fills at bid=1.0012; legacy round (1.0013) would NOT fill here
    eng = _mk_engine(tmp_path, anchor=1.00116,
                     slices=[_sl("usd1", qty=10.0, entry=1.0010)],
                     rungs=[1], fracs=[1.0])
    eng.maker_enabled = False
    eng.min_profit_bp = 1.0
    eng.rest_bps = 14.0
    eng.sell_round = "floor"
    eng.min_sell_margin_bp = 2.0
    eng.bid = 1.0012
    eng.evaluate_fills(0.0)
    assert eng.slices[0]["state"] == "usdt"
    assert eng.slices[0]["sell_px"] == pytest.approx(1.0012)


# === rollback fallback口径 per call point (sell_round=None) ===================
# Invariant 3: deleting the yaml `sell_round` key (-> None) must restore EACH call
# point's ORIGINAL口径 (paper-fill=round, live-order=ceil, dashboard mirrors its mode).
# raw 1.00121 is OFF the grid so ceil(1.0013) != round(1.0012) — a drifted fallback
# literal flips the value and is caught. entry=None isolates pure tick rounding.

def test_evaluate_fills_none_fallback_uses_round_not_ceil(tmp_path):
    # PAPER fill rollback口径 = round. round(1.00121)=1.0012 fills at bid 1.0012;
    # a fallback drifted to ceil (1.0013) would NOT fill -> state stays usd1.
    s = _sl("usd1", qty=10.0)                  # entry=None -> no margin/min_profit floor
    eng = _mk_engine(tmp_path, anchor=1.00111, slices=[s], rungs=[1], fracs=[1.0])
    eng.maker_enabled = False
    eng.sell_round = None                      # simulate deleted yaml key
    eng.min_profit_bp = 0.0
    eng.rest_bps = 0.0
    eng.min_sell_margin_bp = 0.0
    eng.bid = 1.0012
    eng.evaluate_fills(0.0)
    assert eng.slices[0]["state"] == "usdt"
    assert eng.slices[0]["sell_px"] == pytest.approx(1.0012)   # ROUND, not ceil(1.0013)


def test_status_sell_price_none_live_fallback_uses_ceil(tmp_path):
    # DASHBOARD rollback口径 in LIVE mode mirrors the live order path = ceil, NOT round.
    eng = _mk_engine(tmp_path, rungs=[1], fracs=[1.0])
    eng.mode = "live"
    eng.sell_round = None                      # simulate deleted yaml key
    eng.min_profit_bp = 0.0
    eng.rest_bps = 0.0
    eng.min_sell_margin_bp = 0.0
    assert eng._status_sell_price(1.00111, 1, None) == pytest.approx(1.0013)  # CEIL


def test_reconcile_live_order_none_fallback_uses_ceil(tmp_path):
    # LIVE maker ORDER rollback口径 = ceil. The order is PLACED at ceil(1.00121)=1.0013;
    # a fallback drifted to round (1.0012) would mis-price the real order.
    s = _sl("usd1", qty=10.0)                  # entry=None -> isolate pure tick rounding
    eng = _mk_engine(tmp_path, anchor=1.00111, slices=[s], rungs=[1], fracs=[1.0])
    eng.sell_round = None                      # simulate deleted yaml key
    eng.min_profit_bp = 0.0
    eng.rest_bps = 0.0
    eng.min_sell_margin_bp = 0.0
    fake = FakeOrderClient(balance=_bal(usd1=10.0))
    eng.reconcile_orders(0.0, client=fake)
    assert s["order_px"] == pytest.approx(1.0013)             # CEIL, not round(1.0012)


# === Invariant 4: all FOUR sell-price call points must agree =================
def test_four_callpoint_sell_price_parity(tmp_path):
    # backtest == live(desired_orders) == paper(evaluate_fills) == dashboard
    # (_status_sell_price) under the SAME (anchor, entry, rung, tick, sell_round,
    # margin). A call point that drops min_sell_margin_bp or uses a wrong口径 breaks
    # this. final_sell_price is the backtest's literal call (sca/backtest/strategy.py).
    from sca.strategy_rules import final_sell_price
    from sca.live.order_recon import desired_orders
    anchor, entry, rung, tick = 1.00116, 1.0010, 1, 1e-4
    sr, margin, mp, rest = "floor", 2.0, 1.0, 14.0

    bt_px = final_sell_price(anchor, rung, entry, mp, rest, tick,
                             sell_round=sr, min_sell_margin_bp=margin)

    live_px = desired_orders(anchor, [{"state": "usd1", "qty": 10.0, "cash": 0.0,
                                       "entry": entry}], [rung], -1, tick, 1e-6,
                             1000.0, 1000.0, 0.0, 0.0, min_profit_bp=mp, rest_bps=rest,
                             sell_round=sr, min_sell_margin_bp=margin)[0].price

    paper = _mk_engine(tmp_path, anchor=anchor,
                       slices=[_sl("usd1", qty=10.0, entry=entry)],
                       rungs=[rung], fracs=[1.0])
    paper.maker_enabled = False
    paper.sell_round = sr
    paper.min_sell_margin_bp = margin
    paper.min_profit_bp = mp
    paper.rest_bps = rest
    paper.bid = 2.0                            # high bid -> the sell certainly fills at R
    paper.evaluate_fills(0.0)
    paper_px = paper.slices[0]["sell_px"]

    dash = _mk_engine(tmp_path, anchor=anchor, rungs=[rung], fracs=[1.0])
    dash.sell_round = sr
    dash.min_sell_margin_bp = margin
    dash.min_profit_bp = mp
    dash.rest_bps = rest
    dash_px = dash._status_sell_price(anchor, rung, entry)

    assert (bt_px == pytest.approx(live_px) == pytest.approx(paper_px)
            == pytest.approx(dash_px) == pytest.approx(1.0012))
