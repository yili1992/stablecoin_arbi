"""rung_for clamp — slice 数 > 配置 rung/fraction 数时不再 IndexError.

当 slice 数超过配置的 rung/fraction 数(例:USD1 线上 resumed state 有 6 个 slice 但只配 5 个
rung;历史上已移除的 top-up 也会触发),LIVE maker desired_orders / status_doc / dryrun sim
都按 slice index 索引 rungs[i]/fracs[i] => 必须 clamp,否则越界 IndexError 崩 (live maker 崩单
/ status_doc 崩 canary tick).

Run: PYTHONPATH=src python3 -m pytest tests/test_rung_for_clamp.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.strategy_rules import rung_for          # noqa: E402
from sca.live.order_recon import desired_orders   # noqa: E402
from sca.live.engine import PaperEngine           # noqa: E402


def test_rung_for_within_bounds():
    assert rung_for([1, 2, 3], 0) == 1
    assert rung_for([1, 2, 3], 2) == 3


def test_rung_for_clamps_overflow_to_last():
    assert rung_for([1], 1) == 1        # single-rung USDC: slice 1 reuses rung 0
    assert rung_for([1], 5) == 1
    assert rung_for([1, 2], 4) == 2     # multi-rung: clamp to last


def test_desired_orders_two_usd1_slices_single_rung_no_indexerror():
    # 2 USDC-holding (sell) slices but rungs=[1] -> must NOT raise; both rest a SELL.
    slices = [
        {"state": "usd1", "qty": 400.0, "cash": 0.0, "sell_px": 0.0, "entry": 0.9998,
         "order_link_id": None},
        {"state": "usd1", "qty": 600.0, "cash": 0.0, "sell_px": 0.0, "entry": 0.9999,
         "order_link_id": None},
    ]
    out = desired_orders(1.0, slices, [1], rebuy_off_bp=-1.0, tick=1e-4, lot=1e-6,
                         avail_base=1000.0, avail_quote=0.0, min_qty=0.0, min_cost=0.0)
    assert set(out.keys()) == {0, 1}                 # both slices emitted a desired order
    assert all(d.side == "sell" for d in out.values())


def test_status_doc_two_slices_single_config_no_indexerror(tmp_path):
    # Codex P1: status_doc runs every live tick; 2 slices + fractions=[1] must NOT raise
    # (rungs[1] at :909 AND fracs[1] at :916). Overflow slice frac displays 0.0.
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1, csv_path=str(tmp_path / "o.csv"))
    eng.slices = [
        {"state": "usd1", "qty": 400.0, "cash": 0.0, "sell_px": 0.0, "entry": 0.9998},
        {"state": "usdt", "qty": 0.0, "cash": 600.0, "sell_px": 0.0, "entry": None},
    ]
    eng.deployed = True
    eng.anchor = 1.0
    eng.bid, eng.ask = 0.9999, 1.0001
    doc = eng.status_doc(0.0)                         # RED before fix: IndexError on fracs[1]/rungs[1]
    sl = doc["position"]["slices"]
    assert len(sl) == 2
    assert sl[1]["frac"] == 0.0                       # appended slice -> frac clamped to 0.0
