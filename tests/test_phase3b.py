"""Phase 3b — mainnet enablement (dual confirm) + total-alloc canary cap +
per-order -1 unlimited + max-loss kill-switch + live-path markout verification.

3b is a tight DELTA on the merged 3a maker layer: a GATE + CONFIG + SIZING-CAP +
KILL-SWITCH change ONLY. It does NOT touch the maker order-lifecycle logic
(reconcile / poll / cancel-to-terminal / persistence) hardened in 3a
(feedback_multi_mode_parity).

Invariants pinned here (plan docs/phase3b-mainnet-canary-plan.md):
  A. mainnet needs BOTH runtime.allow_mainnet AND env LIVE_MAINNET_CONFIRM=yes;
     testnet/paper default path unchanged.
  B. total-alloc cap enforced in the SIZING path (seed + available pool), not just
     stored (arb-execution-risk); -1 => full wallet.
  C. per-order max_order_usd -1 => no cap (clamp + place assert skipped); finite still
     enforced.
  D. max-loss kill-switch: drawdown >= max_loss_usd => cancel-ALL (cancel-to-terminal)
     + halt + refuse further placement; 0/-1 disabled; boundary; partial-fill aware.
  E. live fills feed the markout gauge; status surfaces markout.

ISOLATION: pure / no network / no disk. Real PaperEngine in paper mode (out_dir ->
tmp_path); maker live fields set DIRECTLY (the documented test seam); FakeOrderClient
records calls + returns canned states.

Run: PYTHONPATH=src python3 -m pytest tests/test_phase3b.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pytest  # noqa: E402

from sca import config  # noqa: E402


# ===========================================================================
# A. config.resolve_allow_mainnet — DUAL confirm (T1)
# ===========================================================================

def test_resolve_allow_mainnet_requires_both_config_and_env():
    # both present -> allowed
    assert config.resolve_allow_mainnet(cfg={"runtime": {"allow_mainnet": True}},
                                        env={"LIVE_MAINNET_CONFIRM": "yes"}) is True


def test_resolve_allow_mainnet_refused_without_config_flag():
    # env confirm alone is NOT enough — the config opt-in must also be true
    assert config.resolve_allow_mainnet(cfg={"runtime": {"allow_mainnet": False}},
                                        env={"LIVE_MAINNET_CONFIRM": "yes"}) is False
    assert config.resolve_allow_mainnet(cfg={"runtime": {}},
                                        env={"LIVE_MAINNET_CONFIRM": "yes"}) is False


def test_resolve_allow_mainnet_refused_without_env_confirm():
    # config flag alone is NOT enough — the NEW env confirm must also equal "yes"
    assert config.resolve_allow_mainnet(cfg={"runtime": {"allow_mainnet": True}},
                                        env={}) is False
    # a non-"yes" value (incl. the 3a LIVE_TRADING_CONFIRM by mistake) does not arm it
    assert config.resolve_allow_mainnet(cfg={"runtime": {"allow_mainnet": True}},
                                        env={"LIVE_MAINNET_CONFIRM": "true"}) is False
    assert config.resolve_allow_mainnet(cfg={"runtime": {"allow_mainnet": True}},
                                        env={"LIVE_TRADING_CONFIRM": "yes"}) is False


def test_resolve_allow_mainnet_default_false():
    # neither set -> default refused (mainnet stays off)
    assert config.resolve_allow_mainnet(cfg={}, env={}) is False


def test_resolve_allow_mainnet_precedence_distinct_from_testnet():
    # allow_mainnet is INDEPENDENT of testnet: a True allow_mainnet does not flip testnet
    # and vice-versa (they are two distinct resolvers / two distinct confirms).
    cfg = {"runtime": {"allow_mainnet": True, "testnet": False}}
    env = {"LIVE_MAINNET_CONFIRM": "yes"}
    assert config.resolve_allow_mainnet(cfg=cfg, env=env) is True
    assert config.resolve_testnet(cfg=cfg, env=env) is False     # unchanged


# ===========================================================================
# C. per-order max_order_usd == -1 => no cap (pure clamp + client assert)  (T2)
# ===========================================================================

from sca.live import order_recon as orec  # noqa: E402


def _orec_slice(state, qty=0.0, cash=0.0):
    return {"state": state, "qty": qty, "cash": cash, "sell_px": 0.0, "entry": None,
            "order_link_id": None, "order_id": None, "order_side": None, "order_px": None}


def test_clamp_minus1_no_cap():
    # -1 => the per-order cap is removed: qty is returned unclamped
    assert orec._clamp_to_cap(100.0, 1.0, 0.001, -1) == pytest.approx(100.0)


def test_clamp_finite_cap_still_enforced():           # regression
    # a positive cap still clamps qty so qty*px <= cap
    out = orec._clamp_to_cap(100.0, 1.0, 0.001, 10.0)
    assert out * 1.0 <= 10.0 + 1e-9
    assert out == pytest.approx(10.0)


def test_desired_minus1_no_per_order_cap():
    # end-to-end: with max_order_usd=-1 desired_orders sizes the FULL slice want
    # (bounded only by the avail pool), never clamped by a per-order cap.
    out = orec.desired_orders(1.0000, [_orec_slice("usd1", qty=100.0)], rungs=[5],
                              rebuy_off_bp=-1, tick=0.0001, lot=0.001,
                              avail_base=100.0, avail_quote=0.0, min_qty=0.001,
                              min_cost=1.0, max_order_usd=-1)
    assert out[0].qty == pytest.approx(100.0)         # full want, no clamp


# ===========================================================================
# A/C. MakerOrderClient mainnet construction + place gating + -1 assert  (T2)
# ===========================================================================

from sca.live import orders as om  # noqa: E402

_OC_CFG = {"api_key_env": "K", "api_secret_env": "S", "confirm_env": "C", "max_order_usd": 2000}
_OC_ENV = {"K": "key-123", "S": "secret-456", "C": "yes"}


class _FakeEx:
    def __init__(self, config):
        self.config = config
        self.sandbox = None
        self.calls = []

    def set_sandbox_mode(self, v):
        self.sandbox = v
        self.calls.append(("set_sandbox_mode", v))

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", amount, price))
        return {"id": "ord1", "clientOrderId": params.get("clientOrderId") if params else None,
                "side": side, "price": price, "amount": amount, "filled": 0.0,
                "remaining": amount, "average": None, "status": "open", "info": {}}


class _FakeCcxt:
    def __init__(self):
        self.last = None

    def bybit(self, config):
        self.last = _FakeEx(config)
        return self.last


def _mk_client(*, testnet=True, allow_mainnet=False, max_order_usd=2000):
    fake = _FakeCcxt()
    cfg = dict(_OC_CFG, max_order_usd=max_order_usd)
    client = om.MakerOrderClient(ccxt_module=fake, live_cfg=cfg, env=_OC_ENV,
                                 testnet=testnet, allow_mainnet=allow_mainnet)
    client._sleep = lambda *_a, **_k: None
    return client, fake.last


def test_maker_client_constructs_on_mainnet_with_allow_mainnet():
    # 3b — mainnet construction is permitted ONLY with the injected allow_mainnet opt-in;
    # the live venue means sandbox OFF.
    client, ex = _mk_client(testnet=False, allow_mainnet=True)
    assert client.testnet is False
    assert client.allow_mainnet is True
    assert ex.sandbox is False                        # mainnet -> NOT sandbox


def test_maker_client_still_refuses_mainnet_without_allow_mainnet():
    # 3a behavior preserved as the default: un-opted-in mainnet hard-raises at construction.
    fake = _FakeCcxt()
    with pytest.raises(RuntimeError):
        om.MakerOrderClient(ccxt_module=fake, live_cfg=_OC_CFG, env=_OC_ENV,
                            testnet=False, allow_mainnet=False)


def test_maker_client_testnet_unchanged_sandbox_on():  # regression
    client, ex = _mk_client(testnet=True)
    assert client.testnet is True and ex.sandbox is True


def test_place_postonly_minus1_skips_assert():
    # max_order_usd = -1 => the hard notional assert is skipped (no per-order cap).
    client, ex = _mk_client(testnet=True, max_order_usd=-1)
    client.place_postonly("USD1/USDT", "buy", 1.0, 1_000_000.0, "sca-0-0")  # huge, no raise
    assert any(c[0] == "create_order" for c in ex.calls)     # reached the exchange


def test_place_postonly_finite_cap_still_asserts():   # regression
    client, ex = _mk_client(testnet=True, max_order_usd=2000)
    with pytest.raises(AssertionError):
        client.place_postonly("USD1/USDT", "buy", 1.0, 2500.0, "sca-1-0")
    assert not any(c[0] == "create_order" for c in ex.calls)  # never reached the exchange


def test_place_postonly_refused_on_mainnet_without_allow():
    # mainnet venue + no allow_mainnet opt-in => place refuses (independent 2nd layer).
    client, ex = _mk_client(testnet=True)
    client.testnet = False                            # flip venue, allow_mainnet stays False
    with pytest.raises(RuntimeError):
        client.place_postonly("USD1/USDT", "buy", 1.0, 10.0, "sca-2-0")
    assert not any(c[0] == "create_order" for c in ex.calls)


def test_place_postonly_allowed_on_mainnet_with_allow():
    client, ex = _mk_client(testnet=False, allow_mainnet=True)
    client.place_postonly("USD1/USDT", "buy", 1.0, 10.0, "sca-3-0")
    assert any(c[0] == "create_order" for c in ex.calls)     # opt-in -> placement proceeds


# ===========================================================================
# Engine fixtures (T3) — real PaperEngine in paper mode; maker fields set DIRECTLY
# ===========================================================================

from sca.live import engine as engine_mod  # noqa: E402
from sca.live.engine import (  # noqa: E402
    PaperEngine, OperatorReconcileHalt, HORIZONS, aggregate_markout,
)

_ENG_ORDER_DEFAULTS = dict(order_id=None, order_link_id=None, order_px=None,
                           order_side=None, order_qty=None, filled_qty=0.0,
                           order_gen=0, reject_streak=0, sell_proceeds=0.0, qty_sold=0.0)


def _sl(state, qty=0.0, cash=0.0, **over):
    s = {"state": state, "qty": qty, "cash": cash, "sell_px": 0.0, "entry": None}
    s.update(_ENG_ORDER_DEFAULTS)
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

    def __init__(self, *, balance=None, max_order_usd=2000.0, state_results=None):
        self._balance = balance if balance is not None else _bal()
        self.max_order_usd = max_order_usd
        self._state_results = state_results or {}
        self.calls = []

    def market_meta(self, symbol):
        self.calls.append(("market_meta", symbol))
        return {"tick": 0.0001, "lot": 0.000001, "min_qty": 0.0, "min_cost": 0.0}

    def fetch_open(self, symbol):
        self.calls.append(("fetch_open", symbol))
        return []

    def balance(self):
        self.calls.append(("balance",))
        return self._balance

    def place_postonly(self, symbol, side, price, qty, link_id):
        self.calls.append(("place", side, price, qty, link_id))
        return _state("open", oid=f"oid-{link_id}", link=link_id, side=side,
                      remaining=qty, price=price)

    def cancel(self, symbol, order_id, *, link_id=None):
        self.calls.append(("cancel", order_id, link_id))
        return _state("open", oid=order_id, link=link_id)

    def fetch_order_state(self, symbol, order_id=None, *, link_id=None):
        self.calls.append(("fetch_state", order_id, link_id))
        key = link_id if link_id is not None else order_id
        return self._state_results.get(key) or _state("cancelled", oid=order_id, link=link_id)

    def kinds(self):
        return [c[0] for c in self.calls]


def _mk_engine(tmp_path, *, slices=None, maker=True, anchor=1.0, bid=None, ask=None,
               last=None, persist=False):
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.persist = persist
    eng.maker_enabled = maker
    eng._r1_ok = True
    eng._sleep = lambda *a, **k: None
    eng.anchor = anchor
    eng.bid, eng.ask, eng.last = bid, ask, last
    eng.deployed = True
    if slices is not None:
        eng.slices = slices
    return eng


def _armed_engine(tmp_path, *, maker=True, persist=True):
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True
    eng.maker_enabled = maker
    eng.persist = persist
    eng._sleep = lambda *a, **k: None
    eng.slices = []
    return eng


# ===========================================================================
# A. engine mainnet gate — _compute_maker_enabled + client construction  (T3)
# ===========================================================================

def test_compute_maker_enabled_mainnet_with_dual_confirm(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, maker=False)
    eng.armed = True
    monkeypatch.setattr(engine_mod, "_resolve_maker_enabled", lambda *a, **k: True)
    monkeypatch.setattr(engine_mod, "_resolve_testnet", lambda *a, **k: False)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: True)
    assert eng._compute_maker_enabled() is True            # mainnet allowed via dual confirm


def test_compute_maker_enabled_mainnet_refused_without_allow(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, maker=False)
    eng.armed = True
    monkeypatch.setattr(engine_mod, "_resolve_maker_enabled", lambda *a, **k: True)
    monkeypatch.setattr(engine_mod, "_resolve_testnet", lambda *a, **k: False)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: False)
    assert eng._compute_maker_enabled() is False           # neither testnet nor mainnet opt-in


def test_compute_maker_enabled_testnet_path_unchanged(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, maker=False)
    eng.armed = True
    monkeypatch.setattr(engine_mod, "_resolve_maker_enabled", lambda *a, **k: True)
    monkeypatch.setattr(engine_mod, "_resolve_testnet", lambda *a, **k: True)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: False)
    assert eng._compute_maker_enabled() is True            # testnet still enables (additive)


def test_build_order_client_passes_allow_mainnet(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path, maker=True)
    eng.order_client = None
    monkeypatch.setattr(engine_mod, "_resolve_testnet", lambda *a, **k: False)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: True)
    captured = {}

    class RecMaker:
        def __init__(self, *, testnet=None, allow_mainnet=None, **k):
            captured["testnet"] = testnet
            captured["allow_mainnet"] = allow_mainnet

    import sca.live.orders as orders_mod
    monkeypatch.setattr(orders_mod, "MakerOrderClient", RecMaker, raising=False)
    eng._build_order_client()
    assert captured == {"testnet": False, "allow_mainnet": True}


# ===========================================================================
# B. total-alloc canary cap — enforced in the SIZING path  (T3)
# ===========================================================================

def test_total_alloc_caps_deployment_below_wallet(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal(usdt=10000.0), open_orders=[])
    assert all(s["state"] == "usdt" for s in eng.slices)
    assert sum(s["cash"] for s in eng.slices) == pytest.approx(300.0)   # capped, NOT 10000


def test_total_alloc_minus1_uses_full_wallet(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = -1.0
    eng._seed_slices_from_balance(_bal(usdt=10000.0), open_orders=[])
    assert sum(s["cash"] for s in eng.slices) == pytest.approx(10000.0)  # full wallet (no cap)


def test_total_alloc_caps_usd1_funded_side(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal(usd1=10000.0), open_orders=[])
    assert all(s["state"] == "usd1" for s in eng.slices)
    assert sum(s["qty"] for s in eng.slices) == pytest.approx(300.0)     # base side capped too


def test_reconcile_respects_total_alloc_budget(tmp_path):
    eng = _mk_engine(tmp_path)
    eng._max_total_alloc_usd = 300.0
    _ab, avail_quote = eng._available_from_balance(_bal(usdt=10000.0), {})
    assert avail_quote == pytest.approx(300.0)            # full free wallet bounded by the cap
    eng._max_total_alloc_usd = -1.0
    _ab2, q2 = eng._available_from_balance(_bal(usdt=10000.0), {})
    assert q2 == pytest.approx(10000.0)                   # -1 => full pool (3a behavior)


# ===========================================================================
# D. max-loss kill-switch  (T3)
# ===========================================================================

def test_max_loss_halts_and_cancels_all(tmp_path):
    s = _sl("usd1", qty=1000.0, entry=1.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=1000.0)
    eng = _mk_engine(tmp_path, slices=[s], last=0.94)     # px=0.94 -> equity=940
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 50.0
    eng._start_equity = 1000.0
    with pytest.raises(OperatorReconcileHalt):            # loss=60 >= 50
        eng._check_max_loss(0.0)
    assert eng._halted is True
    # cancelled via cancel-to-terminal (the 3a-safe path), NOT a blind cancel
    assert ("cancel", "A0", "sca-0-0") in eng.order_client.calls


def test_max_loss_disabled_when_zero(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=1000.0)], last=0.5)  # huge drawdown
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 0.0                                # disabled
    eng._start_equity = 1000.0
    eng._check_max_loss(0.0)                               # no raise
    assert eng._halted is False


def test_loss_just_under_threshold_no_halt(tmp_path):
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=1000.0)], last=0.951)  # loss=49
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 50.0
    eng._start_equity = 1000.0
    eng._check_max_loss(0.0)                               # 49 < 50 -> no halt
    assert eng._halted is False
    # boundary: loss == cap exactly -> halt (>=)
    eng2 = _mk_engine(tmp_path, slices=[_sl("usd1", qty=1000.0)], last=0.950)  # loss=50
    eng2.order_client = FakeOrderClient()
    eng2._max_loss_usd = 50.0
    eng2._start_equity = 1000.0
    with pytest.raises(OperatorReconcileHalt):
        eng2._check_max_loss(0.0)


def test_max_loss_partial_fill_equity_partial_aware(tmp_path):
    # A MID-PARTIAL slice holds BOTH qty(base) AND cash(proceeds); equity must value both
    # (reuse _slice_value). qty=500, cash=500, start=1000, cap=60.
    def mk(px):
        s = _sl("usd1", qty=500.0, cash=500.0, order_id="A0", order_link_id="sca-0-0",
                order_side="sell", order_px=1.0005, order_qty=1000.0, filled_qty=500.0)
        e = _mk_engine(tmp_path, slices=[s], last=px)
        e.order_client = FakeOrderClient()
        e._max_loss_usd = 60.0
        e._start_equity = 1000.0
        return e
    # px=1.0: equity = 500*1 + 500 = 1000 -> loss 0 -> NO halt.
    # (a base-ONLY equity would be 500 -> loss 500 -> false halt; the cash leg saves it)
    e1 = mk(1.0)
    e1._check_max_loss(0.0)
    assert e1._halted is False
    # px=0.80: equity = 500*0.8 + 500 = 900 -> loss 100 >= 60 -> halt.
    # (the base leg moved equity; a cash-ONLY equity is constant 500 and could not)
    with pytest.raises(OperatorReconcileHalt):
        mk(0.80)._check_max_loss(0.0)


def test_max_loss_anchors_start_equity_on_first_step(tmp_path):
    # _start_equity is lazily anchored to the CURRENT equity on the first markable step
    # (NOT to config alloc=10000), so the canary does not false-halt at startup.
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=1000.0)], last=0.5)
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 50.0
    assert eng._start_equity is None
    eng._check_max_loss(0.0)                               # anchors baseline; does NOT halt
    assert eng._halted is False
    assert eng._start_equity == pytest.approx(500.0)       # = 1000 * 0.5 (current mark)


def test_halt_blocks_further_placement(tmp_path):
    eng = _mk_engine(tmp_path, slices=[
        _sl("usd1", qty=1000.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=1000.0)])
    eng.order_client = FakeOrderClient()
    eng._halted = True                                     # a prior halt is active
    with pytest.raises(OperatorReconcileHalt):
        eng.maker_step(0.0)
    assert not any(c[0] == "place" for c in eng.order_client.calls)   # never re-placed


def test_maker_step_checks_loss_between_poll_and_reconcile(tmp_path, monkeypatch):
    eng = _mk_engine(tmp_path)
    order = []
    monkeypatch.setattr(eng, "poll_fills", lambda now, client=None: order.append("poll"))
    monkeypatch.setattr(eng, "_check_max_loss", lambda now, client=None: order.append("loss"))
    monkeypatch.setattr(eng, "reconcile_orders", lambda now, client=None: order.append("recon"))
    eng.maker_step(0.0)
    # loss check sits AFTER fills are booked, BEFORE new placement (pre-trade, atomic)
    assert order == ["poll", "loss", "recon"]


# ===========================================================================
# E. markout on the live path  (T4)
# ===========================================================================

def test_live_fill_feeds_markout_gauge(tmp_path):
    # The canary's PURPOSE is to measure adverse selection: a public trade on the MAKER
    # (live) path must STILL feed the markout gauge (not bypassed by maker_enabled).
    eng = _mk_engine(tmp_path, maker=True, bid=1.0, ask=1.0010)
    eng._push_mid(0.0)                                     # seed a mid at t=0
    eng._handle({"topic": "publicTrade.USD1USDT",
                 "data": [{"p": "1.0006", "S": "Buy"}]}, 0.0)
    assert len(eng.pending) == 1                           # passive SELL@ask recorded
    eng.bid, eng.ask = 1.0008, 1.0018                      # mid moved
    eng._push_mid(max(HORIZONS) + 1)
    eng.flush_markout(max(HORIZONS) + 1)
    assert len(eng.done) == 1                              # matured into done
    agg = aggregate_markout(eng.done, eng.spreads)
    assert agg["n_sell"] == 1                              # the live fill is measured


def test_status_includes_markout(tmp_path):
    eng = _mk_engine(tmp_path, maker=True)
    doc = eng.status_doc(0.0)
    assert "markout" in doc
    assert set(doc["markout"].keys()) == {str(h) for h in HORIZONS}
    assert "n_buy" in doc and "n_sell" in doc and "avg_spread_bp" in doc


# ===========================================================================
# P0 — max-loss / operator HALT must PERSIST across restart (the most critical)
# An auto-restart (docker restart: unless-stopped) must NEVER silently resume a
# halted real-money bot, and must NOT re-anchor the loss baseline to a shrunk
# equity (which would reset the drawdown budget -> a fresh -max_loss bleed loop).
# ===========================================================================

from sca.live.persistence import load_state as _load_state  # noqa: E402


def test_state_dict_carries_halt_and_start_equity(tmp_path):
    # additive snapshot fields (still schema v=2): a halted/anchored engine serialises
    # both so the snapshot is self-describing across a restart.
    eng = _mk_engine(tmp_path)
    eng._halted = True
    eng._start_equity = 1234.5
    doc = eng._state_dict()
    assert doc["v"] == 2                                   # NOT bumped (additive .get on read)
    assert doc["halted"] is True
    assert doc["start_equity"] == pytest.approx(1234.5)


def test_halt_persists_and_resume_refuses(tmp_path):
    # inject a loss -> max-loss halt (persist=True) -> the halt is DURABLE on disk;
    # a NEW engine reloading it stays halted, does NOT re-anchor the baseline, and
    # maker_step refuses all further placement.
    s = _sl("usd1", qty=1000.0, entry=1.0, order_id="A0", order_link_id="sca-0-0",
            order_side="sell", order_px=1.0005, order_qty=1000.0)
    eng = _mk_engine(tmp_path, slices=[s], last=0.94, persist=True)   # equity 940
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 50.0
    eng._start_equity = 1000.0
    with pytest.raises(OperatorReconcileHalt):             # loss 60 >= 50 -> halt
        eng._check_max_loss(0.0)

    on_disk = _load_state(str(tmp_path), "USD1USDT")
    assert on_disk["halted"] is True                       # halt durable (survives restart)
    assert on_disk["start_equity"] == pytest.approx(1000.0)

    eng2 = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                       csv_path=str(tmp_path / "out.csv"))  # reload (persist default ON)
    eng2.maker_enabled = True
    eng2._r1_ok = True
    eng2._sleep = lambda *a, **k: None
    eng2.order_client = FakeOrderClient()
    assert eng2._resumed is True
    assert eng2._halted is True                            # halt restored
    assert eng2._start_equity == pytest.approx(1000.0)     # baseline NOT re-anchored to 940
    with pytest.raises(OperatorReconcileHalt):
        eng2.maker_step(0.0)                               # refuses further maker activity
    assert not any(c[0] == "place" for c in eng2.order_client.calls)


def test_resume_clear_halt_requires_explicit_action(tmp_path):
    # _guard_resumed_halt: a restored halt LOUDLY refuses to (re)enter the maker path
    # unless the operator explicitly clears it. Clearing re-anchors the baseline so the
    # cleared run does not instantly re-halt against the old (higher) start_equity.
    eng = _mk_engine(tmp_path, maker=True)
    eng._halted = True
    eng._start_equity = 1000.0
    with pytest.raises(SystemExit):
        eng._guard_resumed_halt(env={})                    # no explicit clear -> refuse
    assert eng._halted is True                             # refusal does not silently clear
    eng._guard_resumed_halt(env={"LIVE_CLEAR_HALT": "yes"})  # explicit clear -> ok
    assert eng._halted is False
    assert eng._start_equity is None                       # baseline re-anchored


def test_guard_resumed_halt_noop_when_not_halted(tmp_path):
    # a healthy resume (not halted) is never refused.
    eng = _mk_engine(tmp_path, maker=True)
    eng._halted = False
    eng._guard_resumed_halt(env={})                        # no raise


def test_resume_defaults_when_old_state_lacks_halt_fields(tmp_path):
    # an OLD v=2 snapshot written before P0 (no halt/start_equity keys) must resume with
    # safe defaults (_halted False, _start_equity None) — additive read, no fresh-start.
    from sca.live.persistence import save_state
    a = _mk_engine(tmp_path, slices=[_sl("usd1", qty=100.0, entry=1.0)], persist=True)
    doc = a._state_dict()
    doc.pop("halted", None)                                # simulate a pre-P0 snapshot
    doc.pop("start_equity", None)
    save_state(str(tmp_path), "USD1USDT", doc)
    b = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                    csv_path=str(tmp_path / "out.csv"))
    assert b._resumed is True                              # still resumes (NOT a fresh start)
    assert b._halted is False
    assert b._start_equity is None


# ===========================================================================
# P1 — mainnet canary guardrails: real money on mainnet MUST run with a max-loss
# kill-switch AND a finite alloc cap; an uncapped alloc needs a 3rd explicit env.
# (max_loss has NO exemption.) testnet/paper are unaffected.
# ===========================================================================

def _canary_eng(tmp_path, monkeypatch, *, allow_mainnet, max_loss, max_alloc):
    eng = _mk_engine(tmp_path, maker=True)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: allow_mainnet)
    eng._max_loss_usd = max_loss
    eng._max_total_alloc_usd = max_alloc
    return eng


def test_mainnet_refuses_without_max_loss(tmp_path, monkeypatch):
    eng = _canary_eng(tmp_path, monkeypatch, allow_mainnet=True, max_loss=0.0, max_alloc=300.0)
    with pytest.raises(SystemExit):
        eng._guard_mainnet_canary(env={})                  # kill-switch off on mainnet -> refuse


def test_mainnet_refuses_uncapped_alloc_without_confirm(tmp_path, monkeypatch):
    eng = _canary_eng(tmp_path, monkeypatch, allow_mainnet=True, max_loss=50.0, max_alloc=-1.0)
    with pytest.raises(SystemExit):
        eng._guard_mainnet_canary(env={})                  # -1 alloc + no LIVE_UNCAPPED_CONFIRM


def test_mainnet_allows_uncapped_alloc_with_confirm(tmp_path, monkeypatch):
    eng = _canary_eng(tmp_path, monkeypatch, allow_mainnet=True, max_loss=50.0, max_alloc=-1.0)
    eng._guard_mainnet_canary(env={"LIVE_UNCAPPED_CONFIRM": "yes"})   # 3rd confirm -> ok


def test_mainnet_ok_with_finite_caps(tmp_path, monkeypatch):
    eng = _canary_eng(tmp_path, monkeypatch, allow_mainnet=True, max_loss=50.0, max_alloc=300.0)
    eng._guard_mainnet_canary(env={})                      # max-loss armed + finite cap -> ok


def test_testnet_no_canary_guard(tmp_path, monkeypatch):
    # off-mainnet (testnet/paper) the guard is INERT — the shipped defaults (max_loss=0,
    # alloc=-1) stay legal there (provably zero behaviour change off mainnet).
    eng = _canary_eng(tmp_path, monkeypatch, allow_mainnet=False, max_loss=0.0, max_alloc=-1.0)
    eng._guard_mainnet_canary(env={})                      # no raise


# ===========================================================================
# P1 — startup banner must reflect the RESOLVED venue (never hard-code TESTNET)
# ===========================================================================

def test_banner_says_mainnet_on_mainnet(tmp_path, monkeypatch, capsys):
    eng = _mk_engine(tmp_path, maker=True)
    eng.order_client = None
    eng._max_total_alloc_usd = 300.0
    eng._max_loss_usd = 50.0
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: True)
    eng._maker_startup_banner()
    out = capsys.readouterr().out
    assert "MAINNET" in out and "REAL-MONEY" in out        # loud real-money warning
    assert "TESTNET" not in out
    assert "300" in out and "50" in out                    # effective caps surfaced


def test_banner_says_testnet_default(tmp_path, monkeypatch, capsys):
    eng = _mk_engine(tmp_path, maker=True)
    monkeypatch.setattr(engine_mod, "_resolve_allow_mainnet", lambda *a, **k: False)
    eng._maker_startup_banner()
    out = capsys.readouterr().out
    assert "TESTNET" in out
    assert "REAL-MONEY" not in out                         # testnet wording unchanged


# ===========================================================================
# P2 — incidental correctness + coverage (kill surviving qa mutants)
# ===========================================================================

def test_max_loss_excludes_settled_interest(tmp_path):
    # max-loss must reflect PURE trading / markout drawdown — accrued carry
    # (settled_interest) must NOT offset a real position loss (else yield masks the bleed).
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=1000.0)], last=0.94)   # slices = 940
    eng.order_client = FakeOrderClient()
    eng._max_loss_usd = 50.0
    eng._start_equity = 1000.0
    eng.interest.settled = 100.0          # a big carry that WOULD mask the loss if included
    # equity = 940 (slices only); loss = 60 >= 50 -> halt.
    # (if interest were added: 940 + 100 = 1040 > 1000 -> negative loss -> NO halt)
    with pytest.raises(OperatorReconcileHalt):
        eng._check_max_loss(0.0)
    assert eng._halted is True


def _bal_offpeg(coin, amt, usd):
    """A single-side balance where the coin is OFF its $1 peg: wallet=amt, usd=usd
    (=> mark = usd/amt). The other side is dust-free."""
    other = "USDT" if coin == "USD1" else "USD1"
    return {
        "account_type": "UNIFIED",
        "totals": {"equity_usd": usd, "available_usd": usd, "wallet_usd": usd,
                   "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
        "coins": {
            coin: {"wallet": amt, "locked": 0.0, "free": amt, "usd": usd, "borrow": 0.0},
            other: {"wallet": 0.0, "locked": 0.0, "free": 0.0, "usd": 0.0, "borrow": 0.0},
        },
    }


def test_total_alloc_cap_offpeg_mark(tmp_path):
    # the cap is in USD: an off-peg coin (mark 0.98) deploys cap/mark units, NOT cap units.
    # (a mutant that ignores the mark and seeds `cap` directly would give 300, not 300/0.98)
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal_offpeg("USDT", 10000.0, 9800.0), open_orders=[])
    assert all(s["state"] == "usdt" for s in eng.slices)
    assert sum(s["cash"] for s in eng.slices) == pytest.approx(300.0 / 0.98)   # NOT 300


def test_total_alloc_cap_base_side_usd1(tmp_path):
    # the USD1 (base) seed branch must value USD1 at ITS OWN usd mark. A mutant reading the
    # WRONG coin's usd (-> 0 -> $1 fallback -> 300) survives a peg=1 test but dies off-peg:
    # correct deployable = 300/0.98.
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal_offpeg("USD1", 10000.0, 9800.0), open_orders=[])
    assert all(s["state"] == "usd1" for s in eng.slices)
    assert sum(s["qty"] for s in eng.slices) == pytest.approx(300.0 / 0.98)    # NOT 300


def test_cancel_all_two_orders(tmp_path):
    # cancel-all must cancel EVERY resting order (not just the first) — each routed through
    # cancel-to-terminal (the 3a-safe path). Two orders kills a "cancel only slices[0]" /
    # "break after first" mutant.
    s0 = _sl("usd1", qty=500.0, order_id="A0", order_link_id="sca-0-0",
             order_side="sell", order_px=1.0005, order_qty=500.0)
    s1 = _sl("usd1", qty=500.0, order_id="A1", order_link_id="sca-1-0",
             order_side="sell", order_px=1.0006, order_qty=500.0)
    eng = _mk_engine(tmp_path, slices=[s0, s1])
    fake = FakeOrderClient()
    eng._cancel_all_resting(client=fake)
    assert ("cancel", "A0", "sca-0-0") in fake.calls
    assert ("cancel", "A1", "sca-1-0") in fake.calls       # the SECOND order is cancelled too
