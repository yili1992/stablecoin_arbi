"""Robustness: correct anchor, sub-period stability, max-USDT-duration, capacity.
WINNER candidates (ema21, rebuy@ema-1):
  A) equal     5/7/10/14/20  .20x5
  B) hi-weight 5/7/10/14/20  .15/.18/.22/.22/.23
  C) 4-rung    6/9/13/18     .25x4
"""
import os, sys, pandas as pd, numpy as np
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt
from bt_ladder3 import run, hold, ALLOC, BPD

A=[(5,.2),(7,.2),(10,.2),(14,.2),(20,.2)]
B=[(5,.15),(7,.18),(10,.22),(14,.22),(20,.23)]
C=[(6,.25),(9,.25),(13,.25),(18,.25)]

def hold_window(df, ypb):
    qty=ALLOC/df.open.iloc[0]; accr=0.0; eq=[]
    for r in df.itertuples(): accr+=qty*r.close*ypb; eq.append(accr+qty*r.close)
    span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    return round((eq[-1]/ALLOC-1)*100*365/span,2)

def run_window(df, rungs, adv, rp=-1, anchor="ema21_1h", apr=0.10):
    """Same engine as bt_ladder3.run but on a pre-sliced df window."""
    ypb=apr/365/BPD; anc=df[anchor].values
    o=df.open.values;h=df.high.values;l=df.low.values;c=df.close.values;tsv=df.ts.values
    eff0=o[0]*(1+adv/1e4)
    sl=[dict(sp=sp,frac=fr,state='usd1',qty=fr*ALLOC/eff0,cash=0.0,sell_px=0.0,t=0) for sp,fr in rungs]
    accr=0.0;eq=[];n=len(c);maxw=0
    for i in range(n):
        a=anc[i];oi,hi,li,ci=o[i],h[i],l[i],c[i]
        for s in sl:
            if s['state']=='usd1':
                accr+=s['qty']*ci*ypb; R=round(a+s['sp']/1e4,4)
                if (R<=oi) or (hi>=R):
                    f=R*(1-adv/1e4);s['cash']=s['qty']*f;s['sell_px']=f;s['qty']=0.0;s['state']='usdt';s['t']=i
            else:
                B_=round(a+rp/1e4,4)
                if li<=B_:
                    f=B_*(1+adv/1e4);nq=s['cash']/f;s['qty']=nq;s['cash']=0.0;s['state']='usd1'
                    maxw=max(maxw,(i-s['t'])/12/24)  # max USDT hold in days
        eq.append(accr+sum((s['qty']*ci if s['state']=='usd1' else s['cash']) for s in sl))
    span=(tsv[-1]-tsv[0])/86400_000
    return round((eq[-1]/ALLOC-1)*100*365/span,2), maxw

df=bt.load("USD1USDT"); ypb=0.10/365/BPD
N=len(df); h1=df.iloc[:N//2].reset_index(drop=True); h2=df.iloc[N//2:].reset_index(drop=True)
print("=== SUB-PERIOD STABILITY (APR), rebuy@ema21-1bp ===")
print(f"{'window':<14}{'hold':>7}{'A.eq':>8}{'B.hiW':>8}{'C.4rg':>8}   (adv=0.5)   then adv=1.0")
for tag,d in [("FULL(201d)",df),("H1(100d)",h1),("H2(100d)",h2)]:
    hh=hold_window(d,ypb)
    a05=run_window(d,A,0.5)[0]; b05=run_window(d,B,0.5)[0]; c05=run_window(d,C,0.5)[0]
    a10=run_window(d,A,1.0)[0]; b10=run_window(d,B,1.0)[0]; c10=run_window(d,C,1.0)[0]
    print(f"{tag:<14}{hh:>7}{a05:>8}{b05:>8}{c05:>8}     | adv1: A{a10} B{b10} C{c10}")

print("\n=== max single USDT hold (days) over full period — EMA anchor must prevent multi-week stuck ===")
for nm,rg in [("A.equal",A),("B.hiWeight",B),("C.4rung",C)]:
    _,mw=run_window(df,rg,0.5); print(f"  {nm:<12} max USDT hold = {mw:.2f} days")

print("\n=== FULL adv sweep (correct ema21 anchor) ===")
HOLD={a:hold(a) for a in [0,0.5,1,1.5,2]}
print(f"{'cfg':<10}"+"".join(f"adv{a:>5}" for a in [0,0.5,1,1.5,2]))
for nm,rg in [("hold",None),("A.equal",A),("B.hiWeight",B),("C.4rung",C)]:
    if rg is None: print(f"{'hold':<10}"+"".join(f"{HOLD[a]:>8}" for a in [0,0.5,1,1.5,2])); continue
    s=f"{nm:<10}"
    for a in [0,0.5,1,1.5,2]:
        x=run(rg,adv=a,rp=-1,anchor="ema21_1h"); w="*" if x['apr']>HOLD[a] else " "
        s+=f"{x['apr']:>7}{w}"
    print(s)
# capacity + turnover + price-only for chosen B
print("\n=== CHOSEN = B (hi-weight) diagnostics, ema21 rb-1 ===")
for a in [0.5,1,2]:
    x=run(B,adv=a,rp=-1,anchor="ema21_1h")
    po=run(B,adv=a,rp=-1,anchor="ema21_1h",with_yield=False)['apr']
    print(f"  adv={a}: APR={x['apr']} (price-only {po}%) USDT={x['usdt_pct']}% MDD={x['mdd']}% "
          f"sells={x['sells']} turn/d=${x['turn_d']:,.0f} maxcap=${10000*0.02*2_538_200/x['turn_d']:,.0f}")
