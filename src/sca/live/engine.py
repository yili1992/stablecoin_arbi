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

SAFETY (LIVE is gated, default is paper)
    Real orders are only ever permitted when ALL of:
      mode == "live"  AND  env LIVE_TRADING_CONFIRM == "yes"  AND  API keys present.
    Even then, real order placement is an intentionally-unimplemented SCAFFOLD that
    refuses to send (raises) — the simulated fill loop NEVER calls it. Nothing here
    can trade by accident. Unauthorized "live" downgrades to paper with a warning.

Usage:  sca paper  --symbol USD1USDT --seconds 600
        python -m sca.live.engine --symbol USD1USDT --seconds 600 --csv out.csv
        sca live  --symbol USD1USDT          # gated; refuses real orders unless armed
================================================================================
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import os
import statistics
import time
import urllib.request

from sca.interest import DailyMinInterest   # shared carry model (parity with backtest)
from sca.live.persistence import (          # atomic restart/resume primitives
    append_event, load_state, read_events, save_state,
)

# --- config (single source of truth) ----------------------------------------
try:
    from sca.config import CFG as _CFG
except Exception:  # pragma: no cover - config must exist, but stay importable
    _CFG = {}

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
DEFAULT_SYMBOL = _D.get("symbol", "USD1USDT")
DEFAULT_SECONDS = int(_D.get("seconds", 600))

SEC_PER_YEAR = 365 * 24 * 3600
MID_RETAIN = max(HORIZONS) + 60 if HORIZONS else 90  # keep mid history this long
STATUS_EVERY = 12       # write status_<sym>.json + print summary every ~12s
EVENTS_CAP = 60
KLINES_CAP = 120
HISTORY_CAP = 600
ONE_HOUR_MS = 3_600_000


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
    """Return (armed, reason). Armed ONLY when mode==live AND confirm AND keys."""
    if mode != "live":
        return False, "mode is not 'live' (paper simulation)"
    if os.environ.get("LIVE_TRADING_CONFIRM") != "yes":
        return False, "LIVE_TRADING_CONFIRM != 'yes'"
    key = os.environ.get("BYBIT_API_KEY")
    sec = os.environ.get("BYBIT_API_SECRET")
    if not (key and sec):
        return False, "BYBIT_API_KEY / BYBIT_API_SECRET not set"
    return True, "armed (mode=live, confirm=yes, keys present)"


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
    def __init__(self, symbol: str = DEFAULT_SYMBOL, mode: str = "paper",
                 seconds: int = DEFAULT_SECONDS, csv_path: str | None = None):
        self.symbol = symbol
        self.req_mode = mode if mode in ("paper", "live") else "paper"
        self.seconds = int(seconds)
        self.csv_path = csv_path
        self.out_dir = (os.path.dirname(csv_path) if csv_path
                        else os.environ.get("SCA_OUT_DIR", "."))
        if not self.out_dir:
            self.out_dir = "."

        # --- live-trading gate (SAFETY) ---
        self.armed, self.gate_reason = live_authorization(self.req_mode)
        self.order_iface = OrderInterface(self.armed, self.gate_reason)
        # effective/reported mode: unauthorized live runs as paper
        self.mode = "live" if self.armed else "paper"

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
        """v=1 resume snapshot, written SYNCHRONOUSLY on every fill and status
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
            "v": 1,
            "symbol": self.symbol,
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
        }

    def _maybe_resume(self):
        """Restore prior state from ``<out_dir>/<symbol>_state.json`` if present
        (and persistence is enabled). No file / persist off / unknown schema =>
        fresh start, byte-identical to the pre-persistence behaviour."""
        if not self.persist:
            return
        st = load_state(self.out_dir, self.symbol)
        if st is None:
            return                                  # fresh start (no prior state)
        if st.get("v") != 1:                        # unknown schema: don't crash
            print(f"[{self.mode}] resume: unknown state schema v={st.get('v')!r}; "
                  "ignoring it and starting fresh.")
            return
        self.start = st["start"]
        self.deployed = st["deployed"]
        self.realized_capture = st["realized_capture"]
        self.slices = st["slices"]
        self.anchor = st["anchor"]
        self.ema = st["ema"]
        self.last_1h_start = st["last_1h_start"]
        self.history = st["history"]
        self.interest = DailyMinInterest.from_dict(st["interest"])
        self.events = read_events(self.out_dir, self.symbol)[-EVENTS_CAP:]
        self._resumed = True
        print(f"[{self.mode}] resumed {self.symbol}: deployed={self.deployed} "
              f"slices={len(self.slices)} realized={self.realized_capture:.6f} "
              f"settled_int={self.interest.settled:.6f} events={len(self.events)}")

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
        """Total USD1 holding QUANTITY (coins) right now — the snapshot base."""
        return sum(s["qty"] for s in self.slices if s["state"] == "usd1")

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
        # Persist on fill. Order matters for crash-safety: snapshot FIRST, then
        # append the audit line — so the snapshot is always >= the event log
        # ("快照永远 >= 流水"). If we crash in between, the position is captured
        # (snapshot ahead) and only one append-only audit line is missing; never
        # the reverse (which would make resume re-execute an already-done fill
        # against the live market). The snapshot already reflects this fill, since
        # evaluate_fills mutates the slice before calling _log_event.
        if self.persist:
            save_state(self.out_dir, self.symbol, self._state_dict())
            append_event(self.out_dir, self.symbol, event)

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
        if s["state"] == "usd1":
            mark = px if px is not None else (s.get("entry") or 0.0)
            return s["qty"] * mark
        return s["cash"]

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

        # position
        sl_out = []
        usd1_value = usdt_value = 0.0
        n_usd1 = n_usdt = 0
        for i, s in enumerate(self.slices):
            val = self._slice_value(s, px)
            if s["state"] == "usd1":
                usd1_value += val
                n_usd1 += 1
                sell_target = (round(a + self.rungs[i] / 1e4, TICK_DP)
                               if a is not None else None)
                entry = s.get("entry")
            else:
                usdt_value += val
                n_usdt += 1
                sell_target = None
                entry = None
            sl_out.append({
                "i": i, "frac": _r(self.fracs[i], 6), "state": s["state"],
                "qty": _r(s["qty"], 6), "entry_price": _r(entry, 6),
                "sell_target": _r(sell_target, 6), "value_usd": _r(val, 4),
            })
        total_value = usd1_value + usdt_value
        usd1_pct = (usd1_value / total_value * 100) if total_value > 0 else None

        # pnl decomposition: total = realized + SETTLED interest + unrealized.
        # interest is credited only on COMPLETED UTC days (honest); the current
        # day's running estimate is reported separately as pending_interest.
        start_value = self.alloc
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
        """Atomic write of status_<symbol>.json (tmp + rename)."""
        self._append_history(now)
        doc = self.status_doc(now)
        os.makedirs(self.out_dir, exist_ok=True)
        path = os.path.join(self.out_dir, f"status_{self.symbol}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f, allow_nan=False)
        os.replace(tmp, path)
        # Snapshot alongside the status write so that on resume history/events are
        # non-empty and the next write_status never re-truncates to empty.
        if self.persist:
            save_state(self.out_dir, self.symbol, self._state_dict())
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

    # -- main loop ----------------------------------------------------------
    async def run(self):
        import websockets  # lazy import so the module imports without the dep

        if self.req_mode == "live" and not self.armed:
            print(f"[WARN] live requested but NOT authorized: {self.gate_reason}. "
                  "Running as PAPER (no real orders).")
        elif self.armed:
            print("[WARN] LIVE armed. Real order placement is a non-implemented scaffold "
                  "and will REFUSE to send; fills remain simulated. No accidental trading.")

        try:
            self.bootstrap()
        except Exception as e:
            print(f"[{self.mode}] bootstrap failed ({type(e).__name__}: {e}); "
                  "continuing — anchor will build from live 1h closes.")

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
            except Exception as e:
                print(f"[{self.mode}] reconnect ({type(e).__name__}: {e})")
                await asyncio.sleep(2)

        # finalize: mature remaining markout, last write
        self.flush_markout(time.time() + (max(HORIZONS) if HORIZONS else 0))
        now = time.time()
        self.accrue(now)
        self.print_summary(now)
        path = self.write_status(now)
        print(f"[{self.mode}] FINAL status -> {path}")
        if self.csv_path:
            self._write_csv()

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
            self._maybe_deploy()
            self.evaluate_fills(now)
        elif topic.startswith("publicTrade"):
            for tr in d.get("data", []):
                self.last = float(tr["p"])
                self._on_trade_markout(now, tr.get("S"))
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
            self.accrue(now)
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
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args(argv)
    eng = PaperEngine(symbol=a.symbol, mode=a.mode, seconds=a.seconds, csv_path=a.csv)
    asyncio.run(eng.run())


if __name__ == "__main__":
    main()
