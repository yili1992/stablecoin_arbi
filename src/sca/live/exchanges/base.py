"""ExchangeAdapter — the per-exchange coupling surface.

The engine / orders / read-only client used to inline Bybit-specific facts (WS
url + subscribe + quote parse, REST kline url, ccxt client construction, balance
map shape, order params, maker fee). Those are the points that differ per venue.
This abstract base names them so a second venue (Bitget) can be added without the
engine touching ``stream.bybit.com`` / ``api.bybit.com`` / ``ccxt.bybit`` directly.

Scope note (Phase 1, T1): this is a pure refactor. The engine's WS *message
dispatch* (orderbook/publicTrade/kline.5/kline.60 -> mid/maker/EMA) stays in the
engine for now — only the venue-specific *identifiers* (url, subscribe payload,
quote extraction, kline url, client ctor, balance/order/fee shape) move here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ExchangeAdapter(ABC):
    """Per-exchange feed + client + balance/order/fee coupling surface."""

    # --- WS / REST feed -----------------------------------------------------
    @abstractmethod
    def ws_url(self) -> str:
        """Public WS endpoint for the spot quote/kline stream."""

    @abstractmethod
    def rest_base(self) -> str:
        """Public REST base url (no key)."""

    @abstractmethod
    def rest_kline(self, symbol: str, interval: str, limit: int = 200) -> list:
        """Spot klines OLDEST-FIRST: ``[[startMs, o, h, l, c, vol, turn], ...]``."""

    @abstractmethod
    def ws_subscribe_msg(self, symbol: str) -> str:
        """The JSON string to send after connecting to subscribe this symbol's
        quote/trade/kline topics."""

    @abstractmethod
    def ws_parse_quote(self, msg: dict):
        """Extract ``(bid, ask)`` from a parsed WS book message, or ``None`` when
        the message is not a top-of-book update. A side absent from the book comes
        back as ``None`` for that side (never fabricated)."""

    # --- ccxt client / account ----------------------------------------------
    @abstractmethod
    def make_client(self, *, api_key: str, secret: str, options: dict,
                    ccxt_module=None):
        """Construct the venue's ccxt exchange (private REST). ``ccxt_module``
        injectable for tests; production passes the real ``ccxt`` module."""

    @abstractmethod
    def fetch_balance_coins(self, client) -> dict:
        """Fetch + normalize the account balance into a stable, venue-agnostic
        shape ``{"coins": {COIN: {"wallet": float, ...}}, ...}`` (reconcile reads
        ``coins[COIN]["wallet"]``)."""

    # --- order params / fees ------------------------------------------------
    @abstractmethod
    def order_params(self, link_id: str) -> dict:
        """PostOnly maker order params carrying the client order id (link)."""

    @abstractmethod
    def maker_fee(self, symbol: str) -> float:
        """Maker fee fraction for ``symbol``. Stablecoin 0-fee venues return 0.0;
        the ccxt market ``fee`` default (0.1%) is NOT trusted."""
