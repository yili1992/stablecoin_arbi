"""status_doc exposes the per-symbol exchange id (config.exchange_for) so the
dashboard can group its tabs BY EXCHANGE rather than by symbol.

Without this field the front-end has no honest way to know that USD1USDT trades
on Bybit while USDCUSDT trades on Bitget — it would have to hard-code a mapping
that drifts from config/strategy.yaml (the single source of truth, hard rule #1).

Run:  PYTHONPATH=src python -m pytest tests/test_status_doc_exchange.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine  # noqa: E402


def test_status_doc_exposes_exchange_default_bybit():
    """USD1USDT has no `exchange` field in universe -> defaults to bybit."""
    eng = PaperEngine(symbol="USD1USDT", mode="paper", seconds=1)
    assert eng.status_doc(1000.0)["exchange"] == "bybit"


def test_status_doc_exposes_exchange_bitget_for_usdc():
    """USDCUSDT is configured `exchange: bitget` -> status_doc must reflect it,
    proving the field is sourced per-symbol from config, not hard-coded."""
    eng = PaperEngine(symbol="USDCUSDT", mode="paper", seconds=1)
    assert eng.status_doc(1000.0)["exchange"] == "bitget"


def test_status_doc_exchange_comes_from_resolver_not_hardcoded(monkeypatch):
    """Strictly prove the field flows from config.exchange_for (the resolver) and
    is NOT a hard-coded symbol->exchange table: patch the resolver to a sentinel
    and assert it propagates verbatim into status_doc. adapter_for is stubbed to a
    Bybit adapter so the sentinel venue needs no real adapter registration."""
    import sca.live.engine as E
    from sca.live.exchanges.bybit import BybitAdapter

    monkeypatch.setattr(E, "exchange_for", lambda symbol, cfg=None: "sentinel-venue")
    monkeypatch.setattr(E, "adapter_for", lambda symbol, cfg=None: BybitAdapter())

    eng = E.PaperEngine(symbol="USD1USDT", mode="paper", seconds=1)
    assert eng.exchange == "sentinel-venue"
    assert eng.status_doc(1000.0)["exchange"] == "sentinel-venue"
