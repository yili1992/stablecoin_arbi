"""BitgetAdapter — Bitget V2 spot feed + client + balance/order/fee, behind the
ExchangeAdapter interface. NEW logic (not a refactor): every protocol shape below
was captured from live Bitget and pinned in tests/test_bitget_adapter.py.

Bitget V2 spot specifics (vs Bybit):
  - REST candles ``/api/v2/spot/market/candles`` already returns OLDEST-first
    (ts ascending) and 8 columns ``[ts,o,h,l,c,baseVol,quoteVol,usdtVol]`` — the
    adapter does NOT reverse, and trims to the project's 7-col ``[ts,o,h,l,c,vol,turn]``.
  - WS books5 push: ``{"action":..,"arg":{"channel":"books5",..},
    "data":[{"asks":[[px,qty]..],"bids":[[px,qty]..],..}]}`` (asks ascending,
    bids descending → best ask/bid are each ``[0][0]``).
  - WS heartbeat is application-level TEXT ``ping``/``pong`` (handled in the engine
    loop, not here); the subscribe ACK ``{"event":"subscribe",..}`` carries no book.
  - ccxt bitget is OKX-family: needs a ``password`` (passphrase); maker postOnly →
    ``force=post_only``; client order id field is ``clientOid``.
"""
from __future__ import annotations

import json
import re
import urllib.request

from sca.live.exchanges.base import ExchangeAdapter

WS_URL = "wss://ws.bitget.com/v2/ws/public"
REST_BASE = "https://api.bitget.com"

# Bitget clientOid rule: 1-40 ALPHANUMERIC chars (no '-' / '_' / symbols). Our engine
# link id is ``sca-{slice}-{gen}`` (hyphens), so it must be transformed before it can
# be sent as a clientOid OR matched against the echoed clientOid.
CLIENT_OID_MAXLEN = 40
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")


def sanitize_client_oid(link_id: str) -> str:
    """Map an engine link id to a Bitget-legal clientOid (<=40 alphanumeric).

    Each run of non-alphanumeric chars becomes a single uppercase ``X``. For our
    ``sca-{slice}-{gen}`` links (lowercase ``sca`` + digits + hyphens) ``X`` is an
    UNAMBIGUOUS delimiter — it never appears in the content — so the transform is
    collision-free (``sca-1-23`` -> ``scaX1X23`` != ``sca-12-3`` -> ``scaX12X3``),
    unlike a naive hyphen-delete which would collide both to ``sca123``. Then
    truncated to 40 (our links are ~<15 chars, so truncation never triggers in
    practice; the cap is defensive).

    CONSISTENCY (feedback_id_sanitization_consistency): this is the SINGLE transform
    for the Bitget client order id. Any downstream link matching (the R1 reconcile
    ``expected`` set, ``match_live_orders``) MUST apply this SAME function to a stored
    ``order_link_id`` before comparing it to the exchange-echoed clientOid; comparing
    a raw ``sca-*`` link to a sanitized clientOid would orphan every Bitget order."""
    return _NON_ALNUM.sub("X", link_id)[:CLIENT_OID_MAXLEN]

# engine interval token -> Bitget V2 spot candles granularity (REST).
_GRANULARITY = {"5": "5min", "60": "1h"}
# Bitget WS candle channel -> engine interval token.
_CANDLE_INTERVAL = {"candle5m": "5", "candle1H": "60"}
# Bitget lowercase taker side -> Bybit literal (engine markout contract).
_SIDE = {"buy": "Buy", "sell": "Sell"}


class BitgetAdapter(ExchangeAdapter):
    """Bitget V2 spot adapter."""

    # --- WS / REST feed -----------------------------------------------------
    def ws_url(self) -> str:
        return WS_URL

    def rest_base(self) -> str:
        return REST_BASE

    def rest_kline(self, symbol: str, interval: str, limit: int = 200) -> list:
        """Bitget V2 spot candles, OLDEST-first, trimmed to [ts,o,h,l,c,vol,turn].

        Bitget already returns ascending ts + 8 cols (last col = usdtVol); we keep
        the order and drop the 8th column to match the project's 7-col contract.
        A non-``"00000"`` code is raised (never returned as ``[]`` — that would look
        like 'no klines' and silently break the EMA/anchor bootstrap)."""
        gran = _GRANULARITY.get(interval)
        if gran is None:
            raise ValueError(
                f"unsupported Bitget kline interval {interval!r} "
                f"(known: {sorted(_GRANULARITY)})"
            )
        url = (f"{REST_BASE}/api/v2/spot/market/candles?symbol={symbol}"
               f"&granularity={gran}&limit={limit}")
        req = urllib.request.Request(url, headers={"User-Agent": "sca-live"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        code = data.get("code")
        if code != "00000":
            raise RuntimeError(
                f"Bitget candles error for {symbol} {gran}: "
                f"code={code} msg={data.get('msg')!r}"
            )
        rows = data.get("data") or []
        # already oldest-first; trim 8-col -> 7-col [ts,o,h,l,c,vol,turn].
        return [r[:7] for r in rows]

    def ws_subscribe_msg(self, symbol: str) -> str:
        """Subscribe books5 (top-of-book) + trade (markout) + candle5m/candle1H
        (klines/EMA) — the Bitget analogues of Bybit's four spot topics."""
        args = [
            {"instType": "SPOT", "channel": "books5", "instId": symbol},
            {"instType": "SPOT", "channel": "trade", "instId": symbol},
            {"instType": "SPOT", "channel": "candle5m", "instId": symbol},
            {"instType": "SPOT", "channel": "candle1H", "instId": symbol},
        ]
        return json.dumps({"op": "subscribe", "args": args})

    def ws_parse_quote(self, msg: dict):
        """Top-of-book ``(bid, ask)`` from a books5 push; ``None`` for a subscribe
        ack / error event / non-book channel. A missing side stays ``None`` (never
        fabricated). asks are ascending and bids descending, so best = ``[0][0]``."""
        if "event" in msg:                       # subscribe ack / error control frame
            return None
        if msg.get("arg", {}).get("channel") != "books5":
            return None
        data = msg.get("data") or []
        if not data:
            return None
        book = data[0]
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid = float(bids[0][0]) if bids else None
        ask = float(asks[0][0]) if asks else None
        return (bid, ask)

    def ws_parse_trades(self, msg: dict):
        """Public trades from a Bitget ``trade`` push; ``None`` otherwise. Bitget
        ``side`` is lowercase ``"buy"`` / ``"sell"`` — normalized to the Bybit taker
        literal ``"Buy"`` / ``"Sell"`` so the engine's markout stays venue-agnostic."""
        if "event" in msg:                       # subscribe ack / error control frame
            return None
        if msg.get("arg", {}).get("channel") != "trade":
            return None
        out = []
        for tr in msg.get("data", []):
            side = _SIDE.get(str(tr.get("side", "")).lower())
            out.append((float(tr["price"]), side))
        return out

    def ws_parse_klines(self, msg: dict):
        """Klines from a Bitget ``candle5m`` / ``candle1H`` push; ``None`` otherwise.
        Bitget rows are arrays ``[ts,o,h,l,c,baseVol,quoteVol,usdtVol]`` (not dicts)
        and carry NO confirm flag, so every bar is marked ``confirm=True``: the
        engine's ``start>last_1h_start`` guard then dedups the EMA step to one per new
        1h bar (Phase-1 acceptable for a near-peg stablecoin; see module note)."""
        if "event" in msg:                       # subscribe ack / error control frame
            return None
        channel = msg.get("arg", {}).get("channel")
        interval = _CANDLE_INTERVAL.get(channel)
        if interval is None:
            return None
        bars = [{
            "start": int(r[0]),
            "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
            "confirm": True,
        } for r in msg.get("data", [])]
        return (interval, bars)

    # --- ccxt client / account ----------------------------------------------
    def make_client(self, *, api_key: str, secret: str, options: dict,
                    password: str | None = None, ccxt_module=None):
        """Construct ccxt bitget (top-level ctor ``mod.bitget(...)``). The
        passphrase (``password``) is OKX-family-required for any signed call; it is
        only injected when supplied so a feed-only construction stays clean.
        ``verbose=False`` so signed headers/keys never reach logs."""
        mod = ccxt_module if ccxt_module is not None else _import_ccxt()
        cfg = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "verbose": False,
            "options": options,
        }
        if password:
            cfg["password"] = password
        return mod.bitget(cfg)

    def fetch_balance_coins(self, client) -> dict:
        """Bitget SPOT balance normalized to the stable shape (reconcile reads
        ``coins[COIN]["wallet"]``). Reads ccxt's venue-agnostic unified totals
        (``free``/``used``/``total`` per coin) rather than Bitget-raw field names:
        ``wallet`` = total holdings, ``locked`` = used, ``free`` = available. This
        is SPOT (no UTA ``{"type":"unified"}`` param, no margin/borrow)."""
        bal = client.fetch_balance()
        coins = {}
        for coin, amts in bal.items():
            if not isinstance(amts, dict) or "total" not in amts:
                continue  # skip the aggregate 'free'/'used'/'total'/'info' keys
            total = _f(amts.get("total"))
            used = _f(amts.get("used"))
            free = _f(amts.get("free"))
            coins[coin] = {
                "wallet": total,
                "locked": used,
                "free": free,
                "usd": 0.0,        # spot ticker-USD valuation not needed by reconcile
                "equity": total,
                "borrow": 0.0,     # spot: no borrow
            }
        return {
            "account_type": "spot",
            "totals": {
                "equity_usd": 0.0,
                "available_usd": 0.0,
                "wallet_usd": 0.0,
                "im_usd": 0.0,
                "mm_usd": 0.0,
                "perp_upl_usd": 0.0,
            },
            "coins": coins,
            "raw": bal.get("info"),
        }

    # --- order params / fees ------------------------------------------------
    def order_params(self, link_id: str) -> dict:
        """PostOnly maker params carrying the client order id. ccxt bitget maps
        ``postOnly`` → ``force=post_only`` and ``clientOid`` → the request id.

        The link id is SANITIZED to Bitget's <=40-alphanumeric clientOid rule (our
        ``sca-{slice}-{gen}`` links carry hyphens, which Bitget rejects). Matching
        code must apply ``sanitize_client_oid`` identically — see its docstring."""
        return {"postOnly": True, "clientOid": sanitize_client_oid(link_id)}

    def maker_fee(self, symbol: str) -> float:
        """0-fee stablecoin maker — hardcoded 0.0 (ccxt market default 0.1% is NOT
        trusted)."""
        return 0.0


def _f(x) -> float:
    """Tolerant float: None/''/'-' -> 0.0 (mirrors bybit_client._f)."""
    if x is None or x == "" or x == "-":
        return 0.0
    return float(x)


def _import_ccxt():
    import ccxt
    return ccxt
