"""The maker order client for Phase 3a — the ONLY file allowed to place orders.

``MakerOrderClient`` mirrors ``BybitPrivateClient`` construction (spot + Unified) but is
the single component that crosses the no-order boundary. It adds NO order method to the
read-only ``BybitPrivateClient``; that client's no-order invariant
(``test_bybit_client.py:133``) stays intact.

Safety stance (D14 — two modes, dryrun|live):
- Built ONLY on the live (maker) path; the dryrun engine never constructs it (simulated
  fills only). Live IS real-money MAINNET — there is no testnet/sandbox gate.
- A missing API key/secret is a hard RuntimeError at construction (no silent downgrade).
- There is NO per-order notional cap: order size is the ladder's (alloc x fraction); the
  total real-money deployment is bounded upstream by ``live.max_total_alloc_usd``.

Grounded ccxt 4.5.54 (``bybit.py``) facts baked in:
- place: ``create_order(sym,'limit',side,qty,price,{postOnly:True,isLeverage:0,clientOrderId:link})``;
  ccxt sets ``timeInForce='PostOnly'`` itself (never pass GTC). Prices/qtys are
  PRE-snapped by ``order_recon.quantize_*`` and forwarded verbatim (we do NOT trust
  ccxt ``price_to_precision``, which rounds and can cross).
- order state: ``fetch_open_orders(params={'orderLinkId':link})`` for the OPEN/partial
  state; if absent ⇒ ``fetch_canceled_and_closed_orders(params={'orderLinkId':link})``
  (``/v5/order/history``) for the TERMINAL Filled-OR-Cancelled/Rejected state. NEVER
  ``fetch_order``/``fetch_closed_order``/``fetch_closed_orders`` (hard-coded Filled-only,
  hides cancels/rejects — the crux Codex blocked v2 on).
- amend: qty-ONLY (``edit_order`` accepts no postOnly/timeInForce; the exchange
  preserves them). Refuses a price change and refuses a partially-filled order.
- dup clientOrderId: retCodes {170141, 12141, 30001} (or a "duplicate"/"repeated"
  message) ALL ⇒ idempotent "already placed → fetch state to learn truth".
- 429: ``RateLimitExceeded``/``DDoSProtection`` ⇒ exponential backoff (1→2→4→…cap 30s),
  bounded retries, then re-raise.

Dependencies: ``ccxt`` (injectable ``ccxt_module=`` for the exchange instance; the real
ccxt is imported only for its EXCEPTION classes), ``sca.live.creds``, ``sca.config``.
``order_recon`` owns the price/qty quantization (callers pre-snap before calling here).
"""
from __future__ import annotations

import math
import re

import ccxt   # real exception hierarchy (no network on import)

from sca.live.creds import credential_env_names, resolve as resolve_creds
from sca.live.bybit_client import private_ccxt_options, sync_time_difference
from sca.live.exchanges.bybit import BybitAdapter

# --- module constants (params live in YAML; these are code-side fallbacks) ---
BACKOFF_START = 1.0
BACKOFF_CAP = 30.0
MAX_RETRIES = 6

# Duplicate-clientOrderId retCodes — ALL idempotent (C-P2#16):
#   170141 InvalidOrder / 12141 BadRequest / 30001 BadRequest "order_link_id is repeated".
DUP_LINKID_CODES = frozenset({170141, 12141, 30001})

# PostOnly/limit-maker rejection retCodes (P1-6): 170218 "LIMIT-MAKER order is rejected
# due to invalid price" maps to InvalidOrder but its message carries no postonly keyword,
# so it must be classified by CODE -> postonly_rejected (re-quote next tick, not an error).
POSTONLY_CODES = frozenset({170218})

# Substrings that classify a raised InvalidOrder/BadRequest (belt-and-braces fallback
# to the retCode set; matched case-insensitively).
_DUP_SUBSTRINGS = ("duplicate", "repeated", "already exist", "order_link_id exist")
_POSTONLY_SUBSTRINGS = ("postonly", "post only", "post-only", "take liquidity",
                        "immediately match", "limit-maker", "limit maker")
_TOO_SMALL_SUBSTRINGS = ("too small", "lower limit", "minimum", "min order",
                         "min notional", "lower than", "below the min")


def _retcode(msg: str):
    m = re.search(r'retCode["\'\s:]*?(\d+)', msg)
    return int(m.group(1)) if m else None


class MakerOrderClient:
    """PostOnly resting-ladder order client. Live MAINNET only (D14)."""

    def __init__(self, *, ccxt_module=None, live_cfg=None, env=None):
        # Credentials (single source of truth, sca.live.creds). A missing key/secret is a
        # hard RuntimeError — there is no silent downgrade (D14: MODE=live == real money,
        # and missing keys must fail loudly at construction, never trade un-keyed).
        key, secret = resolve_creds(live_cfg=live_cfg, env=env)
        if not (key and secret):
            kn, sn = credential_env_names(live_cfg)
            raise RuntimeError(
                f"Bybit API credentials missing: set {kn} and {sn} in the environment."
            )

        self.adapter = BybitAdapter()
        self.ex = self.adapter.make_client(
            api_key=key, secret=secret, options=private_ccxt_options(live_cfg),
            ccxt_module=ccxt_module,
        )
        # MAINNET only (D14): no testnet/sandbox gate — live IS the real venue.

        # injectable sleeper so backoff is instant under test
        import time
        self._sleep = time.sleep

    def __repr__(self) -> str:
        return "<MakerOrderClient MAINNET key=***redacted***>"

    # --- market meta --------------------------------------------------------
    def market_meta(self, symbol: str) -> dict:
        """``{tick, lot, min_qty, min_cost}`` from the ccxt market (TICK_SIZE mode:
        precision values ARE the grid steps)."""
        self.ex.load_markets()                # ccxt: ex.market() raises if markets unloaded
        m = self.ex.market(symbol)
        prec = m.get("precision", {}) or {}
        limits = m.get("limits", {}) or {}
        amt = limits.get("amount", {}) or {}
        cost = limits.get("cost", {}) or {}
        return {
            "tick": float(prec.get("price")),
            "lot": float(prec.get("amount")),
            "min_qty": float(amt.get("min") or 0.0),
            "min_cost": float(cost.get("min") or 0.0),
        }

    # --- wallet balance (read) ----------------------------------------------
    def balance(self) -> dict:
        """UTA wallet balance, normalized — SAME shape as
        ``BybitPrivateClient.get_wallet_balance`` (engine.reconcile_orders sizes the
        available pool from this). This method existed only on the test FakeOrderClients,
        so the LIVE path — the sole caller, reachable only when ``maker_enabled`` — used to
        ``AttributeError`` and spin an endless reconnect loop. MAINNET unified account (D14)."""
        from sca.live.bybit_client import normalize_balance
        return normalize_balance(
            self._with_backoff(lambda: self.ex.fetch_balance({"type": "unified"}))
        )

    # --- place --------------------------------------------------------------
    def place_postonly(self, symbol: str, side: str, price: float, qty: float,
                       link_id: str) -> dict:
        """Place a PostOnly GTC resting limit. ``price``/``qty`` are PRE-snapped and
        forwarded verbatim. Returns a normalized order-state dict (caller MUST re-poll;
        ``create_order`` returns ``status/filled=None`` => accepted, not filled).

        Order size is the ladder's (alloc x fraction) — there is no per-order notional cap
        (D14; the total real-money deployment is bounded upstream by max_total_alloc_usd)."""
        params = self.adapter.order_params(link_id)
        try:
            o = self._with_backoff(
                lambda: self.ex.create_order(symbol, "limit", side, qty, price, params)
            )
        except ccxt.InsufficientFunds as e:
            return {"status_class": "insufficient_funds", "link_id": link_id,
                    "filled": 0.0, "remaining": 0.0, "error": str(e), "raw": None}
        except (ccxt.InvalidOrder, ccxt.BadRequest, ccxt.ExchangeError) as e:
            # InvalidOrder/BadRequest are ExchangeError subclasses; widening to the parent
            # also catches a plain ExchangeError whose message matches limit-maker/postonly
            # (P1-6). _classify_order_error re-raises anything it can't classify, so an
            # unknown ExchangeError still surfaces — never silently swallowed.
            return self._classify_order_error(e, symbol, link_id)
        return self._normalize(o)

    def _classify_order_error(self, e: Exception, symbol: str, link_id: str) -> dict:
        msg = str(e)
        low = msg.lower()
        code = _retcode(msg)
        # 1. duplicate clientOrderId -> idempotent: learn truth from the exchange.
        if code in DUP_LINKID_CODES or any(s in low for s in _DUP_SUBSTRINGS):
            return self.fetch_order_state(symbol, None, link_id=link_id)
        # 2. PostOnly/limit-maker would cross -> NOT an error: re-quote next tick
        # (cooldown upstream). Classified by retCode 170218 (P1-6) OR a postonly/
        # limit-maker message substring.
        if code in POSTONLY_CODES or any(s in low for s in _POSTONLY_SUBSTRINGS):
            return {"status_class": "postonly_rejected", "link_id": link_id,
                    "filled": 0.0, "remaining": 0.0,
                    "reject_reason": "EC_PostOnlyWillTakeLiquidity", "raw": None}
        # 3. below-minimum qty/cost -> too_small: logged-skip, never hot-retried.
        if any(s in low for s in _TOO_SMALL_SUBSTRINGS):
            return {"status_class": "too_small", "link_id": link_id,
                    "filled": 0.0, "remaining": 0.0, "error": msg, "raw": None}
        raise e  # unknown InvalidOrder/BadRequest -> surface it

    # --- amend (qty-only) ---------------------------------------------------
    def amend(self, symbol: str, order_id, *, link_id=None, qty=None, price=None) -> dict:
        """qty-ONLY amend (NO postOnly/timeInForce — ccxt amend has neither; the exchange
        preserves them). Refuses a price change (re-price is cancel+recreate) and refuses
        a partially-filled order (Bybit amend sets TOTAL qty and would corrupt leaves)."""
        if price is not None:
            raise ValueError("amend refuses a price change; re-price is cancel+recreate")
        st = self.fetch_order_state(symbol, order_id, link_id=link_id)
        f = st.get("filled")
        if f is not None and f > 0:
            raise ValueError(
                "amend refuses a partially-filled order; route to cancel+recreate"
            )
        params = self._id_params(order_id, link_id)
        self._with_backoff(
            lambda: self.ex.edit_order(order_id, symbol, "limit", None, amount=qty,
                                       params=params)
        )
        return self.fetch_order_state(symbol, order_id, link_id=link_id)  # re-poll leaves

    # --- cancel -------------------------------------------------------------
    def cancel(self, symbol: str, order_id, *, link_id=None) -> dict:
        """Issue a cancel. The caller (engine ``_cancel_to_terminal``) polls to terminal
        and books the final fill before clearing state."""
        params = self._id_params(order_id, link_id)
        o = self._with_backoff(
            lambda: self.ex.cancel_order(order_id, symbol, params=params)
        )
        return self._normalize(o)

    # --- order state (open-then-terminal by link) ---------------------------
    def fetch_order_state(self, symbol: str, order_id=None, *, link_id=None) -> dict:
        """OPEN state via ``fetch_open_orders``; if absent ⇒ TERMINAL state via
        ``fetch_canceled_and_closed_orders``. NEVER the Filled-only closed path.
        May be called by ``link_id`` alone (id=None) for crash-resume (F14)."""
        params = self._id_params(order_id, link_id)
        open_rows = self._with_backoff(
            lambda: self.ex.fetch_open_orders(symbol, params=params)
        )
        row = self._pick(open_rows, order_id, link_id)
        if row is None:
            term_rows = self._with_backoff(
                lambda: self.ex.fetch_canceled_and_closed_orders(symbol, params=params)
            )
            row = self._pick(term_rows, order_id, link_id)
        if row is None:
            return {"id": order_id, "link_id": link_id, "status": None,
                    "status_class": "not_found", "filled": 0.0, "remaining": 0.0,
                    "avg": None, "price": None, "reject_reason": None, "raw": None}
        st = self._normalize(row)
        f = st["filled"]
        assert f is None or math.isfinite(f), \
            f"fetch_order_state: non-finite filled {f!r} for {link_id or order_id}"
        return st

    def fetch_open(self, symbol: str) -> list[dict]:
        """The FULL normalized open-order list for the symbol; each row carries
        ``link_id`` (=clientOrderId) so ``match_live_orders`` can map truth → slices."""
        rows = self._with_backoff(lambda: self.ex.fetch_open_orders(symbol))
        return [self._normalize(o) for o in rows]

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _id_params(order_id, link_id) -> dict:
        """Link is authoritative on the maker path; fall back to orderId when no link."""
        if link_id is not None:
            return {"orderLinkId": link_id}
        if order_id is not None:
            return {"orderId": order_id}
        return {}

    @staticmethod
    def _pick(rows, order_id, link_id):
        if not rows:
            return None
        for o in rows:
            info = o.get("info") or {}
            col = o.get("clientOrderId") or info.get("orderLinkId")
            oid = o.get("id") or info.get("orderId")
            if link_id is not None and col == link_id:
                return o
            if order_id is not None and oid == order_id:
                return o
        # link/id-filtered query returning exactly one row => that's ours.
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _num(*vals):
        for v in vals:
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _classify_status(raw_status, reject_reason) -> str:
        rr = reject_reason or ""
        if rr and "postonly" in rr.lower():        # EC_PostOnlyWillTakeLiquidity
            return "postonly_rejected"
        s = (raw_status or "").lower()
        if s in ("new", "created", "active", "untriggered", "partiallyfilled",
                 "pendingcancel"):
            return "open"                          # NON-terminal (PendingCancel can still fill)
        if s == "filled":
            return "filled"
        if s in ("cancelled", "canceled", "partiallyfilledcanceled", "deactivated"):
            return "cancelled"
        if s == "rejected":
            return "rejected"
        return "open"                              # unknown -> non-terminal: keep polling

    def _normalize(self, o: dict) -> dict:
        """Raw ccxt order (parsed top-level + raw V5 ``info``) -> stable state dict."""
        info = o.get("info") or {}
        raw_status = info.get("orderStatus") or o.get("status")
        reject_reason = info.get("rejectReason") or info.get("reject_reason")
        if reject_reason in ("", "EC_NoError"):
            reject_reason = None
        return {
            "id": o.get("id") or info.get("orderId"),
            "link_id": o.get("clientOrderId") or info.get("orderLinkId"),
            "side": o.get("side") or info.get("side"),
            "status": raw_status,
            "status_class": self._classify_status(raw_status, reject_reason),
            "filled": self._num(o.get("filled"), info.get("cumExecQty")),
            "remaining": self._num(o.get("remaining"), info.get("leavesQty")),
            "avg": self._num(o.get("average"), info.get("avgPrice")),
            "price": self._num(o.get("price"), info.get("price")),
            "reject_reason": reject_reason,
            "raw": o,
        }

    def _with_backoff(self, fn):
        """Run ``fn``; on 429/DDoS retry with exponential backoff (cap 30s, bounded),
        honoring ``Retry-After``/``rate_limit_reset_ms`` when present, then re-raise."""
        delay = BACKOFF_START
        for attempt in range(MAX_RETRIES + 1):
            try:
                return fn()
            except ccxt.InvalidNonce as e:
                if attempt >= MAX_RETRIES:
                    raise
                sync_time_difference(self.ex)
                self._sleep(self._retry_wait(e, delay))
                delay = min(delay * 2.0, BACKOFF_CAP)
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection) as e:
                if attempt >= MAX_RETRIES:
                    raise
                self._sleep(self._retry_wait(e, delay))
                delay = min(delay * 2.0, BACKOFF_CAP)

    @staticmethod
    def _retry_wait(e: Exception, delay: float) -> float:
        """Prefer an exchange-provided Retry-After / reset hint; else the backoff delay,
        capped."""
        for attr in ("retry_after", "retryAfter"):
            v = getattr(e, attr, None)
            if v:
                try:
                    return min(float(v), BACKOFF_CAP)
                except (TypeError, ValueError):
                    pass
        m = re.search(r'(?:retry[-_ ]?after|rate_limit_reset_ms)["\'\s:]*?(\d+)', str(e),
                      re.IGNORECASE)
        if m:
            secs = float(m.group(1))
            if secs > 1000:           # looks like ms
                secs /= 1000.0
            return min(secs, BACKOFF_CAP)
        return min(delay, BACKOFF_CAP)


def _import_ccxt():
    import ccxt as _ccxt
    return _ccxt
