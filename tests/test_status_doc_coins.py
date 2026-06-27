"""status_doc exposes the per-symbol (base, quote) coins from ``_coins()`` so the
dashboard can label holdings/deployment with the ACTUAL coin (USDC on the USDC
card) instead of a hard-coded "USD1".

``_coins()`` is the engine's canonical symbol -> (base, quote) splitter, already
trusted by the R1 reconciliation gate. Sourcing the dashboard labels from the
same place keeps coin identity single-source (hard rule #1) and mirrors how the
per-symbol ``exchange`` id is exposed (commit 7e84168 / test_status_doc_exchange).

Run:  PYTHONPATH=src python -m pytest tests/test_status_doc_coins.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine  # noqa: E402


def test_status_doc_exposes_base_quote_for_usd1():
    """USD1USDT -> base USD1 / quote USDT (the legacy default symbol)."""
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1)
    doc = eng.status_doc(1000.0)
    assert doc["base"] == "USD1"
    assert doc["quote"] == "USDT"


def test_status_doc_exposes_base_quote_for_usdc():
    """USDCUSDT card must say USDC, not USD1 — proves the base coin is per-symbol,
    not a hard-coded "USD1". This is the exact bug: the USDC panel showed
    "USD1 持有 0.0%"."""
    eng = PaperEngine(symbol="USDCUSDT", mode="paper", seconds=1)
    doc = eng.status_doc(1000.0)
    assert doc["base"] == "USDC"
    assert doc["quote"] == "USDT"


def test_status_doc_base_quote_flow_from_coins_not_hardcoded(monkeypatch):
    """Strictly prove the fields flow from ``_coins()`` (the canonical splitter)
    and are NOT a hard-coded symbol->coin table: patch ``_coins`` to a sentinel
    and assert it propagates verbatim into status_doc."""
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1)
    monkeypatch.setattr(eng, "_coins", lambda: ("XYZ", "ABC"))
    doc = eng.status_doc(1000.0)
    assert doc["base"] == "XYZ"
    assert doc["quote"] == "ABC"
