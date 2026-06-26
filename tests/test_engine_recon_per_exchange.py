"""Phase 2, Task 1 — engine R1 reconcile builds a PER-EXCHANGE read client.

Before Phase 2 the gate hardcoded ``BybitPrivateClient(testnet=False)``. With USDC
moving to Bitget, the engine must pick the read client from ``self.adapter``:
  - Bybit symbol  -> the existing ``BybitPrivateClient`` (bit-identical path).
  - Bitget symbol -> an adapter-driven read client whose ``get_wallet_balance`` /
    ``get_open_orders`` go through ``BitgetAdapter`` (spot balance) + ccxt
    ``fetch_open_orders``.

The pure ``reconcile`` brain is venue-agnostic already; these tests pin the
SELECTION + the Bitget account-type acceptance, and prove the Bybit selection is
unchanged.

ISOLATION: no network. The default-client factory is monkeypatched / ccxt is
injected; balance comes from canned dicts in the real venue shapes.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_recon_per_exchange.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine                  # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter      # noqa: E402
from sca.live.exchanges.bybit import BybitAdapter        # noqa: E402


def _armed(tmp_path, symbol, *, resumed=False, slices=None):
    eng = PaperEngine(symbol=symbol, mode="paper", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.armed = True
    eng.persist = True
    eng._resumed = resumed
    eng.deployed = bool(slices)
    eng.slices = slices or []
    return eng


# === SELECTION: Bybit symbol still builds BybitPrivateClient ==================

def test_bybit_symbol_builds_bybit_private_client(tmp_path, monkeypatch):
    """A Bybit-adapter engine's default read client is the unchanged
    BybitPrivateClient (constructed mainnet, testnet=False)."""
    built = {}

    class FakeBybitClient:
        def __init__(self, testnet=None, **kw):
            built["cls"] = "bybit"
            built["testnet"] = testnet
        def get_wallet_balance(self):
            return _bybit_bal(usdt=10000.0)
        def get_open_orders(self, symbol=None):
            return []

    import sca.live.bybit_client as bc
    monkeypatch.setattr(bc, "BybitPrivateClient", FakeBybitClient)

    eng = _armed(tmp_path, "USD1USDT")
    assert isinstance(eng.adapter, BybitAdapter)
    # no-state + no opt-in => refuses, but only AFTER building the right client
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse()
    assert built["cls"] == "bybit"
    assert built["testnet"] is False        # mainnet (D14), unchanged


# === SELECTION: Bitget symbol builds the adapter-driven read client ===========

def test_bitget_symbol_builds_adapter_read_client(tmp_path, monkeypatch):
    """A Bitget-adapter engine must NOT construct BybitPrivateClient; its default
    read client is the adapter-driven PrivateReadClient built on self.adapter."""
    import sca.live.bybit_client as bc
    import sca.live.engine as eng_mod

    def boom(*a, **k):
        raise AssertionError("Bitget path must not construct BybitPrivateClient")
    monkeypatch.setattr(bc, "BybitPrivateClient", boom)

    captured = {}

    class FakePrivateReadClient:
        def __init__(self, adapter, **kw):
            captured["adapter"] = adapter
        def get_wallet_balance(self):
            return _bitget_norm_bal(usdc=8000.0, usdt=0.0)
        def get_open_orders(self, symbol=None):
            return []
    monkeypatch.setattr(eng_mod, "PrivateReadClient", FakePrivateReadClient)

    eng = _armed(tmp_path, "USDCUSDT")
    eng.adapter = BitgetAdapter()
    # clean single-side USDC, resumed=False, no opt-in => refuse (no-state), but the
    # POINT is it built the adapter-driven client (Bitget), never BybitPrivateClient.
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse()
    assert isinstance(captured["adapter"], BitgetAdapter)


# === Bitget spot account_type is accepted by the liability guard =============

def test_bitget_resumed_match_proceeds_spot_account(tmp_path):
    """A resumed Bitget position whose spot balance matches local state PROCEEDS.
    The liability guard must accept account_type='spot' (not only 'UNIFIED')."""
    slices = [
        {"state": "usdc", "qty": 5000.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": 3000.0, "sell_px": 1.0, "entry": None},
    ]
    eng = _armed(tmp_path, "USDCUSDT", resumed=True, slices=slices)
    eng.adapter = BitgetAdapter()
    client = _FakeReadClient(_bitget_norm_bal(usdc=5000.0, usdt=3000.0))
    rep = eng._reconcile_or_refuse(client=client)
    assert rep["action"] == "proceed" and rep["ok"] is True
    assert ("orders", None) in client.calls    # account-wide open-order check


def test_bitget_resumed_mismatch_refuses_spot_account(tmp_path):
    slices = [
        {"state": "usdc", "qty": 5000.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": 3000.0, "sell_px": 1.0, "entry": None},
    ]
    eng = _armed(tmp_path, "USDCUSDT", resumed=True, slices=slices)
    eng.adapter = BitgetAdapter()
    client = _FakeReadClient(_bitget_norm_bal(usdc=1000.0, usdt=3000.0))  # 4000 short
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


# === Task 3 — Bitget dedicated_account reconcile is EXACT (not lower-bound) ===

def test_bitget_dedicated_is_exact_over_balance_refuses(tmp_path):
    """USDC@Bitget is a dedicated single-symbol account => EXACT reconcile. An
    OVER-balance beyond tol (exchange holds MORE USDC than local) must REFUSE — the
    lower-bound (shared-UTA DEGRADED) path would wrongly PROCEED. This pins that the
    Bitget single-symbol account uses dedicated=True (EXACT)."""
    slices = [
        {"state": "usdc", "qty": 5000.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": 3000.0, "sell_px": 1.0, "entry": None},
    ]
    eng = _armed(tmp_path, "USDCUSDT", resumed=True, slices=slices)
    eng.adapter = BitgetAdapter()
    # exchange holds 5000 MORE USDC than local (10000 vs 5000) — EXACT must refuse.
    client = _FakeReadClient(_bitget_norm_bal(usdc=10000.0, usdt=3000.0))
    with pytest.raises(SystemExit):
        eng._reconcile_or_refuse(client=client)


def test_bitget_balance_flows_adapter_to_reconcile_end_to_end(tmp_path, monkeypatch):
    """End-to-end: the engine's DEFAULT Bitget read client is PrivateReadClient driving
    BitgetAdapter.fetch_balance_coins over a REAL ccxt-unified spot balance dict; that
    normalized shape feeds reconcile and a matching resumed position PROCEEDS. Proves
    per-exchange balance is correctly wired from ccxt -> adapter -> reconcile (not a
    hand-built norm dict)."""
    from sca.live.exchanges.private_client import PrivateReadClient

    raw = {
        "USDC": {"free": 4900.0, "used": 100.0, "total": 5000.0},
        "USDT": {"free": 3000.0, "used": 0.0, "total": 3000.0},
        "free": {"USDC": 4900.0, "USDT": 3000.0},
        "used": {"USDC": 100.0, "USDT": 0.0},
        "total": {"USDC": 5000.0, "USDT": 3000.0},
        "info": {"data": []},
    }

    class _Ex:
        def fetch_balance(self, params=None):
            return raw
        def fetch_open_orders(self, symbol=None, params=None):
            return []

    class _Ccxt:
        def bitget(self, cfg):
            return _Ex()

    real_client = PrivateReadClient(
        BitgetAdapter(), ccxt_module=_Ccxt(),
        live_cfg={"api_key_env": "K", "api_secret_env": "S"},
        env={"K": "k", "S": "s"},
    )
    slices = [
        {"state": "usdc", "qty": 5000.0, "cash": 0.0, "sell_px": 0.0, "entry": 1.0},
        {"state": "usdt", "qty": 0.0, "cash": 3000.0, "sell_px": 1.0, "entry": None},
    ]
    eng = _armed(tmp_path, "USDCUSDT", resumed=True, slices=slices)
    eng.adapter = BitgetAdapter()
    rep = eng._reconcile_or_refuse(client=real_client)
    assert rep["action"] == "proceed" and rep["ok"] is True
    # the wallet figures reconcile compared came from the adapter's normalization
    assert rep["exchange"]["USDC"]["wallet"] == 5000.0
    assert rep["exchange"]["USDT"]["wallet"] == 3000.0


# === helpers =================================================================

def _bybit_bal(usd1=0.0, usdt=0.0):
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


def _bitget_norm_bal(usdc=0.0, usdt=0.0):
    """The NORMALIZED shape BitgetAdapter.fetch_balance_coins produces (spot)."""
    total = usdc + usdt
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
    """Stand-in for the per-exchange read client (get_wallet_balance/get_open_orders)."""
    def __init__(self, balance, orders=None):
        self._b, self._o = balance, orders or []
        self.calls = []
    def get_wallet_balance(self):
        self.calls.append("balance")
        return self._b
    def get_open_orders(self, symbol=None):
        self.calls.append(("orders", symbol))
        return self._o
