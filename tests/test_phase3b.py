"""Phase 3b (D14 simplified model) — two modes (dryrun|live) + the single
total-alloc deployment cap + live-path markout.

D14 drastically simplified the original 3b: the engine now has exactly TWO modes —
``dryrun`` (run the maker engine but SIMULATE matching; no order client, no keys, no
real orders) and ``live`` (real GTC PostOnly maker orders on MAINNET; ``MODE=live``
ALONE = real money, no extra confirm env; missing keys raise at client construction).
The ONLY real-money fund limit is ``live.max_total_alloc_usd`` (capital = loss cap).
REMOVED (and so are their tests): testnet/allow_mainnet venue gate, maker_enabled
rollback knob, per-order max_order_usd cap, the PnL max-loss kill-switch + its
durable-halt persistence, and the LIVE_*_CONFIRM / LIVE_CLEAR_HALT envs.

Invariants pinned here:
  - mode: default dryrun; MODE=live ALONE arms; maker path switch == (mode=='live');
    dryrun NEVER builds a client / NEVER places a real order; live builds a MAINNET
    client (no venue args) and can place.
  - total-alloc cap enforced in the SIZING path (seed + available pool), not just
    stored (arb-execution-risk); -1 => full wallet; valued at the coin's USD mark.
  - live fills feed the markout gauge; status surfaces markout.

ISOLATION: pure / no network / no disk. Real PaperEngine in dryrun mode (out_dir ->
tmp_path); maker live fields set DIRECTLY (the documented test seam); FakeOrderClient
records calls + returns canned states.

Run: PYTHONPATH=src python3 -m pytest tests/test_phase3b.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pytest  # noqa: E402

from sca import config  # noqa: E402
from sca.live import engine as engine_mod  # noqa: E402
from sca.live.engine import (  # noqa: E402
    PaperEngine, OperatorReconcileHalt, HORIZONS, aggregate_markout,
)


# ===========================================================================
# Fixtures — real PaperEngine in dryrun mode; maker fields set DIRECTLY
# ===========================================================================

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

    def __init__(self, *, balance=None, state_results=None):
        self._balance = balance if balance is not None else _bal()
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
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
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
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True
    eng.maker_enabled = maker
    eng.persist = persist
    eng._sleep = lambda *a, **k: None
    eng.slices = []
    return eng


# ===========================================================================
# SIMPLIFIED MODEL (D14) — two modes dryrun|live; MODE=live ALONE = real money
# (no extra confirm env); maker path switch == (mode=='live'); dryrun NEVER
# builds a client / NEVER places a real order (pure sim). These are the
# discriminating guards against accidentally arming real money.
# ===========================================================================

def test_default_mode_is_dryrun(tmp_path):
    # resolve_mode default + a default-constructed engine are BOTH dryrun (safe default).
    assert config.resolve_mode(cfg={}, env={}) == "dryrun"
    eng = PaperEngine(symbol="USD1USDT", seconds=1, csv_path=str(tmp_path / "out.csv"))
    assert eng.req_mode == "dryrun"
    assert eng.armed is False
    assert eng._compute_maker_enabled() is False


def test_dryrun_default_never_builds_client_never_places(tmp_path, monkeypatch):
    # dryrun runs the engine but SIMULATES fills — it must NEVER construct a real order
    # client and NEVER place a real order.
    import sca.live.orders as orders_mod

    def boom(*a, **k):
        raise AssertionError("dryrun must NEVER construct MakerOrderClient")
    monkeypatch.setattr(orders_mod, "MakerOrderClient", boom, raising=False)

    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng._compute_maker_enabled() is False
    eng._build_order_client()                          # no-op in dryrun (never builds)
    assert eng.order_client is None
    # dryrun still SIMULATES fills (the sim-fill path) — no real placement
    eng.deployed = True
    eng.slices = [_sl("usd1", qty=10.0)]
    calls = []
    monkeypatch.setattr(eng, "evaluate_fills", lambda now: calls.append("sim"))
    eng._handle({"topic": "orderbook.1.USD1USDT",
                 "data": {"b": [["1.0", "5"]], "a": [["1.001", "5"]]}}, 0.0)
    assert calls == ["sim"]                            # simulated; no real order placed


def test_live_builds_client_and_can_place(tmp_path, monkeypatch):
    # MODE=live ALONE arms (no extra confirm env); the engine builds a MAINNET client
    # with NO testnet / allow_mainnet args (the gate is gone — live == mainnet real).
    captured = {}

    class RecMaker:
        def __init__(self, *a, **k):
            captured["built"] = True
            captured["args"] = a
            captured["kwargs"] = k

    import sca.live.orders as orders_mod
    monkeypatch.setattr(orders_mod, "MakerOrderClient", RecMaker, raising=False)

    eng = PaperEngine(symbol="USD1USDT", mode="live", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng.armed is True                           # mode=live alone arms (no confirm)
    assert eng._compute_maker_enabled() is True        # maker path switch == live mode
    eng.maker_enabled = True
    eng._build_order_client()
    assert captured.get("built") is True               # real client constructed
    # Phase 3: built on the per-symbol adapter — the symbol is passed positionally; still
    # NO testnet/mainnet GATE kwargs (the gate is gone — live == mainnet real, D14).
    assert captured["args"] == ("USD1USDT",) and captured["kwargs"] == {}


def test_live_maker_path_places_real_order(tmp_path):
    # a live engine with the maker path active places a real order via reconcile (the
    # order size is the ladder's alloc x fraction — no per-order cap involved).
    eng = _mk_engine(tmp_path, slices=[_sl("usdt", cash=100.0)], anchor=1.0,
                     bid=0.9998, ask=1.0002)
    eng.order_client = FakeOrderClient(balance=_bal(usdt=100.0))
    eng.reconcile_orders(0.0)
    assert any(c[0] == "place" for c in eng.order_client.calls)


def test_compute_maker_enabled_tracks_live_mode(tmp_path):
    # the maker path switch is EXACTLY (mode=='live') — no separate venue/rollback knob.
    dry = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "d.csv"))
    assert dry._compute_maker_enabled() is False
    live = PaperEngine(symbol="USD1USDT", mode="live", seconds=1,
                       csv_path=str(tmp_path / "l.csv"))
    assert live._compute_maker_enabled() is True


# ===========================================================================
# total-alloc canary cap — enforced in the SIZING path (seed + available pool)
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
    assert q2 == pytest.approx(10000.0)                   # -1 => full pool (no cap)


def test_init_reads_per_symbol_cap_from_config(tmp_path):
    """engine __init__ reads the deployment cap PER-SYMBOL via config.max_alloc_for(symbol),
    NOT a hardcoded global. Asserted against max_alloc_for(symbol) (not a literal) so the test
    can't drift when the configured caps change (e.g. USDC 400->1000 capacity raise)."""
    from sca.config import max_alloc_for
    usd1 = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                       csv_path=str(tmp_path / "u1.csv"))
    usdc = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1,
                       csv_path=str(tmp_path / "uc.csv"))
    # each engine reads ITS symbol's per-symbol cap (delegation to max_alloc_for is the mechanism)
    assert usd1._max_total_alloc_usd == max_alloc_for("USD1USDT")
    assert usdc._max_total_alloc_usd == max_alloc_for("USDCUSDT")
    assert usdc._max_total_alloc_usd != 0.0   # sanity: a real per-symbol value was resolved


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


# ===========================================================================
# PnL baseline (status start_value) — LIVE uses the ACTUAL deployed capital, NOT
# the paper/backtest alloc. Else a capped/wallet-funded live deploy reports a
# phantom loss (config alloc $10k vs the ~$1k truly deployed -> total ≈ -9000).
# ===========================================================================

def test_seed_baseline_is_deployed_capital_not_paper_alloc(tmp_path):
    # The canary bug: alloc=$10k (paper notional) used as the PnL baseline while the cap
    # deploys only $1k -> total shows -9000 with ZERO fills. Baseline must be Σ cash (1000).
    eng = _armed_engine(tmp_path, maker=True)
    eng.alloc = 10_000.0                 # paper/backtest notional (the WRONG baseline)
    eng._max_total_alloc_usd = 1_000.0   # the real-money deployment cap
    eng._seed_slices_from_balance(_bal(usdt=1_000.20), open_orders=[])
    doc = eng.status_doc(1.0)
    assert doc["pnl"]["start_value"] == pytest.approx(1_000.0)    # was self.alloc == 10000
    assert doc["pnl"]["total"] == pytest.approx(0.0, abs=1e-6)    # no fills -> ~0, NOT -9000
    assert eng._deployed_capital == pytest.approx(1_000.0)


def test_seed_baseline_offpeg_usdt_equals_quote_face(tmp_path):
    # off-peg USDT (mark 0.98): deployable (quote units) = cap/mark = 300/0.98. The quote leg
    # enters total_value at FACE, so the baseline must equal Σ cash (face), NOT the USD cap.
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal_offpeg("USDT", 10_000.0, 9_800.0), open_orders=[])
    assert eng._deployed_capital == pytest.approx(sum(s["cash"] for s in eng.slices))
    assert eng._deployed_capital == pytest.approx(300.0 / 0.98)   # face quote, not 300
    assert eng.status_doc(1.0)["pnl"]["total"] == pytest.approx(0.0, abs=1e-6)


def test_seed_baseline_usd1_valued_at_seed_mark(tmp_path):
    # USD1-funded seed: base enters total_value at px, so the baseline must value the seeded
    # USD1 at its seed mark (== deployable * mark == the USD it represents). With live px at
    # the seed mark, MTM total is ~0 (no phantom).
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal_offpeg("USD1", 10_000.0, 9_800.0), open_orders=[])
    assert eng._deployed_capital == pytest.approx(300.0)          # deployable(=300/0.98) * mark(0.98)
    eng.bid, eng.ask = 0.9799, 0.9801                            # live mid == seed mark 0.98
    assert eng.status_doc(1.0)["pnl"]["total"] == pytest.approx(0.0, abs=1e-6)


def test_seeded_usd1_slices_track_entry_cost_for_floor(tmp_path):
    eng = _armed_engine(tmp_path, maker=True)
    eng._max_total_alloc_usd = 300.0
    eng._seed_slices_from_balance(_bal_offpeg("USD1", 10_000.0, 9_800.0), open_orders=[])

    assert all(s["entry"] == pytest.approx(0.98) for s in eng.slices)


def test_paper_baseline_unchanged_uses_alloc(tmp_path):
    # A dryrun/paper engine deploys the FULL alloc (never seeds) -> baseline stays alloc.
    # The fix must touch ONLY the seeded path; _deployed_capital is None on the paper path.
    eng = _mk_engine(tmp_path, slices=[_sl("usd1", qty=10_000.0, entry=1.0)], anchor=1.0)
    eng.alloc = 10_000.0
    assert eng._deployed_capital is None
    assert eng.status_doc(1.0)["pnl"]["start_value"] == pytest.approx(10_000.0)


def test_deployed_capital_survives_resume(tmp_path):
    # The baseline must persist in the v2 snapshot so a restarted (docker-recycled) live
    # engine keeps reporting against deployed capital, not the paper alloc.
    eng = _armed_engine(tmp_path, maker=True, persist=True)
    eng.alloc = 10_000.0
    eng._max_total_alloc_usd = 1_000.0
    eng._seed_slices_from_balance(_bal(usdt=1_000.20), open_orders=[])
    eng.write_status(1.0)                                        # persists state (tag=mode)
    eng2 = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                       csv_path=str(tmp_path / "out.csv"))
    assert eng2._resumed is True
    assert eng2._deployed_capital == pytest.approx(1_000.0)
    assert eng2.status_doc(2.0)["pnl"]["start_value"] == pytest.approx(1_000.0)


# ===========================================================================
# markout on the live path — the canary's PURPOSE (measure adverse selection)
# ===========================================================================

def test_live_fill_feeds_markout_gauge(tmp_path):
    # A public trade on the MAKER (live) path must STILL feed the markout gauge (not
    # bypassed by maker_enabled).
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
# cancel-all-on-exit (order-lifecycle kill-switch, KEPT) — cancels EVERY order
# ===========================================================================

def test_cancel_all_two_orders(tmp_path):
    # cancel-all must cancel EVERY resting order (not just the first) — each routed through
    # cancel-to-terminal (the safe path). Two orders kills a "cancel only slices[0]" /
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
