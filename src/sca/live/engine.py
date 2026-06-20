#!/usr/bin/env python3
"""
================================================================================
 sca.live.engine — PAPER (and gated LIVE) slice-ladder engine on LIVE Bybit data
================================================================================

WHAT THIS IS
    A self-contained, async, event-driven engine that runs the EMA-anchored
    take-profit slice ladder (variant r1_6, see sca/backtest/strategy.py) against
    the LIVE Bybit public spot feed and SIMULATES fills — it places NO real orders
    and needs NO API key. It mirrors the backtest slice rules EXACTLY so that
    paper == backtest, and it emits a rich status_<symbol>.json for the dashboard.

STRATEGY (pulled from sca.config.CFG, never hardcoded):
    - Capital ALLOC split into N slices (strategy.fractions, sum=1). Each slice is
      independent and starts long USD1.
    - Floating anchor = EMA(strategy.anchor_ema_span) on the 1h timeframe, using
      ONLY closed 1h candles (no lookahead). Updated on each new closed 1h kline.
    - Slice k in USD1 sells when price reaches  anchor + rungs[k] bp  -> goes "usdt".
    - Slice in USDT rebuys when price reaches   anchor + rebuy_offset_bp (=-1bp) ->
      goes "usd1", booking realized_capture += (sell_px - buy_px)*qty (compounds).
    - Interest (strategy.interest_apr APR) accrues on slices currently in USD1.

FILL MODEL (paper)
    Maker fills simulated off the live top-of-book, matching the backtest "touch"
    model (a resting limit fills when price merely reaches it):
      - a slice SELL at rung R fills when best BID >= R   (market lifted to R)
      - a slice REBUY at B    fills when best ASK <= B     (market dropped to B)
    Fill price is the rung level itself (no adverse haircut). Real adverse
    selection is NOT assumed away — it is measured separately by the markout
    (adverse-selection) gauge, exactly as sca/tools/dryrun.py does. THAT markout
    is the honest edge gauge; the strategy only thinly beats holding and offers no
    guaranteed profit.

SAFETY (two modes; default is dryrun) — D14
    The mode is the ONE switch (config ``runtime.mode`` / env ``MODE``):
      - ``dryrun`` (DEFAULT) — runs the maker engine but SIMULATES matching off the
        live top-of-book; it builds NO order client, needs NO API key, and places NO
        real orders. The markout gauge still records adverse selection.
      - ``live`` — places real GTC PostOnly maker orders on MAINNET (real money).
        ``MODE=live`` ALONE selects it (no extra confirm env); missing API keys raise
        naturally at order-client construction. A real-money loss limit is enforced
        ONLY by the deployment cap ``live.max_total_alloc_usd`` (capital = loss cap).

Usage:  sca paper  --symbol USD1USDT --seconds 600   # dryrun (simulated), no keys
        python -m sca.live.engine --symbol USD1USDT --seconds 600 --csv out.csv
        sca live  --symbol USD1USDT          # real mainnet maker orders (needs keys)
================================================================================
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import os
import signal
import statistics
import sys
import time
import urllib.request

from sca.interest import DailyMinInterest   # shared carry model (parity with backtest)
from sca.live.persistence import (          # atomic restart/resume primitives
    append_event, load_state, read_events, save_state,
)
from sca.live.reconcile import reconcile    # R1 reconciliation brain (pure; no ccxt)
from sca.live.order_recon import (           # PURE maker reconciliation core (no ccxt)
    desired_orders, diff_orders, match_live_orders,
)

# --- config (single source of truth) ----------------------------------------
try:
    from sca.config import (CFG as _CFG, out_dir as _cfg_out_dir,
                            resolve_mode as _resolve_mode, runtime as _cfg_runtime)
except Exception:  # pragma: no cover - config must exist, but stay importable
    _CFG = {}
    def _cfg_out_dir(fallback=".", cfg=None):
        return os.environ.get("SCA_OUT_DIR") or fallback
    def _resolve_mode(cfg=None, env=None):
        m = os.environ.get("MODE") or "dryrun"
        return m if m in ("dryrun", "live") else "dryrun"
    def _cfg_runtime(cfg=None):
        return {"symbol": "USD1USDT", "seconds": 604800, "mode": "dryrun", "dashboard_port": 3015}

_S = _CFG.get("strategy", {})
_B = _CFG.get("backtest", {})
_D = _CFG.get("dryrun", {})
_LIVE = _CFG.get("live", {})

# strategy params (mirror backtest/strategy.py)
ANCHOR_EMA_SPAN = int(_S.get("anchor_ema_span", 21))
RUNG_BP = list(_S.get("rungs", [5, 7, 10, 14, 20]))
FRACS = list(_S.get("fractions", [0.15, 0.18, 0.20, 0.22, 0.25]))
REBUY_OFF_BP = float(_S.get("rebuy_offset_bp", -1))
APR = float(_S.get("interest_apr", 0.10))
ALLOC = float(_B.get("alloc_usd", 10_000.0))
TICK_DP = 4  # tickSize 1bp -> round all order prices to 4 decimals (== backtest)

# runtime / feed params
WS_URL = _D.get("ws_url", "wss://stream.bybit.com/v5/public/spot")
REST_BASE = "https://api.bybit.com"
HORIZONS = list(_D.get("horizons_sec", [5, 30]))   # markout horizons (seconds)
# launch defaults from the consolidated runtime: block (single source), NOT dryrun:
_RT = _cfg_runtime()
DEFAULT_SYMBOL = _RT["symbol"]
DEFAULT_SECONDS = _RT["seconds"]

SEC_PER_YEAR = 365 * 24 * 3600
MID_RETAIN = max(HORIZONS) + 60 if HORIZONS else 90  # keep mid history this long
STATUS_EVERY = 12       # write status_<sym>.json + print summary every ~12s
EVENTS_CAP = 60
KLINES_CAP = 120
HISTORY_CAP = 600
ONE_HOUR_MS = 3_600_000

# --- maker fill driver (Phase 3a) constants ---------------------------------
EPS_LOT = 1e-9                                  # leaves at/below this => fully filled
# Explicit TERMINAL order-state classes (P0-1). The cancel poll-to-terminal loop breaks
# ONLY on one of these; everything else — `open`, `pending_cancel`, AND a transient
# `not_found` (order absent from BOTH fetch_open_orders and the canceled/closed history
# due to eventual consistency) — is "keep polling", never a terminal outcome. On bounded-
# poll exhaustion we HALT fail-closed and NEVER clear local state on an unknown outcome.
TERMINAL_ORDER_CLASSES = frozenset(
    {"filled", "cancelled", "rejected", "postonly_rejected"})
# Bounded backoff (seconds) for the post-cancel poll-to-terminal. Bybit can return
# PendingCancel / still-'open' right after a cancel and the resting leaves CAN still
# fill in that window, so we keep polling until a TERMINAL class is observed; if it
# never reaches terminal within these bounded retries we HALT (fail-closed), never
# clear local state on an unknown outcome (R2-P0).
CANCEL_POLL_BACKOFFS = (0.0, 0.25, 0.5, 1.0, 2.0)
# Bounded backoff (seconds) for the fail-CLOSED durable persist on the maker path.
# Each entry is one save_state attempt; on exhaustion we cancel ALL resting orders
# and halt rather than continue with an in-memory-only fill (A9 / F10).
PERSIST_RETRY_BACKOFFS = (0.0, 0.1, 0.25, 0.5)
# Default per-slice consecutive-PostOnly-reject streak that trips the operator halt
# (anti-livelock, F9). Code-side fallback; overridable via live.reject_halt_threshold.
DEFAULT_REJECT_HALT_THRESHOLD = 5
# Order/accounting fields injected onto every slice on the maker path (A3 / A5).
_ORDER_FIELD_DEFAULTS = {
    "order_id": None, "order_link_id": None, "order_px": None, "order_side": None,
    "order_qty": None, "filled_qty": 0.0, "order_gen": 0, "reject_streak": 0,
    "sell_proceeds": 0.0, "qty_sold": 0.0,
}


class OperatorReconcileHalt(RuntimeError):
    """Fail-closed halt requiring human reconciliation: an unattributable executed
    fill, a cancel that never reached terminal, a tripped reject-streak threshold, or
    a durable-persist failure. Raised so it propagates out of the maker loop; the
    run-loop kill-switch (A10, Task 6) then cancels every resting order on the way out.
    """


# ----------------------------------------------------------------------------
# JSON helpers — emit null, NEVER NaN/Infinity
# ----------------------------------------------------------------------------
def _r(x, nd: int = 6):
    """Round to nd decimals; map None/NaN/Inf -> None (JSON-safe)."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf):
        return None
    return round(xf, nd)


def _sanitize(obj):
    """Recursively replace non-finite floats with None so json is always valid."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _utc(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


# ----------------------------------------------------------------------------
# Live order gate (SAFETY) — scaffold only, can never trade by accident
# ----------------------------------------------------------------------------
def live_authorization(mode: str) -> tuple[bool, str]:
    """Return (armed, reason). Armed iff ``mode == "live"`` (D14).

    The mode is the ONE switch: ``MODE=live`` ALONE selects real-money mainnet maker
    orders — there is no extra confirm env. API keys are NOT pre-checked here; a live
    run with missing keys raises naturally when the order client is constructed (the
    keys still resolve through ``sca.live.creds``, the single source of truth)."""
    if mode != "live":
        return False, "mode is not 'live' (dryrun simulation)"
    return True, "armed (mode=live — real-money mainnet)"


class OrderInterface:
    """Gated real-order hook. Deliberately NOT wired into the simulated fill loop.

    Wiring this into execution requires implementing the Bybit private API AND the
    engine being `armed`. As a final guard it raises even when armed, so the engine
    physically cannot send a real order without a human implementing+arming it.
    """

    def __init__(self, armed: bool, reason: str):
        self.armed = armed
        self.reason = reason

    def place_order(self, side: str, price: float, qty: float):  # pragma: no cover
        if not self.armed:
            raise PermissionError(
                f"REFUSED real order ({side} {qty}@{price}): {self.reason}. "
                "Paper mode places NO orders and needs NO API key."
            )
        raise NotImplementedError(
            "Live order placement is an intentional scaffold and is NOT implemented; "
            "refusing to send a real order. Implement the Bybit private API explicitly."
        )


# ----------------------------------------------------------------------------
# REST bootstrap (public, no key)
# ----------------------------------------------------------------------------
def _rest_kline(symbol: str, interval: str, limit: int = 200) -> list[list]:
    """Return Bybit spot klines OLDEST-FIRST: [[startMs, o, h, l, c, vol, turn], ...]."""
    url = (f"{REST_BASE}/v5/market/kline?category=spot&symbol={symbol}"
           f"&interval={interval}&limit={limit}")
    req = urllib.request.Request(url, headers={"User-Agent": "sca-live"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    return data["result"]["list"][::-1]  # API returns newest-first


# ----------------------------------------------------------------------------
# Markout (adverse-selection) gauge — same method as tools/dryrun.py
# ----------------------------------------------------------------------------
def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def aggregate_markout(done: list, spreads: list) -> dict:
    """Median markout per horizon + counts/spread. None (not NaN) when empty."""
    buys = [mo for s, _, mo in done if s == "buy"]
    sells = [mo for s, _, mo in done if s == "sell"]
    mk = {}
    for h in HORIZONS:
        b = _med([mo.get(h) for mo in buys])
        s = _med([mo.get(h) for mo in sells])
        rt = (b + s) if (b is not None and s is not None) else None
        mk[str(h)] = {"buy": _r(b, 4), "sell": _r(s, 4), "round_trip": _r(rt, 4)}
    return {
        "markout": mk,
        "n_buy": len(buys),
        "n_sell": len(sells),
        "avg_spread_bp": _r(_mean(spreads), 4),
    }


def _fmt(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) and math.isfinite(x) else " n/a"


# ----------------------------------------------------------------------------
# Paper / (gated) live slice-ladder engine
# ----------------------------------------------------------------------------
class PaperEngine:
    def __init__(self, symbol: str = DEFAULT_SYMBOL, mode: str = "dryrun",
                 seconds: int = DEFAULT_SECONDS, csv_path: str | None = None,
                 allow_fresh: bool = False, expect_asset: str | None = None,
                 expect_amount: float | None = None):
        self.symbol = symbol
        self.req_mode = mode if mode in ("dryrun", "live") else "dryrun"
        self.seconds = int(seconds)
        self.csv_path = csv_path
        # operator opt-in + declaration for a FIRST armed-live fresh deploy (Codex P0);
        # paper ignores them. The declaration (asset+amount) must match the exchange.
        self.allow_fresh = bool(allow_fresh)
        self.expect_asset = expect_asset
        self.expect_amount = expect_amount
        self.out_dir = (os.path.dirname(csv_path) if csv_path
                        else _cfg_out_dir("."))
        if not self.out_dir:
            self.out_dir = "."

        # --- live-trading gate (SAFETY) — armed iff mode==live (D14) ---
        self.armed, self.gate_reason = live_authorization(self.req_mode)
        self.order_iface = OrderInterface(self.armed, self.gate_reason)
        # effective/reported mode mirrors the arm decision (dryrun unless live-armed)
        self.mode = "live" if self.armed else "dryrun"

        # --- strategy params ---
        self.fracs = list(FRACS)
        self.rungs = list(RUNG_BP)
        self.n = len(self.fracs)
        self.alloc = ALLOC

        # --- anchor (EMA on closed 1h candles) ---
        self.ema: float | None = None
        self.anchor: float | None = None
        self.last_1h_start: int | None = None
        self._k = 2.0 / (ANCHOR_EMA_SPAN + 1)

        # --- live book / trade state ---
        self.bid: float | None = None
        self.ask: float | None = None
        self.last: float | None = None

        # --- position ---
        self.slices: list[dict] = []
        self.deployed = False
        self.realized_capture = 0.0
        # PnL baseline (status start_value). None on the paper/dryrun path (which deploys the
        # full config ``alloc`` in simulation, so ``alloc`` IS the honest baseline). The LIVE
        # seed-from-balance path sets this to the ACTUAL capital deployed (bounded by
        # ``max_total_alloc_usd``), so a capped / wallet-funded canary does NOT report a
        # phantom loss against the $10k notional. Persisted (v2) + restored on resume.
        self._deployed_capital: float | None = None

        # --- interest: shared Bybit USD1 carry model (identical to backtest) ---
        self.interest = DailyMinInterest(APR / 365.0)

        # --- events / klines / history ---
        self.events: list[dict] = []
        self.klines5: dict[int, dict] = {}   # start_ms -> {t,o,h,l,c}
        self.history: list[dict] = []

        # --- markout gauge (dryrun method) ---
        self.mids_t: list[float] = []
        self.mids_v: list[float] = []
        self.pending: list[list] = []   # [t, side, fill_price]
        self.done: list[list] = []      # [side, fill_price, {h: markout_bp}]
        self.spreads: list[float] = []

        self.start = time.time()
        self.last_status = 0.0

        # --- maker order path (real GTC PostOnly orders; live mode only) -----
        # Lazily-built order client + R1-gate decision cache. ``maker_enabled`` is the
        # maker-path switch (== live mode, D14); it is computed in the run-loop wiring and
        # defaults OFF here so the dryrun (simulated-fill) path is byte-identical. Tests set
        # these fields directly (the documented seam). ``_halted`` is the operator-
        # reconcile halt flag (order-lifecycle safety: an unattributable fill, a cancel
        # that never reaches terminal, a tripped reject streak, or a durable-persist
        # failure) — NOT a PnL kill-switch. D16: it is DURABLE (persisted before the raise
        # + restored on resume) and a resumed-halted engine REFUSES the maker path and
        # exits cleanly, so a docker auto-restart can never silently continue a halted bot.
        self.order_client = None
        self._r1_ok = False
        self._r1_report = None
        self._r1_open_orders = None
        self.maker_enabled = False
        self._halted = False
        self._halt_reason: str | None = None    # human-readable reason (persisted, D16)
        self._reject_anchor: dict[int, float | None] = {}   # slice_idx -> anchor at last reject
        self._reject_halt_threshold = int(
            _LIVE.get("reject_halt_threshold", DEFAULT_REJECT_HALT_THRESHOLD))
        # --- total-alloc deployment cap (the ONLY real-money fund limit, D14) ----
        # Bounds what the SEED + the reconcile available-pool deploy so a funded wallet
        # can't deploy everything (arb-execution-risk: enforce caps in the SIZING path,
        # not just config). -1 => no cap (use the full available wallet — the boss's
        # "用钱包里所有的钱"). On a spot account the capital deployed IS the loss ceiling,
        # so this single cap replaces the removed max-order / max-loss machinery.
        self._max_total_alloc_usd = float(_LIVE.get("max_total_alloc_usd", -1.0))
        self._sleep = time.sleep                            # injectable for the cancel poll
        # Re-entrancy guard for the fail-CLOSED persist primitive: while the
        # cancel-all-on-persist-failure sweep runs, nested _persist_durable_or_halt
        # calls (from _cancel_to_terminal) must be best-effort, not recurse into
        # another cancel-all + halt. Set only inside _persist_durable_or_halt's
        # exhaustion handler.
        self._persist_failing = False

        # --- restart / resume (ADDITIVE; gated by config live.persist) -------
        # Default ON. With no prior state file (or persist=False) this is a
        # no-op and the engine starts byte-identically to before. _maybe_resume
        # runs LAST so it can overwrite the defaults set above (start, slices,
        # interest, ...) when a prior snapshot exists.
        self.persist = bool(_LIVE.get("persist", True))
        self._resumed = False
        self._maybe_resume()

    # -- restart / resume ---------------------------------------------------
    def _state_dict(self) -> dict:
        """v=2 resume snapshot (v=1 + per-slice order/accounting fields), written
        SYNCHRONOUSLY on every fill and status
        write so the snapshot is always >= the event log — resume reads the
        snapshot and never replays the log.

        NOTE: the markout gauge (`self.done`) is intentionally NOT persisted. Its
        per-horizon dicts use INTEGER keys ({30: bp}); JSON would coerce them to
        strings, breaking aggregate_markout's ``mo.get(30)``. Markout is a
        measurement quantity that rebuilds from the live trade stream within tens
        of seconds of resume — an acceptable, bounded loss (vs. the position /
        realized / interest / dashboard state, which must survive exactly).
        """
        return {
            "v": 2,
            "symbol": self.symbol,
            # mode/armed are NEVER restored from snapshot — the live safety gate is
            # always recomputed from env (live_authorization). Restoring a stale
            # mode:live would bypass the gate. Persisted here for human/dashboard
            # readability only; _maybe_resume deliberately ignores this field.
            "mode": self.mode,
            "start": self.start,
            "deployed": self.deployed,
            "realized_capture": self.realized_capture,
            "slices": self.slices,
            "interest": self.interest.to_dict(),
            "anchor": self.anchor,
            "ema": self.ema,
            "last_1h_start": self.last_1h_start,
            "history": self.history,
            # D16 (ADDITIVE — schema stays v=2): the operator-reconcile halt, made
            # durable so a docker auto-restart cannot silently resume a halted bot. A
            # pre-D16 snapshot has neither key; resume reads them with .get(default).
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            # PnL baseline (status start_value) for a LIVE seed-from-balance deploy. Additive
            # (schema stays v=2); a pre-fix snapshot lacks it -> resume .get defaults to None
            # -> the status falls back to ``alloc`` (the old behaviour; harmless for paper).
            "deployed_capital": self._deployed_capital,
        }

    def _maybe_resume(self):
        """Restore prior state from ``<out_dir>/<symbol>_<mode>_state.json`` if present
        (and persistence is enabled). No file / persist off / unknown schema /
        missing-or-invalid key => fresh start, byte-identical to the
        pre-persistence behaviour.

        # D15: the snapshot is segregated by the engine's RESOLVED mode (the
        # persistence ``tag`` == ``self.mode`` ∈ {dryrun, live}). A live run reads
        # ONLY ``<symbol>_live_state.json`` and a dryrun run ONLY
        # ``<symbol>_dryrun_state.json``, so a dryrun simulation can never be loaded
        # by a real-money live run (and a pre-D15 untagged file is simply ignored).

        # mode/armed are NEVER restored from snapshot — the live safety gate is
        # always recomputed from env (live_authorization). Restoring a stale
        # mode:live would bypass the gate. Only position/accounting/dashboard
        # fields below are restored.
        """
        if not self.persist:
            return
        st = load_state(self.out_dir, self.symbol, tag=self.mode)
        if st is None:
            return                                  # fresh start (no prior state)
        v = st.get("v")
        if v not in (1, 2):                         # unknown schema: don't crash
            print(f"[{self.mode}] resume: unknown state schema v={v!r}; "
                  "ignoring it and starting fresh.")
            return
        # ATOMIC RESTORE: a v==1 snapshot may still be missing a key or hold a
        # wrong-typed field (hand-edited / truncated / future-schema drift). Build
        # every restored value into LOCALS first (incl. DailyMinInterest.from_dict,
        # which KeyErrors on a malformed interest sub-dict), THEN type-check those
        # locals; only after BOTH succeed do we commit to self. On any missing key
        # (KeyError/TypeError/ValueError) OR a wrong field type we log and fall
        # back to a FULLY fresh start — never a half-restored hybrid that mixes a
        # stale position with __init__ defaults.
        try:
            start = st["start"]
            deployed = st["deployed"]
            realized_capture = st["realized_capture"]
            slices = st["slices"]
            anchor = st["anchor"]
            ema = st["ema"]
            last_1h_start = st["last_1h_start"]
            history = st["history"]
            interest = DailyMinInterest.from_dict(st["interest"])
        except (KeyError, TypeError, ValueError) as e:
            # Nothing above was assigned to self, so __init__ defaults stand.
            # ValueError covers from_dict on a malformed interest sub-dict (e.g.
            # set() of an unhashable element); KeyError = missing key; TypeError =
            # e.g. interest is a list so from_dict's d["daily_rate"] fails.
            print(f"[resume] v=1 state missing/invalid key ({type(e).__name__}: {e}); "
                  "starting fresh", file=sys.stderr)
            return
        # LIGHTWEIGHT TYPE CHECK (on LOCALS, before any self mutation): a v==1
        # snapshot whose keys are all present but WRONG-TYPED (hand-edited /
        # future-schema drift) assigns cleanly above yet detonates later in
        # _t_end (start+seconds), accrue, status_doc or evaluate_fills, violating
        # the "malformed v1 -> fresh start" contract. Validate TYPE only (no range
        # checks — avoid over-engineering). `deployed` must be a real bool, NOT
        # just truthy, so a "yes" string can't silently flip the engine deployed.
        # bool is an int subclass, so numeric fields accept bool harmlessly (it IS
        # a number); only `deployed` needs the strict bool test.
        type_ok = (
            isinstance(start, (int, float))
            and isinstance(deployed, bool)
            and isinstance(realized_capture, (int, float))
            and isinstance(slices, list)
            and isinstance(history, list)
            and (anchor is None or isinstance(anchor, (int, float)))
            and (ema is None or isinstance(ema, (int, float)))
            and (last_1h_start is None or isinstance(last_1h_start, int))
        )
        if not type_ok:
            # Still nothing assigned to self -> __init__ defaults stand (atomic).
            print("[resume] v=1 state has invalid field type "
                  f"(start={type(start).__name__}, deployed={type(deployed).__name__}, "
                  f"realized_capture={type(realized_capture).__name__}, "
                  f"slices={type(slices).__name__}, history={type(history).__name__}, "
                  f"anchor={type(anchor).__name__}, ema={type(ema).__name__}, "
                  f"last_1h_start={type(last_1h_start).__name__}); starting fresh",
                  file=sys.stderr)
            return
        # v1->v2 MIGRATION + order-field type-check (still on LOCALS, before any self
        # mutation -> atomic). A v=1 snapshot is pre-maker (no live orders, no in-flight
        # sell cycle) so injecting the order/accounting defaults is safe; a v=2 snapshot
        # keeps whatever it already carries (a paper v2 has no order fields at all -> we
        # do NOT fabricate them here; the maker path injects lazily via
        # _ensure_order_fields). Either way a PRESENT but wrong-typed order field => the
        # same FULLY-fresh fallback as a bad core field.
        if not self._migrate_order_fields(slices, migrate=(v == 1)):
            print("[resume] state has invalid order field type; starting fresh",
                  file=sys.stderr)
            return
        # commit (atomic) — self is mutated only past this point
        self.start = start
        self.deployed = deployed
        self.realized_capture = realized_capture
        self.slices = slices
        self.anchor = anchor
        self.ema = ema
        self.last_1h_start = last_1h_start
        self.history = history
        self.interest = interest
        # D16: restore the DURABLE operator-reconcile halt (additive — a pre-D16 snapshot
        # lacks both keys, so .get defaults to a NON-halted resume; this is NOT a fresh
        # start). A resumed ``_halted`` is enforced by ``_enforce_resume_halt_gate`` at the
        # top of run() — the engine refuses the maker path and exits cleanly until a human
        # clears it (delete the state file or set LIVE_CLEAR_HALT=yes). Truthy-coerce so a
        # corrupt non-empty value fails SAFE (stays halted). (There is no PnL max-loss
        # kill-switch to persist — D14 removed it; only this halt is durable.)
        self._halted = bool(st.get("halted", False))
        _hr = st.get("halt_reason")
        self._halt_reason = _hr if isinstance(_hr, str) else None
        # Restore the LIVE PnL baseline (additive; a pre-fix snapshot lacks it -> None -> the
        # status falls back to ``alloc``). Type-guard so a corrupt non-number stays None (safe).
        _dc = st.get("deployed_capital")
        self._deployed_capital = float(_dc) if isinstance(_dc, (int, float)) else None
        self.events = read_events(self.out_dir, self.symbol, tag=self.mode)[-EVENTS_CAP:]
        self._resumed = True
        print(f"[{self.mode}] resumed {self.symbol}: deployed={self.deployed} "
              f"slices={len(self.slices)} realized={self.realized_capture:.6f} "
              f"settled_int={self.interest.settled:.6f} events={len(self.events)}")

    @staticmethod
    def _migrate_order_fields(slices, *, migrate: bool) -> bool:
        """v1->v2 migration + a per-slice order-field type-check, run on the LOCAL
        ``slices`` list BEFORE any self mutation (so a bad field returns False ->
        atomic fresh start). When ``migrate`` (the source was v=1, pre-maker) inject
        the order/accounting defaults; for v=2 leave the slice as-is. Either way,
        any order field that IS present must be correctly typed (an absent field is
        fine — the maker path injects it lazily via ``_ensure_order_fields``).
        Idempotent: re-running on an already-migrated/v2 slice via ``setdefault``
        changes nothing. Returns True iff every slice is a dict with valid order
        fields."""
        checks = {
            "order_id": lambda x: x is None or isinstance(x, str),
            "order_link_id": lambda x: x is None or isinstance(x, str),
            "order_side": lambda x: x in ("buy", "sell", None),
            "filled_qty": lambda x: isinstance(x, (int, float)),
            "order_gen": lambda x: isinstance(x, int),
            "sell_proceeds": lambda x: isinstance(x, (int, float)),
            "qty_sold": lambda x: isinstance(x, (int, float)),
        }
        for s in slices:
            if not isinstance(s, dict):
                return False
            if migrate:                              # v1 -> v2: inject pre-maker defaults
                for k, default in _ORDER_FIELD_DEFAULTS.items():
                    s.setdefault(k, default)
            for k, ok in checks.items():
                if k in s and not ok(s[k]):
                    return False
        return True

    def _t_end(self) -> float:
        """End-of-run wall-clock deadline. seconds<=0 => run forever (inf);
        otherwise relative to the (possibly resumed) start."""
        return float("inf") if self.seconds <= 0 else self.start + self.seconds

    # -- anchor -------------------------------------------------------------
    def _ema_step(self, close: float):
        self.ema = close * self._k + self.ema * (1 - self._k) if self.ema is not None else close
        self.anchor = self.ema

    def bootstrap(self):
        """REST-load closed 1h klines (EMA anchor) + recent 5m klines (chart)."""
        # 1h -> EMA anchor over CLOSED candles only (no lookahead)
        rows = _rest_kline(self.symbol, "60", limit=200)
        now_ms = int(time.time() * 1000)
        closed = [r for r in rows if int(r[0]) + ONE_HOUR_MS <= now_ms]
        if not closed:
            closed = rows[:-1] if len(rows) > 1 else rows
        self.ema = float(closed[0][4])
        for r in closed[1:]:
            self._ema_step(float(r[4]))
        self.anchor = self.ema
        self.last_1h_start = int(closed[-1][0])

        # 5m -> recent candles for the chart
        rows5 = _rest_kline(self.symbol, "5", limit=KLINES_CAP + 10)
        for r in rows5:
            t = int(r[0])
            self.klines5[t] = {"t": t, "o": float(r[1]), "h": float(r[2]),
                               "l": float(r[3]), "c": float(r[4])}
        self._trim_klines()

        # deploy at the most recent 5m close (== backtest deploy at open[0]).
        # Guard: a RESUMED engine already holds its restored slices — re-deploying
        # would wipe them back to a flat ladder. anchor/klines are still rebuilt
        # from REST above (more accurate than the snapshot); only deploy is gated.
        deploy_px = float(rows5[-1][4]) if rows5 else None
        if deploy_px and not self._resumed:
            self._deploy(deploy_px)
        print(f"[{self.mode}] {self.symbol} bootstrapped: anchor(EMA{ANCHOR_EMA_SPAN},1h)"
              f"={self.anchor:.5f}, {self.n} slices, alloc=${self.alloc:,.0f}")

    # -- deploy / position --------------------------------------------------
    def _deploy(self, price: float):
        self.slices = []
        for fr in self.fracs:
            qty = fr * self.alloc / price
            self.slices.append({"state": "usd1", "qty": qty, "cash": 0.0,
                                "sell_px": 0.0, "entry": price})
        self.deployed = True

    def _maybe_deploy(self):
        if not self.deployed:
            px = self._price()
            if px:
                self._deploy(px)

    def _price(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > self.bid:
            return (self.bid + self.ask) / 2
        return self.last

    def _trim_klines(self):
        if len(self.klines5) > KLINES_CAP:
            for k in sorted(self.klines5)[:-KLINES_CAP]:
                del self.klines5[k]

    # -- interest (mirrors Bybit USD1: per-UTC-day min of hourly snapshots) --
    def _usd1_qty(self) -> float:
        """Total USD1 (carry-bearing base) QUANTITY held right now — the snapshot base.

        Summed across ALL slices regardless of ``state`` (F2): a slice mid-partial-fill
        holds a real base residual in ``qty`` while ``state`` is still its pre-flip value,
        and that residual keeps earning carry. On a fully-settled slice the term is a
        no-op (a clean ``usdt`` slice has ``qty==0``), so this equals the old
        state-filtered sum on every clean state — a strict superset."""
        return sum(s["qty"] for s in self.slices)

    @property
    def settled_interest(self) -> float:
        """Interest credited from completed UTC days (shared min-snapshot model)."""
        return self.interest.settled

    def accrue(self, now: float):
        """Feed the current USD1 holding to the shared per-UTC-day min-of-hourly-
        snapshots carry model (identical rule to the backtest — see sca.interest).
        Bybit pays on the DAILY MINIMUM of hourly balances, so a slice parked in
        USDT across even one hourly snapshot forfeits that whole day's interest."""
        if not self.deployed:
            return
        self.interest.observe(now, self._usd1_qty())

    def _pending_interest(self) -> float:
        """Upper-bound estimate of the current (incomplete) UTC day's credit."""
        return self.interest.pending()

    # -- fill evaluation (mirrors backtest slice rules EXACTLY) -------------
    def evaluate_fills(self, now: float):
        if not self.deployed or self.anchor is None:
            return
        a = self.anchor
        for i, s in enumerate(self.slices):
            if s["state"] == "usd1":
                # sell rung floats with EMA: R = round(anchor + rung_bp/1e4, 4)
                R = round(a + self.rungs[i] / 1e4, TICK_DP)
                if self.bid is not None and self.bid >= R:
                    qty = s["qty"]
                    s["cash"] = qty * R
                    s["sell_px"] = R
                    s["qty"] = 0.0
                    s["state"] = "usdt"
                    s["entry"] = None
                    self._log_event(now, "sell", i, R, qty)
            else:  # usdt -> rebuy at anchor - 1bp
                B = round(a + REBUY_OFF_BP / 1e4, TICK_DP)
                if self.ask is not None and self.ask <= B:
                    nq = s["cash"] / B
                    self.realized_capture += (s["sell_px"] - B) * nq
                    s["qty"] = nq
                    s["cash"] = 0.0
                    s["state"] = "usd1"
                    s["entry"] = B
                    self._log_event(now, "buy", i, B, nq)

    def _log_event(self, now: float, side: str, i: int, price: float, qty: float):
        event = {"ts": int(now * 1000), "utc": _utc(now), "side": side,
                 "slice": i, "price": _r(price, 6), "qty": _r(qty, 6)}
        self.events.append(event)
        self.events[:] = self.events[-EVENTS_CAP:]
        if not self.persist:
            return
        if self.maker_enabled:
            # MAKER PATH (F10 — single persistence point + fail-CLOSED): the durable
            # snapshot is written by the caller's _persist_durable_or_halt AFTER all
            # slice mutations (fill + flip + clear) complete — so the fill is NOT
            # double-written from both here and the durable-persist path, and the
            # snapshot can never capture a mid-mutation (pre-flip) state. Here we only
            # append the best-effort audit line; on the maker path resume reconciles
            # from EXCHANGE truth (resume_reconcile_orders), never by replaying this
            # ledger, so the snapshot-before-append ordering is not load-bearing.
            try:
                append_event(self.out_dir, self.symbol, event, tag=self.mode)
            except OSError as e:
                print(f"[PERSISTENCE WARN] maker audit append failed: {e}",
                      file=sys.stderr)
            return
        # PAPER PATH (unchanged, fail-OPEN). Order matters for crash-safety: snapshot
        # FIRST, then append the audit line — so the snapshot is always >= the event
        # log ("快照永远 >= 流水"). If we crash in between, the position is captured
        # (snapshot ahead) and only one append-only audit line is missing; never the
        # reverse (which would make resume re-execute an already-done fill against the
        # live market). The snapshot already reflects this fill, since evaluate_fills
        # mutates the slice before calling _log_event.
        #
        # A persistence DISK error (ENOSPC / EACCES) must surface as a CLEAR, visible
        # log — NOT propagate to run()'s outer `except Exception`, which would misread
        # it as a network drop and spin a 2s reconnect loop, hiding a fatal disk fault.
        # The in-memory state above already records the fill; the next snapshot retries
        # the write. (Exit policy on persistent disk failure is a larger design call —
        # left to backlog. The maker path above is fail-CLOSED instead, F10.)
        try:
            save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
            append_event(self.out_dir, self.symbol, event, tag=self.mode)
        except OSError as e:
            print(f"[PERSISTENCE ERROR] fill persist failed: {e}", file=sys.stderr)

    # -- markout gauge (dryrun method) -------------------------------------
    def _push_mid(self, now: float):
        if self.bid is not None and self.ask is not None and self.ask > self.bid:
            self.mids_t.append(now)
            self.mids_v.append((self.bid + self.ask) / 2)
            self.spreads.append((self.ask - self.bid) / ((self.ask + self.bid) / 2) * 1e4)

    def _mid_at(self, target: float):
        i = bisect.bisect_right(self.mids_t, target) - 1
        return self.mids_v[i] if i >= 0 else None

    def _on_trade_markout(self, now: float, side_taker: str):
        if self.bid is None or self.ask is None:
            return
        # taker sell hits bid -> passive BUY fills at bid; taker buy hits ask -> passive SELL at ask
        if side_taker == "Sell":
            self.pending.append([now, "buy", self.bid])
        elif side_taker == "Buy":
            self.pending.append([now, "sell", self.ask])

    def flush_markout(self, now: float):
        if not HORIZONS:
            return
        maxh = max(HORIZONS)
        while self.pending and now - self.pending[0][0] >= maxh:
            t0, side, fp = self.pending.pop(0)
            mo = {}
            for h in HORIZONS:
                mv = self._mid_at(t0 + h)
                mo[h] = None if mv is None else ((mv - fp) if side == "buy" else (fp - mv)) / fp * 1e4
            self.done.append([side, fp, mo])
        cut = now - MID_RETAIN
        c = bisect.bisect_left(self.mids_t, cut)
        if c > 0:
            del self.mids_t[:c]
            del self.mids_v[:c]

    # -- valuation / pnl ----------------------------------------------------
    def _slice_value(self, s: dict, px: float | None) -> float:
        """Per-slice equity = base leg + parked leg, valued INDEPENDENT of ``state``
        (F2). During a partial fill a slice holds both ``qty`` (base) and ``cash``
        (proceeds); valuing only one under-reports. On a clean slice the other term is
        zero (a ``usd1`` slice has ``cash==0``; a ``usdt`` slice has ``qty==0``), so
        this equals the old state-switched value on every settled state."""
        mark = px if px is not None else (s.get("entry") or 0.0)
        return s["qty"] * mark + s.get("cash", 0.0)

    # -- status doc (the CONTRACT) -----------------------------------------
    def status_doc(self, now: float) -> dict:
        px = self._price()
        mid = ((self.bid + self.ask) / 2
               if self.bid is not None and self.ask is not None and self.ask > self.bid
               else None)
        a = self.anchor

        # indicators
        rebuy_price = round(a + REBUY_OFF_BP / 1e4, TICK_DP) if a is not None else None
        sell_rungs = []
        for i, (fr, bp) in enumerate(zip(self.fracs, self.rungs)):
            price = round(a + bp / 1e4, TICK_DP) if a is not None else None
            sell_rungs.append({"i": i, "frac": _r(fr, 6), "bp": _r(bp, 4),
                               "price": _r(price, 6)})

        # position — valuation split by LEG, not by slice-state (C-P1#12). A
        # mid-partial slice contributes its base to base_value AND its proceeds to
        # quote_value simultaneously; keying the buckets on the stale ``state`` would
        # dump the whole slice into one bucket and mis-state both legs. On a clean
        # state the cross term is zero, so base/quote equal the old usd1/usdt values.
        sl_out = []
        base_value = quote_value = 0.0
        n_usd1 = n_usdt = 0
        for i, s in enumerate(self.slices):
            smark = px if px is not None else (s.get("entry") or 0.0)
            base_value += s["qty"] * smark
            quote_value += s.get("cash", 0.0)
            val = self._slice_value(s, px)
            if s["state"] == "usd1":
                n_usd1 += 1
                sell_target = (round(a + self.rungs[i] / 1e4, TICK_DP)
                               if a is not None else None)
                entry = s.get("entry")
            else:
                n_usdt += 1
                sell_target = None
                entry = None
            sl_out.append({
                "i": i, "frac": _r(self.fracs[i], 6), "state": s["state"],
                "qty": _r(s["qty"], 6), "entry_price": _r(entry, 6),
                "sell_target": _r(sell_target, 6), "value_usd": _r(val, 4),
            })
        usd1_value, usdt_value = base_value, quote_value
        total_value = base_value + quote_value
        usd1_pct = (usd1_value / total_value * 100) if total_value > 0 else None

        # pnl decomposition: total = realized + SETTLED interest + unrealized.
        # interest is credited only on COMPLETED UTC days (honest); the current
        # day's running estimate is reported separately as pending_interest.
        # LIVE baseline = the ACTUAL capital deployed (seed-from-balance, bounded by the cap);
        # paper/dryrun never seeds so it stays the config ``alloc`` (full notional simulated).
        start_value = (self._deployed_capital
                       if self._deployed_capital is not None else self.alloc)
        realized = self.realized_capture
        interest = self.settled_interest
        pending = self._pending_interest()
        if self.deployed:
            unrealized = total_value - start_value - realized
            total = total_value + interest - start_value
        else:
            unrealized = 0.0
            total = 0.0
            pending = 0.0
        elapsed = now - self.start
        # estimated annualized return, in PERCENT (e.g. 10.0 == 10%/yr) — consumers
        # (dashboard, console) append '%'. Gated to >=1 full day: shorter windows
        # annualize pure mark-to-market noise, and interest only settles per UTC day.
        apr_est = (total / start_value * SEC_PER_YEAR / elapsed * 100
                   if elapsed >= 86400 and start_value > 0 else None)

        # markout / fill-quality
        agg = aggregate_markout(self.done, self.spreads)

        # klines (oldest-first, cap)
        klines = [self.klines5[k] for k in sorted(self.klines5)][-KLINES_CAP:]

        doc = {
            "symbol": self.symbol,
            "mode": self.mode,
            "updated_utc": _utc(now),
            "elapsed_sec": int(round(elapsed)),
            "price": {"bid": _r(self.bid, 6), "ask": _r(self.ask, 6),
                      "mid": _r(mid, 6), "last": _r(self.last, 6)},
            "anchor": _r(a, 6),
            "indicators": {"anchor": _r(a, 6), "anchor_ema_span": ANCHOR_EMA_SPAN,
                           "rebuy_price": _r(rebuy_price, 6), "sell_rungs": sell_rungs},
            "position": {"slices": sl_out, "usd1_value": _r(usd1_value, 4),
                         "usdt_value": _r(usdt_value, 4), "usd1_pct": _r(usd1_pct, 3),
                         "total_value": _r(total_value, 4), "n_in_usd1": n_usd1,
                         "n_in_usdt": n_usdt},
            "pnl": {"realized_price": _r(realized, 6), "accrued_interest": _r(interest, 6),
                    "pending_interest": _r(pending, 6),
                    "unrealized": _r(unrealized, 6), "total": _r(total, 6),
                    "apr_est": _r(apr_est, 4), "start_value": _r(start_value, 4)},
            "events": list(self.events),
            "klines": klines,
            "markout": agg["markout"],
            "n_buy": agg["n_buy"],
            "n_sell": agg["n_sell"],
            "avg_spread_bp": agg["avg_spread_bp"],
            "history": list(self.history),
        }
        return _sanitize(doc)

    def _append_history(self, now: float):
        px = self._price()
        equity = (sum(self._slice_value(s, px) for s in self.slices) + self.settled_interest
                  if self.deployed else self.alloc)
        rt30 = None
        if 30 in HORIZONS:
            rt30 = aggregate_markout(self.done, self.spreads)["markout"].get("30", {}).get("round_trip")
        self.history.append({"t": int(round(now - self.start)),
                             "equity": _r(equity, 4), "rt30": _r(rt30, 4)})
        self.history[:] = self.history[-HISTORY_CAP:]

    def write_status(self, now: float):
        """Atomic write of status_<symbol>_<mode>.json (tmp + rename).

        Mode-tagged (D17, mirrors D15's state/events segregation) so a dryrun and a live
        run on the same out_dir never overwrite each other's status/history/markout
        snapshot. The dashboard globs ``status_*.json`` and keys off the full stem, so the
        two modes show up as separate cards automatically (no dashboard change)."""
        self._append_history(now)
        doc = self.status_doc(now)
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"status_{self.symbol}_{self.mode}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, allow_nan=False)
        os.replace(tmp, path)
        # Snapshot alongside the status write so that on resume history/events are
        # non-empty and the next write_status never re-truncates to empty.
        # A persistence OSError here must NOT bubble to run()'s reconnect path
        # (see _log_event) — log it clearly and continue; the status file above
        # already landed and the next snapshot retries.
        if self.persist:
            try:
                save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
            except OSError as e:
                print(f"[PERSISTENCE ERROR] status snapshot failed: {e}", file=sys.stderr)
        return path

    def print_summary(self, now: float):
        doc = self.status_doc(now)
        p = doc["pnl"]
        pos = doc["position"]
        mk30 = doc["markout"].get("30", {})
        apr = doc["pnl"]["apr_est"]
        print(f"[{self.mode}] {self.symbol} t={doc['elapsed_sec']}s "
              f"px={_fmt(doc['price']['mid'])} anchor={_fmt(doc['anchor'])} "
              f"| usd1={pos['n_in_usd1']}/{self.n} "
              f"realized={_fmt(p['realized_price'])} int={_fmt(p['accrued_interest'])} "
              f"pend={_fmt(p['pending_interest'])} "
              f"total={_fmt(p['total'])} apr_est={_fmt(apr)}% "
              f"| sells={doc['n_sell']} buys={doc['n_buy']} "
              f"rt30={_fmt(mk30.get('round_trip'))}bp")

    # -- R1 reconciliation gate (armed-live only) ---------------------------
    def _coins(self) -> tuple[str, str]:
        """Split the trading symbol into (base, quote). The universe is *USDT."""
        s = self.symbol
        return (s[:-4], "USDT") if s.endswith("USDT") else (s, "")

    def _local_summary(self) -> dict:
        """Coin-quantity summary of local state for reconcile (apples-to-apples
        with exchange wallet balances; notional alloc is irrelevant).

        Sums BOTH legs across ALL slices regardless of ``state`` (F2) so a mid-partial
        restart reports the real base residual + the real quote proceeds and does not
        false-refuse against an exchange that shows both. Equals the old state-filtered
        sums on clean states (a ``usd1`` slice has ``cash==0``; a ``usdt`` slice has
        ``qty==0``)."""
        base_qty = sum(s["qty"] for s in self.slices)
        quote_qty = sum(s.get("cash", 0.0) for s in self.slices)
        return {"resumed": self._resumed, "deployed": self.deployed,
                "base_qty": base_qty, "quote_qty": quote_qty}

    @staticmethod
    def _liability_reason(bal: dict) -> str | None:
        """Refuse-reason if the UTA is not a clean spot-only account (Codex P1):
        any borrow, negative equity, or equity materially below wallet (margin/UPL)
        means ``walletBalance - locked`` is not spendable truth."""
        for coin, c in bal.get("coins", {}).items():
            if c.get("borrow", 0.0) > 1e-9:
                return f"{coin} borrow={c['borrow']} (margin/borrow active)"
        t = bal.get("totals", {})
        # account-level margin/derivatives exposure must be ~0 for a spot-only UTA
        for k, label in (("im_usd", "initial margin"), ("mm_usd", "maintenance margin"),
                         ("perp_upl_usd", "perp UPL")):
            v = t.get(k, 0.0)
            if abs(v) > 1e-9:
                return f"account {label} non-zero ({v}) — not a spot-only account"
        eq, wal = t.get("equity_usd", 0.0), t.get("wallet_usd", 0.0)
        if eq < 0:
            return f"account equity negative ({eq})"
        if wal > 0 and eq < wal * 0.99:
            return f"equity {eq} materially below wallet {wal} (margin/UPL present)"
        at = bal.get("account_type")
        if at not in (None, "UNIFIED"):
            return f"unexpected account type {at!r} (expected UNIFIED)"
        return None

    def _refuse(self, msg: str):
        """Loud refusal to start (Codex S1 / review S1) — never a silent downgrade.

        D16: exits CLEANLY (code 0). A refusal (R1 reconcile mismatch / liability guard /
        foreign order / fresh-deploy block) is an INTENTIONAL stop, not a crash — exiting 0
        means docker ``restart: on-failure`` does NOT loop on a deliberate refusal; only a
        genuine uncaught exception exits non-zero and is restarted (transient recovery)."""
        print(f"[live] REFUSED to start: {msg}", file=sys.stderr)
        raise SystemExit(0)

    def _reconcile_or_refuse(self, client=None):
        """Reconcile local state against real exchange truth before trading. Raises
        SystemExit on any refusal. ``client`` is injectable for tests."""
        # precondition 1: persistence must be on (else every restart looks fresh)
        if not self.persist:
            self._refuse("armed live requires live.persist=true (R1 needs durable state)")
        # I/O (only here): real balance + account-wide open orders. The read-client and the
        # maker order client both build for MAINNET (live == real money, D14) — same venue,
        # no split-brain.
        if client is None:
            from sca.live.bybit_client import BybitPrivateClient
            client = BybitPrivateClient(testnet=False)
        bal = client.get_wallet_balance()
        open_orders = client.get_open_orders(None)   # account-wide (Codex P2)
        # precondition 2: liability/margin guard
        reason = self._liability_reason(bal)
        if reason:
            self._refuse(f"UTA liability/margin guard: {reason}")
        # ARMED-MAKER SEED — INSIDE the gate, BEFORE reconcile() decides (A6a / C-P0#5):
        # an armed-maker start with no local state seeds slices from the reconciled balance
        # so the SEEDED summary is what reconcile() compares (a USDT-funded account then
        # PROCEEDS instead of being refused as a fresh deploy). maker_enabled == (mode ==
        # live) (D14), so this IS the mainnet/live path: a first live start with no local
        # state builds the initial position from the already-funded dedicated subaccount and
        # reconcile then takes 'proceed'. A mixed / ambiguous balance or any pre-existing
        # open order is lost state -> REFUSE (C-P1#15).
        if self.maker_enabled and not self.slices:
            self._seed_slices_from_balance(bal, open_orders)
        # decision
        base_coin, quote_coin = self._coins()
        dedicated = bool(_LIVE.get("dedicated_account", True))
        tol = float(_LIVE.get("reconcile_tol", 1.0))
        # P1-3: a maker strategy leaves resting orders BY DESIGN. Feed reconcile() the set
        # of OUR persisted link_ids so our own resting sca-* orders are NOT flagged as
        # anomalies on restart (an unwired `expected` refuses valid resting orders before
        # resume_reconcile_orders ever runs). Non-maker (taker) keeps expected=None, so the
        # refuse-on-any-order behavior is preserved exactly. An order whose link is not in
        # this set (foreign/stale) still refuses.
        expected = None
        if self.maker_enabled:
            expected = {s["order_link_id"] for s in self.slices if s.get("order_link_id")}
        rep = reconcile(self._local_summary(), bal, open_orders,
                        base_coin=base_coin, quote_coin=quote_coin,
                        tol=tol, dedicated=dedicated, allow_fresh=self.allow_fresh,
                        expect_asset=self.expect_asset, expect_amount=self.expect_amount,
                        expected=expected)
        if rep["action"] == "refuse":
            self._refuse("R1 reconciliation refused: " + "; ".join(rep["discrepancies"]))
        # FRESH-DEPLOY GUARD (D14/D15): reconcile may APPROVE a fresh deploy, but we never
        # blindly build a config-`alloc`-sized position. The maker order path EXISTS (3b), so
        # this is NOT a "not built yet" stop — it is a deliberate safety stance: a config-sized
        # position would not match the real exchange balance and would hollow out the R1
        # reconciliation guard. The initial position MUST instead come from seed-from-balance
        # (fund the dedicated subaccount with a clean SINGLE coin, which makes reconcile take
        # the 'proceed' path). Hitting fresh_deploy means the balance is empty, mixed, or
        # ambiguous. (Resuming a reconciled position via action=="proceed" stays allowed.)
        if rep["action"] == "fresh_deploy":
            self._refuse(
                "fresh live deploy was approved by reconcile, but we never blindly build a "
                "config-`alloc`-sized position: it would not match the real exchange balance "
                "and would hollow out the R1 reconciliation guard. The initial position MUST "
                "come from seed-from-balance — fund the dedicated subaccount with a clean "
                "SINGLE coin so reconcile takes the 'proceed' path. Hitting this refusal means "
                "the balance is empty, mixed, or ambiguous.")
        print(f"[live] R1 reconciliation OK -> {rep['action']} "
              f"(exchange {base_coin}={rep['exchange'].get(base_coin, {}).get('wallet')}, "
              f"{quote_coin}={rep['exchange'].get(quote_coin, {}).get('wallet')})")
        return rep

    def _maybe_gate(self):
        """Run the R1 gate when armed-live; no-op for paper (never builds a client).

        On success it records the gate's decision + the account-wide open-order list it
        fetched (``_r1_report`` / ``_r1_open_orders``) so ``resume_reconcile_orders`` can
        reuse the SAME list (no refetch, C-P0#5) and sets ``_r1_ok`` — the defence-in-depth
        flag that ``reconcile_orders``/``poll_fills`` assert at entry (F22)."""
        if self.armed:
            rep = self._reconcile_or_refuse()
            self._r1_report = rep
            self._r1_open_orders = rep.get("open_orders", [])
            self._r1_ok = True

    @staticmethod
    def _wallet_coin(bal: dict, coin: str) -> float:
        c = (bal.get("coins") or {}).get(coin, {})
        v = c.get("wallet")
        return float(v) if v is not None else 0.0

    @staticmethod
    def _coin_usd(bal: dict, coin: str) -> float:
        """USD value of the wallet holding (Bybit balance carries a per-coin ``usd``)."""
        c = (bal.get("coins") or {}).get(coin, {})
        v = c.get("usd")
        return float(v) if v is not None else 0.0

    def _deployable_amt(self, amt: float, usd_value: float) -> float:
        """Cap a fundable coin amount by the total-alloc USD budget (3b canary, 3b-2).

        ``max_total_alloc_usd < 0`` (e.g. -1) => NO cap: deploy the full wallet (the
        boss's "用钱包里所有的钱"). Otherwise deploy at most ``cap`` USD worth, valued at
        the coin's own wallet mark (``usd_value/amt``) so the cap is enforced in USD even
        if the stablecoin is slightly off $1. This is the arb-execution-risk guard: a
        configured cap that the live SIZING path fails to apply is a real defect, so it is
        enforced HERE (the single seed entry point), not merely stored in config."""
        cap = self._max_total_alloc_usd
        if cap < 0:
            return amt
        if amt <= 0:
            return 0.0
        mark = (usd_value / amt) if usd_value > 0 else 1.0   # $1 stablecoin fallback
        max_amt = (cap / mark) if mark > 0 else amt
        return min(amt, max_amt)

    def _seed_slices_from_balance(self, bal: dict, open_orders):
        """Seed local slices from a clean single-side exchange balance so an armed-maker
        testnet start can actually exercise the lifecycle (A6a / F3). USDT holdings ->
        ``usdt``-state slices (cash, wanting BUYs); USD1 holdings -> ``usd1``-state slices
        (qty, wanting SELLs); each split by the configured fractions. Marks the engine
        resumed so reconcile() takes the PROCEED path against the same balance.

        Safety (C-P1#15): seeds ONLY when there are NO open orders AND the balance is a
        clean single side (the other side <= dust ``tol``). A mixed balance or any
        pre-existing open order is ambiguous lost state -> REFUSE (never silently
        legitimize it, even on testnet)."""
        base_coin, quote_coin = self._coins()
        tol = float(_LIVE.get("reconcile_tol", 1.0))
        if open_orders:
            self._refuse("armed-maker seed: pre-existing open order(s) on the account — "
                         "ambiguous lost state; refusing to seed (seed only a clean, "
                         "order-free single-side balance)")
        base_amt = self._wallet_coin(bal, base_coin)
        quote_amt = self._wallet_coin(bal, quote_coin)
        base_material = base_amt > tol
        quote_material = quote_amt > tol
        if base_material and quote_material:
            self._refuse(f"armed-maker seed: mixed balance ({base_coin}={base_amt}, "
                         f"{quote_coin}={quote_amt}) — ambiguous lost state; refusing "
                         "(cannot infer the intended slice split)")
        if not base_material and not quote_material:
            self._refuse("armed-maker seed: no material balance to seed (account "
                         f"effectively empty: {base_coin}={base_amt}, {quote_coin}={quote_amt})")
        self.slices = []
        if quote_material:                          # USDT-funded -> usdt slices wanting BUYs
            deployable = self._deployable_amt(quote_amt, self._coin_usd(bal, quote_coin))
            # PnL baseline == the quote we deployed (== Σ cash). The quote leg enters
            # total_value at FACE, so the baseline is the face amount, NOT the USD cap
            # (they differ when the quote coin is off its $1 peg).
            self._deployed_capital = deployable
            for fr in self.fracs:
                s = {"state": "usdt", "qty": 0.0, "cash": fr * deployable,
                     "sell_px": 0.0, "entry": None}
                s.update(dict(_ORDER_FIELD_DEFAULTS))
                self.slices.append(s)
        else:                                       # USD1-funded -> usd1 slices wanting SELLs
            base_usd = self._coin_usd(bal, base_coin)
            deployable = self._deployable_amt(base_amt, base_usd)
            # PnL baseline == the USD the seeded USD1 represents (== deployable * seed mark).
            # The base leg enters total_value at px, so value it at the seed mark for an
            # honest mark-to-market (mark == coin usd / coin amount; $1 fallback if unknown).
            mark = (base_usd / base_amt) if base_amt > 0 else 1.0
            self._deployed_capital = deployable * mark
            for fr in self.fracs:
                s = {"state": "usd1", "qty": fr * deployable, "cash": 0.0,
                     "sell_px": 0.0, "entry": None}
                s.update(dict(_ORDER_FIELD_DEFAULTS))
                self.slices.append(s)
        self.deployed = True
        self._resumed = True

    # ======================================================================
    # MAKER FILL DRIVER (Phase 3a) — real (incl. partial) fills drive slice
    # transitions; declarative order reconciliation; REST poll. Reachable only
    # when maker_enabled (== live mode, D14); the dryrun (simulated-fill) path
    # never enters here, so the existing simulated-fill tests are untouched.
    # ======================================================================
    def _ensure_order_fields(self):
        """Idempotently inject the order/accounting fields onto every slice so the
        maker path can rely on them (seeded slices / migrated state already carry
        them; this is the belt-and-braces guard)."""
        for s in self.slices:
            for k, v in _ORDER_FIELD_DEFAULTS.items():
                s.setdefault(k, v)

    def _clear_slice_order(self, i: int):
        """Drop the live-order identity for a slice (the order is terminal). Keeps the
        monotonic ``order_gen`` and the cycle-level ``reject_streak`` / ``sell_proceeds``
        / ``qty_sold`` (those reset on a completed BUY via ``_flip_state``)."""
        s = self.slices[i]
        s["order_id"] = None
        s["order_link_id"] = None
        s["order_px"] = None
        s["order_side"] = None
        s["order_qty"] = None
        s["filled_qty"] = 0.0

    # -- book a real exec onto a slice (mirrors paper accounting) -----------
    def _apply_exec(self, i: int, side: str, dq: float, px: float, now: float):
        """Book an executed quantity ``dq`` at price ``px`` onto slice ``i``. SELL adds
        proceeds + accumulates the persistent blended-avg basis; BUY books realized
        capture (over the blended avg sell price) BEFORE reducing cash. ``min(calc,
        available)`` everywhere so a fill can never overshoot/flip a leg negative."""
        s = self.slices[i]
        if side == "sell":
            dq = min(dq, s["qty"])              # never sell more than held
            s["qty"] -= dq
            s["cash"] += dq * px
            s["sell_proceeds"] += dq * px       # persistent basis for blended avg_sell
            s["qty_sold"] += dq
            s["sell_px"] = px                   # last-sell, DISPLAY only (not realized)
        else:                                   # buy / rebuy
            if s["qty_sold"] > 0:               # blended avg over ALL partial sells (F6)
                avg_sell = s["sell_proceeds"] / s["qty_sold"]
                self.realized_capture += (avg_sell - px) * dq   # booked BEFORE cash drops
            s["qty"] += dq
            s["cash"] = max(0.0, s["cash"] - dq * px)
            s["entry"] = px                     # last buy price -> entry (parity with B)
        self._log_event(now, side, i, px, dq)   # single persistence point per fill

    def _flip_state(self, i: int):
        """Transition a slice on a FULL fill, resetting the SAME fields the paper
        ``evaluate_fills`` resets so a maker full-cycle yields identical accounting
        (F18). A completed SELL -> ``usdt`` (qty->0, entry=None, KEEP proceeds basis for
        the upcoming rebuy); a completed BUY/rebuy -> ``usd1`` (cash->0, entry=buy px,
        RESET the proceeds basis — the cycle is closed)."""
        s = self.slices[i]
        if s["state"] == "usd1":                # SELL completed -> USDT
            s["state"] = "usdt"
            s["qty"] = 0.0
            s["entry"] = None
        else:                                   # BUY/rebuy completed -> USD1
            s["state"] = "usd1"
            s["cash"] = 0.0
            s["entry"] = s.get("order_px")      # the rebuy price we placed at (== B)
            s["sell_proceeds"] = 0.0
            s["qty_sold"] = 0.0

    def _apply_exec_delta(self, i: int, st: dict, now: float) -> bool:
        """Shared book-a-fill + terminal-resolve helper — the SINGLE decision point so
        every caller (poll_fills, the vanished-sync, the cancel-rebook, resume) resolves an
        order's lifecycle identically. Books the exec delta since the last observation, then:

          * genuine FULL fill (``leaves <= EPS_LOT``) -> FLIP state, then CLEAR the order
            identity (the cycle's leg completed);
          * any OTHER ``TERMINAL_ORDER_CLASSES`` outcome — a `cancelled` with a partial OR
            zero fill, a `rejected`, a `postonly_rejected` — -> CLEAR the order identity so
            the slice is free to be re-quoted next reconcile (NO permanent ghost), but do
            NOT flip state (the already-delta-updated partial position is kept);
          * a NON-terminal `open` / `not_found` (PendingCancel classifies as `open`) is left
            INTACT — never cleared on an unknown/live outcome (preserve R2-P0/P0-1); the
            resting leaves can still fill.

        Returns True if it booked any delta OR flipped state (used to abort a now-stale
        paired place); a clear-only terminal that booked nothing returns False so the paired
        place still proceeds (an empty cancelled order is simply re-quoted). Guards None AND
        non-finite on BOTH ``filled`` and the order total before subtracting (F20); also
        refuses to book a fill without a finite price."""
        s = self.slices[i]
        filled, total = st.get("filled"), s.get("order_qty")
        if (filled is None or total is None
                or not (math.isfinite(filled) and math.isfinite(total))):
            return False                        # skip; re-poll next tick
        exec_delta = filled - s.get("filled_qty", 0.0)
        changed = False
        if exec_delta > 0:
            avg = st.get("avg")
            if avg is None or not math.isfinite(avg):
                return False                    # a fill with no price -> re-poll
            self._apply_exec(i, st["side"], exec_delta, avg, now)
            s["filled_qty"] = filled
            changed = True
        if total - filled <= EPS_LOT:           # genuine FULL fill -> flip THEN clear
            self._flip_state(i)                 # (reads order_px) MUST precede the clear
            self._clear_slice_order(i)
            return True
        if st.get("status_class") in TERMINAL_ORDER_CLASSES:
            # Terminal but NOT a full fill (cancelled partial/zero | rejected |
            # postonly_rejected): the delta (if any) is booked above; CLEAR the order
            # identity so the slice is re-quotable — no permanent ghost. Do NOT flip state
            # (keep the partial position). `open`/`not_found` are non-terminal and fall
            # through here, leaving the order intact (never clear on an unknown outcome).
            print(f"[live] slice {i} order terminal={st.get('status_class')} non-full "
                  f"(filled {filled}/{total}) -> cleared order, re-quote next reconcile",
                  file=sys.stderr)
            self._clear_slice_order(i)
        return changed

    # -- cancel + poll-to-terminal (never clear before terminal truth) ------
    def _cancel_to_terminal(self, order_id, link_id, now, slice_idx=None, client=None):
        """Cancel an order, then POLL ``fetch_order_state`` until ``status_class`` is
        TERMINAL (filled|cancelled|rejected|postonly_rejected). Bybit can return
        PendingCancel / still-'open' right after a cancel and the resting leaves CAN
        still fill, so we MUST NOT clear local state while ``status_class=='open'``.
        Bounded backoff; on exhaustion -> halt (fail-closed, never clear on an unknown
        outcome). When ``slice_idx`` is given, books the FINAL cumExecQty delta to that
        slice (flips/clears) ONLY after terminal is confirmed. ``client`` is threaded
        from the caller so the SAME injected client is used (test seam, R3-P1)."""
        client = client or self.order_client
        client.cancel(self.symbol, order_id, link_id=link_id)
        st = None
        for backoff in CANCEL_POLL_BACKOFFS:
            st = client.fetch_order_state(self.symbol, order_id, link_id=link_id)
            if st["status_class"] in TERMINAL_ORDER_CLASSES:   # terminal -> stop polling
                break
            self._sleep(backoff)                # open/PendingCancel/not_found -> keep polling
        else:
            self._halt_operator_reconcile(
                f"cancel never reached terminal: {link_id or order_id}")
            return {**(st or {}), "changed": False}   # pragma: no cover - halt raises
        changed = False
        if slice_idx is not None:               # book ONLY after terminal confirmed
            changed = self._apply_exec_delta(slice_idx, st, now)
            self._clear_slice_order(slice_idx)
            self._persist_durable_or_halt()
        return {**st, "changed": changed}

    # -- PostOnly-reject cooldown / operator halt (anti-livelock, F9) -------
    def _note_reject(self, i: int):
        """Record a consecutive PostOnly reject for slice ``i``; arm its cooldown
        (keyed off the current anchor) and trip the operator halt at the threshold."""
        s = self.slices[i]
        s["reject_streak"] = s.get("reject_streak", 0) + 1
        self._reject_anchor[i] = self.anchor
        if s["reject_streak"] >= self._reject_halt_threshold:
            self._halt_operator_reconcile(
                f"slice {i} PostOnly reject streak {s['reject_streak']} "
                f">= {self._reject_halt_threshold}")

    def _reset_reject(self, i: int):
        """Clear a slice's reject streak + cooldown after a successful place."""
        self.slices[i]["reject_streak"] = 0
        self._reject_anchor.pop(i, None)

    def _in_cooldown(self, i: int, desired) -> bool:
        """True while slice ``i`` is in PostOnly-reject cooldown. The cooldown lifts
        when the anchor changes (a new 1h close) OR top-of-book has moved enough that
        the rung would now rest (a SELL above the bid / a BUY below the ask)."""
        if i not in self._reject_anchor:
            return False
        if self.anchor != self._reject_anchor[i]:   # anchor moved -> re-attempt
            self._reject_anchor.pop(i, None)
            return False
        if desired is not None:
            if (desired.side == "sell" and self.bid is not None
                    and self.bid < desired.price):
                self._reject_anchor.pop(i, None)
                return False
            if (desired.side == "buy" and self.ask is not None
                    and self.ask > desired.price):
                self._reject_anchor.pop(i, None)
                return False
        return True

    def _halt_operator_reconcile(self, reason: str):
        """Fail-closed halt requiring human reconciliation. Raises so the maker loop
        unwinds; the run-loop kill-switch (A10) cancels resting orders on the way out.

        D16: persist the halt BEFORE raising so it is DURABLE even if the process exits
        immediately (docker auto-restart). Best-effort — gated by ``self.persist`` and
        swallowing OSError — because the fail-closed RAISE must never be blocked by a disk
        error (e.g. this can run from the dead-disk cancel-all sweep). On restart
        ``_enforce_resume_halt_gate`` reads this and refuses to continue."""
        self._halted = True
        self._halt_reason = reason
        if self.persist:
            try:
                save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
            except OSError as e:
                print(f"[live] WARN: could not persist halt flag ({e}); "
                      "halt still raised (fail-closed)", file=sys.stderr)
        print(f"[live] HALT — operator reconcile required: {reason}", file=sys.stderr)
        raise OperatorReconcileHalt(reason)

    def _persist_durable_or_halt(self):
        """Fail-CLOSED durable snapshot of the order<->slice map (maker path). Retries
        the atomic write with bounded backoff; on exhaustion it CANCELS ALL resting
        orders and HALTS rather than continue with an in-memory-only fill (A9 / F10).

        Re-entrancy: the cancel-all sweep routes through ``_cancel_to_terminal`` which
        calls this again; while ``_persist_failing`` is set those nested calls are
        best-effort (one attempt, swallow) so a dead disk cannot recurse into an
        endless cancel-all/halt cascade."""
        if not self.persist:
            return
        if self._persist_failing:                # nested call during the cancel-all sweep
            try:
                save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
            except OSError:
                pass                             # best-effort: disk is already known-bad
            return
        last: OSError | None = None
        for backoff in PERSIST_RETRY_BACKOFFS:
            try:
                save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
                return
            except OSError as e:
                last = e
                self._sleep(backoff)
        # exhausted -> fail closed: cancel every resting order, then halt
        self._persist_failing = True
        try:
            self._cancel_all_resting()
        finally:
            self._persist_failing = False
        self._halt_operator_reconcile(f"durable persist failed after retries: {last}")

    def _cancel_all_resting(self, client=None):
        """Cancel EVERY persisted resting order, routing each through
        ``_cancel_to_terminal`` (poll-to-terminal + book any shutdown-window fill
        before clearing — never a blind cancel-and-clear, R3-P1). Used by the
        fail-closed persist primitive and (Task 6) the kill switch / cancel-all-on-exit.
        Skips slices with no live order. ``client`` is threaded for the test seam."""
        client = client or self.order_client
        self._ensure_order_fields()
        now = time.time()
        for i, s in enumerate(self.slices):
            if not s.get("order_id") and not s.get("order_link_id"):
                continue
            self._cancel_to_terminal(s.get("order_id"), s.get("order_link_id"),
                                     now, slice_idx=i, client=client)

    # -- available-balance pool: free + our own resting leaves (C-P1#13) ----
    @staticmethod
    def _free_coin(bal: dict, coin: str) -> float:
        c = (bal.get("coins") or {}).get(coin, {})
        v = c.get("free")
        return float(v) if v is not None else 0.0

    def _available_from_balance(self, bal: dict, live: dict):
        """Sizing pool = wallet free + the leaves locked in OUR OWN valid resting orders
        (re-deployable via cancel+re-price). NOT raw free (would needlessly shrink/cancel
        orders we intend to keep, killing queue position) and NOT raw wallet (includes
        foreign holds -> overcommit -> InsufficientFunds)."""
        base_coin, quote_coin = self._coins()
        own_locked_base = sum(l.qty for l in live.values() if l.side == "sell")
        own_locked_quote = sum(l.qty * l.price for l in live.values() if l.side == "buy")
        avail_base = self._free_coin(bal, base_coin) + own_locked_base
        avail_quote = self._free_coin(bal, quote_coin) + own_locked_quote
        # 3b total-alloc cap (3b-2): bound each side's sizing pool by the canary budget so
        # a re-quote NEVER sizes from the whole wallet (an over-funded account has free >>
        # cap). cap<0 => no cap (3a behaviour). USD≈coin for the $1 stablecoin universe;
        # this is a conservative UPPER bound (desired_orders is ALSO bounded by each
        # slice's real holdings, so it only ever further-restricts, never over-permits).
        # NOTE (P2 — intentional, do NOT "fix"): this caps EACH side INDEPENDENTLY at `cap`
        # (a conservative PER-SIDE cap), it is NOT a true shared "remaining budget" split
        # across both legs. In practice that is safe because the strategy seeds a SINGLE
        # side (clean single-side balance, see _seed_slices_from_balance), so the active
        # side's pool ≈ the whole canary budget and the dormant side holds ~0 anyway. A real
        # remaining-budget accountant would add state + a new failure surface for no gain at
        # canary scale — deliberately deferred.
        cap = self._max_total_alloc_usd
        if cap >= 0:
            avail_base = min(avail_base, cap)
            avail_quote = min(avail_quote, cap)
        return avail_base, avail_quote

    # -- declarative order reconciliation (place/cancel/amend/leave) --------
    def reconcile_orders(self, now: float, client=None):
        """Bring resting orders to the desired set: cancel unattributable orders to
        terminal (halt on any executed qty we can't book), then place/cancel/amend/leave
        per the pure diff. Cancels run before places (a BUY locks cash), the cancel polls
        to terminal and books any residual FIRST, and a cancel that books a delta / flips
        state aborts the paired (now stale) place this tick (C-P0#3/#4)."""
        assert self._r1_ok, "reconcile_orders before R1 gate"
        if not self.maker_enabled:
            return
        if self.anchor is None:                 # no anchor -> no desired set (F19)
            return
        client = client or self.order_client
        self._ensure_order_fields()
        meta = client.market_meta(self.symbol)
        matched, unattributed = match_live_orders(self.slices, client.fetch_open(self.symbol))
        # Ambiguous / unknown live orders are NEVER mapped to a guessed slice (R2-P1):
        # cancel each to terminal with NO slice (books nothing); any executed qty on an
        # unattributable order cannot be safely booked -> HALT for operator reconcile.
        for u in unattributed:
            st = self._cancel_to_terminal(u.order_id, u.link_id, now, client=client)
            if st.get("filled") and st["filled"] > 0:
                self._halt_operator_reconcile(
                    f"fill on unattributable order {u.link_id or u.order_id}")
        # Defence-in-depth (R3-P0): a slice whose persisted order has VANISHED from the
        # open set (it filled/cancelled in the sub-tick window since poll_fills, or was
        # never polled) must be terminal-synced BEFORE the desired set is computed —
        # otherwise the diff sees "no live order" for it and PLACEs a new one, overwriting
        # `order_link_id` and double-placing while the prior order's final cumExecQty is
        # still unbooked. We book the fill (flip/clear on a full fill) and SKIP placing for
        # that slice this tick (re-derive next tick); a not_found (eventual consistency:
        # absent from open AND terminal history) is left intact and skipped — never placed
        # over an unknown outcome. (No double-book: _apply_exec_delta books only the DELTA
        # since the last observation, so a fill already booked by poll_fills is a no-op.)
        aborted: set[int] = set()
        for i, s in enumerate(self.slices):
            if i in matched:
                continue                         # still open + attributed -> the diff handles it
            if not s.get("order_id") and not s.get("order_link_id"):
                continue                         # no persisted order -> nothing vanished
            st = client.fetch_order_state(self.symbol, s.get("order_id"),
                                          link_id=s.get("order_link_id"))
            if st.get("status_class") == "not_found":
                aborted.add(i)                   # unknown outcome -> wait, never place this tick
                continue
            self._apply_exec_delta(i, st, now)   # book fill; flip + clear on a full fill
            self._persist_durable_or_halt()
            aborted.add(i)                       # synced this tick -> re-derive desired next tick
        avail_base, avail_quote = self._available_from_balance(client.balance(), matched)
        desired = desired_orders(self.anchor, self.slices, self.rungs, REBUY_OFF_BP,
                                 meta["tick"], meta["lot"], avail_base, avail_quote,
                                 meta["min_qty"], meta["min_cost"])
        for a in diff_orders(desired, matched, meta["tick"], meta["lot"] / 2):
            if a.kind == "leave":
                continue
            if a.kind == "place" and a.slice_idx in aborted:
                continue                         # paired place is stale after a state flip
            if self._in_cooldown(a.slice_idx, desired.get(a.slice_idx)):
                continue                         # rejected rung -> skip until anchor moves
            if a.kind == "cancel":
                st = self._cancel_to_terminal(a.live.order_id, a.live.link_id, now,
                                              slice_idx=a.slice_idx, client=client)
                if st.get("changed"):            # booked a delta / flipped -> drop the place
                    aborted.add(a.slice_idx)
            elif a.kind == "amend":              # qty-only, unfilled order (diff guaranteed)
                client.amend(self.symbol, a.live.order_id, link_id=a.live.link_id,
                             qty=a.desired.qty)
                self.slices[a.slice_idx]["order_qty"] = a.desired.qty
                self._persist_durable_or_halt()
            else:                                # "place" — the only remaining kind
                self._place(a, client)           #   ("leave" continued; diff emits no others)

    def _place(self, action, client):
        """Place one resting PostOnly order, persisting the link/gen INTENT before the
        network call (a crash never orphans a live order) and classifying the result."""
        i = action.slice_idx
        s = self.slices[i]
        s["order_gen"] += 1
        link = f"sca-{i}-{s['order_gen']}"
        s["order_link_id"] = link
        s["order_side"] = action.desired.side
        s["order_px"] = action.desired.price
        s["order_qty"] = action.desired.qty
        self._persist_durable_or_halt()         # persist INTENT before the call
        r = client.place_postonly(self.symbol, action.desired.side,
                                  action.desired.price, action.desired.qty, link)
        sc = r.get("status_class")
        if sc == "postonly_rejected":
            self._note_reject(i)                 # cooldown (F9)
            self._clear_slice_order(i)
        elif sc == "too_small":
            self._clear_slice_order(i)           # logged-skip, never hot-retried (F19)
        elif sc == "insufficient_funds":
            # P1-7: the order was NOT placed. Treating this as success would leave a ghost
            # order_link_id/order_qty with no order_id. Clear the intent + skip (re-sized
            # next tick); NOT a postonly reject, so the reject streak is untouched.
            print(f"[live] place slice {i}: insufficient_funds -> skip (no order placed)",
                  file=sys.stderr)
            self._clear_slice_order(i)
        else:
            self._reset_reject(i)
            s["order_id"] = r.get("id")
        self._persist_durable_or_halt()

    # -- REST poll: real fills drive transitions (replaces evaluate_fills) --
    def poll_fills(self, now: float, client=None):
        """One cheap state poll per slice with a live order; books the exec delta and
        flips state via the shared ``_apply_exec_delta``. Polls when EITHER ``order_id``
        or ``order_link_id`` is set; skips ONLY when BOTH are absent (crash-after-place
        recovery, C-P1#11)."""
        assert self._r1_ok, "poll_fills before R1 gate"
        if not self.maker_enabled:
            return
        if self.anchor is None:                 # F19
            return
        client = client or self.order_client
        self._ensure_order_fields()
        for i, s in enumerate(self.slices):
            if not s.get("order_id") and not s.get("order_link_id"):
                continue
            st = client.fetch_order_state(self.symbol, s.get("order_id"),
                                          link_id=s.get("order_link_id"))
            if st["status_class"] == "postonly_rejected":
                self._note_reject(i)
                self._clear_slice_order(i)
                self._persist_durable_or_halt()
                continue
            self._apply_exec_delta(i, st, now)
            self._persist_durable_or_halt()

    # -- crash-resume order reconciliation (gate-fetched list, NO refetch) --
    def resume_reconcile_orders(self, open_orders, client=None, now=None):
        """Reconcile persisted slice<->order state against EXCHANGE truth on restart,
        using the open-orders list the R1 gate already fetched (F23/C-P0#5 — never
        refetches). ``reconcile.py`` already DECIDED proceed/refuse; this performs only
        the side-effects, in this order:

          1. RE-LINK every slice matched (by link_id -> id -> unambiguous approx) to a
             still-open order — recovering an ``order_id`` that was never persisted
             (crash after place-ack, F14) so we never place a duplicate.
          2. Each UNATTRIBUTED open order: an ``sca-*`` orphan (its idx/gen no longer
             maps to a slice) is cancel-to-terminal'd + logged (any executed qty we
             cannot attribute -> HALT, R2-P1); a FOREIGN (non-``sca``) order in the
             dedicated account is a hard REFUSAL (the subaccount must be dedicated).
          3. LOST-FILL recovery: for EVERY slice that still carries an ``order_link_id``
             — regardless of whether ``order_id`` was persisted — fetch the order state
             BY LINK and book it. A fill that completed while the engine was down (and a
             terminal order absent from the open list) is recovered; a still-open order
             confirms its absence is NOT assumed from the (non-authoritative) open list."""
        if not self.maker_enabled:
            return
        client = client or self.order_client
        now = time.time() if now is None else now
        self._ensure_order_fields()
        matched, unattributed = match_live_orders(self.slices, open_orders)
        # 1. re-link matched slices to exchange truth (recover order_id)
        for i, live in matched.items():
            self.slices[i]["order_id"] = live.order_id
        # 2. resolve unattributed: orphan sca-* -> cancel(+halt on fill); foreign -> refuse
        for u in unattributed:
            link = u.link_id or ""
            if not str(link).startswith("sca-"):
                self._refuse(
                    f"foreign open order {u.link_id or u.order_id} in the (dedicated) "
                    "account — refusing (the subaccount must hold only sca-* orders)")
            st = self._cancel_to_terminal(u.order_id, u.link_id, now, client=client)
            if st.get("filled") and st["filled"] > 0:
                self._halt_operator_reconcile(
                    f"fill on orphan order {u.link_id or u.order_id} (cannot attribute)")
            print(f"[live] resume: cancelled orphan order {u.link_id or u.order_id}")
        # 3. lost-fill + uncertain-retry recovery (BY LINK) before resuming the loop
        for i, s in enumerate(self.slices):
            if not s.get("order_link_id"):
                continue
            st = client.fetch_order_state(self.symbol, s.get("order_id"),
                                          link_id=s["order_link_id"])
            if st["status_class"] == "postonly_rejected":
                self._note_reject(i)
                self._clear_slice_order(i)
                continue
            self._apply_exec_delta(i, st, now)
        self._persist_durable_or_halt()

    # -- run-loop wiring: maker switch, order-client, startup banner, step --
    def _compute_maker_enabled(self) -> bool:
        """The maker (real-order) path switch == live mode (D14): ``self.armed`` is True
        iff ``mode == "live"`` (see ``live_authorization``). Off (dryrun) => the engine runs
        the simulated ``evaluate_fills`` path with zero behaviour change. There is no
        separate venue gate or rollback knob — live is unconditionally MAINNET."""
        return bool(self.armed)

    def _build_order_client(self):
        """Lazily build the MAKER order client on the live (maker) path only. It builds
        unconditionally for MAINNET (live == real money, D14); a missing API key raises a
        clear RuntimeError from the client constructor (keys are NOT pre-checked here)."""
        if self.maker_enabled and self.order_client is None:
            from sca.live.orders import MakerOrderClient
            self.order_client = MakerOrderClient()

    def _maker_startup_banner(self):
        """Loud real-money LIVE startup banner (live == MAINNET, D14). Informational only
        (no side effects). Surfaces the single deployment cap in force
        (``max_total_alloc_usd``; -1 = whole wallet) — on a spot account the capital
        deployed IS the loss ceiling, which is why it is the only fund limit."""
        print("[WARN] *** REAL-MONEY MAINNET *** LIVE mode: real PostOnly GTC maker orders "
              "WILL be placed with REAL FUNDS. "
              f"Deployment cap: max_total_alloc_usd={self._max_total_alloc_usd} "
              "(-1 = the WHOLE wallet). Any exit cancels all resting orders.")

    def maker_step(self, now: float):
        """One throttled maker cycle: ``poll_fills`` FIRST (terminal-sync every slice whose
        order left the open book — book fills, flip state, clear completed orders), THEN
        ``reconcile_orders`` (R3-P0 — poll BEFORE reconcile so a completed order is never
        overwritten unbooked). ``accrue(now)`` has already run in ``_tick`` so the
        top-of-hour carry snapshot precedes any fill mutation (F4). Self-guards on
        ``maker_enabled`` so a dryrun engine call is a safe no-op (never asserts ``_r1_ok``);
        a prior OPERATOR-RECONCILE halt is terminal — refuse all further maker activity (no
        placement) until restart + human reset."""
        if not self.maker_enabled:
            return
        if self._halted:                            # a prior operator halt is terminal
            raise OperatorReconcileHalt(
                "engine halted — refusing further maker activity (restart + human reset)")
        self.poll_fills(now)
        self.reconcile_orders(now)

    def _on_exit_signal(self, signum, frame):
        """SIGINT/SIGTERM kill-switch (A10/F12): resting maker orders MUST NOT survive the
        process, so a ``docker stop`` / Ctrl-C cancels EVERY resting order (routed through
        ``_cancel_to_terminal`` — booking any shutdown-window fill before clearing) and then
        raises ``KeyboardInterrupt`` to unwind ``run()`` (whose ``finally`` is idempotent)."""
        print(f"[live] signal {signum} received -> cancelling all resting orders + exit",
              file=sys.stderr)
        try:
            self._cancel_all_resting()
        finally:
            raise KeyboardInterrupt(f"shutdown signal {signum}")

    def _install_signal_handlers(self):
        """Route SIGINT/SIGTERM to the cancel-all kill-switch. Best-effort: signal handlers
        can only be installed from the main thread, so a ValueError (e.g. inside a worker
        thread) is tolerated — the ``run()`` ``finally`` is the always-present backstop."""
        try:
            signal.signal(signal.SIGINT, self._on_exit_signal)
            signal.signal(signal.SIGTERM, self._on_exit_signal)
        except (ValueError, OSError) as e:  # pragma: no cover - non-main-thread only
            print(f"[live] could not install signal handlers ({e}); "
                  "relying on run() finally for cancel-all", file=sys.stderr)

    def _enforce_resume_halt_gate(self):
        """D16 startup gate: if this engine RESUMED a durable operator-reconcile halt,
        REFUSE to enter the maker path and exit CLEANLY (code 0) so docker
        ``restart: on-failure`` does NOT loop a halted bot back to life (a production gate
        — only a human reset resumes). The only ways to clear are explicit operator actions:

          * delete the (mode-tagged) ``<symbol>_<mode>_state.json`` -> a fresh start; or
          * set env ``LIVE_CLEAR_HALT=yes`` -> clears the halt but KEEPS the resumed
            position (re-persisted, so a later restart without the env stays cleared).

        A non-halted resume (the common case) passes through untouched. Runs before the
        order client is built, so a halted engine never touches the exchange."""
        if not self._halted:
            return
        if os.environ.get("LIVE_CLEAR_HALT", "").strip().lower() == "yes":
            print("[live] LIVE_CLEAR_HALT=yes — operator cleared the persisted operator-"
                  f"reconcile halt (was: {self._halt_reason!r}); keeping the resumed "
                  "position and resuming. Root-cause must already be resolved.",
                  file=sys.stderr)
            self._halted = False
            self._halt_reason = None
            if self.persist:                            # durably clear so a later restart stays clear
                try:
                    save_state(self.out_dir, self.symbol, self._state_dict(), tag=self.mode)
                except OSError as e:
                    print(f"[live] WARN: could not persist cleared halt ({e})",
                          file=sys.stderr)
            return
        print("[live] *** ENGINE RESUMED HALTED *** refusing to enter the maker path "
              f"(operator-reconcile halt: {self._halt_reason!r}). A human must root-cause "
              "and explicitly clear it: delete the state file for a fresh start, or set "
              "LIVE_CLEAR_HALT=yes to clear the halt while keeping the position. Exiting "
              "cleanly (code 0) so an auto-restart container does NOT resume trading.",
              file=sys.stderr)
        raise SystemExit(0)

    # -- main loop ----------------------------------------------------------
    async def run(self):
        import websockets  # lazy import so the module imports without the dep

        # Maker (real-order) path switch == live mode (D14). Computed ONCE here so the recv
        # loop / _handle / _tick read a stable value; dryrun (the default) keeps it OFF.
        self.maker_enabled = self._compute_maker_enabled()

        # D16: a resumed durable operator-reconcile halt refuses + clean-exits HERE, before
        # the order client is built or any exchange call — a docker auto-restart can never
        # silently continue a halted bot.
        self._enforce_resume_halt_gate()

        if self.maker_enabled:
            # LIVE == real-money MAINNET: build the order client (a missing API key raises
            # here, naturally), install the cancel-all-on-exit kill-switch, and print the
            # loud real-money banner. dryrun skips all of this (simulated fills only).
            self._build_order_client()
            self._install_signal_handlers()
            self._maker_startup_banner()

        # D16: an OperatorReconcileHalt that propagates out of startup / the recv loop is an
        # INTENTIONAL fail-closed stop, not a crash — convert it to a CLEAN exit (code 0) so
        # docker ``restart: on-failure`` does NOT loop a halted bot back to life. The inner
        # cancel-all finally still runs FIRST (resting orders never survive). A genuine
        # uncaught exception is NOT caught here -> propagates -> non-zero exit -> on-failure
        # restarts it (transient-fault recovery). The halt was persisted before it raised
        # (see _halt_operator_reconcile), so even that restart refuses to resume.
        try:
            # KILL SWITCH (A10/F12 + P1-4 startup coverage): resting maker orders MUST NOT
            # survive the process. The cancel-all finally wraps the WHOLE maker startup — the
            # R1 gate/seed, bootstrap, AND restart-resume — as well as the recv loop, so a halt
            # / refuse / exception during startup (after the order client is built) still
            # cancels EVERY persisted resting order (poll-to-terminal + book any in-flight fill
            # before clearing), not just a failure inside the recv loop. Paper has no resting
            # orders so cancel-all is a no-op.
            try:
                # R1 gate (Codex P0): armed-live reconciles against the exchange BEFORE
                # bootstrap/deploy; refusal exits cleanly (D16). No-op for paper. On the maker
                # path it also seeds local state (A6a) and stores _r1_ok + the open-order list.
                self._maybe_gate()

                try:
                    self.bootstrap()
                except Exception as e:
                    print(f"[{self.mode}] bootstrap failed ({type(e).__name__}: {e}); "
                          "continuing — anchor will build from live 1h closes.")

                # Restart reconciliation: re-link / cancel-orphan / recover down-time fills
                # using the SAME open-order list the gate fetched (no refetch, C-P0#5). No-op
                # for paper.
                if self.maker_enabled:
                    self.resume_reconcile_orders(self._r1_open_orders or [])

                topics = [f"orderbook.1.{self.symbol}", f"publicTrade.{self.symbol}",
                          f"kline.5.{self.symbol}", f"kline.60.{self.symbol}"]
                t_end = self._t_end()

                while time.time() < t_end:
                    try:
                        async with websockets.connect(WS_URL, ping_interval=20,
                                                      ping_timeout=20, max_queue=None) as ws:
                            await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                            while time.time() < t_end:
                                try:
                                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                                except asyncio.TimeoutError:
                                    self._tick(time.time())
                                    continue
                                self._handle(json.loads(msg), time.time())
                                self._tick(time.time())
                    except OperatorReconcileHalt:
                        raise                            # fail-closed halt: do NOT reconnect
                    except Exception as e:
                        print(f"[{self.mode}] reconnect ({type(e).__name__}: {e})")
                        await asyncio.sleep(2)
            finally:
                if self.maker_enabled:
                    try:
                        self._cancel_all_resting()
                    except Exception as e:              # pragma: no cover - best-effort shutdown
                        print(f"[live] cancel-all on exit raised ({type(e).__name__}: {e})",
                              file=sys.stderr)

            # finalize: mature remaining markout, last write
            self.flush_markout(time.time() + (max(HORIZONS) if HORIZONS else 0))
            now = time.time()
            self.accrue(now)
            self.print_summary(now)
            path = self.write_status(now)
            print(f"[{self.mode}] FINAL status -> {path}")
            if self.csv_path:
                self._write_csv()
        except OperatorReconcileHalt as e:
            print(f"[live] HALT propagated to run() — intentional fail-closed stop ({e}). "
                  "Resting orders cancelled; exiting cleanly (code 0) so an auto-restart "
                  "container does NOT loop. Resolve + clear the halt (delete the state file "
                  "or set LIVE_CLEAR_HALT=yes) to resume.", file=sys.stderr)
            raise SystemExit(0)

    def _handle(self, d: dict, now: float):
        # Take the hourly carry snapshot BEFORE any fill this event mutates the
        # position, so the integer-hour snapshot reflects the holding at the top
        # of the hour (pre-fill) — matching the backtest, which snapshots at the
        # bar boundary before that bar's fills. (No-op until an hour is crossed;
        # _tick() also calls accrue() to cover the recv-timeout path.)
        self.accrue(now)
        topic = d.get("topic", "")
        if topic.startswith("orderbook.1"):
            ob = d.get("data", {})
            if ob.get("b"):
                self.bid = float(ob["b"][0][0])
            if ob.get("a"):
                self.ask = float(ob["a"][0][0])
            self._push_mid(now)
            if not self.maker_enabled:               # maker path: real fills replace the
                self._maybe_deploy()                 #   simulated deploy/evaluate (A4b/F4);
                self.evaluate_fills(now)             #   the markout gauge above still runs
        elif topic.startswith("publicTrade"):
            for tr in d.get("data", []):
                self.last = float(tr["p"])
                self._on_trade_markout(now, tr.get("S"))
            if not self.maker_enabled:               # maker path bypasses paper sim (A4b/F4)
                self._maybe_deploy()
                self.evaluate_fills(now)
        elif topic.startswith("kline.5"):
            for it in d.get("data", []):
                t = int(it["start"])
                self.klines5[t] = {"t": t, "o": float(it["open"]), "h": float(it["high"]),
                                   "l": float(it["low"]), "c": float(it["close"])}
            self._trim_klines()
        elif topic.startswith("kline.60"):
            for it in d.get("data", []):
                if not it.get("confirm"):
                    continue
                start = int(it["start"])
                if self.last_1h_start is None or start > self.last_1h_start:
                    self._ema_step(float(it["close"]))
                    self.last_1h_start = start

    def _tick(self, now: float):
        self.flush_markout(now)
        if now - self.last_status >= STATUS_EVERY:
            # order is accrue -> maker_step -> status write (A4b): the hourly carry snapshot
            # (accrue) is taken BEFORE poll_fills mutates qty (F4), and order churn matches
            # the status cadence (~12s) rather than every WS frame.
            self.accrue(now)
            if self.maker_enabled:
                self.maker_step(now)
            self.print_summary(now)
            self.write_status(now)
            self.last_status = now

    def _write_csv(self):
        import csv
        with open(self.csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_ms", "utc", "side", "slice", "price", "qty"])
            for e in self.events:
                w.writerow([e["ts"], e["utc"], e["side"], e["slice"], e["price"], e["qty"]])
        print(f"[{self.mode}] wrote {len(self.events)} events -> {self.csv_path}")


# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="Paper/live slice-ladder engine on live Bybit data")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    ap.add_argument("--mode", choices=["dryrun", "live"], default=_resolve_mode())
    ap.add_argument("--csv", default=None)
    ap.add_argument("--allow-fresh-live-deploy", action="store_true",
                    help="authorize a FIRST armed-live fresh deploy (R1 — requires --expect-asset/"
                         "--expect-amount matching a clean exchange; never use to recover lost "
                         "state over a real position)")
    ap.add_argument("--expect-asset", default=None,
                    help="declared funding coin for a fresh deploy (e.g. USDT)")
    ap.add_argument("--expect-amount", type=float, default=None,
                    help="declared funding amount (coin units) for a fresh deploy")
    a = ap.parse_args(argv)
    eng = PaperEngine(symbol=a.symbol, mode=a.mode, seconds=a.seconds, csv_path=a.csv,
                      allow_fresh=a.allow_fresh_live_deploy,
                      expect_asset=a.expect_asset, expect_amount=a.expect_amount)
    asyncio.run(eng.run())


if __name__ == "__main__":
    main()
