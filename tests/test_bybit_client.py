"""Tests for BybitPrivateClient (sca.live.bybit_client) — Phase 1, T2.

READ-ONLY client: UTA wallet balance + open orders. NO order placement (Phase 3).
Because the boss uses a *trade-capable* API key, "no orders" is enforced in CODE
and asserted here (the fake exchange's order methods must never be touched).

ISOLATION: zero network. A FakeCcxt module is injected; creds come from injected
dicts. Nothing reads the real env, config, or ccxt.

Run: PYTHONPATH=src python3 -m pytest tests/test_bybit_client.py -q
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live import bybit_client as bc  # noqa: E402

# --- canned Bybit V5 UTA fetch_balance payload (ccxt shape) ------------------
CANNED_BALANCE = {
    "info": {"result": {"list": [{
        "accountType": "UNIFIED",
        "totalEquity": "10250.5",
        "totalAvailableBalance": "10000.0",
        "totalWalletBalance": "10250.5",
        "coin": [
            {"coin": "USD1", "walletBalance": "6000.0", "locked": "100.0",
             "usdValue": "6000.0", "equity": "6000.0", "spotBorrow": "0"},
            {"coin": "USDT", "walletBalance": "4250.5", "locked": "0",
             "usdValue": "4250.5", "equity": "4250.5", "spotBorrow": "0"},
        ],
    }]}},
    "USD1": {"free": 5900.0, "used": 100.0, "total": 6000.0},
    "USDT": {"free": 4250.5, "used": 0.0, "total": 4250.5},
}

CANNED_ORDERS = [
    {"id": "abc", "symbol": "USD1/USDT", "side": "sell", "price": 1.0005,
     "amount": 1500.0, "type": "limit", "clientOrderId": "sca-0-1"},
]

_GOOD_CFG = {"api_key_env": "K", "api_secret_env": "S", "confirm_env": "C"}
_GOOD_ENV = {"K": "key-123", "S": "secret-456", "C": "yes"}


class FakeExchange:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.sandbox = False

    def set_sandbox_mode(self, v):
        self.sandbox = v
        self.calls.append(("set_sandbox_mode", v))

    def fetch_balance(self, params=None):
        self.calls.append(("fetch_balance", params))
        return CANNED_BALANCE

    def fetch_open_orders(self, symbol=None, *a, **k):
        self.calls.append(("fetch_open_orders", symbol))
        return CANNED_ORDERS

    # order methods exist but MUST NEVER be called in Phase 1+2
    def create_order(self, *a, **k):
        self.calls.append(("create_order", a))
        raise AssertionError("create_order must never be called in read-only phase")

    def cancel_order(self, *a, **k):
        self.calls.append(("cancel_order", a))
        raise AssertionError("cancel_order must never be called in read-only phase")


class FakeCcxt:
    def __init__(self):
        self.last = None

    def bybit(self, config):
        self.last = FakeExchange(config)
        return self.last


def _mk(**over):
    fake = FakeCcxt()
    kwargs = dict(ccxt_module=fake, live_cfg=_GOOD_CFG, env=_GOOD_ENV)
    kwargs.update(over)
    client = bc.BybitPrivateClient(**kwargs)
    return client, fake


# --- construction -----------------------------------------------------------

def test_missing_credentials_raises():
    fake = FakeCcxt()
    with pytest.raises(RuntimeError):
        bc.BybitPrivateClient(ccxt_module=fake, live_cfg=_GOOD_CFG, env={})  # no key/secret


def test_constructs_spot_unified_no_verbose():
    client, fake = _mk()
    cfg = fake.last.config
    assert cfg["options"]["defaultType"] == "spot"
    assert cfg["enableRateLimit"] is True
    assert cfg.get("verbose") is False
    assert cfg["apiKey"] == "key-123" and cfg["secret"] == "secret-456"


def test_testnet_enables_sandbox():
    client, fake = _mk(testnet=True)
    assert fake.last.sandbox is True
    assert ("set_sandbox_mode", True) in fake.last.calls


def test_testnet_defaults_from_config():
    # live_cfg says testnet true -> sandbox on without explicit arg
    cfg = dict(_GOOD_CFG, testnet=True)
    client = bc.BybitPrivateClient(ccxt_module=(f := FakeCcxt()), live_cfg=cfg, env=_GOOD_ENV)
    assert f.last.sandbox is True


def test_repr_redacts_secret():
    client, _ = _mk()
    r = repr(client)
    assert "secret-456" not in r and "key-123" not in r


# --- no order surface (trade-capable key compensation) ----------------------

def test_client_exposes_no_order_methods():
    for m in ("create_order", "place_order", "cancel_order", "create_limit_order",
              "create_market_order", "cancel_all_orders"):
        assert not hasattr(bc.BybitPrivateClient, m), f"client must not expose {m}"


def test_balance_and_orders_never_touch_order_methods():
    client, fake = _mk()
    client.get_wallet_balance()
    client.get_open_orders("USD1/USDT")
    called = {name for name, _ in fake.last.calls}
    assert "create_order" not in called and "cancel_order" not in called


# --- balance normalization --------------------------------------------------

def test_get_wallet_balance_normalizes_uta():
    client, fake = _mk()
    bal = client.get_wallet_balance()
    assert ("fetch_balance", {"type": "unified"}) in fake.last.calls
    assert bal["account_type"] == "UNIFIED"
    assert bal["totals"]["equity_usd"] == 10250.5
    assert bal["totals"]["available_usd"] == 10000.0
    assert bal["totals"]["wallet_usd"] == 10250.5
    # margin fields captured for the liability guard (absent in canned -> 0.0)
    assert bal["totals"]["im_usd"] == 0.0 and bal["totals"]["mm_usd"] == 0.0
    usd1 = bal["coins"]["USD1"]
    assert usd1["wallet"] == 6000.0 and usd1["locked"] == 100.0
    assert usd1["free"] == 5900.0          # free := walletBalance - locked
    assert usd1["usd"] == 6000.0 and usd1["borrow"] == 0.0
    assert bal["coins"]["USDT"]["free"] == 4250.5


def test_normalize_balance_is_pure():
    # callable directly on a canned dict, no client/exchange needed
    bal = bc.normalize_balance(CANNED_BALANCE)
    assert bal["coins"]["USD1"]["free"] == 5900.0


# --- open orders ------------------------------------------------------------

def test_get_open_orders_normalizes_keeps_client_order_id():
    # C-P1#6 — the open-order shape now also carries clientOrderId (read-only) so the R1
    # gate's account-wide list lets resume_reconcile_orders match by link_id. NO order
    # method is added; test_client_exposes_no_order_methods (:133) stays green.
    client, fake = _mk()
    orders = client.get_open_orders("USD1/USDT")
    assert orders == [{"id": "abc", "symbol": "USD1/USDT", "side": "sell",
                       "price": 1.0005, "qty": 1500.0, "type": "limit",
                       "clientOrderId": "sca-0-1"}]


def test_get_open_orders_account_wide_when_symbol_none():
    client, fake = _mk()
    client.get_open_orders(None)
    assert ("fetch_open_orders", None) in fake.last.calls


# --- CLI `sca balance` ------------------------------------------------------

def test_cli_prints_balance_table(monkeypatch):
    class StubClient:
        testnet = False

        def __init__(self, *a, **k):
            pass

        def get_wallet_balance(self):
            return bc.normalize_balance(CANNED_BALANCE)

    monkeypatch.setattr(bc, "BybitPrivateClient", StubClient)
    buf = io.StringIO()
    with redirect_stdout(buf):
        bc.main([])
    out = buf.getvalue()
    assert "USD1" in out and "USDT" in out and "equity" in out  # coin rows + totals header
    assert "10,250.50" in out                                   # total equity rendered
