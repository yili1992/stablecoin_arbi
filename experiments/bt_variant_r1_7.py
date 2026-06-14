"""
bt_variant_r1_7.py  —  INDEPENDENT faithful backtest of variant r1_7:
"PAAL — Peg-Anchored Asymmetric Ladder (default-long, extreme-premium skim)".

Self-contained reimplementation (NOT a wrapper around bt_paal.run_paal) so the
numbers are an independent cross-check of the canonical impl. Reuses ONLY
load()/ALLOC/BPD from bt_faithful (same no-lookahead 5m+1h merge, same
$10k alloc, same simple 365/span APR annualization) for apples-to-apples.

STATE MACHINE (single all-in position):
  Anchor peg = 1.0000. Exactly one of two states.
  LONG USD1 (default, earns 10% APR on qty*close each bar):
    - rest passive SELL limit S = round(peg + 15bp, 4) = 1.0015
    - wick model : fill if (S<=open) or (high>=S)   [correct model for a resting maker]
      strict model: fill if close>=S                [ultra-conservative, no wick]
    - on fill -> realize sell at S*(1-adv/1e4), go FLAT, re-arm rebuy. NO stop-loss,
      NO time-based forced sell (USD1 always re-pegs -> holding is safe & earns interest).
  FLAT USDT (brief, 0% yield -> interest bleed):
    - INITIAL entry: on the very first bar, MARKET-buy 100% at open (idle USDT yields 0).
    - REBUY: rest passive BUY limit B = round(peg + 4bp, 4) = 1.0004
      wick model : fill if low<=B ;  strict model: fill if close<=B
    - IDLE-TIMEOUT GUARD: if FLAT for >= 2.0 days (576 5m bars) and B has not filled,
      cancel and MARKET-buy 100% at open (cap the bleed).
    - on fill -> go LONG.

ADVERSE SELECTION (applied to EVERY fill, both limit and market, per task):
    buy  effective = px * (1 + adv/1e4)
    sell effective = px * (1 - adv/1e4)
TICK: round limit prices to 4 decimals (1bp floor).
INTEREST: 10% APR while LONG, accrued per bar on qty*close (matches engine).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, ALLOC, BPD
import pandas as pd

PEG = 1.0
SELL_BP = 15      # sell_premium_bp        -> S = 1.0015
REBUY_BP = 4      # rebuy_level_bp         -> B = 1.0004
TIMEOUT_BARS = 576  # idle_timeout 2.0 days * 288 bars/day
MKT_VOL_USD1 = 2_538_200.0   # USD1USDT avg daily turnover (from data, matches engine)


def run_variant(sym, with_yield, adv, wick=True, enable_sell=True, apr=0.10):
    """One full event-driven pass. wick=True -> resting-maker fill (high/low touch);
       wick=False -> strict close-through (no wick exploitation).
       enable_sell=False -> pure buy&hold from bar 0 (benchmark)."""
    df = load(sym)
    ypb = (apr / 365 / BPD) if with_yield else 0.0
    cash = ALLOC; pos = None; accr = 0.0
    trades = []; eq = []; turn = 0.0
    inpos = 0; nbar = 0
    started = False; flat_bars = 0; max_flat = 0; max_hold = 0.0
    S = round(PEG + SELL_BP / 1e4, 4)      # 1.0015
    B = round(PEG + REBUY_BP / 1e4, 4)     # 1.0004

    for r in df.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close
        nbar += 1
        if pos:
            accr += pos["qty"] * c * ypb
            inpos += 1

        if pos is None:                         # ---- FLAT ----
            flat_bars += 1; max_flat = max(max_flat, flat_bars)
            buy_px = None
            if not started:
                buy_px = o                      # INITIAL market entry, bar 0
            else:
                hit = (l <= B) if wick else (c <= B)   # passive rebuy limit
                if hit:
                    buy_px = B
                if buy_px is None and flat_bars >= TIMEOUT_BARS:
                    buy_px = o                  # IDLE-TIMEOUT market buy
            if buy_px is not None:
                eff = buy_px * (1 + adv / 1e4)
                qty = cash / eff
                pos = dict(buy=eff, qty=qty, ft=r.ts)
                turn += cash; cash = 0.0
                started = True; flat_bars = 0
        else:                                   # ---- LONG ----
            if enable_sell:
                hit = ((S <= o) or (h >= S)) if wick else (c >= S)
                if hit:
                    f = S * (1 - adv / 1e4)
                    proc = pos["qty"] * f
                    hold_d = (r.ts - pos["ft"]) / 86400e3
                    max_hold = max(max_hold, hold_d)
                    trades.append(dict(hold_d=hold_d,
                                       price_bp=(f - pos["buy"]) / pos["buy"] * 1e4))
                    cash += proc + accr; turn += proc
                    pos = None; accr = 0.0; flat_bars = 0
        eq.append(cash + (pos["qty"] * c + accr if pos else 0))

    if pos:   # account final open hold duration
        max_hold = max(max_hold, (df.ts.iloc[-1] - pos["ft"]) / 86400e3)
    final = eq[-1]
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400e3
    tr = pd.DataFrame(trades)
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq) - peak) / peak).min()
    return dict(
        n=len(tr),
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 2),
        ret=round((final / ALLOC - 1) * 100, 3),
        mdd=round(dd * 100, 3),
        tim=round(inpos / nbar * 100, 1),
        turn_day=round(turn / span, 0),
        max_flat_d=round(max_flat / BPD, 2),
        max_hold_d=round(max_hold, 1),
        avg_hold_d=round(tr.hold_d.mean(), 2) if len(tr) else 0,
        win=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0,
        worst_bp=round(tr.price_bp.min(), 2) if len(tr) else 0,
        open_end=(pos is not None),
        span_d=round(span, 1),
    )


if __name__ == "__main__":
    SYM = "USD1USDT"; ADVS = [0, 0.5, 1.0, 1.5]
    print("=" * 78)
    print("VARIANT r1_7: PAAL extreme-premium skim (S=+15bp, B=+4bp, idle-timeout 2d)")
    print(f"  data span ~6.7mo, alloc ${ALLOC:.0f}, USD1 10% APR while long")
    print("  BENCHMARK: holding USD1 = 10.00% APR (flat bar)")
    print("=" * 78)

    bh = {a: run_variant(SYM, True, a, enable_sell=False) for a in ADVS}
    print("\n--- buy & hold (do-nothing) benchmark, total APR (interest+price drift) ---")
    for a in ADVS:
        print(f"   adv={a:<4} APR={bh[a]['apr']:>6.2f}%   (1 trade, TIM={bh[a]['tim']})")

    print("\n--- STRATEGY total APR (price + 10% interest), WICK maker-fill model ---")
    res = {a: run_variant(SYM, True, a, wick=True) for a in ADVS}
    print(f"   {'adv':>5} {'APR%':>7} {'vs10':>7} {'vsB&H':>7} {'n':>4} {'TIM':>6} "
          f"{'maxFlat_d':>10} {'maxHold_d':>10} {'turn/d$':>9} {'MDD%':>7}")
    for a in ADVS:
        x = res[a]
        print(f"   {a:>5} {x['apr']:>7.2f} {x['apr']-10:>+7.2f} {x['apr']-bh[a]['apr']:>+7.2f} "
              f"{x['n']:>4} {x['tim']:>6} {x['max_flat_d']:>10} {x['max_hold_d']:>10} "
              f"{x['turn_day']:>9.0f} {x['mdd']:>7.3f}")

    print("\n--- STRATEGY total APR, STRICT close-through (no-wick, ultra-conservative) ---")
    res_s = {a: run_variant(SYM, True, a, wick=False) for a in ADVS}
    for a in ADVS:
        x = res_s[a]
        print(f"   adv={a:<4} APR={x['apr']:>6.2f}%  vs10={x['apr']-10:>+5.2f}  "
              f"vsB&H={x['apr']-bh[a]['apr']:>+5.2f}  n={x['n']}")

    print("\n--- CAPACITY (2% of ${:,.0f}/day = ${:,.0f}/day cap) ---".format(
        MKT_VOL_USD1, 0.02 * MKT_VOL_USD1))
    x = res[0.5]
    pct = x['turn_day'] / MKT_VOL_USD1 * 100
    max_cap = ALLOC * (0.02 * MKT_VOL_USD1 / x['turn_day'])
    print(f"   @ $10k: turnover/day=${x['turn_day']:,.0f} = {pct:.3f}% of mkt")
    print(f"   max capital under 2% cap: ${max_cap:,.0f}")
    print(f"   max idle(FLAT) days: {x['max_flat_d']}   max single hold days: {x['max_hold_d']}")
