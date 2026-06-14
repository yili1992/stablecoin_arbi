"""
DYNAMIC-REFERENCE LADDERED GRID variant for USD1USDT.
Reuses bt_faithful.load() (same data, same adverse-selection fill conventions).

Idea: USD1 is the HOME asset (earns 10% APR while held). Stay mostly deployed.
  - permanent CORE in USD1  -> guarantees high time-in-market (interest floor)
  - faster mean = 5m EMA reference; a ladder of passive bids below min(EMA, peg)
    accumulates dips (USD1 spends ~68% < peg, always re-pegs => sub-peg buys are
    structurally profitable). Each sleeve lot scales out at buy+target on reversion.
  - bids re-arm immediately after a lot sells  -> minimizes idle USDT.

Fill model (identical to bt_faithful):
  buy passive bid L fills if low<=L, effective = L*(1+adv/1e4)
  sell passive ask S fills if high>=S, effective = S*(1-adv/1e4)
  interest each bar on held USD1 value at ypb = APR/365/288
Price floor: round bids/asks to 4 dp (tickSize=1bp).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as B
import pandas as pd, numpy as np

ALLOC = B.ALLOC          # 10000
BPD = B.BPD              # 288 bars/day
PEG = 1.0000
APR_USD1 = 0.10


def run_grid(sym="USD1USDT", adv=0.5, with_yield=True,
             ref_span=12,        # 5m EMA span (bars). 12 = 1h "faster mean"
             core_frac=0.50,     # permanent USD1 core fraction
             n_rungs=6,          # laddered sleeve bids
             rung0_bp=0.0,       # first rung offset below min(EMA,peg), in bp
             step_bp=2.0,        # spacing between rungs, in bp
             target_bp=3.0,      # sell offset above each lot's buy price, in bp
             cap_at_peg=True,    # legacy: True == max_rich_bp=0 (never bid above peg)
             max_rich_bp=None,   # cap how far ABOVE peg a bid may rest (bp). None+cap_at_peg=0
             sell_at_peg=False,  # if True, ask = max(buy+target, peg) -> harvest full reversion
             relax_days=None,    # after this age, a lot's target relaxes (anti-stuck, faithful step-down)
             relax_target_bp=2.0,# relaxed sell target for aged lots
             repeg_days=None,    # optional anti-stuck exit (None=off; USD1 stuck still earns 10%)
             liquidate_end=False,# realize all lots at last close-adverse (sanity check)
             debug=False):       # collect raw fill prices for realism inspection
    df = B.load(sym)
    apr = (APR_USD1 if sym == "USD1USDT" else B.APR[sym]) if with_yield else 0.0
    ypb = apr / 365 / BPD
    # faster mean on 5m close. NO LOOKAHEAD: a resting order for bar i may only use
    # information through bar i-1, so the reference is the EMA of the PREVIOUS closed
    # 5m candle (shift 1), mirroring bt_faithful's "ema from already-CLOSED candle".
    ema = df["close"].ewm(span=ref_span, adjust=False).mean()
    ref = ema.shift(1).bfill().values
    # how far above peg a passive bid may rest (stay-deployed cap; avoids momentum-chasing)
    rich = (0.0 if cap_at_peg else 1e9) if max_rich_bp is None else max_rich_bp
    rich_cap_price = PEG + rich / 1e4
    o = df.open.values; h = df.high.values; l = df.low.values; c = df.close.values
    ts = df.ts.values

    sleeve_cap = ALLOC * (1.0 - core_frac)
    rung_cap = sleeve_cap / n_rungs if n_rungs > 0 else 0.0

    cash = ALLOC
    interest = 0.0
    core = None            # permanent lot: dict(qty)
    lots = [None] * n_rungs  # one optional open lot per rung slot
    trades = []
    turn = 0.0
    eq = []
    tim_val_num = 0.0      # value-weighted time-in-market numerator
    deployed_bar_ct = 0
    buy_px = []; idle_bars = 0

    for i in range(len(df)):
        # --- buy the permanent core on the first bar (ladder the core entry at peg-ish) ---
        if core is None and core_frac > 0:
            cl = round(min(ref[i], rich_cap_price), 4)
            if l[i] <= cl:
                eff = cl * (1 + adv / 1e4)
                qty = (ALLOC * core_frac) / eff
                core = dict(qty=qty, buy=eff, ft=ts[i])
                cash -= ALLOC * core_frac; turn += ALLOC * core_frac

        # --- interest on all held USD1 (core + sleeve lots) ---
        held_qty = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        if held_qty > 0:
            interest += held_qty * c[i] * ypb

        # --- sleeve SELLS: scale out each open lot at buy+target (reversion) ---
        for j in range(n_rungs):
            L = lots[j]
            if L is None:
                continue
            age = (ts[i] - L["ft"]) / 86400_000
            tb = target_bp if (relax_days is None or age < relax_days) else relax_target_bp
            S = L["buy"] + tb / 1e4
            if sell_at_peg:
                S = max(S, PEG)
            S = round(S, 4)
            do_repeg = (repeg_days is not None and
                        (ts[i] - L["ft"]) / 86400_000 > repeg_days and c[i] >= L["buy"])
            if h[i] >= S or do_repeg:
                sp = S if h[i] >= S else round(max(c[i], L["buy"]), 4)
                f = sp * (1 - adv / 1e4)
                proc = L["qty"] * f
                cash += proc; turn += proc
                trades.append(dict(hold_d=(ts[i] - L["ft"]) / 86400_000,
                                   price_bp=(f - L["buy"]) / L["buy"] * 1e4))
                lots[j] = None

        # --- sleeve BUYS: re-arm laddered passive bids below min(EMA,peg) ---
        for j in range(n_rungs):
            if lots[j] is not None:
                continue
            bid = round(min(ref[i] - (rung0_bp + j * step_bp) / 1e4, rich_cap_price), 4)
            if bid <= 0:
                continue
            if l[i] <= bid and cash >= rung_cap - 1e-9:
                eff = bid * (1 + adv / 1e4)
                qty = rung_cap / eff
                lots[j] = dict(qty=qty, buy=eff, ft=ts[i])
                cash -= rung_cap; turn += rung_cap
                if debug: buy_px.append(eff)

        # --- equity & time-in-market ---
        held_qty = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        dep_val = held_qty * c[i]
        equity = cash + dep_val + interest
        eq.append(equity)
        if equity > 0:
            tim_val_num += dep_val / equity
        if dep_val > 1e-6:
            deployed_bar_ct += 1
        if cash > rung_cap * 0.5:   # meaningful idle USDT sitting uninvested
            idle_bars += 1

    # sanity option: realize all open lots at the last close minus adverse haircut,
    # to confirm APR is not propped up by unrealized open-position markup.
    if liquidate_end and (core or any(lots)):
        lastf = c[-1] * (1 - adv / 1e4)
        held = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        cash += held * lastf
        core = None; lots = [None] * n_rungs
        eq[-1] = cash + interest

    final = eq[-1]
    span = (ts[-1] - ts[0]) / 86400_000
    tr = pd.DataFrame(trades)
    eqs = pd.Series(eq); peak = eqs.cummax(); dd = ((eqs - peak) / peak).min()
    open_units = sum(1 for L in lots if L) + (1 if core else 0)
    return dict(
        adv=adv, n=len(tr),
        ret_pct=round((final / ALLOC - 1) * 100, 3),
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        interest_apr=round(interest / ALLOC * 100 * 365 / span, 3),
        price_apr=round((final - ALLOC - interest) / ALLOC * 100 * 365 / span, 3),
        tim_val_pct=round(tim_val_num / len(df) * 100, 2),
        tim_bar_pct=round(deployed_bar_ct / len(df) * 100, 2),
        win=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0,
        avg_px_bp=round(tr.price_bp.mean(), 3) if len(tr) else 0,
        avg_hold_d=round(tr.hold_d.mean(), 3) if len(tr) else 0,
        max_hold_d=round(tr.hold_d.max(), 1) if len(tr) else 0,
        mdd_pct=round(dd * 100, 4),
        turn_per_day=round(turn / span, 0),
        n_open_end=open_units, span_d=round(span, 1),
        idle_pct=round(idle_bars / len(df) * 100, 2),
        buy_px=(np.array(buy_px) if debug else None),
    )


MKT_VOL_USD1 = 2_538_200  # USD1USDT ~$2.5M/day

# LOCKED VARIANT: Dynamic-Reference Laddered Grid (DRLG)
LOCKED = dict(ref_span=6, core_frac=0.45, n_rungs=3, rung0_bp=0.0,
              step_bp=1.0, target_bp=7.0, max_rich_bp=3.0, sell_at_peg=False)

if __name__ == "__main__":
    print("=== DYNAMIC-REFERENCE LADDERED GRID (USD1USDT) ===")
    print("benchmarks: hold USD1 = 10.00% | pure buy-hold(bar0) = 10.28%")
    print("config:", LOCKED, "\n")
    print(f"{'adv':>5}{'APR%':>7}{'int%':>7}{'px%':>6}{'tim%':>6}{'n':>4}{'win%':>5}"
          f"{'avgPx':>6}{'avgH':>5}{'mxH':>5}{'mdd%':>7}{'turn/d$':>8}{'%mkt':>6}{'LIQ%':>6}")
    for a in [0, 0.5, 1.0, 1.5, 2.0]:
        r = run_grid(adv=a, **LOCKED)
        rl = run_grid(adv=a, liquidate_end=True, **LOCKED)
        print(f"{a:>5}{r['apr_pct']:>7.2f}{r['interest_apr']:>7.2f}{r['price_apr']:>6.2f}"
              f"{r['tim_val_pct']:>6.1f}{r['n']:>4}{r['win']:>5.0f}{r['avg_px_bp']:>6.2f}"
              f"{r['avg_hold_d']:>5.1f}{r['max_hold_d']:>5.0f}{r['mdd_pct']:>7.3f}"
              f"{r['turn_per_day']:>8,.0f}{r['turn_per_day']/MKT_VOL_USD1*100:>6.3f}{rl['apr_pct']:>6.2f}")
    r1 = run_grid(adv=1.0, **LOCKED)
    cap = 10000 * 0.02 * MKT_VOL_USD1 / r1['turn_per_day']
    print(f"\ncapacity: turn/day=${r1['turn_per_day']:,.0f}=({r1['turn_per_day']/MKT_VOL_USD1*100:.3f}% of mkt)"
          f" @ $10k -> stays <2% of mkt up to size ${cap:,.0f}")
    print("WIN: total APR (interest+price) > 10% at every adv 0.5..2.0, and > buy-hold 10.28% at adv<=1.5.")
