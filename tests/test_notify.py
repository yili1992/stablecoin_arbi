"""Tests for Feishu webhook notification formatting and delivery."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.notify import FeishuNotifier, notify_from_config  # noqa: E402


def test_order_payload_includes_strategy_and_key_trade_fields():
    sent = []
    notifier = FeishuNotifier(webhook_url="https://open.feishu.test/hook", sender=sent.append)

    notifier.order_placed(
        strategy_name="USD1 EMA Slice Ladder",
        mode="live",
        symbol="USD1USDT",
        side="sell",
        slice_idx=2,
        price=1.0005,
        qty=123.456789,
        link_id="sca-2-7",
        order_id="oid-1",
    )

    body = json.loads(sent[0]["body"])
    text = body["content"]["text"]
    assert body["msg_type"] == "text"
    assert "timestamp" not in body
    assert "sign" not in body
    assert "USD1 EMA Slice Ladder" in text
    assert "实盘挂单" in text
    assert "卖出" in text
    assert "USD1USDT" in text
    assert "slice #2" in text
    assert "price=1.000500" in text
    assert "qty=123.456789" in text
    assert "link=sca-2-7" in text
    assert "order=oid-1" in text


def test_daily_payload_includes_strategy_profit_and_apr():
    sent = []
    notifier = FeishuNotifier(webhook_url="https://open.feishu.test/hook", sender=sent.append)

    notifier.daily_pnl(
        strategy_name="USD1 EMA Slice Ladder",
        mode="live",
        symbol="USD1USDT",
        day="2026-06-20",
        pnl={
            "realized_price": 1.234567,
            "accrued_interest": 0.5,
            "pending_interest": 0.125,
            "unrealized": -0.2,
            "total": 1.534567,
            "apr_est": 8.7654,
            "start_value": 1000.0,
        },
        position={"total_value": 1001.23, "usd1_pct": 75.5, "n_in_usd1": 4, "n_in_usdt": 1},
        markout={"30": {"round_trip": -0.12}},
    )

    text = json.loads(sent[0]["body"])["content"]["text"]
    assert "USD1 EMA Slice Ladder" in text
    assert "每日收益" in text
    assert "USD1USDT" in text
    assert "2026-06-20" in text
    assert "total=$1.534567" in text
    assert "realized=$1.234567" in text
    assert "interest=$0.500000" in text
    assert "pending=$0.125000" in text
    assert "unrealized=$-0.200000" in text
    assert "apr_est=8.7654%" in text
    assert "USD1=75.500%" in text
    assert "rt30=-0.12bp" in text


def test_notify_from_config_is_disabled_without_webhook_env(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    sent = []
    cfg = {"notifications": {"feishu": {"enabled": True, "webhook_env": "FEISHU_WEBHOOK_URL"}}}

    notifier = notify_from_config(cfg, env={}, sender=sent.append)

    notifier.order_placed(
        strategy_name="s", mode="live", symbol="USD1USDT", side="buy",
        slice_idx=0, price=1.0, qty=1.0, link_id="x", order_id=None,
    )
    assert sent == []


def test_notifier_swallow_delivery_errors(capsys):
    def boom(_req):
        raise OSError("network down")

    notifier = FeishuNotifier(webhook_url="https://open.feishu.test/hook", sender=boom)

    notifier.daily_pnl(
        strategy_name="s", mode="live", symbol="USD1USDT", day="2026-06-20",
        pnl={}, position={}, markout={},
    )

    err = capsys.readouterr().err
    assert "Feishu notification failed" in err
    assert "https://open.feishu.test/hook" not in err
