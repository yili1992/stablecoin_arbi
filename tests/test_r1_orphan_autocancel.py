"""R1 auto-cancel-orphans relaxation (boss 2026-06-30): live maker bot should NOT
hard-refuse on resting orders — auto-cancel OUR own (sca-*/scaX-*) orphans, IGNORE
foreign. Gated by ``live.auto_cancel_orphans`` (code default False = strict legacy;
shipped config True). Other safeties (balance/liability lower-bound, halt-on-orphan-
FILL) are preserved.

Run: PYTHONPATH=src python3 -m pytest tests/test_r1_orphan_autocancel.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.reconcile import reconcile          # noqa: E402


def _ex(base=0.0, quote=0.0):
    return {"coins": {"USDC": {"wallet": base}, "USDT": {"wallet": quote}}}


def _resumed(base_qty):
    return {"resumed": True, "deployed": True, "base_qty": base_qty, "quote_qty": 0.0}


def test_reconcile_autocancel_true_does_not_refuse_on_unexpected():
    # an unexpected resting order present; auto_cancel_orphans=True => reconcile must NOT
    # refuse (disposition delegated to resume_reconcile_orders); proceeds on resumed balance.
    rep = reconcile(_resumed(100.0), _ex(base=100.0), [{"clientOrderId": "scaX9-9"}],
                    base_coin="USDC", quote_coin="USDT", tol=1.0, dedicated=False,
                    expected=set(), auto_cancel_orphans=True)
    assert rep["action"] == "proceed"


def test_reconcile_strict_default_still_refuses_on_unexpected():
    # regression: code default (auto_cancel_orphans=False) keeps the strict P1 refuse.
    rep = reconcile(_resumed(100.0), _ex(base=100.0), [{"clientOrderId": "scaX9-9"}],
                    base_coin="USDC", quote_coin="USDT", tol=1.0, dedicated=False,
                    expected=set())
    assert rep["action"] == "refuse"


def test_reconcile_autocancel_true_still_refuses_on_balance_shortfall():
    # the relaxation is ONLY about resting orders — a real balance shortfall (coins missing)
    # must STILL refuse even with auto_cancel_orphans=True (lower-bound safety preserved).
    rep = reconcile(_resumed(100.0), _ex(base=10.0), [{"clientOrderId": "scaX9-9"}],
                    base_coin="USDC", quote_coin="USDT", tol=1.0, dedicated=False,
                    expected=set(), auto_cancel_orphans=True)
    assert rep["action"] == "refuse"   # exchange base 10 << local 100 -> shortfall
