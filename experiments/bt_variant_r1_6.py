"""
VARIANT r1_6 — EMA-Anchored Sell-Side Take-Profit Ladder (home = USD1).
Standalone backtest. Reuses bt_faithful.load() (same no-lookahead 1h-EMA merge:
1h EMA usable only after candle close, +3,600,000ms) and the SAME fill / adverse
conventions copied verbatim from bt_faithful.

STRATEGY (exact spec):
  Home base = 100% LONG USD1 (earns 10% APR UTA interest while held). USDT is a
  transient state right after a take-profit fill (earns 0%).
  - INITIAL DEPLOY: at t0 buy all capital USDT->USD1 at open[0] (adverse haircut applied).
    All 5 slices start in 'usd1'.
  - LADDER (exit): position split into 5 independent slices. Slice k rests a sell limit
    L_sell_k = round(ema21_1h + rung_bp[k]/1e4, 4), rung_bp=[5,7,10,14,20].
    Fills (maker) when L_sell_k<=open (gap) or high>=L_sell_k; recv = L_sell_k*(1-adv/1e4).
    On fill: slice -> 'usdt', arms its rebuy.
  - RE-ENTRY (entry): a slice in 'usdt' rests a single buy limit
    L_buy = round(ema21_1h - 0.0001, 4)  (1bp BELOW the EMA-21 anchor).
    Fills (maker) when low<=L_buy; pay = L_buy*(1+adv/1e4). On fill: slice -> 'usd1'.
  - Re-price ALL orders every 5m bar as the EMA floats. NO stop-loss, NO time-stop.
  - Sizing: fractions of NAV [0.15,0.18,0.20,0.22,0.25] for rungs [+5,+7,+10,+14,+20] (sum=1).

ACCOUNTING (faithful to bt_faithful / bt_ladder3 conventions):
  - 10% APR interest accrues ONLY on slices in 'usd1': qty_k*close*ypb per 5m bar
    (ypb = 0.10/365/288). Interest accumulates in a separate bucket (not reinvested into
    slices -> conservative; matches bt_faithful where interest is not re-deployed).
  - Per-slice PRICE capture DOES compound (sell high -> rebuy low -> more USD1 units).
  - Conservative: at most ONE state transition per slice per bar (no free intra-bar
    round trip; a slice that just sold cannot rebuy in the same bar, and vice-versa).
  - adverse haircut on EVERY fill incl. initial deploy: buy=L*(1+adv/1e4), sell=L*(1-adv/1e4).
"""
import pandas as pd, numpy as np, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt

ALLOC = bt.ALLOC          # 10000.0
BPD = bt.BPD              # 288 5m-bars/day
APR_USD1 = 0.10           # UTA interest while holding USD1
SYM = "USD1USDT"
ANCHOR = "ema21_1h"
RUNG_BP = [5, 7, 10, 14, 20]
FRACS   = [0.15, 0.18, 0.20, 0.22, 0.25]
REBUY_OFF_BP = -1         # L_buy = ema - 1bp
MKT_VOL = bt.MKT_VOL[SYM] # 2,538,200 USD1/day ADV
CAP_FRAC = 0.02           # stay < 2% of ADV


def run(adv=0.0, with_yield=True, apr=APR_USD1, anchor=ANCHOR,
        rung_bp=RUNG_BP, fracs=FRACS, rebuy_off=REBUY_OFF_BP, sym=SYM):
    assert abs(sum(fracs) - 1.0) < 1e-9, "fractions must sum to 1.0"
    df = bt.load(sym)
    ypb = (apr / 365 / BPD) if with_yield else 0.0
    anc = df[anchor].values
    o = df.open.values; h = df.high.values; l = df.low.values; c = df.close.values
    tsv = df.ts.values; n = len(c)

    # INITIAL DEPLOY at open[0] (adverse haircut applied to every fill)
    eff0 = o[0] * (1 + adv / 1e4)
    sl = [dict(rb=rb, frac=fr, state='usd1', qty=fr * ALLOC / eff0,
               cash=0.0, sell_px=0.0, t=0)
          for rb, fr in zip(rung_bp, fracs)]

    accr = 0.0                 # interest bucket (separate; not reinvested -> conservative)
    turn = ALLOC               # initial deploy notional counts toward turnover
    eq = []
    usdt_val_bars = 0.0        # sum over bars of (USDT value) -> value-weighted USDT time
    tot_val_bars = 0.0
    sells = rebuys = 0
    realized_capture = 0.0     # $ price pnl booked at rebuy (genuine trading edge)
    max_stuck_bars = 0
    dwell_days_list = []

    for i in range(n):
        a = anc[i]; oi, hi, li, ci = o[i], h[i], l[i], c[i]
        usdt_val = 0.0; tot_val = 0.0
        for s in sl:
            if s['state'] == 'usd1':
                accr += s['qty'] * ci * ypb
                R = round(a + s['rb'] / 1e4, 4)                 # sell rung, floats w/ EMA
                if (R <= oi) or (hi >= R):                       # gap-through or touched
                    f = R * (1 - adv / 1e4)
                    s['cash'] = s['qty'] * f; s['sell_px'] = f
                    s['qty'] = 0.0; s['state'] = 'usdt'; s['t'] = i
                    turn += s['cash']; sells += 1
            else:                                                # 'usdt' -> rest rebuy
                B = round(a + rebuy_off / 1e4, 4)                # ema - 1bp, floats w/ EMA
                if li <= B:                                      # maker fill at B
                    f = B * (1 + adv / 1e4)
                    nq = s['cash'] / f
                    realized_capture += (s['sell_px'] - f) * nq  # sell_px(net) - buy(net)
                    dwell = i - s['t']
                    max_stuck_bars = max(max_stuck_bars, dwell)
                    dwell_days_list.append(dwell / BPD)
                    s['qty'] = nq; s['cash'] = 0.0; s['state'] = 'usd1'
                    turn += nq * f; rebuys += 1
            # value snapshot (use post-transition state)
            v = (s['qty'] * ci) if s['state'] == 'usd1' else s['cash']
            tot_val += v
            if s['state'] == 'usdt':
                usdt_val += v
        usdt_val_bars += usdt_val
        tot_val_bars += tot_val
        eq.append(accr + tot_val)

    # slices still stuck in USDT at the very end count toward max-dwell
    for s in sl:
        if s['state'] == 'usdt':
            dwell = (n - 1) - s['t']
            max_stuck_bars = max(max_stuck_bars, dwell)
            dwell_days_list.append(dwell / BPD)

    final = eq[-1]
    span = (tsv[-1] - tsv[0]) / 86400_000
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq) - peak) / peak).min()
    return dict(
        adv=adv,
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        ret=round((final / ALLOC - 1) * 100, 3),
        price_cap_pct=round(realized_capture / ALLOC * 100, 3),
        mdd=round(dd * 100, 4),
        turn_per_day=turn / span,
        sells=sells, rebuys=rebuys,
        usdt_time_pct=round(usdt_val_bars / tot_val_bars * 100, 3),
        max_stuck_days=round(max_stuck_bars / BPD, 3),
        slices_usdt_end=sum(1 for s in sl if s['state'] == 'usdt'),
        span_d=round(span, 1), n=n)


def hold(adv=0.0, with_yield=True, apr=APR_USD1, sym=SYM):
    """Realized buy-and-hold USD1 benchmark with SAME entry haircut + 10% interest."""
    df = bt.load(sym); ypb = (apr / 365 / BPD) if with_yield else 0.0
    eff0 = df.open.iloc[0] * (1 + adv / 1e4)
    qty = ALLOC / eff0; accr = 0.0; eq = []
    for r in df.itertuples():
        accr += qty * r.close * ypb
        eq.append(accr + qty * r.close)
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400_000
    return round((eq[-1] / ALLOC - 1) * 100 * 365 / span, 3)


if __name__ == "__main__":
    import json
    ADVS = [0, 0.5, 1.0, 1.5]
    print("=" * 84)
    print("VARIANT r1_6 — EMA-21 Anchored Sell-Side Take-Profit Ladder (home=USD1, 10% APR)")
    print(f"data: {SYM} 5m+1h | rungs(bp)={RUNG_BP} fracs={FRACS} rebuy=ema{REBUY_OFF_BP}bp | anchor={ANCHOR}")
    print("=" * 84)

    base = run(0.0)
    span = base['span_d']
    print(f"span={span}d  bars={base['n']}  benchmark: flat-hold=10.000%  |  "
          f"realized-hold APR (w/ entry haircut + interest):")
    print("   adv:    " + "".join(f"{a:>10}" for a in ADVS))
    print("  hold:    " + "".join(f"{hold(a):>10.3f}" for a in ADVS))

    print("\n--- TOTAL APR (price + 10% interest) ---")
    print("   adv:    " + "".join(f"{a:>10}" for a in ADVS))
    tot = {a: run(a) for a in ADVS}
    print(" strat:    " + "".join(f"{tot[a]['apr']:>10.3f}" for a in ADVS))
    print(" vs10%:    " + "".join(f"{tot[a]['apr']-10:>+10.3f}" for a in ADVS))
    print(" vsHold:   " + "".join(f"{tot[a]['apr']-hold(a):>+10.3f}" for a in ADVS))

    print("\n--- PRICE-ONLY APR (interest OFF -> is the trading edge real?) ---")
    print("   adv:    " + "".join(f"{a:>10}" for a in ADVS))
    po = {a: run(a, with_yield=False) for a in ADVS}
    print(" strat:    " + "".join(f"{po[a]['apr']:>10.3f}" for a in ADVS))
    print(" hold:     " + "".join(f"{hold(a, with_yield=False):>10.3f}" for a in ADVS))
    print(" edge:     " + "".join(f"{po[a]['apr']-hold(a, with_yield=False):>+10.3f}" for a in ADVS))

    print("\n--- MECHANICS (@adv=0.5) ---")
    x = tot[0.5]
    print(f"  sells={x['sells']}  rebuys={x['rebuys']}  USDT-time={x['usdt_time_pct']}%  "
          f"max-stuck={x['max_stuck_days']}d  slices-stuck-end={x['slices_usdt_end']}")
    print(f"  realized price-capture={x['price_cap_pct']}%  MDD={tot[1.0]['mdd']}%")

    print("\n--- CAPACITY (@adv=0.5, $10k alloc) ---")
    tpd = x['turn_per_day']; pct = tpd / MKT_VOL * 100
    cap = ALLOC * (CAP_FRAC * MKT_VOL / tpd)   # max alloc keeping turnover < 2% of ADV
    print(f"  turnover/day=${tpd:,.0f} = {pct:.3f}% of ${MKT_VOL:,}/day ADV  "
          f"-> stay <2%: size <= ${cap:,.0f}")

    print("\n--- SUMMARY JSON ---")
    out = dict(
        variant="r1_6 EMA21-anchored sell-side TP ladder",
        apr_adv0=float(tot[0.0]['apr']), apr_adv05=float(tot[0.5]['apr']),
        apr_adv10=float(tot[1.0]['apr']), apr_adv15=float(tot[1.5]['apr']),
        hold_adv05=float(hold(0.5)), hold_adv10=float(hold(1.0)),
        price_only_adv05=float(po[0.5]['apr']), price_only_adv10=float(po[1.0]['apr']),
        beats_10pct_adv05=bool(tot[0.5]['apr'] > 10.0),
        beats_hold_adv05=bool(tot[0.5]['apr'] > hold(0.5)),
        max_stuck_days=float(x['max_stuck_days']),
        turn_per_day=round(float(tpd), 1), capacity_pct=round(float(pct), 3),
        capacity_ok=bool(pct < 2.0), mdd_pct=float(tot[1.0]['mdd']))
    print(json.dumps(out, indent=2))
