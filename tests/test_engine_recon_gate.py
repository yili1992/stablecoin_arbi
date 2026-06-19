"""Tests for the armed-live R1 reconciliation gate in PaperEngine — Phase 2, T5.

The gate (_reconcile_or_refuse) runs ONLY when armed (live); paper never touches
the private API. Refusal is loud + a CLEAN exit (SystemExit 0, D16 — a deliberate
refusal is an intentional stop, so docker restart:on-failure does not loop on it).
Fresh deploy needs explicit opt-in. persist=false and UTA liability both refuse as
preconditions.

ISOLATION: no network. The engine is built in paper mode (out_dir -> tmp_path, no
state), fields are set directly, and a FakeClient supplies exchange truth.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_recon_gate.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import bybit_client  # noqa: E402
from sca.live.engine import PaperEngine  # noqa: E402


def _bal(usd1=0.0, usdt=0.0, usd1_borrow=0.0, equity=None, im=0.0):
    total = usd1 + usdt
    return {
        "account_type": "UNIFIED",
        "totals": {"equity_usd": total if equity is None else equity,
                   "available_usd": total, "wallet_usd": total,
                   "im_usd": im, "mm_usd": 0.0, "perp_upl_usd": 0.0},
        "coins": {
            "USD1": {"wallet": usd1, "locked": 0.0, "free": usd1, "usd": usd1, "borrow": usd1_borrow},
            "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": usdt, "borrow": 0.0},
        },
    }


class FakeClient:
    def __init__(self, balance, orders=None):
        self._b, self._o = balance, orders or []
        self.calls = []

    def get_wallet_balance(self):
        self.calls.append("balance")
        return self._b

    def get_open_orders(self, symbol=None):
        self.calls.append(("orders", symbol))
        return self._o


def _armed_engine(tmp_path, *, resumed=False, slices=None, persist=True, allow_fresh=False,
                  expect_asset=None, expect_amount=None):
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True                      # simulate the live-auth gate having passed
    eng.persist = persist
    eng.allow_fresh = allow_fresh
    eng.expect_asset = expect_asset
    eng.expect_amount = expect_amount
    eng._resumed = resumed
    eng.deployed = bool(slices)
    eng.slices = slices or []
    return eng


def _deployed_slices(usd1_qty, usdt_value):
    # one USD1 slice + one USDT slice summing to the given holdings
    return [
        {"state": "usd1", "qty": usd1_qty, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": usdt_value, "sell_px": 1.0, "entry": None},
    ]


# (h) paper path never constructs the private client -------------------------

def test_paper_gate_is_noop_and_never_builds_client(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("paper must never construct BybitPrivateClient")
    monkeypatch.setattr(bybit_client, "BybitPrivateClient", boom)
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    assert eng.armed is False
    eng._maybe_gate()        # no-op for paper; must NOT raise


# (f) persist precondition (checked before any I/O) --------------------------

def test_armed_persist_false_refuses_before_io(tmp_path):
    eng = _armed_engine(tmp_path, persist=False)

    class ExplodingClient:
        def get_wallet_balance(self): raise AssertionError("must refuse before I/O")
        def get_open_orders(self, s=None): raise AssertionError("must refuse before I/O")

    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=ExplodingClient())


# (g) UTA liability guard ----------------------------------------------------

def test_armed_borrow_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=True)
    client = FakeClient(_bal(usdt=10000.0, usd1_borrow=5.0))
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_armed_negative_equity_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=True)
    client = FakeClient(_bal(usdt=10000.0, equity=-1.0))
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (b)(c) no local state: opt-in required, clean required ---------------------

def test_armed_no_state_no_optin_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=False)
    client = FakeClient(_bal(usdt=10000.0))           # clean, but no opt-in
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_armed_no_state_optin_mixed_holdings_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=True)
    client = FakeClient(_bal(usd1=6000.0, usdt=4000.0))  # a position -> refuse
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (g2) UTA margin active -> refuse -------------------------------------------

def test_armed_margin_active_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    client = FakeClient(_bal(usdt=10000.0, im=50.0))   # initial margin in use
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (a) no local state + opt-in + declaration: reconcile approves, but engine REFUSES
#     to act on fresh_deploy in read-only Phase 1+2 (no order path) — code-review P1.

def test_armed_no_state_fresh_deploy_refused_until_phase3(tmp_path):
    # reconcile() APPROVES the declared fresh start, but the engine must REFUSE to act on
    # it: bootstrap()/_deploy() would create a config-alloc-sized SIMULATED USD1 position
    # that doesn't match the real balance, with no order path. Gated until Phase 3.
    eng = _armed_engine(tmp_path, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    client = FakeClient(_bal(usdt=10000.0))
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_armed_no_state_optin_without_declaration_refuses(tmp_path):
    eng = _armed_engine(tmp_path, allow_fresh=True)   # no expect_asset/amount
    client = FakeClient(_bal(usdt=10000.0))
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (e2) resumed + matching balances but an open order on the account -> refuse -

def test_armed_resumed_open_order_refuses(tmp_path):
    eng = _armed_engine(tmp_path, resumed=True, slices=_deployed_slices(6000.0, 4000.0))
    client = FakeClient(_bal(usd1=6000.0, usdt=4000.0), orders=[{"id": "x"}])
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (d)(e) resumed state must match exchange -----------------------------------

def test_armed_resumed_match_proceeds(tmp_path):
    eng = _armed_engine(tmp_path, resumed=True, slices=_deployed_slices(6000.0, 4000.0))
    client = FakeClient(_bal(usd1=6000.0, usdt=4000.0))
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed" and rep["ok"] is True
    assert ("orders", None) in client.calls          # account-wide open-order check (Codex P2)


def test_armed_resumed_mismatch_refuses(tmp_path):
    eng = _armed_engine(tmp_path, resumed=True, slices=_deployed_slices(6000.0, 4000.0))
    client = FakeClient(_bal(usd1=1000.0, usdt=4000.0))   # 5000 USD1 short
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# (a2) D15 — fresh_deploy STILL refuses, but the message must not cite the stale
# "Phase 3 / real order placement is NOT built" reason (3b built the order path).
# The real reason: we never blindly build a config-`alloc`-sized position (it would
# not match the real balance and would hollow out R1); the initial position must come
# from seed-from-balance (a clean single-coin dedicated subaccount -> reconcile
# 'proceed'). Refusal CONDITION is unchanged; only the message is corrected.

def test_fresh_deploy_message_no_phase3(tmp_path, capsys):
    eng = _armed_engine(tmp_path, allow_fresh=True, expect_asset="USDT", expect_amount=10000.0)
    client = FakeClient(_bal(usdt=10000.0))           # clean -> reconcile approves fresh_deploy
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)
    err = capsys.readouterr().err
    assert "REFUSED" in err                            # behaviour unchanged: still refuses
    assert "Phase 3" not in err                        # stale reason removed
    assert "Phase" not in err                          # no lingering "Phase N" wording at all
    assert "seed" in err.lower()                       # cites the real path: seed-from-balance
