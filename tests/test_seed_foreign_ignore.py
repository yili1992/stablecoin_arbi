"""Shared-account seed: a from-scratch seed must IGNORE foreign (the operator's MANUAL) open
orders — only OUR OWN sca-*/scaX- resting orders block it as ambiguous lost state — mirroring
how resume ignores foreign orders (auto_cancel_orphans). Strict (flag off) still refuses on ANY
order. The single-side balance gate is NOT relaxed (a mixed wallet still refuses — a fresh seed
cannot infer which coins are the strategy's vs the operator's manual capital).

Run: PYTHONPATH=src python3 -m pytest tests/test_seed_foreign_ignore.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine          # noqa: E402


def _eng(tmp_path, cap=1000.0, auto_cancel=True):
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1, csv_path=str(tmp_path / "o.csv"))
    eng.armed = True
    eng.maker_enabled = True
    eng._max_total_alloc_usd = cap
    eng._auto_cancel_orphans = auto_cancel
    return eng


def _bal(usdc=0.0, usdt=0.0):
    # Bitget-shape (per-coin usd=0); single-side USDT when usdc=0
    return {"account_type": "spot",
            "totals": {"equity_usd": 0.0, "wallet_usd": 0.0, "available_usd": 0.0,
                       "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
            "coins": {"USDC": {"wallet": usdc, "locked": 0.0, "free": usdc, "usd": 0.0, "borrow": 0.0},
                      "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": 0.0, "borrow": 0.0}}}


def _foreign(side="sell", price=1.0008, link="manual-abc-123"):
    return {"id": "f1", "clientOrderId": link, "side": side, "price": price, "amount": 400.0}


def _ours(eng, side="buy", price=1.0009):
    # an order carrying OUR venue-echoed ownership prefix (Bybit "sca-" / Bitget "scaX")
    return {"id": "o1", "clientOrderId": eng._ours_prefix() + "0-0",
            "side": side, "price": price, "amount": 100.0}


def test_seed_ignores_foreign_order_on_single_side(tmp_path):
    # The boss's shared-account scenario (after the manual USDC sale clears): single-side USDT
    # 1200 + a leftover FOREIGN manual order resting -> the foreign order is IGNORED and the
    # strategy seeds $1000 from USDT (no longer hard-refused).
    eng = _eng(tmp_path)
    eng._seed_slices_from_balance(_bal(usdc=0.0, usdt=1200.0), [_foreign()])
    assert eng.deployed
    assert len(eng.slices) == len(eng.fracs)             # seeded; foreign order ignored
    assert all(s["state"] == "usdt" for s in eng.slices)


def test_seed_ignores_linkless_foreign_order(tmp_path):
    # a manual order placed via the exchange UI may carry NO clientOrderId — it is still NOT ours
    # (we always tag our own orders sca-*/scaX-), so it is ignored on a shared account.
    eng = _eng(tmp_path)
    o = {"id": "f2", "side": "sell", "price": 1.0008, "amount": 400.0}   # no clientOrderId/link_id
    eng._seed_slices_from_balance(_bal(usdc=0.0, usdt=1200.0), [o])
    assert eng.deployed and len(eng.slices) == len(eng.fracs)


def test_seed_still_refuses_our_own_orphan(tmp_path):
    # an OUR (sca-*/scaX-) order with NO local state = lost position -> still refuse (conservative:
    # never auto-cancel our own orders when we have no idea what state they represent).
    eng = _eng(tmp_path)
    with pytest.raises(SystemExit):
        eng._seed_slices_from_balance(_bal(usdc=0.0, usdt=1200.0), [_ours(eng)])


def test_seed_strict_refuses_any_order(tmp_path):
    # auto_cancel_orphans OFF (dedicated account) -> ANY pre-existing order refuses (backward compat).
    eng = _eng(tmp_path, auto_cancel=False)
    with pytest.raises(SystemExit):
        eng._seed_slices_from_balance(_bal(usdc=0.0, usdt=1200.0), [_foreign()])


def test_seed_foreign_ignored_but_mixed_balance_still_refuses(tmp_path):
    # foreign order ignored, BUT USDC 400 + USDT 1200 both material -> the single-side gate still
    # refuses (mixed). The order relaxation does NOT bypass the single-side requirement.
    eng = _eng(tmp_path)
    with pytest.raises(SystemExit):
        eng._seed_slices_from_balance(_bal(usdc=400.0, usdt=1200.0), [_foreign()])
