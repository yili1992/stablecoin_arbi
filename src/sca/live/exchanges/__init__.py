"""Exchange adapter registry.

``adapter_for(symbol)`` returns the ExchangeAdapter for that symbol's configured
venue (``config.exchange_for``). Default is Bybit (every current symbol); Bitget
(Phase 1 T2) is the second venue for the USDC-on-Bitget plan.
"""
from __future__ import annotations

from sca.config import CFG, exchange_for
from sca.live.exchanges.base import ExchangeAdapter
from sca.live.exchanges.bitget import BitgetAdapter
from sca.live.exchanges.bybit import BybitAdapter


def adapter_for(symbol: str, cfg: dict | None = None) -> ExchangeAdapter:
    """Return the ExchangeAdapter for ``symbol``'s configured exchange.

    Bybit is the default (zero-change). The WS url override (``dryrun.ws_url``,
    matching the legacy engine ``WS_URL = _D.get("ws_url", ...)``) is threaded into
    BybitAdapter so an existing config that set it keeps working bit-identically.
    """
    ex = exchange_for(symbol, cfg=cfg)
    if ex == "bybit":
        dry = (CFG if cfg is None else cfg).get("dryrun", {}) or {}
        return BybitAdapter(ws_url=dry.get("ws_url"))
    if ex == "bitget":
        return BitgetAdapter()
    raise ValueError(f"unknown exchange {ex!r} for symbol {symbol!r}")
