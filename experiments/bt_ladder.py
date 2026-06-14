"""
SELL-SIDE LADDER variant. Home base = USD1 (long, earning 10% APR).
Scale OUT of USD1 across rising premium rungs; rebuy (scale back in) near peg.
Reuses bt_faithful.load() (same no-lookahead merge, same fill/adverse conventions).

Model:
  - At bar 0, deploy 100% into USD1 (buy at open[0], adverse haircut). Same as hold.
  - Position split into N independent slices. Slice k:
        sell rung r_k (premium above peg), fraction f_k of ALLOC, rebuy level b_k.
  - Each slice is a 2-state machine: 'usd1' (holds qty_k USD1) <-> 'usdt' (holds cash_k).
        usd1: sell limit at r_k. fills if (r_k<=open) or (high>=r_k). recv r_k*(1-adv/1e4).
        usdt: rebuy limit at b_k. fills if low<=b_k. pay b_k*(1+adv/1e4).
  - Interest (10% APR) accrues ONLY on slices in 'usd1' state: qty_k*close*ypb per bar.
  - Conservative: at most ONE transition per slice per bar (no free intra-bar round trip).
  - Slice value compounds (rebuy cheaper -> more qty next cycle).

Fill/adverse conventions copied verbatim from bt_faithful (sell at limit even on gap-through;
buy fills on low<=L at L; +adv haircut on buy, -adv haircut on sell).
"""
import pandas as pd, numpy as np, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt

ALLOC = bt.ALLOC; BPD = bt.BPD

def run_ladder(rungs, adv=0.0, with_yield=True, apr=0.10, sym="USD1USDT"):
    """rungs = list of (sell_premium_bp, fraction, rebuy_premium_bp).
       rebuy_premium_bp can be negative (below peg). peg=1.0000."""
    df = bt.load(sym); ypb = (apr/365/BPD) if with_yield else 0.0
    o0 = df.open.iloc[0]
    eff0 = o0*(1+adv/1e4)
    slices = []
    for (sp, frac, rp) in rungs:
        r = round(1.0 + sp/1e4, 4); b = round(1.0 + rp/1e4, 4)
        qty = (frac*ALLOC)/eff0
        slices.append(dict(r=r, b=b, frac=frac, state='usd1', qty=qty, cash=0.0))
    accr = 0.0; turn = ALLOC; eq = []; nbar = 0
    usdt_bars = 0.0  # fraction-weighted time slices spend in USDT
    sells = 0; rebuys = 0
    realized_capture = 0.0  # $ price pnl booked on rebuys (capture per completed leg)
    for r in df.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close
        nbar += 1
        usdt_frac_now = 0.0
        for s in slices:
            if s['state'] == 'usd1':
                accr += s['qty']*c*ypb
                # try sell at rung
                if (s['r'] <= o) or (h >= s['r']):
                    f = s['r']*(1-adv/1e4)
                    s['cash'] = s['qty']*f; s['sell_px']=f; s['qty']=0.0
                    s['state'] = 'usdt'; turn += s['cash']; sells += 1
            else:  # usdt -> try rebuy
                usdt_frac_now += s['frac']
                if l <= s['b']:
                    f = s['b']*(1+adv/1e4)
                    newqty = s['cash']/f
                    realized_capture += (s['sell_px']-f)*newqty
                    s['qty'] = newqty; s['cash']=0.0
                    s['state']='usd1'; turn += newqty*f; rebuys += 1
        usdt_bars += usdt_frac_now
        eqv = accr + sum((s['qty']*c if s['state']=='usd1' else s['cash']) for s in slices)
        eq.append(eqv)
    final = eq[-1]; span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    peak=pd.Series(eq).cummax(); dd=((pd.Series(eq)-peak)/peak).min()
    n_in_usdt_end = sum(1 for s in slices if s['state']=='usdt')
    return dict(adv=adv, ret_pct=round((final/ALLOC-1)*100,3),
                apr_pct=round((final/ALLOC-1)*100*365/span,2),
                mdd_pct=round(dd*100,4), turn_per_day=turn/span,
                sells=sells, rebuys=rebuys,
                usdt_time_pct=round(usdt_bars/nbar*100,2),  # frac-weighted idle USDT
                slices_usdt_end=n_in_usdt_end, span_d=round(span,1),
                price_capture_pct=round(realized_capture/ALLOC*100,3))

def run_hold(adv=0.0, apr=0.10, sym="USD1USDT"):
    df = bt.load(sym); ypb=apr/365/BPD; o0=df.open.iloc[0]; eff0=o0*(1+adv/1e4)
    qty=ALLOC/eff0; accr=0.0; eq=[]
    for r in df.itertuples():
        accr += qty*r.close*ypb
        eq.append(accr + qty*r.close)
    final=eq[-1]; span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    return dict(apr_pct=round((final/ALLOC-1)*100*365/span,2), ret_pct=round((final/ALLOC-1)*100,3))

DEFAULT = [(3,0.30,0),(5,0.25,0),(8,0.25,0),(12,0.20,0)]

if __name__=="__main__":
    import json
    print("HOLD benchmark (buy USD1 @start, never sell, accrue 10% APR):")
    for a in [0,0.5,1,1.5,2]:
        print(f"  adv={a}: APR={run_hold(a)['apr_pct']}%  ret={run_hold(a)['ret_pct']}%")
    print("\nSELL-SIDE LADDER default rungs (+3/30%, +5/25%, +8/25%, +12/20%), rebuy@peg:")
    print(f"  {'adv':>5}{'APR%':>9}{'ret%':>9}{'priceCap%':>11}{'sells':>7}{'rebuys':>8}{'USDTtime%':>10}{'turn/day':>11}{'MDD%':>9}")
    for a in [0,0.5,1,1.5,2]:
        x=run_ladder(DEFAULT, adv=a)
        print(f"  {a:>5}{x['apr_pct']:>9}{x['ret_pct']:>9}{x['price_capture_pct']:>11}{x['sells']:>7}{x['rebuys']:>8}{x['usdt_time_pct']:>10}{x['turn_per_day']:>11,.0f}{x['mdd_pct']:>9}")
    print(f"\nspan_d={run_ladder(DEFAULT,0.5)['span_d']}  capacity: keep turn/day < 2% of $2.538M = $50,764/day")
