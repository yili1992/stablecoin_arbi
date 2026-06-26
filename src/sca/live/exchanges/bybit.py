"""BybitAdapter — the existing Bybit-specific feed + client + balance/order/fee
logic, moved behind the ExchangeAdapter interface. Behavior is bit-identical to
the pre-refactor inline code (engine.py WS/REST, orders.py order params,
bybit_client.py balance map / ccxt ctor); this is a pure refactor.
"""
from __future__ import annotations

import json
import urllib.request

from sca.live.bybit_client import normalize_balance
from sca.live.exchanges.base import ExchangeAdapter

# Bybit public endpoints (legacy engine.py:122-123 defaults).
WS_URL = "wss://stream.bybit.com/v5/public/spot"
REST_BASE = "https://api.bybit.com"


class BybitAdapter(ExchangeAdapter):
    """Bybit V5 spot adapter (the original, only venue)."""

    def __init__(self, ws_url: str | None = None):
        # ws_url overridable from config (engine read live.ws_url before); default
        # is the legacy constant so an un-configured deploy is unchanged.
        self._ws_url = ws_url or WS_URL

    # --- WS / REST feed -----------------------------------------------------
    def ws_url(self) -> str:
        return self._ws_url

    def rest_base(self) -> str:
        return REST_BASE

    def rest_kline(self, symbol: str, interval: str, limit: int = 200) -> list:
        """Return Bybit spot klines OLDEST-FIRST: [[startMs, o, h, l, c, vol, turn], ...].
        (Verbatim from legacy engine.py:_rest_kline.)"""
        url = (f"{REST_BASE}/v5/market/kline?category=spot&symbol={symbol}"
               f"&interval={interval}&limit={limit}")
        req = urllib.request.Request(url, headers={"User-Agent": "sca-live"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return data["result"]["list"][::-1]  # API returns newest-first

    def ws_subscribe_msg(self, symbol: str) -> str:
        """The subscribe payload for the four spot topics (legacy engine.py:1933-1941)."""
        topics = [f"orderbook.1.{symbol}", f"publicTrade.{symbol}",
                  f"kline.5.{symbol}", f"kline.60.{symbol}"]
        return json.dumps({"op": "subscribe", "args": topics})

    def ws_parse_quote(self, msg: dict):
        """Top-of-book (bid, ask) from an ``orderbook.1.*`` message (legacy
        engine.py:1987-1992 field access ``b[0][0]`` / ``a[0][0]``); ``None`` for a
        non-book message, and a missing side stays ``None``."""
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook.1"):
            return None
        ob = msg.get("data", {})
        bid = float(ob["b"][0][0]) if ob.get("b") else None
        ask = float(ob["a"][0][0]) if ob.get("a") else None
        return (bid, ask)

    def ws_parse_trades(self, msg: dict):
        """Public trades from a ``publicTrade.*`` message (legacy engine.py:2000-2003
        field access ``tr["p"]`` / ``tr.get("S")``); ``None`` for a non-trade topic.
        Bybit ``S`` is already the ``"Buy"`` / ``"Sell"`` taker literal."""
        if not msg.get("topic", "").startswith("publicTrade"):
            return None
        return [(float(tr["p"]), tr.get("S")) for tr in msg.get("data", [])]

    def ws_parse_klines(self, msg: dict):
        """Klines from a ``kline.5.*`` / ``kline.60.*`` message (legacy
        engine.py:2007-2020). Returns ``(interval, bars)``; ``None`` otherwise. Bybit
        carries a native ``confirm`` flag (the 5m path ignores it, the 60m path gates
        the EMA on it)."""
        topic = msg.get("topic", "")
        if topic.startswith("kline.5"):
            interval = "5"
        elif topic.startswith("kline.60"):
            interval = "60"
        else:
            return None
        bars = [{
            "start": int(it["start"]),
            "o": float(it["open"]), "h": float(it["high"]),
            "l": float(it["low"]), "c": float(it["close"]),
            "confirm": bool(it.get("confirm")),
        } for it in msg.get("data", [])]
        return (interval, bars)

    # --- ccxt client / account ----------------------------------------------
    def make_client(self, *, api_key: str, secret: str, options: dict,
                    password: str | None = None, ccxt_module=None):
        """Construct ccxt bybit (legacy orders.py:89 / bybit_client.py:136). The
        injected ``ccxt_module`` (real ``ccxt`` in prod) exposes the exchange
        constructor at the TOP LEVEL — ``mod.bybit(...)`` — never under ``.default``.
        ``password`` is accepted for interface parity but unused (Bybit has no
        passphrase); the constructed config is byte-for-byte unchanged."""
        mod = ccxt_module if ccxt_module is not None else _import_ccxt()
        return mod.bybit({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "verbose": False,                 # never log signed headers/keys (Codex S2)
            "options": options,
        })

    def fetch_balance_coins(self, client) -> dict:
        """UTA wallet balance normalized to the stable shape (legacy bybit_client
        ``normalize_balance`` over ``fetch_balance({"type": "unified"})``)."""
        return normalize_balance(client.fetch_balance({"type": "unified"}))

    # --- order params / fees ------------------------------------------------
    def order_params(self, link_id: str) -> dict:
        """PostOnly GTC maker params (legacy orders.py:143)."""
        return {"postOnly": True, "isLeverage": 0, "clientOrderId": link_id}

    def sanitize_link(self, link):
        """IDENTITY — Bybit sends the link verbatim as ``clientOrderId`` and echoes it
        unchanged, so the reconcile match transform is a no-op. (Mangling it would break
        the ``sca-*`` stale guard + the R1 ``expected`` set the Bybit tests pin.)"""
        return link

    def maker_fee(self, symbol: str) -> float:
        """0-fee stablecoin maker — hardcoded 0.0 (ccxt market default 0.1% is
        NOT trusted; verified)."""
        return 0.0


def _import_ccxt():
    import ccxt
    return ccxt
