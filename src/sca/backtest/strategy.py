"""
================================================================================
 usd1_strategy_final.py  —  USD1USDT EMA-Anchored Take-Profit Ladder (FINAL)
================================================================================

WHAT THIS IS
    A standalone (NON-freqtrade), self-contained, event-driven backtest + reference
    implementation of the EMA-anchored sell-side take-profit ladder (variant r1_6,
    "home = USD1"). The live engine (sca.live.engine) runs this SAME strategy and
    shares this repo's one carry model (sca.interest), so backtest == paper.

THE STRATEGY IN ONE SENTENCE
    Stay long USD1 (collect the UTA carry), use a low-rung sell ladder as a
    canary probe, protect normal sells with an entry-cost floor, and surrender
    after a configured anchor break to reset cost and keep measuring live fills.

--------------------------------------------------------------------------------
HONEST CURRENT FINDING  (sell_round=floor + min_sell_margin_bp=2bp; min_profit=1bp/rest=14bp; rungs [1,2,3,4,5])
    Bybit credits USD1 carry on the per-UTC-day MINIMUM of hourly balance snapshots
    (see sca.interest), so a slice parked in USDT across even one hourly snapshot
    forfeits that whole day's carry on it. The current low-floor config is a live
    canary probe for fill rate / queue loss, not a long-term carry allocation:

      TOTAL APR (price skim + carry) @adv0.5, ~6.6-month USD1USDT window:
        touch (optimistic) ....... ~8.2%   (< realized hold)
        strict + 20% vol gate .... ~6.7%   (< realized hold)

      (sell_round=floor fills MORE easily than the old round/ceil口径 — @adv0 it lifts
       touch APR 2.66->3.89% ex-carry — but @adv0.5 the extra fills each eat the haircut,
       so floor (~8.2%) sits slightly BELOW old round (~8.4%). Floor is chosen for
       fill-rate + backtest==live口径 unification, NOT as an adv-robust trading edge.)

    The floor keeps the probe close to holding while still creating fills; adverse
    selection and the min-snapshot carry penalty still prevent promoting it as
    durable edge. It is kept to generate live markout measurement, NOT because it
    beats holding.

    Historical wider-rung variants looked better in-sample because they stayed closer
    to carry, but no parameter config beat buy-and-hold out of sample. The honest
    default remains HOLD USD1. Current canary invariants are pinned in tests/test_smoke.py.

    (Historical note: before the carry model was corrected to min-snapshot AND the
    rungs were lowered, this showed a thin ~+0.4..0.9% in-sample win over flat-10 —
    an optimistic continuous-carry result that survived NEITHER correction. See git
    history / docs/FINDINGS.md.)

--------------------------------------------------------------------------------
HONEST CAVEATS  (read before trusting this with real money)
  1. ADVERSE SELECTION IS THE KILLER KNOB. Real adv is UNKNOWN until measured on
     live infra (see dryrun.py); the trading result degrades monotonically with it.
     At non-zero adverse selection the probe trails realized hold; any cushion
     shrinks further as adv rises.
  2. THIN-TO-NEGATIVE MARGIN vs ACTUALLY HOLDING. The current canary probe is close
     to holding but still loses to honest hold once adv is non-zero. The only
     repeatable, drift-neutral component is a small positive PRICE-ONLY skim that
     adverse selection + the min-snapshot carry penalty can erase.
  3. REGIME-DEPENDENT. Edge is concentrated in the choppy/mean-reverting first half;
     the second half is mostly carry plus a thin skim. If USD1 stops printing
     recurring spikes, this degenerates toward buy&hold with extra execution risk.
  4. FILL MODEL. Headline uses maker "touch" fills (a resting limit fills if price
     reaches it). The STRICT and volume-gated columns above are the defensible
     numbers; trust those, not the touch headline.
  5. SINGLE ASSET, SINGLE 6.7-MONTH WINDOW. n=68 round trips. Not a large sample.
     Re-validate on more history / other re-pegging stables before sizing up.
  6. LOCKED ASSUMPTION: USD1 always re-pegs (no permanent-depeg tail). There is NO
     stop-loss. A real permanent depeg would be an uncapped loss this model ignores.

REPRODUCE:   python3 -m sca.backtest.strategy   (or: sca backtest)
================================================================================
"""
from __future__ import annotations
import os
import pandas as pd
import numpy as np

# ----------------------------------------------------------------------------
# CONSTANTS (locked task constraints + verified strategy params)
# ----------------------------------------------------------------------------
from sca.config import DATA_DIR as _DATA_DIR, CFG as _CFG, strategy_for
from sca.interest import DailyMinInterest   # shared carry model (parity with live engine)
from sca.strategy_rules import rounded_rebuy_price, final_sell_price
_S = _CFG.get("strategy", {}); _B = _CFG.get("backtest", {}); _M = _CFG.get("market", {})
DATA_DIR = str(_DATA_DIR)
SYMBOL   = _CFG.get("primary_symbol", "USD1USDT")
ALLOC    = float(_B.get("alloc_usd", 10_000.0))
BPD      = int(_M.get("bars_per_day_5m", 288))
APR_UTA  = float(_S.get("interest_apr", 0.10))
TICK     = 1e-4             # tickSize = 1 bp price floor (round all order prices to 4dp)
MKT_VOL  = 2_538_200         # USD1USDT ~ $2.538M/day average daily volume
CAP_FRAC = 0.02              # capacity rule: keep strategy turnover < 2% of ADV

# --- verified strategy parameters (variant r1_6) ---
ANCHOR_EMA_SPAN = int(_S.get("anchor_ema_span", 21))
RUNG_BP = list(_S.get("rungs", [5, 7, 10, 14, 20]))
FRACS   = list(_S.get("fractions", [0.15, 0.18, 0.20, 0.22, 0.25]))
REBUY_OFF_BP = float(_S.get("rebuy_offset_bp", -1))
MIN_PROFIT_BP = float(_S.get("min_profit_bp", 0.0))
REST_BPS = float(_S.get("rest_bps", 0.0))
SELL_ROUND = _S.get("sell_round")                                     # None if yaml unset -> legacy round
MIN_SELL_MARGIN_BP = float(_S.get("min_sell_margin_bp", 0.0) or 0.0)  # so no-arg backtest tracks yaml口径


# ----------------------------------------------------------------------------
# DATA LOADER  (self-contained; same no-lookahead convention as bt_faithful.load)
#   - 1h EMA is usable only AFTER the 1h candle closes (avail_ts = ts + 3,600,000ms)
#   - merge_asof(direction="backward") attaches the latest already-closed 1h EMA
#   - decisions use the 5m bar's own open as the live market + that lagged 1h EMA
# ----------------------------------------------------------------------------
def load(sym: str = SYMBOL, data_dir: str = DATA_DIR, ema_span: int | None = None) -> pd.DataFrame:
    span = ANCHOR_EMA_SPAN if ema_span is None else int(ema_span)
    d5 = pd.read_csv(f"{data_dir}/{sym}_5m.csv")
    d1 = pd.read_csv(f"{data_dir}/{sym}_1h.csv")
    for c in ["ts", "open", "high", "low", "close", "volume", "turnover"]:
        if c in d5:
            d5[c] = pd.to_numeric(d5[c])
    for c in ["ts", "close"]:
        d1[c] = pd.to_numeric(d1[c])
    d5 = d5.sort_values("ts").reset_index(drop=True)
    d1 = d1.sort_values("ts").reset_index(drop=True)
    d1["ema_anchor"] = d1["close"].ewm(span=span, adjust=False).mean()
    d1["avail_ts"] = d1["ts"] + 3_600_000          # 1h usable only after it closes
    m = pd.merge_asof(
        d5, d1[["avail_ts", "ema_anchor"]],
        left_on="ts", right_on="avail_ts", direction="backward",
    )
    return m.dropna(subset=["ema_anchor"]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# BACKTEST  (event-driven, faithful adverse + interest accounting)
#
#   Realism knobs (set these honestly):
#     adv         : adverse selection in bp PER SIDE, charged on EVERY fill
#                   (buy = L*(1+adv/1e4), sell = L*(1-adv/1e4)), incl. t0 deploy.
#     fill_mode   : 'touch'  -> resting limit fills if price merely reaches it (optimistic)
#                   'strict' -> fills only if price trades THROUGH the level (no exact-kiss)
#     liq_gate    : None, or a fraction f in (0,1]. A fill is allowed only if its slice
#                   notional <= f * bar turnover (conservative binary feasibility gate;
#                   models that a thin bar cannot fully absorb a resting maker order).
#     with_yield  : accrue the 10% UTA interest on USD1 slices (True for TOTAL APR).
#
#   Accounting (conservative):
#     - interest accrues per bar ONLY on slices currently in USD1, into a SEPARATE
#       bucket that is NOT reinvested into slices (matches bt_faithful; understates).
#     - at most ONE state transition per slice per bar (no free intra-bar round trip).
#     - price capture DOES compound per slice (sell high -> rebuy low -> more units).
# ----------------------------------------------------------------------------
def backtest(adv: float = 0.5, *, symbol: str | None = None, params: dict | None = None,
             with_yield: bool = True, fill_mode: str = "touch",
             liq_gate: float | None = None, df: pd.DataFrame | None = None) -> dict:
    # per-symbol params — 三层优先级: params > strategy_for(symbol) > 模块全局(无参=现状零变化)
    if params is not None:
        sp = params
    elif symbol is not None:
        sp = strategy_for(symbol)
    else:
        sp = {"rungs": RUNG_BP, "fractions": FRACS, "min_profit_bp": MIN_PROFIT_BP,
              "rest_bps": REST_BPS, "anchor_ema_span": ANCHOR_EMA_SPAN,
              "rebuy_offset_bp": REBUY_OFF_BP, "interest_apr": APR_UTA,
              "sell_round": SELL_ROUND, "min_sell_margin_bp": MIN_SELL_MARGIN_BP}
    fracs_p = list(sp["fractions"]); rungs_p = list(sp["rungs"])
    min_profit_p = float(sp["min_profit_bp"]); rest_p = float(sp["rest_bps"])
    rebuy_off_p = float(sp["rebuy_offset_bp"]); apr_uta_p = float(sp["interest_apr"])
    sell_round_p = sp.get("sell_round") or "round"          # backtest legacy口径 = round
    min_sell_margin_p = float(sp.get("min_sell_margin_bp", 0.0) or 0.0)
    assert fill_mode in ("touch", "strict")
    assert abs(sum(fracs_p) - 1.0) < 1e-9
    if df is None:
        df = load(symbol or SYMBOL, ema_span=sp["anchor_ema_span"])
    o = df.open.values; h = df.high.values; l = df.low.values; c = df.close.values
    anc = df.ema_anchor.values; ts = df.ts.values
    turn_bar = (df.turnover.astype(float).values if "turnover" in df.columns
                else np.full(len(df), np.inf))
    n = len(c)
    # interest: shared per-UTC-day min-of-hourly-snapshots carry model — IDENTICAL
    # rule to the live engine (sca.interest), so backtest and paper cannot drift.
    interest = DailyMinInterest(apr_uta_p / 365.0) if with_yield else None

    # t0 deploy: 100% into USD1 at open[0], adverse haircut applied
    eff0 = o[0] * (1 + adv / 1e4)
    sl = [dict(state="usd1", qty=fr * ALLOC / eff0, cash=0.0, sell_px=0.0,
               entry=eff0, t=0)
          for fr in fracs_p]
    rungs = list(rungs_p)

    turn = ALLOC                 # initial deploy counts toward turnover
    sells = rebuys = 0
    realized_capture = 0.0       # $ price pnl booked at rebuy = genuine trading edge
    usdt_val_bars = tot_val_bars = 0.0
    max_dwell = 0
    eq = []

    def _sell_hits(R, oi, hi):
        return (R < oi) or (hi > R) if fill_mode == "strict" else (R <= oi) or (hi >= R)

    def _buy_hits(B, oi, li):
        return (B > oi) or (li < B) if fill_mode == "strict" else (B >= oi) or (li <= B)

    for i in range(n):
        a = anc[i]; oi, hi, li, ci = o[i], h[i], l[i], c[i]
        cap = liq_gate * turn_bar[i] if liq_gate is not None else float("inf")
        if interest is not None:        # hourly snapshot = USD1 holding at bar start (top of hour)
            interest.observe(ts[i] / 1000.0,
                             sum(s["qty"] for s in sl if s["state"] == "usd1"))
        bar_usdt = bar_tot = 0.0
        for k, s in enumerate(sl):
            if s["state"] == "usd1":
                R = final_sell_price(a, rungs[k], s.get("entry"),
                                     min_profit_p, rest_p, 1e-4,
                                     sell_round=sell_round_p,
                                     min_sell_margin_bp=min_sell_margin_p)
                if _sell_hits(R, oi, hi) and (s["qty"] * R) <= cap:
                    f = R * (1 - adv / 1e4)
                    s["cash"] = s["qty"] * f; s["sell_px"] = f
                    s["qty"] = 0.0; s["state"] = "usdt"; s["entry"] = None; s["t"] = i
                    turn += s["cash"]; sells += 1
            else:                                                # 'usdt' -> rest rebuy
                B = rounded_rebuy_price(a, rebuy_off_p, 4)
                if _buy_hits(B, oi, li) and s["cash"] <= cap:
                    f = B * (1 + adv / 1e4)
                    nq = s["cash"] / f
                    realized_capture += (s["sell_px"] - f) * nq
                    max_dwell = max(max_dwell, i - s["t"])
                    s["qty"] = nq; s["cash"] = 0.0; s["state"] = "usd1"
                    s["entry"] = f; s["t"] = i
                    turn += nq * f; rebuys += 1
            v = (s["qty"] * ci) if s["state"] == "usd1" else s["cash"]
            bar_tot += v
            if s["state"] == "usdt":
                bar_usdt += v
        usdt_val_bars += bar_usdt; tot_val_bars += bar_tot
        # equity counts SETTLED interest only (completed UTC days) — matches the
        # live engine's `total` (current-day pending is never booked into equity).
        eq.append((interest.settled if interest is not None else 0.0) + bar_tot)

    for s in sl:                                                 # still-idle slices at end
        if s["state"] == "usdt":
            max_dwell = max(max_dwell, (n - 1) - s["t"])

    final = eq[-1]
    span = (ts[-1] - ts[0]) / 86400_000
    eqs = pd.Series(eq); mdd = ((eqs - eqs.cummax()) / eqs.cummax()).min()
    return dict(
        adv=adv, fill_mode=fill_mode, liq_gate=liq_gate,
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        price_cap_pct=round(realized_capture / ALLOC * 100, 3),
        mdd_pct=round(mdd * 100, 3),
        turn_per_day=turn / span,
        sells=sells, rebuys=rebuys,
        usdt_time_pct=round(usdt_val_bars / tot_val_bars * 100, 3),
        max_idle_usdt_days=round(max_dwell / BPD, 3),
        slices_idle_end=sum(1 for s in sl if s["state"] == "usdt"),
        span_d=round(span, 1), n_bars=n)


def hold_benchmark(adv: float = 0.0, *, with_yield: bool = True,
                   df: pd.DataFrame | None = None) -> float:
    """Realized buy-&-hold USD1: deploy all $10k at open[0] (one adverse haircut),
    hold to the end, accrue 10% UTA. This is the HONEST opportunity cost; the LOCKED
    benchmark is flat 10.000%."""
    if df is None:
        df = load()
    eff0 = df.open.iloc[0] * (1 + adv / 1e4)
    qty = ALLOC / eff0
    # same shared carry model; holding is constant, so every complete UTC day
    # credits the full qty*APR/365 (a pure hold suffers no min-snapshot penalty).
    interest = DailyMinInterest(APR_UTA / 365.0) if with_yield else None
    if interest is not None:
        for t in df.ts.values:
            interest.observe(t / 1000.0, qty)
    settled = interest.settled if interest is not None else 0.0
    last = settled + qty * df.close.iloc[-1]
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400_000
    return round((last / ALLOC - 1) * 100 * 365 / span, 3)


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    df = load()
    ADVS = [0.0, 0.5, 1.0, 1.5]
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400_000
    print("=" * 78)
    print("USD1USDT EMA-Anchored Take-Profit Ladder — FINAL (variant r1_6)")
    print(f"  {len(df)} 5m bars, span {span:.1f}d (~{span/30.4:.1f}mo), ${ALLOC:,.0f}, "
          f"10% UTA carry | LOCKED benchmark: hold USD1 = 10.000% APR")
    print("=" * 78)

    print(f"\n  realized buy&hold USD1 (honest opp. cost):  "
          + "  ".join(f"adv{a}={hold_benchmark(a):.2f}" for a in ADVS))

    def row(label, fn):
        print(f"  {label:<26}" + "".join(f"{fn(a):>9.3f}" for a in ADVS))

    print("\n  TOTAL APR (price skim + 10% interest)")
    print("  adv:                      " + "".join(f"{a:>9}" for a in ADVS))
    row("touch (engine maker)",    lambda a: backtest(a, fill_mode="touch")["apr"])
    row("STRICT trade-through",    lambda a: backtest(a, fill_mode="strict")["apr"])
    row("STRICT + 20% vol gate",   lambda a: backtest(a, fill_mode="strict", liq_gate=0.2)["apr"])

    print("\n  PRICE-ONLY edge vs hold (interest OFF -> real trading alpha?)")
    print("  adv:                      " + "".join(f"{a:>9}" for a in ADVS))
    row("touch px-only - hold",
        lambda a: round(backtest(a, with_yield=False, fill_mode="touch")["apr"]
                        - hold_benchmark(a, with_yield=False), 3))

    x = backtest(0.5, fill_mode="touch")
    print(f"\n  Mechanics @adv0.5: sells={x['sells']} rebuys={x['rebuys']} "
          f"idle-USDT={x['usdt_time_pct']}% max-idle={x['max_idle_usdt_days']}d "
          f"stuck-end={x['slices_idle_end']} price-cap={x['price_cap_pct']}% "
          f"MDD={x['mdd_pct']}%")
    tpd = x["turn_per_day"]; pct = tpd / MKT_VOL * 100
    print(f"  Capacity @adv0.5: turnover ${tpd:,.0f}/day = {pct:.3f}% of "
          f"${MKT_VOL:,}/day ADV -> size up to ${ALLOC * CAP_FRAC * MKT_VOL / tpd:,.0f} "
          f"at the 2% cap")

    win = all(backtest(a, fill_mode="strict", liq_gate=0.2)["apr"] > 10.0
              for a in [0.5, 1.0, 1.5])
    print(f"\n  VERDICT: beats flat-10% at adv>=0.5 under strict+gated fills? {win}")
    print("  (See module docstring for the full honest caveat list.)")
