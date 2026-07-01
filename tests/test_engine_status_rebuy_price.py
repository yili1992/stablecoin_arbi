"""Task 7 — Codex #5: dashboard hold 期显示真实挂单价 (order_px).

_status_rebuy_price 当前始终返回 fresh target，hold 期 (有活买单) 应返回
该 slice 的 order_px (真实挂单价)，而非重新计算的 fresh target.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_status_rebuy_price.py -q
"""
import pytest
from sca.live.engine import PaperEngine, TICK_DP


def _make_rebuy_engine_stub(*, mode="dryrun", rebuy_off_bp=1.0, ask=None, slices=None):
    """Minimal engine stub for testing _status_rebuy_price.

    Uses object.__new__ to bypass __init__, then patches the exact attributes
    that _status_rebuy_price reads. Pattern from test_engine_last_place_ts.py.
    """
    eng = object.__new__(PaperEngine)
    eng.mode = mode
    eng.rebuy_off_bp = float(rebuy_off_bp)
    eng.ask = ask
    eng.slices = slices if slices is not None else []
    return eng


def _buy_slice(order_px):
    """A slice with an active buy order at order_px."""
    return {
        "state": "usd1",
        "qty": 100.0,
        "cash": 0.0,
        "sell_px": 0.0,
        "entry": 1.0010,
        "order_id": "oid-001",
        "order_link_id": "link-001",
        "order_px": order_px,
        "order_side": "buy",
        "order_qty": 100.0,
        "filled_qty": 0.0,
    }


def _no_order_slice():
    """A slice with no resting order."""
    return {
        "state": "usd1",
        "qty": 100.0,
        "cash": 0.0,
        "sell_px": 0.0,
        "entry": 1.0010,
        "order_id": None,
        "order_link_id": None,
        "order_px": None,
        "order_side": None,
        "order_qty": None,
        "filled_qty": 0.0,
    }


# ---------------------------------------------------------------------------
# RED tests (written before implementation)
# ---------------------------------------------------------------------------

def test_status_rebuy_shows_resting_price_when_held():
    """Hold 期有活买单时显示真实挂单价 (order_px)，不显示 fresh target."""
    eng = _make_rebuy_engine_stub(
        mode="dryrun",
        rebuy_off_bp=1.0,
        ask=1.0012,   # fresh target would be ~1.0011 (1.0012 - 1bp)
        slices=[_buy_slice(order_px=1.0009)],
    )
    # Fresh target ≠ 1.0009; should show the pinned order_px instead.
    result = eng._status_rebuy_price(1.0011)
    assert result == pytest.approx(1.0009)


def test_status_rebuy_shows_fresh_target_when_no_order():
    """无活买单时，显示 fresh target (原行为保持)."""
    eng = _make_rebuy_engine_stub(
        mode="dryrun",
        rebuy_off_bp=1.0,
        ask=1.0012,
        slices=[_no_order_slice()],
    )
    # rebuy_price_raw(anchor=1.0011, rebuy_off_bp=1.0, ask=1.0012) → 1.0011 - 1bp = 1.001
    result = eng._status_rebuy_price(1.0011)
    assert result is not None
    # Must NOT return 1.0009 (no active order_px); must be close to fresh target
    assert abs(result - 1.0009) > 1e-6


def test_status_rebuy_sell_side_order_ignored():
    """卖单 (order_side='sell') 不触发 order_px 显示逻辑，走 fresh target."""
    eng = _make_rebuy_engine_stub(
        mode="dryrun",
        rebuy_off_bp=1.0,
        ask=1.0012,
        slices=[{
            "state": "usd1",
            "qty": 0.0,
            "cash": 10.0,
            "sell_px": 1.0020,
            "entry": 1.0010,
            "order_id": "oid-sell",
            "order_link_id": "link-sell",
            "order_px": 1.0020,
            "order_side": "sell",
            "order_qty": 100.0,
            "filled_qty": 0.0,
        }],
    )
    result = eng._status_rebuy_price(1.0011)
    # sell-side slice → no active buy order → fresh target, not 1.0020
    assert result != pytest.approx(1.0020)


def test_status_rebuy_multi_slice_first_buy_wins():
    """多 slice 时第一个活买单的 order_px 胜出."""
    eng = _make_rebuy_engine_stub(
        mode="dryrun",
        rebuy_off_bp=1.0,
        ask=1.0012,
        slices=[
            _no_order_slice(),               # no buy order
            _buy_slice(order_px=1.0008),     # first active buy
            _buy_slice(order_px=1.0007),     # second active buy (not reached)
        ],
    )
    result = eng._status_rebuy_price(1.0011)
    assert result == pytest.approx(1.0008)


def test_status_rebuy_paper_mode_preserves_original_behavior():
    """paper 模式走 rounded_rebuy_price(原逻辑)，即使有活买单也走原分支.

    paper 分支在 mode not in ('dryrun','live') 下不走 hold-aware check，
    保持原有 paper 口径 (rounded_rebuy_price 不用 ask)。
    """
    eng = _make_rebuy_engine_stub(
        mode="paper",
        rebuy_off_bp=1.0,
        ask=1.0012,
        slices=[_buy_slice(order_px=1.0009)],
    )
    # paper 分支: rounded_rebuy_price(anchor, rebuy_off_bp, TICK_DP)
    # anchor=1.0011, off=1bp → ~1.001
    result = eng._status_rebuy_price(1.0011)
    # paper 不受 order_px 影响，结果不应该是 1.0009
    assert result != pytest.approx(1.0009)


def test_status_rebuy_none_anchor_returns_none():
    """anchor is None → None (边界，原有行为保持)."""
    eng = _make_rebuy_engine_stub(
        mode="dryrun",
        slices=[_buy_slice(order_px=1.0009)],
    )
    assert eng._status_rebuy_price(None) is None
