"""Smoke tests — data loads, backtests run, and the headline FINDINGS hold.

These encode the project's conclusions as invariants (boros-style):
  - the high-frequency rungs [1,2,3,4,5] (current config, lowered for trade
    frequency) UNDERPERFORM the flat-10% hold under BOTH touch and strict fills —
    kept to generate fills for markout measurement, NOT because it beats holding
  - the ORIGINAL Freqtrade strategy LOSES to holding USD1 at realistic adverse selection

Run:  PYTHONPATH=src python -m pytest tests/    (or: python tests/test_smoke.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.backtest import strategy as S   # noqa: E402
from sca.backtest import engine as E     # noqa: E402


def test_data_loads():
    df = S.load()
    assert len(df) > 1000
    assert {"ts", "open", "high", "low", "close", "ema_anchor"} <= set(df.columns)


def test_highfreq_rungs_lose_to_flat10_touch():
    """rungs [1,2,3,4,5] underperform flat-10 hold even under optimistic touch
    fills: extra trades skim a thin price edge but cost more carry (time out of
    USD1) + adverse drag than they earn. Honest finding — config kept for markout
    measurement, not edge. (Was 10.949% under old rungs [5,7,10,14,20].)"""
    r = S.backtest(0.5, fill_mode="touch")
    assert r["apr"] < 10.0                  # loses to flat-10 even optimistically
    assert r["apr"] > 8.0                   # carry floor keeps it close, not catastrophic
    assert r["price_cap_pct"] > 0.0         # it does trade (the reason to keep it)


def test_highfreq_rungs_lose_to_flat10_strict_gate():
    """Under conservative strict + 20% liquidity gate the gap to hold widens —
    the trade edge does not survive realistic fills. (Was 10.419% under old rungs.)"""
    r = S.backtest(0.5, fill_mode="strict", liq_gate=0.2)
    assert r["apr"] < 10.0                  # clearly loses to flat-10
    assert r["apr"] > 5.0                   # still carry-supported, not blown up


def test_engine_baseline_loses_to_hold():
    r = E.run("USD1USDT", with_yield=True, adverse_bp=1.0)
    assert r["apr_pct"] < 10.0             # original strategy < 10% hold (the core finding)


if __name__ == "__main__":
    test_data_loads()
    test_highfreq_rungs_lose_to_flat10_touch()
    test_highfreq_rungs_lose_to_flat10_strict_gate()
    test_engine_baseline_loses_to_hold()
    print("all smoke tests passed")
