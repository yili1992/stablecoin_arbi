"""Unit tests for shared strategy price rules.

Run: PYTHONPATH=src python -m pytest tests/test_strategy_rules.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import pytest  # noqa: E402

from sca.strategy_rules import rebuy_price_raw  # noqa: E402


def test_rebuy_uses_anchor_when_bid_is_above_anchor():
    assert rebuy_price_raw(1.0009, -1, bid=1.0012) == pytest.approx(1.0008)


def test_rebuy_uses_bid_when_bid_is_below_anchor():
    assert rebuy_price_raw(1.0009, -1, bid=1.0002) == pytest.approx(1.0001)
