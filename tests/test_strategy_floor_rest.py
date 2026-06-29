"""Regression tests for the shared floor/rest sell-pricing rule.

Run: PYTHONPATH=src python3 -m pytest tests/test_strategy_floor_rest.py -q
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.backtest import strategy as S  # noqa: E402


def _df(*, anchor=0.9990, high=0.9991):
    return pd.DataFrame({
        "ts": [1_700_000_000_000, 1_700_000_300_000],
        "open": [1.0, 1.0],
        "high": [high, high],
        "low": [0.9990, 0.9990],
        "close": [1.0, 1.0],
        "ema_anchor": [anchor, anchor],
        "turnover": [1_000_000.0, 1_000_000.0],
    })


def test_backtest_min_profit_floor_blocks_anchor_loss_sale(monkeypatch):
    monkeypatch.setattr(S, "FRACS", [1.0])
    monkeypatch.setattr(S, "RUNG_BP", [1])
    monkeypatch.setattr(S, "MIN_PROFIT_BP", 1.0, raising=False)
    monkeypatch.setattr(S, "REST_BPS", 0.0, raising=False)
    monkeypatch.setattr(S, "MIN_SELL_MARGIN_BP", 0.0, raising=False)  # isolate min_profit floor (margin would mask its regression)

    r = S.backtest(0.0, with_yield=False, fill_mode="touch", df=_df())

    assert r["sells"] == 0
    assert r["rebuys"] == 0


def test_backtest_rest_bps_triggers_surrender_sale(monkeypatch):
    monkeypatch.setattr(S, "FRACS", [1.0])
    monkeypatch.setattr(S, "RUNG_BP", [1])
    monkeypatch.setattr(S, "MIN_PROFIT_BP", 1.0, raising=False)
    monkeypatch.setattr(S, "REST_BPS", 15.0, raising=False)

    r = S.backtest(0.0, with_yield=False, fill_mode="touch",
                   df=_df(anchor=0.9984, high=0.9985))

    assert r["sells"] == 1
    assert r["rebuys"] == 0
    assert r["slices_idle_end"] == 1


def test_backtest_floor_zero_degenerates_to_anchor_rung(monkeypatch):
    monkeypatch.setattr(S, "FRACS", [1.0])
    monkeypatch.setattr(S, "RUNG_BP", [1])
    monkeypatch.setattr(S, "MIN_PROFIT_BP", 0.0, raising=False)
    monkeypatch.setattr(S, "REST_BPS", 0.0, raising=False)
    monkeypatch.setattr(S, "MIN_SELL_MARGIN_BP", 0.0, raising=False)  # isolate min_profit=0 anchor+rung (no margin floor)

    r = S.backtest(0.0, with_yield=False, fill_mode="touch", df=_df())

    assert r["sells"] == 1
