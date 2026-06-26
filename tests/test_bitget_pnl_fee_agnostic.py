"""Phase 2, Task 4 — Bitget PnL is 0-fee / fee-agnostic (no hardcoded Bybit fee).

Two guarantees:
  1. Both venue adapters report ``maker_fee == 0`` (stablecoin 0-fee; the ccxt market
     default of 0.1% is NOT trusted). A regression that let a non-zero ccxt default
     leak in would silently haircut Bitget USDC PnL.
  2. The engine's realized PnL is computed as a PURE price spread
     (``(sell_px - buy_px) * qty``) with NO fee term — verified by driving a real
     sell->rebuy round-trip through an engine whose adapter is BitgetAdapter and
     asserting the booked ``realized_capture`` equals the unhaircut spread exactly.
     This pins that Bitget PnL is computed the SAME fee-free way as Bybit (the engine
     never multiplies a fee into the capture), so no Bybit fee assumption corrupts it.

ISOLATION: no network; the engine runs in dryrun (simulated fills), fields set
directly. ``evaluate_fills`` is the dryrun mid-based settlement that books capture.

Run: PYTHONPATH=src python3 -m pytest tests/test_bitget_pnl_fee_agnostic.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine                     # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter         # noqa: E402
from sca.live.exchanges.bybit import BybitAdapter           # noqa: E402
from sca.strategy_rules import rounded_rebuy_price          # noqa: E402


def test_both_venues_report_zero_maker_fee():
    """Cross-venue parity: a 0-fee stablecoin maker is 0 on BOTH venues."""
    assert BybitAdapter().maker_fee("USD1USDT") == 0.0
    assert BitgetAdapter().maker_fee("USDCUSDT") == 0.0


def test_bitget_realized_pnl_is_pure_spread_no_fee(tmp_path):
    """Drive one sell->rebuy round-trip on a Bitget-adapter engine; the booked
    realized capture must equal the UNHAIRCUT price spread (sell_px - buy_px) * qty —
    proving no fee is subtracted from Bitget PnL."""
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.adapter = BitgetAdapter()
    eng.deployed = True
    eng.anchor = 1.0000
    eng.realized_capture = 0.0
    # one slice already SOLD (usdt state) at sell_px=1.0010, holding cash to rebuy.
    sell_px = 1.0010
    cash = 3000.0
    eng.slices = [{"state": "usdt", "qty": 0.0, "cash": cash,
                   "sell_px": sell_px, "entry": None}]
    # rebuy price the engine will compute, then make the ask cross it so the fill fires.
    from sca.live.engine import TICK_DP
    B = rounded_rebuy_price(eng.anchor, eng.rebuy_off_bp, TICK_DP, bid=None)
    eng.bid = None
    eng.ask = B                     # ask <= B -> rebuy fills @ B

    eng.evaluate_fills(now=1000.0)

    nq = cash / B
    expected = (sell_px - B) * nq   # PURE spread, zero fee
    assert eng.slices[0]["state"] == "usd1"
    assert abs(eng.realized_capture - expected) < 1e-12
    # sanity: a fee-haircut PnL would be strictly smaller; assert it is NOT haircut.
    assert eng.realized_capture == expected
