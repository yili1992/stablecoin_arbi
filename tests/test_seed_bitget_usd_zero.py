"""Seed valuation mark must fall back to $1 FACE when the venue reports per-coin usd=0.

Bitget ``normalize_balance`` hardcodes ``usd: 0.0`` (spot USD valuation not computed).
``_seed_slices_from_balance`` base-funded branch marks slice ``entry`` and
``_deployed_capital`` off ``coin_usd / amount``; without the ``base_usd > 0`` guard that
mark collapses to 0 -> entry=0 (sell loses its cost floor) + _deployed_capital=0 (broken
PnL). This guards that face-fallback. (Extracted from the removed test_engine_topup.py when
top-up was deleted, 2026-06-30 — the SEED fix is independent of top-up and stays.)

Run: PYTHONPATH=src python3 -m pytest tests/test_seed_bitget_usd_zero.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine          # noqa: E402


def _bal_bitget(usdc=0.0, usdt=0.0):
    """Bitget-shape balance: per-coin ``usd`` field is 0 (bitget.py normalize_balance hardcodes
    ``"usd": 0.0`` — spot USD valuation not computed). The valuation mark MUST fall back to $1
    face here, else mark=0 zeroes the position. Real-money regression fixture (2026-06-30)."""
    return {"account_type": "spot",
            "totals": {"equity_usd": 0.0, "wallet_usd": 0.0, "available_usd": 0.0,
                       "im_usd": 0.0, "mm_usd": 0.0, "perp_upl_usd": 0.0},
            "coins": {"USDC": {"wallet": usdc, "locked": 0.0, "free": usdc, "usd": 0.0, "borrow": 0.0},
                      "USDT": {"wallet": usdt, "locked": 0.0, "free": usdt, "usd": 0.0, "borrow": 0.0}}}


def test_seed_base_funded_bitget_usd_zero_marks_at_face(tmp_path):
    # A fresh seed from a base (USDC) balance must mark entry + _deployed_capital at $1 FACE,
    # not $0 — else cost basis=0 (sell pricing loses its cost floor) and PnL baseline=0.
    eng = PaperEngine(symbol="USDCUSDT", mode="dryrun", seconds=1, csv_path=str(tmp_path / "s.csv"))
    eng.armed = True
    eng.maker_enabled = True
    eng._max_total_alloc_usd = 1000.0
    eng._seed_slices_from_balance(_bal_bitget(usdc=500.0, usdt=0.0), [])   # clean single-side USDC, usd=0
    assert eng._deployed_capital > 0                    # NOT 0 (face mark, not mark=0)
    assert all(s["entry"] == 1.0 for s in eng.slices)   # base slices: entry=$1 face, not 0
