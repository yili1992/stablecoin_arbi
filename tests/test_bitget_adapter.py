"""Tests for BitgetAdapter (sca.live.exchanges.bitget) — Phase 1, T2.

BitgetAdapter is the second venue behind the ExchangeAdapter interface. Unlike
the BybitAdapter (a pure refactor of pre-existing code), this is NEW logic, so the
tests encode REAL Bitget V2 spot protocol shapes captured live:

  - REST  ``/api/v2/spot/market/candles`` row =
        [ts_ms, open, high, low, close, baseVol, quoteVol, usdtVol]  (OLDEST-first)
  - WS    books5 push = {"action":"snapshot","arg":{...,"channel":"books5"},
        "data":[{"asks":[[px,qty]...],"bids":[[px,qty]...],"ts":...,"seq":...}]}
        (asks ascending -> best ask = asks[0][0]; bids descending -> best bid = bids[0][0])
  - WS    subscribe ack = {"event":"subscribe","arg":{...}}  (no action/data -> skip)

ISOLATION: zero network. ``rest_kline`` is tested by intercepting urllib; ccxt is
injected; balance normalization runs on a canned ccxt-unified dict.

Run: PYTHONPATH=src python3 -m pytest tests/test_bitget_adapter.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.exchanges.base import ExchangeAdapter             # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter             # noqa: E402


class _JsonResp:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._p).encode()


# === identity / interface ===================================================

def test_bitget_adapter_is_exchange_adapter():
    assert isinstance(BitgetAdapter(), ExchangeAdapter)


def test_ws_url_is_bitget_v2_public():
    assert BitgetAdapter().ws_url() == "wss://ws.bitget.com/v2/ws/public"


def test_rest_base_is_bitget():
    assert BitgetAdapter().rest_base() == "https://api.bitget.com"


# === REST kline =============================================================

def test_rest_kline_maps_5_to_5min_granularity(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["ua"] = req.headers.get("User-agent")
        captured["timeout"] = timeout
        return _JsonResp({"code": "00000", "msg": "success", "data": []})

    import sca.live.exchanges.bitget as bg_mod
    monkeypatch.setattr(bg_mod.urllib.request, "urlopen", fake_urlopen)

    BitgetAdapter().rest_kline("USDCUSDT", "5", limit=130)
    assert captured["url"] == (
        "https://api.bitget.com/api/v2/spot/market/candles?symbol=USDCUSDT"
        "&granularity=5min&limit=130"
    )
    assert captured["ua"] == "sca-live"
    assert captured["timeout"] == 15


def test_rest_kline_maps_60_to_1h_granularity(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _JsonResp({"code": "00000", "msg": "success", "data": []})

    import sca.live.exchanges.bitget as bg_mod
    monkeypatch.setattr(bg_mod.urllib.request, "urlopen", fake_urlopen)

    BitgetAdapter().rest_kline("USDCUSDT", "60", limit=200)
    assert "granularity=1h" in captured["url"]
    assert "limit=200" in captured["url"]


def test_rest_kline_returns_oldest_first_7_cols(monkeypatch):
    # Bitget returns OLDEST-first already (ts ascending) and 8 columns;
    # adapter must NOT reverse, and must return the project's 7-col shape
    # [ts, o, h, l, c, vol(base), turn(quote)] (drop the trailing usdtVol).
    real_rows = [
        ["1782447000000", "1.0012", "1.0012", "1.0011", "1.0011", "23235.1434", "23262.4735", "23262.4735"],
        ["1782447300000", "1.0011", "1.0013", "1.0011", "1.0012", "1241.2886", "1242.7442", "1242.7442"],
    ]

    def fake_urlopen(req, timeout=None):
        return _JsonResp({"code": "00000", "msg": "success", "data": real_rows})

    import sca.live.exchanges.bitget as bg_mod
    monkeypatch.setattr(bg_mod.urllib.request, "urlopen", fake_urlopen)

    rows = BitgetAdapter().rest_kline("USDCUSDT", "5", limit=2)
    # oldest first preserved (ts ascending), 7 columns
    assert [r[0] for r in rows] == ["1782447000000", "1782447300000"]
    assert rows[0] == ["1782447000000", "1.0012", "1.0012", "1.0011", "1.0011", "23235.1434", "23262.4735"]
    assert all(len(r) == 7 for r in rows)


def test_rest_kline_raises_on_error_code(monkeypatch):
    # A non-"00000" code is an API error; do not silently return [] (would look
    # like "no klines" and corrupt the EMA/anchor bootstrap).
    def fake_urlopen(req, timeout=None):
        return _JsonResp({"code": "40034", "msg": "Parameter symbol error", "data": None})

    import sca.live.exchanges.bitget as bg_mod
    monkeypatch.setattr(bg_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(Exception):
        BitgetAdapter().rest_kline("BADSYM", "5", limit=10)


def test_rest_kline_unknown_interval_raises():
    with pytest.raises(Exception):
        BitgetAdapter().rest_kline("USDCUSDT", "15", limit=10)


# === WS subscribe ===========================================================

def test_ws_subscribe_msg_books5_trade_candles():
    msg = BitgetAdapter().ws_subscribe_msg("USDCUSDT")
    payload = json.loads(msg)
    assert payload["op"] == "subscribe"
    chans = [(a["channel"], a["instId"], a["instType"]) for a in payload["args"]]
    assert ("books5", "USDCUSDT", "SPOT") in chans
    assert ("trade", "USDCUSDT", "SPOT") in chans
    assert ("candle5m", "USDCUSDT", "SPOT") in chans
    assert ("candle1H", "USDCUSDT", "SPOT") in chans


# === WS parse quote (real books5 payload) ===================================

def _books5_msg(asks, bids, action="snapshot"):
    return {
        "action": action,
        "arg": {"instType": "SPOT", "channel": "books5", "instId": "USDCUSDT"},
        "data": [{"asks": asks, "bids": bids, "ts": "1782448258316", "seq": 130561264091}],
        "ts": 1782448258320,
    }


def test_ws_parse_quote_extracts_top_of_book():
    # Real shape: asks ascending (best=first), bids descending (best=first).
    msg = _books5_msg(
        asks=[["1.0012", "4725723.24"], ["1.0013", "150081.53"]],
        bids=[["1.0011", "751841.75"], ["1.001", "6976275.60"]],
    )
    assert BitgetAdapter().ws_parse_quote(msg) == (1.0011, 1.0012)


def test_ws_parse_quote_handles_update_action():
    msg = _books5_msg(
        asks=[["1.0014", "10"]], bids=[["1.0013", "20"]], action="update",
    )
    assert BitgetAdapter().ws_parse_quote(msg) == (1.0013, 1.0014)


def test_ws_parse_quote_returns_none_for_subscribe_ack():
    ack = {"event": "subscribe",
           "arg": {"instType": "SPOT", "channel": "books5", "instId": "USDCUSDT"}}
    assert BitgetAdapter().ws_parse_quote(ack) is None


def test_ws_parse_quote_returns_none_for_trade_channel():
    trade = {"action": "snapshot",
             "arg": {"instType": "SPOT", "channel": "trade", "instId": "USDCUSDT"},
             "data": [{"ts": "1", "price": "1.0011", "size": "10", "side": "sell",
                       "tradeId": "x"}]}
    assert BitgetAdapter().ws_parse_quote(trade) is None


def test_ws_parse_quote_partial_book_returns_present_side_only():
    # bid present, asks empty -> (bid, None); never fabricate the missing side.
    msg = _books5_msg(asks=[], bids=[["1.0011", "100"]])
    assert BitgetAdapter().ws_parse_quote(msg) == (1.0011, None)


def test_ws_parse_quote_returns_none_for_error_event():
    err = {"event": "error", "code": 30001, "msg": "channel not exist"}
    assert BitgetAdapter().ws_parse_quote(err) is None


# === WS parse trades (real trade payload) ===================================

def test_ws_parse_trades_maps_lowercase_side_to_bybit_literal():
    # Bitget trade side is lowercase "buy"/"sell"; the engine's markout expects
    # the Bybit literal "Buy"/"Sell" (taker side), so the adapter normalizes it.
    msg = {
        "action": "snapshot",
        "arg": {"instType": "SPOT", "channel": "trade", "instId": "USDCUSDT"},
        "data": [
            {"ts": "1782448271982", "price": "1.0011", "size": "10",
             "side": "sell", "tradeId": "x1"},
            {"ts": "1782448269483", "price": "1.0012", "size": "150",
             "side": "buy", "tradeId": "x2"},
        ],
    }
    assert BitgetAdapter().ws_parse_trades(msg) == [(1.0011, "Sell"), (1.0012, "Buy")]


def test_ws_parse_trades_returns_none_for_books5():
    msg = _books5_msg(asks=[["1.0012", "1"]], bids=[["1.0011", "1"]])
    assert BitgetAdapter().ws_parse_trades(msg) is None


def test_ws_parse_trades_returns_none_for_subscribe_ack():
    ack = {"event": "subscribe",
           "arg": {"instType": "SPOT", "channel": "trade", "instId": "USDCUSDT"}}
    assert BitgetAdapter().ws_parse_trades(ack) is None


# === WS parse klines (real candle payload, array-of-arrays) =================

def _candle_msg(channel, rows, action="snapshot"):
    return {
        "action": action,
        "arg": {"instType": "SPOT", "channel": channel, "instId": "USDCUSDT"},
        "data": rows,
    }


def test_ws_parse_klines_candle5m_maps_to_interval_5():
    # Bitget candle row = [ts,o,h,l,c,baseVol,quoteVol,usdtVol] (array, not dict).
    msg = _candle_msg("candle5m", [
        ["1782298500000", "1.0012", "1.0013", "1.0011", "1.0011",
         "12397.4", "12411.4", "12411.4"],
    ])
    interval, bars = BitgetAdapter().ws_parse_klines(msg)
    assert interval == "5"
    assert bars == [{"start": 1782298500000, "o": 1.0012, "h": 1.0013,
                     "l": 1.0011, "c": 1.0011, "confirm": True}]


def test_ws_parse_klines_candle1H_maps_to_interval_60():
    msg = _candle_msg("candle1H", [
        ["1780650000000", "1.0005", "1.0006", "1.0004", "1.0006",
         "4548794.8", "4551071.2", "4551071.2"],
    ])
    interval, bars = BitgetAdapter().ws_parse_klines(msg)
    assert interval == "60"
    assert bars[0]["start"] == 1780650000000
    assert bars[0]["c"] == 1.0006
    # Bitget candle WS has no confirm flag; mark True so the engine's
    # start>last_1h_start guard dedups the EMA step (one step per new 1h bar).
    assert bars[0]["confirm"] is True


def test_ws_parse_klines_returns_none_for_books5():
    msg = _books5_msg(asks=[["1.0012", "1"]], bids=[["1.0011", "1"]])
    assert BitgetAdapter().ws_parse_klines(msg) is None


def test_ws_parse_klines_returns_none_for_trade():
    msg = {"action": "snapshot",
           "arg": {"channel": "trade", "instId": "USDCUSDT"},
           "data": [{"price": "1.0", "side": "buy"}]}
    assert BitgetAdapter().ws_parse_klines(msg) is None


# === ccxt client ============================================================

class FakeBitgetExchange:
    def __init__(self, config):
        self.config = config

    def fetch_balance(self, params=None):
        self.last_params = params
        # ccxt-unified spot balance: {CCY:{free,used,total}, info:[...]}
        return {
            "USDC": {"free": 5900.0, "used": 100.0, "total": 6000.0},
            "USDT": {"free": 4250.5, "used": 0.0, "total": 4250.5},
            "free": {"USDC": 5900.0, "USDT": 4250.5},
            "used": {"USDC": 100.0, "USDT": 0.0},
            "total": {"USDC": 6000.0, "USDT": 4250.5},
            "info": {"raw": "bitget"},
        }


class FakeBitgetCcxt:
    """Mimics the REAL ccxt module export shape: ctor at the top level."""
    def __init__(self):
        self.last = None

    def bitget(self, config):
        self.last = FakeBitgetExchange(config)
        return self.last


def test_make_client_constructs_bitget_with_passphrase():
    fake = FakeBitgetCcxt()
    ex = BitgetAdapter().make_client(
        api_key="k", secret="s", password="phrase",
        options={"defaultType": "spot"}, ccxt_module=fake,
    )
    assert ex is fake.last
    cfg = ex.config
    assert cfg["apiKey"] == "k" and cfg["secret"] == "s"
    assert cfg["password"] == "phrase"          # Bitget passphrase (OKX-family)
    assert cfg["enableRateLimit"] is True
    assert cfg["verbose"] is False              # never log signed headers/keys
    assert cfg["options"] == {"defaultType": "spot"}


def test_make_client_without_password_omits_it():
    # If no passphrase is supplied, do not inject an empty one (would 400 on a
    # real signed call, but the ctor itself must stay clean for feed-only use).
    fake = FakeBitgetCcxt()
    ex = BitgetAdapter().make_client(
        api_key="k", secret="s", options={}, ccxt_module=fake,
    )
    assert "password" not in ex.config


# === balance map (spot) =====================================================

def test_fetch_balance_coins_normalizes_spot():
    fake = FakeBitgetCcxt()
    ad = BitgetAdapter()
    ex = ad.make_client(api_key="k", secret="s", password="p", options={}, ccxt_module=fake)
    bal = ad.fetch_balance_coins(ex)
    # spot balance: NOT the Bybit {"type":"unified"} param
    assert ex.last_params != {"type": "unified"}
    assert bal["account_type"] == "spot"
    # reconcile reads coins[COIN]["wallet"]; wallet = total holdings
    assert bal["coins"]["USDC"]["wallet"] == 6000.0
    assert bal["coins"]["USDC"]["locked"] == 100.0
    assert bal["coins"]["USDC"]["free"] == 5900.0
    assert bal["coins"]["USDT"]["wallet"] == 4250.5


# === order params ===========================================================

def test_order_params_postonly_clientoid():
    # Bitget ccxt: postOnly -> force=post_only; clientOid carries the link id.
    p = BitgetAdapter().order_params("sca-0-1")
    assert p["postOnly"] is True
    assert p["clientOid"] == "sca-0-1"


# === maker fee ==============================================================

def test_maker_fee_is_zero_for_stablecoin():
    # 0-fee hardcoded; ccxt market default (0.1%) is NOT trusted.
    assert BitgetAdapter().maker_fee("USDCUSDT") == 0.0
