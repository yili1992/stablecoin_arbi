"""Engine _handle dispatches WS messages through the per-symbol adapter — Phase 1, T2.

The engine used to inline Bybit topic parsing (orderbook.1/publicTrade/kline.*).
T2 routes parsing through ``self.adapter.ws_parse_quote / ws_parse_trades /
ws_parse_klines`` so a non-Bybit venue (Bitget) drives the same engine. These
tests feed BITGET-format payloads (no Bybit ``topic`` key; ``arg.channel`` +
array candle rows + lowercase trade side) through a real PaperEngine whose adapter
is swapped to BitgetAdapter, and assert the engine state updates identically to the
Bybit path.

Run: PYTHONPATH=src python3 -m pytest tests/test_engine_adapter_dispatch.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.engine import PaperEngine                         # noqa: E402
from sca.live.exchanges.bitget import BitgetAdapter             # noqa: E402


def _bitget_engine(tmp_path, *, maker=False, bid=None, ask=None):
    eng = PaperEngine(symbol="USD1USDT", mode="dryrun", seconds=1,
                      csv_path=str(tmp_path / "out.csv"))
    eng.adapter = BitgetAdapter()        # route the same engine onto Bitget feeds
    eng.maker_enabled = maker
    eng.deployed = True
    eng.bid, eng.ask = bid, ask
    return eng


def _books5(asks, bids):
    return {"action": "snapshot",
            "arg": {"instType": "SPOT", "channel": "books5", "instId": "USDCUSDT"},
            "data": [{"asks": asks, "bids": bids, "ts": "1", "seq": 1}]}


def _trade(price, side):
    return {"action": "snapshot",
            "arg": {"instType": "SPOT", "channel": "trade", "instId": "USDCUSDT"},
            "data": [{"ts": "1", "price": str(price), "size": "10", "side": side,
                      "tradeId": "x"}]}


def _candle(channel, rows):
    return {"action": "snapshot",
            "arg": {"instType": "SPOT", "channel": channel, "instId": "USDCUSDT"},
            "data": rows}


def test_handle_books5_sets_bid_ask(tmp_path):
    eng = _bitget_engine(tmp_path)
    eng._handle(_books5(asks=[["1.0012", "5"]], bids=[["1.0011", "5"]]), 0.0)
    assert eng.bid == 1.0011
    assert eng.ask == 1.0012


def test_handle_trade_sets_last_and_records_markout(tmp_path):
    # taker buy hits the ask -> a passive SELL @ ask is recorded for markout.
    eng = _bitget_engine(tmp_path, bid=1.0, ask=1.0010)
    eng._push_mid(0.0)
    eng._handle(_trade(1.0006, "buy"), 0.0)
    assert eng.last == 1.0006
    assert len(eng.pending) == 1
    assert eng.pending[0][1] == "sell"      # passive side = opposite of taker buy


def test_handle_candle5m_populates_klines5(tmp_path):
    eng = _bitget_engine(tmp_path)
    eng._handle(_candle("candle5m", [
        ["1782298500000", "1.0012", "1.0013", "1.0011", "1.0011", "1", "1", "1"],
    ]), 0.0)
    assert 1782298500000 in eng.klines5
    bar = eng.klines5[1782298500000]
    assert bar["o"] == 1.0012 and bar["c"] == 1.0011


def test_handle_candle1h_steps_ema_once_per_new_bar(tmp_path):
    eng = _bitget_engine(tmp_path)
    eng.ema = 1.0000
    eng.anchor = 1.0000
    eng.last_1h_start = 1000
    # a NEW 1h bar (start>last_1h_start) steps the EMA once
    eng._handle(_candle("candle1H", [
        ["3600000", "1.0", "1.0", "1.0", "1.0010", "1", "1", "1"],
    ]), 0.0)
    assert eng.last_1h_start == 3600000
    stepped = eng.ema
    assert stepped != 1.0000                # EMA advanced toward 1.0010
    # the SAME bar resent (no new start) must NOT step the EMA again
    eng._handle(_candle("candle1H", [
        ["3600000", "1.0", "1.0", "1.0", "1.0010", "1", "1", "1"],
    ]), 0.0)
    assert eng.ema == stepped


def test_handle_unknown_channel_is_noop(tmp_path):
    eng = _bitget_engine(tmp_path, bid=1.0, ask=1.001)
    before = (eng.bid, eng.ask, eng.last, len(eng.klines5))
    eng._handle({"action": "snapshot",
                 "arg": {"channel": "ticker", "instId": "USDCUSDT"},
                 "data": [{"last": "1.0005"}]}, 0.0)
    assert (eng.bid, eng.ask, eng.last, len(eng.klines5)) == before
