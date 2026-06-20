"""Feishu webhook notifications for the live stablecoin strategy.

The notifier is deliberately side-effect thin: it formats operational messages and posts to
an incoming webhook. Delivery failure is logged and swallowed so alerts can never affect the
order/reconcile path.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Callable


Sender = Callable[[dict], object]


class NullNotifier:
    def order_placed(self, **_kwargs) -> None:
        return None

    def fill_executed(self, **_kwargs) -> None:
        return None

    def daily_pnl(self, **_kwargs) -> None:
        return None


def _fmt_money(v) -> str:
    return "n/a" if v is None else f"${float(v):.6f}"


def _fmt_num(v, nd=6) -> str:
    return "n/a" if v is None else f"{float(v):.{nd}f}"


def _side_zh(side: str) -> str:
    return "买入" if side == "buy" else "卖出" if side == "sell" else str(side)


def _mode_zh(mode: str) -> str:
    return "实盘" if mode == "live" else mode


def _card_payload(*, title: str, template: str, text: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            ],
        },
    }


class FeishuNotifier:
    def __init__(self, *, webhook_url: str, sender: Sender | None = None):
        self.webhook_url = webhook_url
        self._sender = sender or self._urlopen_sender

    def order_placed(self, *, strategy_name: str, mode: str, symbol: str, side: str,
                     slice_idx: int, price: float, qty: float, link_id: str,
                     order_id: str | None) -> None:
        buy = side == "buy"
        title = f"{'🟢 挂单买入' if buy else '🟡 挂单卖出'} | {symbol}"
        template = "green" if buy else "yellow"
        text = "\n".join([
            f"**策略**：{strategy_name}",
            f"**模式**：{_mode_zh(mode)}",
            f"**标的**：{symbol} · {_side_zh(side)}",
            "**类型**：GTC PostOnly 挂单",
            f"**档位**：#{slice_idx}",
            f"**价格**：{float(price):.6f}",
            f"**数量**：{float(qty):.6f}",
            f"**link**：{link_id}",
            f"**order**：{order_id or 'pending'}",
        ])
        self._post_payload(_card_payload(title=title, template=template, text=text))

    def fill_executed(self, *, strategy_name: str, mode: str, symbol: str, side: str,
                      slice_idx: int, price: float, qty: float, filled: float,
                      total: float, status_class: str, realized_capture: float,
                      link_id: str | None, order_id: str | None) -> None:
        buy = side == "buy"
        title = f"{'🟢 成交买入' if buy else '🟡 成交卖出'} | {symbol}"
        template = "green" if buy else "yellow"
        text = "\n".join([
            f"**策略**：{strategy_name}",
            f"**模式**：{_mode_zh(mode)}",
            f"**标的**：{symbol} · {_side_zh(side)}",
            "**类型**：成交回报",
            f"**档位**：#{slice_idx}",
            f"**成交均价**：{float(price):.6f}",
            f"**本次成交**：{float(qty):.6f}",
            f"**累计成交**：{float(filled):.6f}/{float(total):.6f}",
            f"**订单状态**：{status_class}",
            f"**已实现收益**：{_fmt_money(realized_capture)}",
            f"**link**：{link_id or 'n/a'}",
            f"**order**：{order_id or 'n/a'}",
        ])
        self._post_payload(_card_payload(title=title, template=template, text=text))

    def daily_pnl(self, *, strategy_name: str, mode: str, symbol: str, day: str,
                  pnl: dict, position: dict, markout: dict) -> None:
        mk30 = (markout or {}).get("30", {}) or {}
        title = f"📊 每日摘要 | {symbol}"
        text = "\n".join([
            f"**策略**：{strategy_name}",
            f"**模式**：{_mode_zh(mode)}",
            "**类型**：每日收益",
            f"**日期**：{day}",
            f"**标的**：{symbol}",
            f"**合计 PnL**：{_fmt_money(pnl.get('total'))}",
            "已实现 "
            f"{_fmt_money(pnl.get('realized_price'))} · "
            f"已结利息 {_fmt_money(pnl.get('accrued_interest'))} · "
            f"待结 {_fmt_money(pnl.get('pending_interest'))} · "
            f"浮动 {_fmt_money(pnl.get('unrealized'))}",
            f"**预计年化**：{_fmt_num(pnl.get('apr_est'), 4)}%",
            f"**账户价值**：{_fmt_money(position.get('total_value'))}",
            "**仓位**："
            f"USD1 {_fmt_num(position.get('usd1_pct'), 3)}% · "
            f"{position.get('n_in_usd1', 'n/a')}/{position.get('n_in_usdt', 'n/a')} 片",
            f"**rt30**：{_fmt_num(mk30.get('round_trip'), 2)}bp",
        ])
        self._post_payload(_card_payload(title=title, template="blue", text=text))

    def _post_payload(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = {"url": self.webhook_url, "body": body.decode("utf-8"),
               "headers": {"Content-Type": "application/json; charset=utf-8"}}
        try:
            self._sender(req)
        except Exception as e:  # pragma: no cover - precise failures are sender-specific
            print(f"[notify] Feishu notification failed: {type(e).__name__}",
                  file=sys.stderr)

    @staticmethod
    def _urlopen_sender(req: dict):
        request = urllib.request.Request(
            req["url"], data=req["body"].encode("utf-8"),
            headers=req.get("headers") or {}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.read()


def notify_from_config(cfg: dict, *, env: dict | None = None,
                       sender: Sender | None = None):
    env = os.environ if env is None else env
    feishu = ((cfg or {}).get("notifications") or {}).get("feishu") or {}
    if not feishu.get("enabled", False):
        return NullNotifier()
    webhook = env.get(feishu.get("webhook_env", "FEISHU_WEBHOOK_URL"))
    if not webhook:
        return NullNotifier()
    return FeishuNotifier(webhook_url=webhook, sender=sender)
