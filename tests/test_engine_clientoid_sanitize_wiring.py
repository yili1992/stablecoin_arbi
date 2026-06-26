"""Engine wires the per-exchange clientOid transform into ALL link-matching sites — Phase 3.

The live-money reconcile core (feedback_id_sanitization_consistency): Bitget ECHOES the
SANITIZED clientOid (e.g. ``scaX0X0``) while a slice stores the RAW engine link
(``sca-0-0``). Every place the engine compares a stored link to an exchange-echoed id
MUST apply ``adapter.sanitize_link`` to the stored side (and use the venue-echoed "sca-"
ownership prefix for the stale guard), or every Bitget order is orphaned at restart:

  1. ``reconcile_orders`` -> ``match_live_orders(...)``
  2. ``resume_reconcile_orders`` -> ``match_live_orders(...)`` + the foreign-order stale guard
  3. the R1 ``expected`` set (reconcile() compares it to the echoed clientOid)

BYBIT ZERO-CHANGE: ``BybitAdapter.sanitize_link`` is identity and the prefix is "sca-",
so all three sites are byte-for-byte unchanged on the Bybit path — pinned here too.

ISOLATION: no network/disk. A real PaperEngine with its adapter swapped to BitgetAdapter;
``match_live_orders`` is captured to assert the transform the engine threads through.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_clientoid_sanitize_wiring.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import engine as engine_mod                       # noqa: E402
from sca.live.engine import PaperEngine                          # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter, sanitize_client_oid  # noqa: E402
from sca.live.exchanges.bybit import BybitAdapter                # noqa: E402


_ORDER_DEFAULTS = dict(order_id=None, order_link_id=None, order_px=None,
                       order_side=None, order_qty=None, filled_qty=0.0,
                       order_gen=0, reject_streak=0, sell_proceeds=0.0, qty_sold=0.0)


def _sl(state, **over):
    s = {"state": state, "qty": 0.0, "cash": 0.0, "sell_px": 0.0, "entry": None}
    s.update(_ORDER_DEFAULTS)
    s.update(over)
    return s


def _state(status_class, *, oid=None, link=None, side=None, filled=0.0, remaining=0.0,
           avg=None, price=None):
    return {"id": oid, "link_id": link, "side": side, "status": status_class,
            "status_class": status_class, "filled": filled, "remaining": remaining,
            "avg": avg, "price": price, "reject_reason": None, "raw": None}


class _Client:
    """Minimal order client for resume_reconcile_orders: fetch_open + fetch_order_state."""

    def __init__(self, open_orders, states=None):
        self._open = open_orders
        self._states = states or {}
        self.cancelled = []

    def fetch_open(self, symbol):
        return list(self._open)

    def fetch_order_state(self, symbol, order_id=None, *, link_id=None):
        key = link_id if link_id is not None else order_id
        return self._states.get(key, _state("not_found", oid=order_id, link=link_id))

    def cancel(self, symbol, order_id, *, link_id=None):
        self.cancelled.append(link_id or order_id)
        return _state("cancelled", oid=order_id, link=link_id)


def _bitget_engine(tmp_path, slices):
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.adapter = BitgetAdapter()
    eng.maker_enabled = True
    eng._r1_ok = True
    eng._sleep = lambda *a, **k: None
    eng.slices = slices
    return eng


def _bybit_engine(tmp_path, slices):
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.maker_enabled = True
    eng._r1_ok = True
    eng._sleep = lambda *a, **k: None
    eng.slices = slices
    return eng


# === resume_reconcile_orders: match site is sanitize-aware ===================

def test_resume_match_passes_adapter_sanitize_for_bitget(tmp_path, monkeypatch):
    # The stored slice link is RAW; the resume match must receive the Bitget sanitize
    # transform + the sanitized "sca-" prefix so a sanitized echo attributes correctly.
    eng = _bitget_engine(tmp_path, [_sl("usd1", order_link_id="sca-0-0", order_id="A0")])
    captured = {}
    real = engine_mod.match_live_orders

    def _spy(slices, open_orders, link_norm=None, ours_prefix="sca-"):
        captured["link_norm"] = link_norm
        captured["ours_prefix"] = ours_prefix
        return real(slices, open_orders, link_norm=link_norm, ours_prefix=ours_prefix)

    monkeypatch.setattr(engine_mod, "match_live_orders", _spy)
    open_orders = [{"clientOrderId": "scaX0X0", "id": "A0", "side": "sell",
                    "price": 1.0005, "qty": 8.0, "filled_qty": 0.0}]
    client = _Client(open_orders=open_orders, states={"sca-0-0": _state("open", oid="A0",
                                                               link="scaX0X0", remaining=8.0)})
    eng.resume_reconcile_orders(open_orders, client=client, now=0.0)
    # the transform threaded in must be the adapter's (sanitizes "sca-0-0" -> "scaX0X0")
    assert captured["link_norm"] is not None
    assert captured["link_norm"]("sca-0-0") == "scaX0X0"
    # the ownership prefix is the venue-echoed form of "sca-"
    assert captured["ours_prefix"] == sanitize_client_oid("sca-") == "scaX"


def test_resume_attributes_bitget_sanitized_echo_end_to_end(tmp_path):
    # END-TO-END: a resting Bitget order echoes the SANITIZED clientOid; the slice that
    # owns the RAW link must be RE-LINKED (order_id recovered), NOT cancelled as an orphan.
    eng = _bitget_engine(tmp_path, [_sl("usd1", order_link_id="sca-0-0", qty=8.0)])
    open_order = {"clientOrderId": "scaX0X0", "id": "A0", "side": "sell",
                  "price": 1.0005, "qty": 8.0, "filled_qty": 0.0}
    client = _Client(open_orders=[open_order],
                     states={"sca-0-0": _state("open", oid="A0", link="scaX0X0",
                                               remaining=8.0)})
    eng.resume_reconcile_orders([open_order], client=client, now=0.0)
    assert eng.slices[0]["order_id"] == "A0"        # re-linked (matched), recovered id
    assert client.cancelled == []                    # NOT treated as an orphan


def test_resume_bitget_orphan_sanitized_link_recognized_as_ours(tmp_path):
    # A Bitget echo ``scaX9X9`` (sanitized form of an orphan ``sca-9-9``) owns no slice.
    # It must be recognized as OURS (cancel-to-terminal), NOT refused as a FOREIGN order.
    eng = _bitget_engine(tmp_path, [_sl("usd1", order_link_id="sca-0-0", order_id="A0",
                                        qty=8.0)])
    open_orders = [
        {"clientOrderId": "scaX0X0", "id": "A0", "side": "sell", "price": 1.0005,
         "qty": 8.0, "filled_qty": 0.0},                        # ours, attributed
        {"clientOrderId": "scaX9X9", "id": "Z9", "side": "sell", "price": 1.0010,
         "qty": 4.0, "filled_qty": 0.0},                        # ours, orphan
    ]
    # fetch_order_state is keyed by link_id when present; the orphan's terminal state is
    # under its (sanitized) link "scaX9X9" so cancel-to-terminal resolves cleanly.
    client = _Client(open_orders=open_orders,
                     states={"sca-0-0": _state("open", oid="A0", link="scaX0X0",
                                               remaining=8.0),
                             "scaX9X9": _state("cancelled", oid="Z9", link="scaX9X9")})
    eng.resume_reconcile_orders(open_orders, client=client, now=0.0)  # must NOT SystemExit
    assert "scaX9X9" in client.cancelled                  # recognized as ours -> cancelled


# === R1 expected set is sanitize-aware ======================================

def test_expected_set_sanitized_for_bitget(tmp_path):
    # reconcile() compares the echoed clientOid to the ``expected`` set; for Bitget the
    # set must hold the SANITIZED links so our own resting orders are by-design (not
    # flagged as unexpected/foreign). We assert via the helper the engine builds it from.
    eng = _bitget_engine(tmp_path, [
        _sl("usd1", order_link_id="sca-0-0"),
        _sl("usdt", order_link_id="sca-1-0"),
    ])
    expected = eng._expected_links()
    assert expected == {"scaX0X0", "scaX1X0"}


def test_expected_set_raw_for_bybit(tmp_path):
    # BYBIT ZERO-CHANGE: identity transform => the expected set holds the RAW links,
    # exactly as before the per-exchange change.
    eng = _bybit_engine(tmp_path, [
        _sl("usd1", order_link_id="sca-0-0"),
        _sl("usdt", order_link_id="sca-1-0"),
    ])
    expected = eng._expected_links()
    assert expected == {"sca-0-0", "sca-1-0"}


# === R1 gate END-TO-END: a Bitget resting order is by-design, not foreign ====

def _bitget_norm_bal(usdc=0.0, usdt=0.0):
    return {
        "account_type": "spot",
        "totals": {"equity_usd": 0.0, "available_usd": 0.0, "wallet_usd": 0.0,
                   "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
        "coins": {
            "USDC": {"wallet": usdc, "locked": 0.0, "free": usdc, "usd": 0.0,
                     "equity": usdc, "borrow": 0.0},
            "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": 0.0,
                     "equity": usdt, "borrow": 0.0},
        },
        "raw": None,
    }


class _FakeReadClient:
    def __init__(self, balance, orders=None):
        self._b, self._o = balance, orders or []

    def get_wallet_balance(self):
        return self._b

    def get_open_orders(self, symbol=None):
        return list(self._o)


def test_r1_gate_bitget_resting_order_with_sanitized_clientoid_proceeds(tmp_path):
    # THE live-money crux: a resumed Bitget position holds a resting SELL whose echoed
    # clientOid is the SANITIZED ``scaX0X0`` while the slice stores the RAW ``sca-0-0``.
    # reconcile() compares the echoed clientOid to the ``expected`` set; only because the
    # engine sanitizes that set does the order count as BY-DESIGN (not an unexpected
    # foreign order that would REFUSE). Without the fix this raises SystemExit.
    eng = PaperEngine(symbol="USDCUSDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True
    eng.persist = True
    eng._resumed = True
    eng.maker_enabled = True
    eng.adapter = BitgetAdapter()
    eng.slices = [
        {"state": "usdc", "qty": 5000.0, "cash": 0.0, "sell_px": 1.0005, "entry": 1.0,
         "order_link_id": "sca-0-0", "order_id": "A0", "order_px": 1.0005,
         "order_side": "sell", "order_qty": 5000.0},
    ]
    eng.deployed = True
    resting = {"clientOrderId": "scaX0X0", "id": "A0", "side": "sell",
               "price": 1.0005, "amount": 5000.0}
    client = _FakeReadClient(_bitget_norm_bal(usdc=5000.0), orders=[resting])
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed"          # our sanitized resting order is by-design


# === Bybit zero-change: reconcile/resume match sites stay identity ===========

def test_resume_match_passes_identity_for_bybit(tmp_path, monkeypatch):
    eng = _bybit_engine(tmp_path, [_sl("usd1", order_link_id="sca-0-0", order_id="A0")])
    captured = {}
    real = engine_mod.match_live_orders

    def _spy(slices, open_orders, link_norm=None, ours_prefix="sca-"):
        captured["link_norm"] = link_norm
        captured["ours_prefix"] = ours_prefix
        return real(slices, open_orders, link_norm=link_norm, ours_prefix=ours_prefix)

    monkeypatch.setattr(engine_mod, "match_live_orders", _spy)
    open_orders = [{"clientOrderId": "sca-0-0", "id": "A0", "side": "sell",
                    "price": 1.0005, "qty": 8.0, "filled_qty": 0.0}]
    client = _Client(open_orders=open_orders, states={"sca-0-0": _state("open", oid="A0",
                                                               link="sca-0-0", remaining=8.0)})
    eng.resume_reconcile_orders(open_orders, client=client, now=0.0)
    assert captured["link_norm"]("sca-0-0") == "sca-0-0"        # identity
    assert captured["ours_prefix"] == "sca-"                     # unchanged prefix
