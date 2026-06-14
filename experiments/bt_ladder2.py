"""Sell-side ladder v2: tight matched rebuy + TIME-STOP on USDT idle.
Home=USD1 (10% APR). Scale out at premium rungs; rebuy at sell-spread (limit);
if a slice sits in USDT > max_usdt_h, force-rebuy at market next bar (cap interest bleed).
Reuses bt_faithful.load(); fill/adverse conventions copied verbatim."""
import pandas as pd, numpy as np, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt
ALLOC=bt.ALLOC; BPD=bt.BPD

def run(rungs, adv=0.0, with_yield=True, apr=0.10, max_usdt_h=None, sym="USD1USDT",
        collect=False):
    """rungs=[(sell_bp, frac, rebuy_bp)] (rebuy_bp absolute premium, peg=1.0).
       max_usdt_h: force market-rebuy after this many hours in USDT (None=off)."""
    df=bt.load(sym); ypb=(apr/365/BPD) if with_yield else 0.0
    o0=df.open.iloc[0]; eff0=o0*(1+adv/1e4)
    maxbars = None if max_usdt_h is None else max_usdt_h*12  # 5m bars/hour=12
    sl=[]
    for (sp,fr,rp) in rungs:
        sl.append(dict(r=round(1+sp/1e4,4), b=round(1+rp/1e4,4), frac=fr,
                       state='usd1', qty=fr*ALLOC/eff0, cash=0.0, sell_px=0.0, t_sold=0))
    accr=0.0; turn=ALLOC; eq=[]; nbar=0; usdt_bars=0.0
    sells=rebuys=tstops=0; cycles=[]; cap_sum=0.0; cap_loss=0.0
    for i,row in enumerate(df.itertuples()):
        o,h,l,c=row.open,row.high,row.low,row.close; nbar+=1; uf=0.0
        for s in sl:
            if s['state']=='usd1':
                accr+=s['qty']*c*ypb
                if (s['r']<=o) or (h>=s['r']):
                    f=s['r']*(1-adv/1e4); s['cash']=s['qty']*f; s['sell_px']=f
                    s['qty']=0.0; s['state']='usdt'; s['t_sold']=i
                    turn+=s['cash']; sells+=1
            else:
                uf+=s['frac']
                hit = l<=s['b']
                forced = (maxbars is not None) and (i-s['t_sold']>=maxbars)
                if hit or forced:
                    if hit and not (forced and s['b']<l):  # limit fill at b
                        f=s['b']*(1+adv/1e4)
                    else:                                   # market force-rebuy at open
                        f=o*(1+adv/1e4); tstops+=1
                    nq=s['cash']/f; pnl=(s['sell_px']-f)*nq
                    cap_sum+=pnl;  cap_loss += pnl if pnl<0 else 0.0
                    if collect: cycles.append(((i-s['t_sold'])/12, (s['sell_px']-f)/f*1e4))
                    s['qty']=nq; s['cash']=0.0; s['state']='usd1'; turn+=nq*f; rebuys+=1
        usdt_bars+=uf
        eq.append(accr+sum((s['qty']*c if s['state']=='usd1' else s['cash']) for s in sl))
    final=eq[-1]; span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    peak=pd.Series(eq).cummax(); dd=((pd.Series(eq)-peak)/peak).min()
    out=dict(adv=adv, apr=round((final/ALLOC-1)*100*365/span,2),
             ret=round((final/ALLOC-1)*100,3), cap=round(cap_sum/ALLOC*100,3),
             cap_loss=round(cap_loss/ALLOC*100,3), mdd=round(dd*100,4),
             turn_d=turn/span, sells=sells, rebuys=rebuys, tstops=tstops,
             usdt_pct=round(usdt_bars/nbar*100,2), span=round(span,1))
    if collect:
        cy=pd.DataFrame(cycles, columns=['usdt_d','net_bp'])
        out['cyc']=cy
    return out

def hold(adv=0.0, apr=0.10, sym="USD1USDT"):
    df=bt.load(sym); ypb=apr/365/BPD; qty=ALLOC/(df.open.iloc[0]*(1+adv/1e4)); accr=0.0; eq=[]
    for r in df.itertuples(): accr+=qty*r.close*ypb; eq.append(accr+qty*r.close)
    span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    return round((eq[-1]/ALLOC-1)*100*365/span,2)

if __name__=="__main__":
    # First: measure cycle USDT-duration distribution (no time stop) to calibrate
    base=[(3,.30,1),(5,.25,3),(8,.25,6),(12,.20,10)]  # spread2
    r=run(base, adv=0.5, max_usdt_h=None, collect=True); cy=r['cyc']
    print(f"baseline spread2 no-stop: {len(cy)} cycles. USDT duration (days):")
    print(f"  median={cy.usdt_d.median():.3f}  p75={cy.usdt_d.quantile(.75):.3f}  p90={cy.usdt_d.quantile(.9):.3f}  p95={cy.usdt_d.quantile(.95):.3f}  max={cy.usdt_d.max():.2f}  mean={cy.usdt_d.mean():.3f}")
    print(f"  frac cycles with usdt>1d: {(cy.usdt_d>1).mean()*100:.1f}%   >0.5d: {(cy.usdt_d>0.5).mean()*100:.1f}%   >0.25d: {(cy.usdt_d>0.25).mean()*100:.1f}%")
    print(f"  net_bp per cycle: median={cy.net_bp.median():.2f} mean={cy.net_bp.mean():.2f}")
