"""Bybit private REST client — READ-ONLY (Phase 1+2): UTA wallet balance + open orders.

Scope (hard): this client places NO orders. It deliberately exposes NO order
method, and because the live key may be trade-capable, the tests assert the ccxt
order methods are never invoked on any path here. Real order placement is Phase 3
(`engine.OrderInterface`), behind separate authorization.

Design:
  - Credentials resolve through ``sca.live.creds`` (single source of truth with the
    arm-check) — missing key/secret raises a clear RuntimeError.
  - ccxt is forced to **spot + Unified** (`defaultType='spot'`); ccxt auto-detects
    UTA and sends `accountType=UNIFIED`. `verbose=False` so signed headers/keys
    never hit logs (Codex S2).
  - ``testnet`` routes ccxt sandbox (api-testnet.bybit.com); default from config.
  - Balance is normalized from the raw V5 ``info`` fields (``walletBalance``,
    ``locked``, ...) — NOT ccxt's ``free/used``, which for a UTA reflect margin,
    not the spot open-order lock (Codex P1). ``free := walletBalance - locked`` is
    DISPLAY-grade; reconcile (T4/T5) additionally guards on borrow/equity.

CLI: ``sca balance [--testnet]`` prints the UTA balance table (read-only).
"""
from __future__ import annotations

import argparse
import sys

from sca.config import CFG
from sca.live.creds import credential_env_names, resolve as resolve_creds


def _f(x) -> float:
    """Tolerant float: None/''/'-' -> 0.0."""
    if x is None or x == "" or x == "-":
        return 0.0
    return float(x)


def normalize_balance(raw: dict) -> dict:
    """Map a ccxt bybit ``fetch_balance`` result (UTA) into a stable, ccxt-agnostic
    shape. Reads the raw V5 fields under ``info.result.list[0]``.

    Raises ValueError if the expected UTA structure is absent (fail loud, don't
    silently report an empty/zero balance)."""
    try:
        acct = raw["info"]["result"]["list"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"unexpected Bybit wallet-balance shape: {e}") from e

    coins = {}
    for c in acct.get("coin", []):
        wallet = _f(c.get("walletBalance"))
        locked = _f(c.get("locked"))
        coins[c["coin"]] = {
            "wallet": wallet,
            "locked": locked,
            "free": wallet - locked,          # DISPLAY-grade; see module docstring
            "usd": _f(c.get("usdValue")),
            "equity": _f(c.get("equity")),
            "borrow": _f(c.get("spotBorrow")),
        }
    return {
        "account_type": acct.get("accountType"),
        "totals": {
            "equity_usd": _f(acct.get("totalEquity")),
            "available_usd": _f(acct.get("totalAvailableBalance")),
            "wallet_usd": _f(acct.get("totalWalletBalance")),
            # account-level margin/derivatives exposure — must be ~0 for a spot-only
            # UTA; the reconcile liability guard refuses on any non-zero value (Codex P1).
            "im_usd": _f(acct.get("totalInitialMargin")),
            "mm_usd": _f(acct.get("totalMaintenanceMargin")),
            "perp_upl_usd": _f(acct.get("totalPerpUPL")),
        },
        "coins": coins,
        "raw": raw.get("info"),
    }


def normalize_order(o: dict) -> dict:
    return {
        "id": o.get("id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "price": o.get("price"),
        "qty": o.get("amount"),
        "type": o.get("type"),
    }


class BybitPrivateClient:
    """Read-only Bybit V5 private client (UTA). No order surface."""

    def __init__(self, testnet: bool | None = None, *, ccxt_module=None,
                 live_cfg: dict | None = None, env: dict | None = None):
        live = CFG.get("live", {}) if live_cfg is None else live_cfg
        _, key, secret = resolve_creds(live_cfg=live_cfg, env=env)
        if not (key and secret):
            _, kn, sn = credential_env_names(live_cfg)
            raise RuntimeError(
                f"Bybit API credentials missing: set {kn} and {sn} in the environment "
                "(read-only key recommended; never commit/log them)."
            )
        if testnet is None:
            testnet = bool(live.get("testnet", False))
        self.testnet = bool(testnet)

        mod = ccxt_module if ccxt_module is not None else _import_ccxt()
        self.ex = mod.bybit({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "verbose": False,                 # never log signed headers/keys (Codex S2)
            "options": {"defaultType": "spot"},
        })
        if self.testnet:
            self.ex.set_sandbox_mode(True)

    def __repr__(self) -> str:  # never leak credentials
        return f"<BybitPrivateClient testnet={self.testnet} key=***redacted***>"

    def get_wallet_balance(self) -> dict:
        """UTA wallet balance, normalized (read-only)."""
        return normalize_balance(self.ex.fetch_balance({"type": "unified"}))

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Open orders, normalized. ``symbol=None`` => account-wide (Codex P2)."""
        return [normalize_order(o) for o in self.ex.fetch_open_orders(symbol)]


def _import_ccxt():
    import ccxt
    return ccxt


# ---------------------------------------------------------------------------
# CLI: `sca balance [--testnet]`
# ---------------------------------------------------------------------------
def _print_balance(bal: dict) -> None:
    t = bal["totals"]
    print(f"Bybit {bal.get('account_type') or '?'} account "
          f"({'TESTNET' if bal.get('_testnet') else 'mainnet'})")
    print(f"  total equity (USD):    {t['equity_usd']:,.2f}")
    print(f"  total available (USD): {t['available_usd']:,.2f}")
    print(f"  total wallet (USD):    {t['wallet_usd']:,.2f}")
    print(f"  {'coin':<8}{'wallet':>16}{'locked':>14}{'free':>16}{'usd':>14}{'borrow':>12}")
    for coin, c in sorted(bal["coins"].items()):
        print(f"  {coin:<8}{c['wallet']:>16.6f}{c['locked']:>14.6f}"
              f"{c['free']:>16.6f}{c['usd']:>14.2f}{c['borrow']:>12.6f}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="sca balance",
                                 description="Print Bybit UTA wallet balance (read-only; needs API key in env)")
    ap.add_argument("--testnet", action="store_true",
                    help="route to api-testnet.bybit.com (overrides config live.testnet)")
    a = ap.parse_args(argv)
    try:
        client = BybitPrivateClient(testnet=True if a.testnet else None)
    except RuntimeError as e:
        print(f"sca balance: {e}", file=sys.stderr)
        raise SystemExit(2)
    bal = client.get_wallet_balance()
    bal["_testnet"] = client.testnet
    _print_balance(bal)


if __name__ == "__main__":
    main()
