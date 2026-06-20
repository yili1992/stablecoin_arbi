"""Tests for Feishu webhook notification formatting and delivery."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from sca.live.notify import FeishuNotifier, notify_from_config  # noqa: E402


def test_order_payload_is_feishu_card_with_strategy_and_key_trade_fields():
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
    card = body["card"]
    title = card["header"]["title"]["content"]
    text = card["elements"][0]["text"]["content"]
    assert body["msg_type"] == "interactive"
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "yellow"
    assert "timestamp" not in body
    assert "sign" not in body
    assert "🟡 挂单卖出 | USD1USDT" == title
    assert "USD1 EMA Slice Ladder" in text
    assert "**策略**：USD1 EMA Slice Ladder" in text
    assert "**模式**：实盘" in text
    assert "卖出" in text
    assert "USD1USDT" in text
    assert "**类型**：GTC PostOnly 挂单" in text
    assert "**档位**：#2" in text
    assert "**价格**：1.000500" in text
    assert "**数量**：123.456789" in text
    assert "**link**：sca-2-7" in text
    assert "**order**：oid-1" in text


def test_order_buy_payload_uses_green_card():
    sent = []
    notifier = FeishuNotifier(webhook_url="https://open.feishu.test/hook", sender=sent.append)

    notifier.order_placed(
        strategy_name="USD1 EMA Slice Ladder",
        mode="live",
        symbol="USD1USDT",
        side="buy",
        slice_idx=0,
        price=0.9999,
        qty=50.0,
        link_id="sca-0-1",
        order_id=None,
    )

    body = json.loads(sent[0]["body"])
    assert body["msg_type"] == "interactive"
    assert body["card"]["header"]["template"] == "green"
    assert body["card"]["header"]["title"]["content"] == "🟢 挂单买入 | USD1USDT"
    text = body["card"]["elements"][0]["text"]["content"]
    assert "**order**：pending" in text


def test_fill_payload_is_feishu_card_with_strategy_and_key_trade_fields():
    sent = []
    notifier = FeishuNotifier(webhook_url="https://open.feishu.test/hook", sender=sent.append)

    notifier.fill_executed(
        strategy_name="USD1 EMA Slice Ladder",
        mode="live",
        symbol="USD1USDT",
        side="sell",
        slice_idx=2,
        price=1.0005,
        qty=4.0,
        filled=4.0,
        total=10.0,
        status_class="open",
        realized_capture=1.234567,
        link_id="sca-2-7",
        order_id="oid-1",
    )

    body = json.loads(sent[0]["body"])
    card = body["card"]
    title = card["header"]["title"]["content"]
    text = card["elements"][0]["text"]["content"]
    assert body["msg_type"] == "interactive"
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "yellow"
    assert "timestamp" not in body
    assert "sign" not in body
    assert title == "🟡 成交卖出 | USD1USDT"
    assert "**策略**：USD1 EMA Slice Ladder" in text
    assert "**模式**：实盘" in text
    assert "**类型**：成交回报" in text
    assert "**档位**：#2" in text
    assert "**成交均价**：1.000500" in text
    assert "**本次成交**：4.000000" in text
    assert "**累计成交**：4.000000/10.000000" in text
    assert "**订单状态**：open" in text
    assert "**已实现收益**：$1.234567" in text
    assert "**link**：sca-2-7" in text
    assert "**order**：oid-1" in text


def test_daily_payload_is_blue_card_with_strategy_profit_and_apr():
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

    body = json.loads(sent[0]["body"])
    card = body["card"]
    text = card["elements"][0]["text"]["content"]
    assert body["msg_type"] == "interactive"
    assert card["header"]["template"] == "blue"
    assert card["header"]["title"]["content"] == "📊 每日摘要 | USD1USDT"
    assert "timestamp" not in body
    assert "sign" not in body
    assert "USD1 EMA Slice Ladder" in text
    assert "每日收益" in text
    assert "USD1USDT" in text
    assert "2026-06-20" in text
    assert "**合计 PnL**：$1.534567" in text
    assert "已实现 $1.234567" in text
    assert "已结利息 $0.500000" in text
    assert "待结 $0.125000" in text
    assert "浮动 $-0.200000" in text
    assert "**预计年化**：8.7654%" in text
    assert "**仓位**：USD1 75.500% · 4/1 片" in text
    assert "**rt30**：-0.12bp" in text


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
