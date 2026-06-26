"""PrivateReadClient — the adapter-driven, READ-ONLY private client (Phase 2, T1).

The venue-agnostic analogue of ``BybitPrivateClient`` for non-Bybit venues (Bitget).
The engine's R1 reconcile needs two reads from real exchange truth — the wallet
balance and the account-wide open orders — for ANY venue. This client provides them
through an ``ExchangeAdapter``:

  - ``get_wallet_balance()`` -> ``adapter.fetch_balance_coins(ex)`` (already the
    stable, reconcile-ready ``{"coins": {COIN: {"wallet": ...}}, ...}`` shape).
  - ``get_open_orders(symbol)`` -> ccxt ``fetch_open_orders(symbol)`` (``symbol=None``
    => account-wide, matching ``BybitPrivateClient.get_open_orders``).

Scope (hard): NO order surface. It exposes no order method; ``orders.py``'s
``MakerOrderClient`` remains the only component that crosses the no-order boundary.

Credentials resolve through ``sca.live.creds`` (configurable per-venue env names) +
the OKX-family passphrase (Bitget). ``ccxt_module`` / ``env`` / ``live_cfg`` are
injectable purely for unit tests; production passes none and gets real ccxt + env +
config. ``verbose=False`` so signed headers/keys never reach logs.

This is deliberately venue-neutral: the ONLY venue-specific facts (client ctor,
balance shape) live in the adapter, so Bybit can keep its bespoke
``BybitPrivateClient`` (with the UTA ``{"type":"unified"}`` call + InvalidNonce
retry) UNCHANGED while Bitget — and any future venue — uses this.
"""
from __future__ import annotations

from sca.live.creds import (
    credential_env_names,
    resolve as resolve_creds,
    resolve_passphrase,
)
from sca.live.exchanges.base import ExchangeAdapter


class PrivateReadClient:
    """Read-only private client built from an ExchangeAdapter. No order surface."""

    def __init__(self, adapter: ExchangeAdapter, *, ccxt_module=None,
                 live_cfg: dict | None = None, env: dict | None = None,
                 options: dict | None = None, exchange: str | None = None):
        self.adapter = adapter
        # PER-EXCHANGE creds (Phase 3): the engine passes ``exchange`` so a Bitget read
        # client (built with NO live_cfg override) reads BITGET_* env names, not the
        # global Bybit defaults. ``exchange=None`` keeps the legacy global behavior so the
        # explicit-``live_cfg`` unit tests (and any Bybit-default caller) are unchanged.
        key, secret = resolve_creds(live_cfg=live_cfg, env=env, exchange=exchange)
        if not (key and secret):
            kn, sn = credential_env_names(live_cfg, exchange=exchange)
            raise RuntimeError(
                f"API credentials missing: set {kn} and {sn} in the environment "
                "(read-only key recommended; never commit/log them)."
            )
        password = resolve_passphrase(live_cfg=live_cfg, env=env, exchange=exchange)
        # spot read client: ccxt defaultType=spot so fetch_balance returns the SPOT
        # wallet (not a margin/contract sub-account). Adapters that need more options
        # can be threaded via `options=`.
        self.ex = adapter.make_client(
            api_key=key, secret=secret,
            options=options if options is not None else {"defaultType": "spot"},
            password=password, ccxt_module=ccxt_module,
        )

    def __repr__(self) -> str:  # never leak credentials
        return (f"<PrivateReadClient {type(self.adapter).__name__} "
                "key=***redacted***>")

    def get_wallet_balance(self) -> dict:
        """Wallet balance normalized to the venue-agnostic reconcile shape."""
        return self.adapter.fetch_balance_coins(self.ex)

    def get_open_orders(self, symbol: str | None = None) -> list:
        """Open orders; ``symbol=None`` => account-wide (matches BybitPrivateClient)."""
        return self.ex.fetch_open_orders(symbol)
