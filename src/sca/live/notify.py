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

    def daily_pnl(self, **_kwargs) -> None:
        return None


def _fmt_money(v) -> str:
    return "n/a" if v is None else f"${float(v):.6f}"


def _fmt_num(v, nd=6) -> str:
    return "n/a" if v is None else f"{float(v):.{nd}f}"


def _side_zh(side: str) -> str:
    return "买入" if side == "buy" else "卖出" if side == "sell" else str(side)


class FeishuNotifier:
    def __init__(self, *, webhook_url: str, sender: Sender | None = None):
        self.webhook_url = webhook_url
        self._sender = sender or self._urlopen_sender

    def order_placed(self, *, strategy_name: str, mode: str, symbol: str, side: str,
                     slice_idx: int, price: float, qty: float, link_id: str,
                     order_id: str | None) -> None:
        mode_zh = "实盘" if mode == "live" else mode
        text = "\n".join([
            f"[{strategy_name}] {mode_zh}挂单",
            f"symbol={symbol} side={_side_zh(side)} slice #{slice_idx}",
            f"price={float(price):.6f} qty={float(qty):.6f}",
            f"link={link_id} order={order_id or 'pending'}",
        ])
        self._post_text(text)

    def daily_pnl(self, *, strategy_name: str, mode: str, symbol: str, day: str,
                  pnl: dict, position: dict, markout: dict) -> None:
        mode_zh = "实盘" if mode == "live" else mode
        mk30 = (markout or {}).get("30", {}) or {}
        text = "\n".join([
            f"[{strategy_name}] {mode_zh}每日收益 {day}",
            f"symbol={symbol}",
            "pnl: "
            f"total={_fmt_money(pnl.get('total'))} "
            f"realized={_fmt_money(pnl.get('realized_price'))} "
            f"interest={_fmt_money(pnl.get('accrued_interest'))} "
            f"pending={_fmt_money(pnl.get('pending_interest'))} "
            f"unrealized={_fmt_money(pnl.get('unrealized'))}",
            f"apr_est={_fmt_num(pnl.get('apr_est'), 4)}%",
            "position: "
            f"value={_fmt_money(position.get('total_value'))} "
            f"USD1={_fmt_num(position.get('usd1_pct'), 3)}% "
            f"slices={position.get('n_in_usd1', 'n/a')}/{position.get('n_in_usdt', 'n/a')}",
            f"rt30={_fmt_num(mk30.get('round_trip'), 2)}bp",
        ])
        self._post_text(text)

    def _post_text(self, text: str) -> None:
        payload = {"msg_type": "text", "content": {"text": text}}
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
