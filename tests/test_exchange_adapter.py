"""Tests for the ExchangeAdapter abstraction (sca.live.exchanges) — Phase 1, T1.

This is a pure refactor: BybitAdapter must reproduce the EXISTING Bybit-specific
behavior that engine.py / orders.py / bybit_client.py used to inline (WS url +
subscribe + quote parse, REST kline url, ccxt client construction, balance map,
order params, maker fee). Behavior must be bit-identical to the pre-refactor code.

ISOLATION: zero network. ``rest_kline`` is tested by intercepting urllib; ccxt is
injected; balance normalization runs on a canned dict.

Run: PYTHONPATH=src python3 -m pytest tests/test_exchange_adapter.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.exchanges import adapter_for                      # noqa: E402
from sca.live.exchanges.base import ExchangeAdapter             # noqa: E402
from sca.live.exchanges.bybit import BybitAdapter               # noqa: E402


# --- canned Bybit V5 UTA fetch_balance payload (ccxt shape) -----------------
CANNED_BALANCE = {
    "info": {"result": {"list": [{
        "accountType": "UNIFIED",
        "totalEquity": "10250.5",
        "totalAvailableBalance": "10000.0",
        "totalWalletBalance": "10250.5",
        "coin": [
            {"coin": "USD1", "walletBalance": "6000.0", "locked": "100.0",
             "usdValue": "6000.0", "equity": "6000.0", "spotBorrow": "0"},
            {"coin": "USDT", "walletBalance": "4250.5", "locked": "0",
             "usdValue": "4250.5", "equity": "4250.5", "spotBorrow": "0"},
        ],
    }]}},
}


class FakeExchange:
    def __init__(self, config):
        self.config = config

    def fetch_balance(self, params=None):
        self.last_params = params
        return CANNED_BALANCE


class FakeCcxt:
    """Mimics the REAL ccxt module export shape: exchange constructors at the top
    level only (NOT under .default — see ccxt default-export trap)."""
    def __init__(self):
        self.last = None

    def bybit(self, config):
        self.last = FakeExchange(config)
        return self.last


# --- adapter registry -------------------------------------------------------

def test_adapter_for_defaults_to_bybit():
    a = adapter_for("USD1USDT")
    assert isinstance(a, BybitAdapter)


def test_bybit_adapter_is_exchange_adapter():
    assert isinstance(BybitAdapter(), ExchangeAdapter)


# --- WS feed ----------------------------------------------------------------

def test_ws_url_matches_legacy_bybit_default():
    # legacy engine.py:122 default
    assert BybitAdapter().ws_url() == "wss://stream.bybit.com/v5/public/spot"


def test_ws_subscribe_msg_carries_legacy_topics():
    # legacy engine.py:1933-1934 + 1941: op=subscribe with the four spot topics.
    msg = BybitAdapter().ws_subscribe_msg("USD1USDT")
    payload = json.loads(msg)
    assert payload["op"] == "subscribe"
    assert payload["args"] == [
        "orderbook.1.USD1USDT", "publicTrade.USD1USDT",
        "kline.5.USD1USDT", "kline.60.USD1USDT",
    ]


def test_ws_parse_quote_extracts_orderbook_bid_ask():
    # legacy engine.py:1987-1992: topic orderbook.1.* -> b[0][0]/a[0][0].
    d = {"topic": "orderbook.1.USD1USDT",
         "data": {"b": [["1.0001", "500"]], "a": [["1.0003", "400"]]}}
    assert BybitAdapter().ws_parse_quote(d) == (1.0001, 1.0003)


def test_ws_parse_quote_returns_none_for_non_orderbook_topic():
    d = {"topic": "publicTrade.USD1USDT", "data": [{"p": "1.0002", "S": "Buy"}]}
    assert BybitAdapter().ws_parse_quote(d) is None


def test_ws_parse_quote_partial_book_returns_present_side_only():
    # bid present, ask absent -> (bid, None); never fabricate the missing side.
    d = {"topic": "orderbook.1.USD1USDT", "data": {"b": [["1.0001", "500"]]}}
    assert BybitAdapter().ws_parse_quote(d) == (1.0001, None)


# --- WS trade parse (markout) ----------------------------------------------

def test_ws_parse_trades_extracts_price_and_taker_side():
    # legacy engine.py publicTrade branch: data[*].p (price) + .S (taker side).
    d = {"topic": "publicTrade.USD1USDT",
         "data": [{"p": "1.0002", "S": "Buy"}, {"p": "1.0001", "S": "Sell"}]}
    assert BybitAdapter().ws_parse_trades(d) == [(1.0002, "Buy"), (1.0001, "Sell")]


def test_ws_parse_trades_returns_none_for_non_trade_topic():
    d = {"topic": "orderbook.1.USD1USDT", "data": {"b": [["1.0", "5"]]}}
    assert BybitAdapter().ws_parse_trades(d) is None


# --- WS kline parse (klines5 / EMA) ----------------------------------------

def test_ws_parse_klines_5_normalizes_bars():
    # legacy kline.5 branch: start/open/high/low/close (confirm unused for 5m).
    d = {"topic": "kline.5.USD1USDT",
         "data": [{"start": 1000, "open": "1.0", "high": "1.1", "low": "0.9",
                   "close": "1.05", "confirm": False}]}
    interval, bars = BybitAdapter().ws_parse_klines(d)
    assert interval == "5"
    assert bars == [{"start": 1000, "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05,
                     "confirm": False}]


def test_ws_parse_klines_60_carries_confirm_flag():
    # legacy kline.60 branch: only confirmed bars step the EMA.
    d = {"topic": "kline.60.USD1USDT",
         "data": [{"start": 3600, "open": "1.0", "high": "1.0", "low": "1.0",
                   "close": "1.0009", "confirm": True}]}
    interval, bars = BybitAdapter().ws_parse_klines(d)
    assert interval == "60"
    assert bars[0]["confirm"] is True
    assert bars[0]["c"] == 1.0009
    assert bars[0]["start"] == 3600


def test_ws_parse_klines_returns_none_for_non_kline_topic():
    d = {"topic": "orderbook.1.USD1USDT", "data": {"b": [["1.0", "5"]]}}
    assert BybitAdapter().ws_parse_klines(d) is None


# --- REST kline -------------------------------------------------------------

def test_rest_kline_builds_legacy_v5_spot_url_and_reverses(monkeypatch):
    # legacy engine.py:250-257: /v5/market/kline?category=spot, newest-first -> reversed.
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["ua"] = req.headers.get("User-agent")
        captured["timeout"] = timeout
        # API returns newest-first; adapter must reverse to oldest-first.
        return _JsonResp({"result": {"list": [[3], [2], [1]]}})

    import sca.live.exchanges.bybit as bybit_mod
    monkeypatch.setattr(bybit_mod.urllib.request, "urlopen", fake_urlopen)

    rows = BybitAdapter().rest_kline("USD1USDT", "60", limit=200)
    assert captured["url"] == (
        "https://api.bybit.com/v5/market/kline?category=spot&symbol=USD1USDT"
        "&interval=60&limit=200"
    )
    assert captured["ua"] == "sca-live"
    assert captured["timeout"] == 15
    assert rows == [[1], [2], [3]]            # reversed to oldest-first


class _JsonResp:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()


def test_rest_base_matches_legacy():
    assert BybitAdapter().rest_base() == "https://api.bybit.com"


# --- ccxt client ------------------------------------------------------------

def test_make_client_constructs_bybit_via_injected_module():
    # legacy orders.py:89 / bybit_client.py:136: mod.bybit({...}) — top-level ctor.
    fake = FakeCcxt()
    ex = BybitAdapter().make_client(
        api_key="k", secret="s", options={"defaultType": "spot"},
        ccxt_module=fake,
    )
    assert ex is fake.last
    cfg = ex.config
    assert cfg["apiKey"] == "k" and cfg["secret"] == "s"
    assert cfg["enableRateLimit"] is True
    assert cfg["verbose"] is False
    assert cfg["options"] == {"defaultType": "spot"}


# --- balance map ------------------------------------------------------------

def test_fetch_balance_coins_normalizes_uta():
    fake = FakeCcxt()
    ex = BybitAdapter().make_client(api_key="k", secret="s", options={}, ccxt_module=fake)
    bal = BybitAdapter().fetch_balance_coins(ex)
    assert ex.last_params == {"type": "unified"}
    assert bal["account_type"] == "UNIFIED"
    assert bal["coins"]["USD1"]["wallet"] == 6000.0
    assert bal["coins"]["USD1"]["free"] == 5900.0       # walletBalance - locked
    assert bal["coins"]["USDT"]["free"] == 4250.5


# --- order params -----------------------------------------------------------

def test_order_params_postonly_clientorderid():
    # legacy orders.py:143: {"postOnly": True, "isLeverage": 0, "clientOrderId": link}
    assert BybitAdapter().order_params("sca-0-1") == {
        "postOnly": True, "isLeverage": 0, "clientOrderId": "sca-0-1",
    }


# --- maker fee --------------------------------------------------------------

def test_maker_fee_is_zero_for_stablecoin():
    # 0-fee hardcoded; ccxt default 0.1% is NOT trusted.
    assert BybitAdapter().maker_fee("USD1USDT") == 0.0
