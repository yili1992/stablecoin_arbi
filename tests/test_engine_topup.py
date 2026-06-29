"""_topup_to_cap — resumed-deployed restart 部署闲置 USDT 到 cap, 保留旧仓成本.

headroom = cap - 现有持仓 MTM 估值;deploy = min(真实 idle USDT, headroom/quote_mark)。
幂等(补满后 no-op)、cap-bounded、不动现有 slice 的 entry、_deployed_capital 诚实累加。

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_topup.py -q
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
    return eng


def test_topup_deploys_headroom(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 2
    new = eng.slices[1]
    assert new["state"] == "usdt" and new["entry"] is None
    assert abs(new["cash"] - 600.0) < 1e-6          # headroom = 1000 - 400
    assert eng.slices[0] == _usdc_slice(400.0)      # existing slice (cost) UNTOUCHED


def test_topup_idempotent(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0), {"state": "usdt", "qty": 0.0, "cash": 600.0,
                                               "sell_px": 0.0, "entry": None}], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 2                      # already at cap -> no-op


def test_topup_cap_bound_ignores_excess_idle(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=5000.0), [])   # overfunded
    assert abs(eng.slices[1]["cash"] - 600.0) < 1e-6          # only headroom, NOT 5000


def test_topup_no_idle_quote_noop(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=0.0), [])
    assert len(eng.slices) == 1


def test_topup_cap_negative_noop(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=-1.0)
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert len(eng.slices) == 1


def test_topup_accumulates_deployed_capital(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    base = eng._deployed_capital
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert abs(eng._deployed_capital - (base + 600.0)) < 1e-6


def test_topup_deployed_capital_none_seeds_from_slice_value(tmp_path):
    # Codex P1: older resumed state has _deployed_capital=None (engine.py:371/:598).
    # Must NOT crash (None+x) NOR reset baseline to only top-up (fake loss): seed from pre-topup MTM.
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng._deployed_capital = None
    eng._topup_to_cap(_bal_uc(usdc=400.0, usdt=600.0), [])
    assert abs(eng._deployed_capital - 1000.0) < 1e-6     # 400 (pre-topup MTM) + 600 (deploy)


# --- Task 3: wiring inside the R1 gate (_reconcile_or_refuse) ---------------
class _FakeClient:
    def __init__(self, bal, orders=None):
        self._b, self._o = bal, orders or []

    def get_wallet_balance(self):
        return self._b

    def get_open_orders(self, symbol=None):
        return self._o


def test_gate_tops_up_then_proceeds(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng.persist = True
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=600.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 2                       # topped up INSIDE the gate
    assert abs(eng.slices[1]["cash"] - 600.0) < 1e-6


def test_gate_no_idle_resumes_unchanged(tmp_path):
    eng = _eng(tmp_path, [_usdc_slice(400.0)], cap=1000.0)
    eng.persist = True
    rep = eng._reconcile_or_refuse(client=_FakeClient(_bal_uc(usdc=400.0, usdt=0.0), []))
    assert rep["action"] == "proceed"
    assert len(eng.slices) == 1                       # nothing idle -> no top-up
