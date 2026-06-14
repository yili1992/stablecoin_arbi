"""Smoke tests — data loads, backtests run, and the headline FINDINGS hold.

These encode the project's conclusions as invariants (boros-style):
  - the recommended slice-ladder THINLY beats the flat-10% bar in-sample (touch fills)
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


def test_strategy_thinly_beats_flat10_in_sample_touch():
    r = S.backtest(0.5, fill_mode="touch")
    assert 10.0 < r["apr"] < 12.0          # beats flat-10 in-sample under optimistic fills


def test_strategy_survives_strict_liquidity_gate():
    r = S.backtest(0.5, fill_mode="strict", liq_gate=0.2)
    assert r["apr"] > 10.0                  # still clears flat-10 under conservative fills


def test_engine_baseline_loses_to_hold():
    r = E.run("USD1USDT", with_yield=True, adverse_bp=1.0)
    assert r["apr_pct"] < 10.0             # original strategy < 10% hold (the core finding)


if __name__ == "__main__":
    test_data_loads()
    test_strategy_thinly_beats_flat10_in_sample_touch()
    test_strategy_survives_strict_liquidity_gate()
    test_engine_baseline_loses_to_hold()
    print("all smoke tests passed")
