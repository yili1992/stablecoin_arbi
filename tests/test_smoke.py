"""Smoke tests — data loads, backtests run, and the headline FINDINGS hold.

These encode the project's conclusions as invariants (boros-style):
  - the canary floor/rest probe (current config: min_profit=1bp/rest=15bp on
    low rungs [1,2,3,4,5]) is kept to generate fills for markout measurement,
    NOT because optimistic touch-fill results prove a durable edge
  - under conservative strict + liquidity-gated fills, it still underperforms
    honest buy-and-hold once adverse selection is non-zero
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


def test_canary_floor_rest_probe_generates_touch_fill_markout_samples_adv05():
    """Touch fills are optimistic; this path is kept to ensure the probe still
    creates positive price skim and enough samples to measure live markout."""
    r = S.backtest(0.5, fill_mode="touch")
    assert r["price_cap_pct"] > 0.0         # it does trade (the reason to keep it)
    assert r["sells"] / r["span_d"] > 1.0   # enough samples for markout measurement


def test_canary_floor_rest_probe_loses_to_hold_strict_gate():
    """Under conservative strict + 20% liquidity gate the probe remains below
    honest buy-and-hold; do not promote this as durable edge."""
    r = S.backtest(0.5, fill_mode="strict", liq_gate=0.2)
    hold = S.hold_benchmark(0.5)
    assert r["apr"] < hold
    assert 6.5 < r["apr"] < hold


def test_engine_baseline_loses_to_hold():
    hold_apr = E.APR["USD1USDT"] * 100
    r = E.run("USD1USDT", with_yield=True, adverse_bp=1.0)
    assert r["apr_pct"] < hold_apr          # original strategy < hold carry (the core finding)


if __name__ == "__main__":
    test_data_loads()
    test_canary_floor_rest_probe_generates_touch_fill_markout_samples_adv05()
    test_canary_floor_rest_probe_loses_to_hold_strict_gate()
    test_engine_baseline_loses_to_hold()
    print("all smoke tests passed")
