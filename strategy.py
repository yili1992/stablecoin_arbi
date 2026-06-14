"""
================================================================================
 usd1_strategy_final.py  —  USD1USDT EMA-Anchored Take-Profit Ladder (FINAL)
================================================================================

WHAT THIS IS
    A standalone (NON-freqtrade), self-contained, event-driven backtest + reference
    implementation of the single strategy that VERIFIABLY beats the "hold USD1 @ 10%
    APR" benchmark on the ~6.7-month USD1USDT sample under realistic adverse selection.

    It is variant r1_6 ("EMA21-anchored sell-side take-profit ladder, home = USD1"),
    re-derived here cleanly. It was independently re-implemented from scratch
    (backtest/bt_verify_r1_6.py) and reproduces to the decimal; it then survived a
    liquidity-feasibility audit (per-bar turnover gate) that SANK the higher-headline
    variants (PAAL/r1_7, dip-gated/r1_2 sold $10k into $4-$200-volume spike bars).

--------------------------------------------------------------------------------
THE EDGE IN ONE SENTENCE
    Stay long USD1 (collect the 10% UTA carry the whole time), and skim the recurring
    +5..+20 bp mean-reverting spikes above a 1h EMA anchor with a 5-slice sell ladder,
    re-buying 1 bp below the same (floating) anchor. Idle-USDT time is held to ~2.5%,
    so almost no carry is forfeited, and the price skim is pure gravy on top.

WHY IT WORKS WHERE THE BASE STRATEGY LOSES
    The user's base freqtrade strategy LOSES to hold (Codex-confirmed: 9.5% @adv0.5,
    9.2% @adv1.0) because its fixed-peg re-entry traps capital idle in 0%-yield USDT
    waiting for a peg dip that may not come. This variant re-buys at a FLOATING EMA
    anchor (ema21_1h - 1 bp), so capital almost always re-deploys into USD1 within ~2
    days -> minimal idle-yield drag, which is the whole game at a 10% carry.

--------------------------------------------------------------------------------
BACKTEST RESULTS  (USD1USDT, 58,000 5m bars, span 201.4d ~ 6.6mo, $10k, 10% UTA)
    Benchmark (LOCKED):     hold USD1 = 10.000% APR (flat)
    Realized buy&hold USD1: ~10.27% APR over THIS window (+15bp one-time re-peg drift
                            0.9994 -> 1.0009 that any holder captures; non-repeatable)

    TOTAL APR (price skim + 10% interest), adverse selection swept per side:
                                   adv=0.0   adv=0.5   adv=1.0   adv=1.5
      engine maker fill (touch)    11.186    10.949    10.713    10.477
      STRICT trade-through         10.544    10.377    10.211    10.045   <- queue-pos
      STRICT + 20% volume gate       --      10.419    10.283    10.147   <- + liquidity
    --> beats the locked 10% bar at EVERY adv>=0.5 under EVERY fill model. (WIN.)

    PRICE-ONLY edge vs hold (interest off, drift-neutral, proves real trading alpha):
      touch:  +1.112 / +0.892 / +0.673 / +0.455   (adv 0/0.5/1.0/1.5)
      strict: ~+0.45 @adv0.5  (still POSITIVE -> not an interest mirage / not just drift)

    Mechanics @adv0.5: 68 sells / 68 rebuys, idle-USDT time 2.46%, max idle dwell 1.98d,
      0 slices stuck in USDT at end, realized price-capture 0.49%, MDD -0.53%, 100% in
      USD1 the rest of the time (the GOOD state, earning the 10% carry).

    Capacity: turnover ~$1,248/day at $10k = 0.049% of the $2.538M/day USD1 market.
      Scales to ~$407k of capital before turnover hits 2% of ADV. Trivially feasible.

--------------------------------------------------------------------------------
HONEST CAVEATS  (read before trusting this with real money)
  1. ADVERSE SELECTION IS THE KILLER KNOB. The win shrinks monotonically with adv.
     It clears flat-10% out to ~adv2.5 (touch) / the realistic 1-2 bp band is fine,
     but the cushion over flat-10% under the conservative strict model is only ~+0.38%
     at adv0.5. Real adv is UNKNOWN until measured on live infra (see dryrun.py).
  2. THIN MARGIN vs ACTUALLY HOLDING. Against realized buy&hold USD1 (10.27% on this
     window), the trading overlay adds +0.68% (touch) but only ~+0.1% (strict) at
     adv0.5, and it slightly TRAILS realized-hold in the up-trending second half
     (10.51 vs 10.60) -- it merely matches the 10% floor when USD1 trends into peg.
     It robustly beats the LOCKED flat-10% bar; it does NOT robustly beat a lucky
     holder in a one-way up-repeg. The repeatable, drift-neutral alpha is the
     positive PRICE-ONLY edge (~+0.45% strict @adv0.5).
  3. REGIME-DEPENDENT. Edge is concentrated in the choppy/mean-reverting first half
     (H1 +1.3..+1.7 vs flat-10); the second half is carry + a thin skim. If USD1
     stops printing recurring +5..+20 bp spikes, this degenerates toward buy&hold
     (still >10% via carry, downside bounded by hold by construction).
  4. FILL MODEL. Headline uses maker "touch" fills (a resting limit fills if price
     reaches it). The STRICT and volume-gated columns above are the defensible
     numbers; trust those, not the touch headline.
  5. SINGLE ASSET, SINGLE 6.7-MONTH WINDOW. n=68 round trips. Not a large sample.
     Re-validate on more history / other re-pegging stables before sizing up.
  6. LOCKED ASSUMPTION: USD1 always re-pegs (no permanent-depeg tail). There is NO
     stop-loss. A real permanent depeg would be an uncapped loss this model ignores.

REPRODUCE:   python3 usd1_strategy_final.py
================================================================================
"""
from __future__ import annotations
import os
import pandas as pd
import numpy as np

# ----------------------------------------------------------------------------
# CONSTANTS (locked task constraints + verified strategy params)
# ----------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SYMBOL   = "USD1USDT"
ALLOC    = 10_000.0          # capital per backtest
BPD      = 288               # 5-minute bars per day
APR_UTA  = 0.10              # 10% APR UTA interest while holding USD1 (the benchmark)
TICK     = 1e-4             # tickSize = 1 bp price floor (round all order prices to 4dp)
MKT_VOL  = 2_538_200         # USD1USDT ~ $2.538M/day average daily volume
CAP_FRAC = 0.02              # capacity rule: keep strategy turnover < 2% of ADV

# --- verified strategy parameters (variant r1_6) ---
ANCHOR_EMA_SPAN = 21         # 1h EMA span used as the floating anchor (ema21_1h)
RUNG_BP = [5, 7, 10, 14, 20] # sell-ladder rungs: bp ABOVE the anchor
FRACS   = [0.15, 0.18, 0.20, 0.22, 0.25]  # NAV fraction per slice (sums to 1.0)
REBUY_OFF_BP = -1            # re-buy a slice at anchor - 1 bp (floating)


# ----------------------------------------------------------------------------
# DATA LOADER  (self-contained; same no-lookahead convention as bt_faithful.load)
#   - 1h EMA is usable only AFTER the 1h candle closes (avail_ts = ts + 3,600,000ms)
#   - merge_asof(direction="backward") attaches the latest already-closed 1h EMA
#   - decisions use the 5m bar's own open as the live market + that lagged 1h EMA
# ----------------------------------------------------------------------------
def load(sym: str = SYMBOL, data_dir: str = DATA_DIR) -> pd.DataFrame:
    d5 = pd.read_csv(f"{data_dir}/{sym}_5m.csv")
    d1 = pd.read_csv(f"{data_dir}/{sym}_1h.csv")
    for c in ["ts", "open", "high", "low", "close", "volume", "turnover"]:
        if c in d5:
            d5[c] = pd.to_numeric(d5[c])
    for c in ["ts", "close"]:
        d1[c] = pd.to_numeric(d1[c])
    d5 = d5.sort_values("ts").reset_index(drop=True)
    d1 = d1.sort_values("ts").reset_index(drop=True)
    d1["ema_anchor"] = d1["close"].ewm(span=ANCHOR_EMA_SPAN, adjust=False).mean()
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
def backtest(adv: float = 0.5, *, with_yield: bool = True, fill_mode: str = "touch",
             liq_gate: float | None = None, df: pd.DataFrame | None = None) -> dict:
    assert fill_mode in ("touch", "strict")
    assert abs(sum(FRACS) - 1.0) < 1e-9
    if df is None:
        df = load()
    o = df.open.values; h = df.high.values; l = df.low.values; c = df.close.values
    anc = df.ema_anchor.values; ts = df.ts.values
    turn_bar = (df.turnover.astype(float).values if "turnover" in df.columns
                else np.full(len(df), np.inf))
    n = len(c)
    ypb = (APR_UTA / 365.0 / BPD) if with_yield else 0.0

    # t0 deploy: 100% into USD1 at open[0], adverse haircut applied
    eff0 = o[0] * (1 + adv / 1e4)
    sl = [dict(state="usd1", qty=fr * ALLOC / eff0, cash=0.0, sell_px=0.0, t=0)
          for fr in FRACS]
    rungs = list(RUNG_BP)

    accr = 0.0
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
        bar_usdt = bar_tot = 0.0
        for k, s in enumerate(sl):
            if s["state"] == "usd1":
                accr += s["qty"] * ci * ypb
                R = round(a + rungs[k] / 1e4, 4)                 # sell rung, floats w/ EMA
                if _sell_hits(R, oi, hi) and (s["qty"] * R) <= cap:
                    f = R * (1 - adv / 1e4)
                    s["cash"] = s["qty"] * f; s["sell_px"] = f
                    s["qty"] = 0.0; s["state"] = "usdt"; s["t"] = i
                    turn += s["cash"]; sells += 1
            else:                                                # 'usdt' -> rest rebuy
                B = round(a + REBUY_OFF_BP / 1e4, 4)             # anchor - 1bp, floats
                if _buy_hits(B, oi, li) and s["cash"] <= cap:
                    f = B * (1 + adv / 1e4)
                    nq = s["cash"] / f
                    realized_capture += (s["sell_px"] - f) * nq
                    max_dwell = max(max_dwell, i - s["t"])
                    s["qty"] = nq; s["cash"] = 0.0; s["state"] = "usd1"; s["t"] = i
                    turn += nq * f; rebuys += 1
            v = (s["qty"] * ci) if s["state"] == "usd1" else s["cash"]
            bar_tot += v
            if s["state"] == "usdt":
                bar_usdt += v
        usdt_val_bars += bar_usdt; tot_val_bars += bar_tot
        eq.append(accr + bar_tot)

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
    ypb = (APR_UTA / 365.0 / BPD) if with_yield else 0.0
    eff0 = df.open.iloc[0] * (1 + adv / 1e4)
    qty = ALLOC / eff0; accr = 0.0; last = 0.0
    for c in df.close.values:
        accr += qty * c * ypb
        last = accr + qty * c
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
