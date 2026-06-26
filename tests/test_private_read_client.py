"""Phase 2, Task 1 — PrivateReadClient: the adapter-driven, READ-ONLY private client.

This is the venue-agnostic analogue of ``BybitPrivateClient`` for non-Bybit venues
(Bitget). It places NO orders — it exposes ONLY ``get_wallet_balance`` (via
``adapter.fetch_balance_coins``) and ``get_open_orders`` (via ccxt
``fetch_open_orders``). Credentials resolve through ``sca.live.creds`` (so the env
var names stay configurable per venue); the ccxt module + env are injectable for
tests.

ISOLATION: zero network. A FakeCcxt module is injected; creds come from an injected
env dict. The fake raises if any order method is touched (no-order invariant).

Run: PYTHONPATH=src python3 -m pytest tests/test_private_read_client.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.exchanges.bitget import BitgetAdapter            # noqa: E402
from sca.live.exchanges.private_client import PrivateReadClient  # noqa: E402


def _raw_bal(usdc=0.0, usdt=0.0):
    return {
        "USDC": {"free": usdc, "used": 0.0, "total": usdc},
        "USDT": {"free": usdt, "used": 0.0, "total": usdt},
        "free": {"USDC": usdc, "USDT": usdt},
        "used": {"USDC": 0.0, "USDT": 0.0},
        "total": {"USDC": usdc, "USDT": usdt},
        "info": {"data": []},
    }


class _FakeExchange:
    NO_ORDER = ("create_order", "edit_order", "cancel_order", "cancel_all_orders")

    def __init__(self, cfg, raw_balance, open_orders):
        self.cfg = cfg
        self._raw = raw_balance
        self._open = open_orders
        self.fetch_balance_params = "UNSET"
        self.fetch_open_args = None
        self.sandbox = None

    def fetch_balance(self, params=None):
        self.fetch_balance_params = params
        return self._raw

    def fetch_open_orders(self, symbol=None, params=None):
        self.fetch_open_args = (symbol, params)
        return list(self._open)

    def set_sandbox_mode(self, on):
        self.sandbox = on

    def __getattr__(self, name):
        if name in _FakeExchange.NO_ORDER:
            raise AssertionError(f"read-only client must never call {name}")
        raise AttributeError(name)


class _FakeCcxt:
    def __init__(self, raw_balance, open_orders=None):
        self.constructed_cfg = None
        self.ex = None
        self._raw = raw_balance
        self._open = open_orders or []

    def bitget(self, cfg):
        self.constructed_cfg = cfg
        self.ex = _FakeExchange(cfg, self._raw, self._open)
        return self.ex


_CFG = {"api_key_env": "BITGET_API_KEY", "api_secret_env": "BITGET_API_SECRET",
        "api_passphrase_env": "BITGET_API_PASSPHRASE"}
_ENV = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}


def test_get_wallet_balance_returns_adapter_normalized_shape():
    fc = _FakeCcxt(_raw_bal(usdc=8000.0, usdt=10.0))
    c = PrivateReadClient(BitgetAdapter(), ccxt_module=fc, live_cfg=_CFG, env=_ENV)
    bal = c.get_wallet_balance()
    # the EXACT shape reconcile reads: coins[COIN]["wallet"]
    assert bal["account_type"] == "spot"
    assert bal["coins"]["USDC"]["wallet"] == 8000.0
    assert bal["coins"]["USDT"]["wallet"] == 10.0


def test_get_open_orders_account_wide_passes_none_symbol():
    fc = _FakeCcxt(_raw_bal(usdc=1.0), open_orders=[{"clientOrderId": "x"}])
    c = PrivateReadClient(BitgetAdapter(), ccxt_module=fc, live_cfg=_CFG, env=_ENV)
    rows = c.get_open_orders(None)
    assert fc.ex.fetch_open_args == (None, None)   # account-wide
    assert rows == [{"clientOrderId": "x"}]


def test_construct_resolves_creds_and_passphrase():
    fc = _FakeCcxt(_raw_bal())
    PrivateReadClient(BitgetAdapter(), ccxt_module=fc, live_cfg=_CFG, env=_ENV)
    cfg = fc.constructed_cfg
    assert cfg["apiKey"] == "k" and cfg["secret"] == "s"
    assert cfg["password"] == "p"            # Bitget passphrase wired in
    assert cfg["verbose"] is False           # never log signed headers/keys


def test_missing_creds_raise_clear_error():
    fc = _FakeCcxt(_raw_bal())
    with pytest.raises(RuntimeError, match="credentials missing"):
        PrivateReadClient(BitgetAdapter(), ccxt_module=fc, live_cfg=_CFG, env={})


def test_repr_redacts_credentials():
    fc = _FakeCcxt(_raw_bal())
    c = PrivateReadClient(BitgetAdapter(), ccxt_module=fc, live_cfg=_CFG, env=_ENV)
    r = repr(c)
    assert "k" not in r or "redact" in r.lower()
    assert "redact" in r.lower()
