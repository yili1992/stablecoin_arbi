"""Regression lock: a RESUMED-DEPLOYED maker restart must NOT auto-deploy idle wallet quote.

top-up (``_topup_to_cap``) was REMOVED 2026-06-30 — capacity expansion is now a manual
clean-then-reseed, never an implicit grab of idle funds on restart. A resumed bot
reconciles its EXISTING slices and PROCEEDS; idle USDT sitting in the (possibly shared)
account is left untouched. This pins that contract so top-up can't silently return.

Run: PYTHONPATH=src python3 -m pytest tests/test_resume_no_topup.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine          # noqa: E402


def _bal_uc(usdc=0.0, usdt=0.0):
    total = usdc + usdt
    return {"account_type": "spot",
            "totals": {"equity_usd": total, "wallet_usd": total, "available_usd": total,
                       "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
            "coins": {"USDC": {"wallet": usdc, "locked": 0.0, "free": usdc, "usd": usdc, "borrow": 0.0},
                      "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": usdt, "borrow": 0.0}}}


def _usdc_slice(qty, entry=0.9998):
    return {"state": "usd1", "qty": qty, "cash": 0.0, "sell_px": 0.0, "entry": entry}


def _eng(tmp_path, slices, cap=1000.0):
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1, csv_path=str(tmp_path / "o.csv"))
    eng.armed = True
    eng.maker_enabled = True
    eng.slices = [dict(s) for s in slices]
    eng.deployed = bool(slices)
    eng._resumed = True
    eng._max_total_alloc_usd = cap
    eng._deployed_capital = sum(s["qty"] for s in slices) + sum(s.get("cash", 0.0) for s in slices)
    eng.persist = True
    return eng


class _FakeClient:
    def __init__(self, bal, orders=None):
        self._b, self._o = bal, orders or []

    def get_wallet_balance(self):
        return self._b

    def get_open_orders(self, symbol=None):
        return self._o


def test_resume_with_ample_idle_does_not_deploy(tmp_path):
    # A resumed-deployed USDC bot (one $400 slice, cap 1000) restarts with $5000 idle USDT
    # in the wallet. With top-up REMOVED it must reconcile the existing slice and PROCEED
    # WITHOUT appending any quote slice — the idle USDT is never auto-deployed. (Under the
    # old top-up this deployed headroom=$600 as a 2nd slice; that auto-grab is now gone.)
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=5000.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 1                       # idle USDT untouched (no top-up)
    assert eng.slices[0] == _usdc_slice(400.0)        # existing slice (cost basis) byte-identical


def test_resume_no_idle_proceeds_unchanged(tmp_path):
    # Resume with no idle quote also proceeds unchanged (held both before and after removal).
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=0.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 1
