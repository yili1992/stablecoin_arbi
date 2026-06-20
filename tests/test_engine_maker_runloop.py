"""Task 6 — engine run-loop wiring + seed-in-gate + kill switch + lifecycle gate.

Pins the maker code's call sites so it can never silently not-run / double-run:
  - ``_handle`` skips ``_maybe_deploy``/``evaluate_fills`` when ``maker_enabled`` (A4b/F4).
  - ``_tick`` runs ``maker_step`` in the throttled branch, AFTER ``accrue`` (F4).
  - ``maker_step`` = ``poll_fills`` THEN ``reconcile_orders`` (R3-P0 — poll FIRST).
  - seeding runs INSIDE the gate BEFORE ``reconcile()`` decides; mixed balance refuses
    (C-P0#5 / C-P1#15).
  - the three-flag master switch (armed AND testnet AND maker_enabled knob).
  - both clients read the ONE ``resolve_testnet`` (no split-brain, F13).
  - kill-switch: ``run()`` try/finally + SIGINT/SIGTERM handler -> cancel ALL resting
    orders (A10/F12); ``fresh_deploy`` stays UNCONDITIONALLY refused (F22).
  - paper provably never builds the order client (130-test safety).

ISOLATION: no network, no disk. A real ``PaperEngine`` is built in paper mode
(out_dir -> tmp_path); maker live fields are set DIRECTLY; ``_sleep`` is a no-op; a
``FakeOrderClient`` records calls + returns canned states; the heavy ``run()``
internals (gate/bootstrap/finalize/websockets) are monkeypatched so the wiring is
exercised without I/O.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_maker_runloop.py -q
"""
import os
import signal
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import bybit_client, engine as engine_mod  # noqa: E402
from sca.live.engine import PaperEngine, STATUS_EVERY  # noqa: E402


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


def _state(status_class="cancelled", *, oid=None, link=None, side=None, filled=0.0,
           remaining=0.0, avg=None, price=None):
    return {"id": oid, "link_id": link, "side": side, "status": status_class,
            "status_class": status_class, "filled": filled, "remaining": remaining,
            "avg": avg, "price": price, "reject_reason": None, "raw": None}


class FakeOrderClient:
    """Records every call; returns canned market meta / balance / order state."""

    def __init__(self, *, balance=None, meta=None, open_orders=None,
                 state_results=None, max_order_usd=2000.0):
        self._balance = balance if balance is not None else _bal()
        self._meta = meta or {"tick": 0.0001, "lot": 0.000001,
                              "min_qty": 0.0, "min_cost": 0.0}
        self._open_orders = open_orders or []
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
        return _state("open", oid=f"oid-{link_id}", link=link_id, side=side,
                      filled=0.0, remaining=qty, price=price)

    def amend(self, symbol, order_id, *, link_id=None, qty=None):
        self.calls.append(("amend", order_id, link_id, qty))
        return _state("open", oid=order_id, link=link_id, filled=0.0, remaining=qty)

    def cancel(self, symbol, order_id, *, link_id=None):
        self.calls.append(("cancel", order_id, link_id))
        return _state("open", oid=order_id, link=link_id)

    def fetch_order_state(self, symbol, order_id=None, *, link_id=None):
        self.calls.append(("fetch_state", order_id, link_id))
        key = link_id if link_id is not None else order_id
        st = self._state_results.get(key)
        if st is None:
            return _state("cancelled", oid=order_id, link=link_id)
        return st

    def kinds(self):
        return [c[0] for c in self.calls]


class FakeReconClient:
    """Read-only R1 gate client (balance + account-wide open orders)."""

    def __init__(self, balance, orders=None):
        self._b, self._o = balance, orders or []
        self.calls = []

    def get_wallet_balance(self):
        self.calls.append("balance")
        return self._b

    def get_open_orders(self, symbol=None):
        self.calls.append(("orders", symbol))
        return list(self._o)


def _mk_engine(tmp_path, *, anchor=1.0, slices=None, bid=None, ask=None,
               maker=True, r1=True, persist=False):
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.persist = persist
    eng.maker_enabled = maker
    eng._r1_ok = r1
    eng._sleep = lambda *a, **k: None
    eng.anchor = anchor
    eng.bid = bid
    eng.ask = ask
    eng.deployed = True
    if slices is not None:
        eng.slices = slices
    return eng


def _armed_engine(tmp_path, *, maker=True, persist=True, slices=None,
                  allow_fresh=False, expect_asset=None, expect_amount=None):
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True
    eng.maker_enabled = maker
    eng.persist = persist
    eng.allow_fresh = allow_fresh
    eng.expect_asset = expect_asset
    eng.expect_amount = expect_amount
    eng._sleep = lambda *a, **k: None
    eng.slices = slices or []
    eng.deployed = bool(slices)
    return eng


def _ob_msg(bid=1.0, ask=1.001):
    return {"topic": "orderbook.1.USD1USDT",
            "data": {"b": [[str(bid), "5"]], "a": [[str(ask), "5"]]}}


# === A4b — _handle skips paper paths when maker_enabled (F4) ================

def test_evaluate_fills_and_maybe_deploy_skipped_when_maker_enabled(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10.0)])
    calls = []
    monkeypatch.setattr(eng, "_maybe_deploy", lambda: calls.append("deploy"))
    monkeypatch.setattr(eng, "evaluate_fills", lambda now: calls.append("fills"))
    eng._handle(_ob_msg(), 0.0)
    assert calls == []                              # maker path bypasses both
    # flip OFF -> paper path runs both verbatim
    eng.maker_enabled = False
    eng._handle(_ob_msg(), 0.0)
    assert calls == ["deploy", "fills"]


# === A4b — _tick runs maker_step (throttled), AFTER accrue (F4) =============

def test_maker_step_invoked_on_armed_path(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    called = []
    monkeypatch.setattr(eng, "maker_step", lambda now: called.append(now))
    eng.last_status = 0.0
    eng._tick(STATUS_EVERY + 1)                     # throttled branch fires
    assert called == [STATUS_EVERY + 1]
    # un-throttled tick (within STATUS_EVERY) does NOT churn orders
    called.clear()
    eng._tick(STATUS_EVERY + 1.5)
    assert called == []
    # paper path never calls maker_step even when throttled
    eng.maker_enabled = False
    eng.last_status = 0.0
    eng._tick(STATUS_EVERY + 100)
    assert called == []


def test_tick_keeps_status_cadence_but_throttles_console_summary(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    calls = []
    monkeypatch.setattr(eng, "maker_step", lambda now: None)
    monkeypatch.setattr(eng, "print_summary", lambda now: calls.append(("summary", now)))
    monkeypatch.setattr(eng, "write_status", lambda now: calls.append(("status", now)))
    eng.last_status = 0.0
    eng.last_summary = 0.0

    eng._tick(STATUS_EVERY + 1)
    eng._tick((STATUS_EVERY + 1) * 2)
    eng._tick(STATUS_EVERY + 62)

    assert calls == [
        ("summary", STATUS_EVERY + 1),
        ("status", STATUS_EVERY + 1),
        ("summary", (STATUS_EVERY + 1) * 2),
        ("status", (STATUS_EVERY + 1) * 2),
    ]


def test_gtc_order_maintenance_waits_for_status_cadence(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    called = []
    monkeypatch.setattr(eng, "maker_step", lambda now: called.append(now))
    eng.last_status = 0.0

    eng._tick(30.0)
    assert called == []

    eng._tick(STATUS_EVERY + 1)
    assert called == [STATUS_EVERY + 1]


def test_print_summary_uses_five_decimals_for_price_and_anchor(tmp_path, capsys):
    eng = _mk_engine(
        tmp_path,
        anchor=1.00104,
        bid=1.00003,
        ask=1.00007,
        slices=[_sl("usdt", cash=100.0)],
    )

    eng.print_summary(100.0)

    out = capsys.readouterr().out
    assert "px=1.00005" in out
    assert "anchor=1.00104" in out


def test_carry_accrues_before_poll_fills_across_hour(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    order = []
    monkeypatch.setattr(eng, "accrue", lambda now: order.append("accrue"))
    monkeypatch.setattr(eng, "poll_fills", lambda now, client=None: order.append("poll"))
    monkeypatch.setattr(eng, "reconcile_orders", lambda now, client=None: order.append("recon"))
    monkeypatch.setattr(eng, "print_summary", lambda now: None)
    monkeypatch.setattr(eng, "write_status", lambda now: None)
    eng.last_status = 0.0
    eng._tick(STATUS_EVERY + 1)
    # carry snapshot (accrue) must precede the fill mutation (poll_fills)
    assert order == ["accrue", "poll", "recon"]


# === maker_step ordering: poll BEFORE reconcile (R3-P0) =====================

def test_maker_step_polls_before_reconciles(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    order = []
    monkeypatch.setattr(eng, "poll_fills", lambda now, client=None: order.append("poll"))
    monkeypatch.setattr(eng, "reconcile_orders", lambda now, client=None: order.append("recon"))
    eng.maker_step(0.0)
    assert order == ["poll", "recon"]
    # self-guards: a paper engine maker_step is a no-op (never asserts _r1_ok)
    order.clear()
    eng.maker_enabled = False
    eng.maker_step(0.0)
    assert order == []


# === A6a — seed INSIDE the gate, BEFORE reconcile decides (C-P0#5) ==========

def test_seed_before_reconcile_decides(tmp_path):
    # USDT-funded clean account, NO local state, NO allow_fresh, NO declaration.
    # WITHOUT seed-before-decide reconcile() would refuse (no local + no opt-in).
    # WITH it, the SEEDED summary makes reconcile() proceed -> proves seed ran first.
    eng = _armed_engine(tmp_path, maker=True, allow_fresh=False)
    eng._max_total_alloc_usd = -1.0   # this test asserts FULL-wallet seeding; the cap value
                                      # (D15 canary default 1000) is exercised in test_phase3b
    client = FakeReconClient(_bal(usdt=10000.0), orders=[])
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed"
    assert eng._resumed is True and eng.deployed is True
    # seeded as usdt slices wanting BUYs; cash sums to the wallet balance
    assert all(s["state"] == "usdt" for s in eng.slices)
    assert sum(s["cash"] for s in eng.slices) == pytest.approx(10000.0)


def test_seed_usd1_funded_account_seeds_sell_side(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = -1.0   # full-wallet seed (cap value tested in test_phase3b)
    client = FakeReconClient(_bal(usd1=5000.0), orders=[])
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed"
    assert all(s["state"] == "usd1" for s in eng.slices)
    assert sum(s["qty"] for s in eng.slices) == pytest.approx(5000.0)


# === A6a — refuse a mixed / ambiguous balance or pre-existing orders (C-P1#15) ==

def test_seed_refuses_mixed_balance(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    client = FakeReconClient(_bal(usd1=6000.0, usdt=4000.0), orders=[])  # both material
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_seed_refuses_when_open_orders_present(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    client = FakeReconClient(_bal(usdt=10000.0), orders=[{"id": "x", "clientOrderId": None}])
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_seed_refuses_empty_account(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    client = FakeReconClient(_bal(), orders=[])      # nothing material to seed
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# === A6a — armed-maker testnet start reaches a real place action (F3) =======

def test_armed_maker_testnet_start_emits_place_action(tmp_path):
    eng = _armed_engine(tmp_path, maker=True, persist=True)
    gate_client = FakeReconClient(_bal(usdt=1000.0), orders=[])
    rep = eng._reconcile_or_refuse(client=gate_client)
    assert rep["action"] == "proceed"
    # gate passed -> _r1_ok would be set by _maybe_gate; set it for the apply step
    eng._r1_ok = True
    eng.anchor = 1.0
    eng.bid, eng.ask = 0.9995, 1.0005
    fake = FakeOrderClient(balance=_bal(usdt=1000.0))
    eng.reconcile_orders(0.0, client=fake)
    assert "place" in fake.kinds()                  # at least one resting BUY placed


# === P1-3 — R1 gate accepts our EXPECTED maker resting orders on restart =====

def test_r1_gate_accepts_expected_maker_orders_on_restart(tmp_path):
    # An armed-maker RESTART with persisted resting sca-* orders must pass the R1 gate:
    # reconcile() is given expected={our link_ids}, so our by-design resting orders are
    # NOT flagged as anomalies. Without wiring `expected`, valid resting orders refuse
    # (SystemExit, D16: code 0) before resume_reconcile_orders ever runs.
    s = _sl("usd1", qty=5000.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=5000.0)
    eng = _armed_engine(tmp_path, maker=True, persist=True, slices=[s])
    eng._resumed = True                               # a restored persisted position
    client = FakeReconClient(
        _bal(usd1=5000.0),
        orders=[{"id": "A0", "clientOrderId": "sca-0-0", "side": "sell",
                 "price": 1.0005, "qty": 5000.0}])    # our resting order, live on the exchange
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed"                 # by-design resting order is not an anomaly


def test_r1_gate_still_refuses_unexpected_order_on_maker_restart(tmp_path):
    # The maker-aware gate must NOT blanket-accept orders: an order whose link is NOT in
    # our expected set (foreign / stale) still refuses, even on the maker path.
    s = _sl("usd1", qty=5000.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=5000.0)
    eng = _armed_engine(tmp_path, maker=True, persist=True, slices=[s])
    eng._resumed = True
    client = FakeReconClient(
        _bal(usd1=5000.0),
        orders=[{"id": "Z9", "clientOrderId": "sca-7-7", "side": "sell",
                 "price": 1.0005, "qty": 5000.0}])    # NOT one of our expected links
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# === run() wiring: resume uses the gate-fetched open-order list (C-P0#5) =====

def _stub_run(eng, monkeypatch, *, fresh_open_orders=None, resume_spy=None):
    """Stub the heavy ``run()`` internals so the WIRING runs without I/O."""
    monkeypatch.setattr(eng, "_compute_maker_enabled", lambda: True)
    monkeypatch.setattr(eng, "_build_order_client", lambda: None)   # keep the injected fake
    monkeypatch.setattr(eng, "_install_signal_handlers", lambda: None)

    def fake_gate():
        eng._r1_ok = True
        eng._r1_open_orders = fresh_open_orders if fresh_open_orders is not None else []
        eng._r1_report = {"action": "proceed"}
    monkeypatch.setattr(eng, "_maybe_gate", fake_gate)
    monkeypatch.setattr(eng, "bootstrap", lambda: None)
    monkeypatch.setattr(eng, "flush_markout", lambda *a, **k: None)
    monkeypatch.setattr(eng, "accrue", lambda *a, **k: None)
    monkeypatch.setattr(eng, "print_summary", lambda *a, **k: None)
    monkeypatch.setattr(eng, "write_status", lambda *a, **k: str(eng.out_dir))
    if resume_spy is not None:
        monkeypatch.setattr(eng, "resume_reconcile_orders", resume_spy)
    monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    eng.csv_path = None                              # skip _write_csv
    eng.start = engine_mod.time.time() - 10_000.0    # t_end already in the past
    eng.seconds = 1


def test_resume_uses_gate_fetched_open_orders(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, slices=[])
    fake = FakeOrderClient()
    eng.order_client = fake
    sentinel = [{"id": "G1", "clientOrderId": "sca-9-9"}]
    seen = {}
    monkeypatch.setattr(eng, "resume_reconcile_orders",
                        lambda oo, client=None, now=None: seen.update(oo=oo))
    _stub_run(eng, monkeypatch, fresh_open_orders=sentinel,
              resume_spy=lambda oo, client=None, now=None: seen.update(oo=oo))
    import asyncio
    asyncio.run(eng.run())
    assert seen["oo"] is sentinel                    # SAME list, no refetch


# === A10 — kill switch: any exit cancels ALL resting orders (F12) ===========

def test_run_exit_cancels_all_resting_orders(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, slices=[
        _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0),
        _sl("usdt", cash=10.0, order_id="B0", order_link_id="sca-1-0",
            order_side="buy", order_px=1.0, order_qty=10.0),
    ])
    fake = FakeOrderClient()
    eng.order_client = fake
    _stub_run(eng, monkeypatch,
              resume_spy=lambda oo, client=None, now=None: None)   # skip resume mutation
    import asyncio
    asyncio.run(eng.run())
    cancelled = [c for c in fake.calls if c[0] == "cancel"]
    assert {c[1] for c in cancelled} == {"A0", "B0"}              # both resting orders cancelled


def test_cancel_all_runs_on_startup_failure_after_client_built(tmp_path, monkeypatch):
    # P1-4: a halt/exception during STARTUP (gate / bootstrap / resume) AFTER the order
    # client is built must STILL cancel every persisted resting order. The cancel-all
    # finally must wrap the WHOLE maker startup — not just the recv loop — else a halt
    # during resume leaves resting orders dangling on the exchange.
    eng = _mk_engine(tmp_path, slices=[
        _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0),
    ])
    fake = FakeOrderClient()
    eng.order_client = fake

    def boom(oo, client=None, now=None):
        raise RuntimeError("resume blew up during startup")

    _stub_run(eng, monkeypatch, resume_spy=boom)
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(eng.run())
    assert ("cancel", "A0", "sca-0-0") in fake.calls   # cancelled despite the startup failure


def test_sigterm_triggers_cancel_all(tmp_path):
    eng = _mk_engine(tmp_path, slices=[
        _sl("usd1", qty=10.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=10.0),
    ])
    fake = FakeOrderClient()
    eng.order_client = fake
    with pytest.raises(KeyboardInterrupt):
        eng._on_exit_signal(signal.SIGTERM, None)
    assert ("cancel", "A0", "sca-0-0") in fake.calls


def test_install_signal_handlers_registers_sigint_sigterm(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    registered = {}
    monkeypatch.setattr(engine_mod.signal, "signal",
                        lambda sig, handler: registered.__setitem__(sig, handler))
    eng._install_signal_handlers()
    assert registered[signal.SIGINT] == eng._on_exit_signal
    assert registered[signal.SIGTERM] == eng._on_exit_signal


# === F22 — fresh_deploy stays UNCONDITIONALLY refused =======================

def test_fresh_deploy_still_refused_on_testnet_and_mainnet(tmp_path, monkeypatch):
    # maker OFF so seeding does not convert it to proceed: reconcile() returns
    # fresh_deploy (clean + opt-in + declaration) and the engine must still REFUSE,
    # regardless of the testnet venue.
    for venue in ("true", "false"):
        monkeypatch.setenv("BYBIT_TESTNET", venue)
        eng = _armed_engine(tmp_path, maker=False, allow_fresh=True,
                            expect_asset="USDT", expect_amount=10000.0)
        client = FakeReconClient(_bal(usdt=10000.0), orders=[])
        with pytest.raises(SystemExit):
            eng._reconcile_or_refuse(client=client)


# === D14 — both clients build MAINNET (no split-brain, no venue gate) =======

def test_both_clients_build_mainnet_no_split_brain(tmp_path, monkeypatch):
    # D14: live == real MAINNET. The maker client takes NO testnet/venue arg (mainnet
    # always); the R1 read-client builds with testnet=False. Same venue, no split-brain.
    captured = {}

    class RecMaker:
        def __init__(self, *a, **k):
            captured["maker_args"] = (a, k)

    class RecBybit:
        def __init__(self, testnet=None, **k):
            captured["bybit_testnet"] = testnet

        def get_wallet_balance(self):
            return _bal(usdt=1000.0)

        def get_open_orders(self, symbol=None):
            return []

    import sca.live.orders as orders_mod
    monkeypatch.setattr(orders_mod, "MakerOrderClient", RecMaker, raising=False)
    monkeypatch.setattr(bybit_client, "BybitPrivateClient", RecBybit)

    eng = _armed_engine(tmp_path, maker=True, persist=True)
    eng._build_order_client()
    eng._reconcile_or_refuse(client=None)            # builds RecBybit(testnet=False)
    assert captured["maker_args"] == ((), {})        # maker client: mainnet, no venue arg
    assert captured["bybit_testnet"] is False        # R1 read-client: mainnet


# === maker path switch == live mode (D14) ===================================

def test_maker_enabled_iff_armed(tmp_path):
    # D14: the maker (real-order) path switch is EXACTLY self.armed (== live mode).
    # No separate venue gate or rollback knob.
    eng = _mk_engine(tmp_path, maker=False, r1=False)
    eng.armed = True
    assert eng._compute_maker_enabled() is True
    eng.armed = False                                # dryrun -> off
    assert eng._compute_maker_enabled() is False


def test_maker_enabled_off_falls_back_to_paper_path(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, maker=False, r1=False, slices=[_sl("usd1", qty=10.0)])
    calls = []
    monkeypatch.setattr(eng, "_maybe_deploy", lambda: calls.append("deploy"))
    monkeypatch.setattr(eng, "evaluate_fills", lambda now: calls.append("fills"))
    eng._handle(_ob_msg(), 0.0)
    assert calls == ["deploy", "fills"]              # paper evaluate_fills path


# === dryrun never builds the order client (real-money safety) ===============

def test_dryrun_mode_never_builds_order_client_still_simulates(tmp_path, monkeypatch):
    import sca.live.orders as orders_mod

    def boom(*a, **k):
        raise AssertionError("dryrun must never construct MakerOrderClient")
    monkeypatch.setattr(orders_mod, "MakerOrderClient", boom, raising=False)

    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng.armed is False
    assert eng._compute_maker_enabled() is False     # dryrun -> off
    eng.maker_enabled = False
    eng.deployed = True
    eng.slices = [_sl("usd1", qty=10.0)]
    calls = []
    monkeypatch.setattr(eng, "evaluate_fills", lambda now: calls.append("fills"))
    eng._handle(_ob_msg(), 0.0)                      # still simulates via the sim-fill path
    assert calls == ["fills"]
    assert eng.order_client is None
