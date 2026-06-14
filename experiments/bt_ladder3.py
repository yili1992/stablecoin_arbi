"""Sell-side ladder v3: EMA-ANCHORED rungs. Home=USD1 (10% APR).
Rungs float with ema55_1h (no-lookahead, from last closed 1h candle, via bt_faithful.load).
  sell rung k  : ema + sp_k bp   (scale OUT into local premium)
  rebuy level  : ema + rp   bp   (scale back IN at local mean -> always fills -> tiny USDT time)
Because the whole ladder floats with the regime, a sell during a sustained premium regime
still rebuys quickly (EMA rises with it) instead of getting stuck for weeks at a fixed peg.
Fill/adverse conventions copied verbatim from bt_faithful."""
import pandas as pd, numpy as np, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt
ALLOC=bt.ALLOC; BPD=bt.BPD

def run(rungs, adv=0.0, with_yield=True, apr=0.10, rp=0.0, anchor="ema55_1h",
        max_usdt_h=None, sym="USD1USDT", collect=False):
    """rungs=[(sell_bp_above_anchor, frac)]; rp=rebuy bp offset from anchor (<=0 typical).
       max_usdt_h: optional force market-rebuy after this many hours (None=off)."""
    df=bt.load(sym); ypb=(apr/365/BPD) if with_yield else 0.0
    anc=df[anchor].values
    o=df.open.values; h=df.high.values; l=df.low.values; c=df.close.values; tsv=df.ts.values
    eff0=o[0]*(1+adv/1e4)
    maxbars=None if max_usdt_h is None else max_usdt_h*12
    sl=[dict(sp=sp, frac=fr, state='usd1', qty=fr*ALLOC/eff0, cash=0.0, sell_px=0.0, t=0)
        for (sp,fr) in rungs]
    accr=0.0; turn=ALLOC; eq=[]; n=len(c); usdt_bars=0.0
    sells=rebuys=tstops=0; cap_sum=0.0; cap_loss=0.0; cyc=[]
    for i in range(n):
        a=anc[i]; oi,hi,li,ci=o[i],h[i],l[i],c[i]; uf=0.0
        for s in sl:
            if s['state']=='usd1':
                accr+=s['qty']*ci*ypb
                R=round(a+s['sp']/1e4,4)
                if (R<=oi) or (hi>=R):
                    f=R*(1-adv/1e4); s['cash']=s['qty']*f; s['sell_px']=f
                    s['qty']=0.0; s['state']='usdt'; s['t']=i; turn+=s['cash']; sells+=1
            else:
                uf+=s['frac']; B=round(a+rp/1e4,4)
                hit=li<=B; forced=(maxbars is not None) and (i-s['t']>=maxbars)
                if hit or forced:
                    if hit: f=B*(1+adv/1e4)
                    else:   f=oi*(1+adv/1e4); tstops+=1
                    nq=s['cash']/f; pnl=(s['sell_px']-f)*nq
                    cap_sum+=pnl; cap_loss+=pnl if pnl<0 else 0.0
                    if collect: cyc.append(((i-s['t'])/12,(s['sell_px']-f)/f*1e4,s['sp']))
                    s['qty']=nq; s['cash']=0.0; s['state']='usd1'; turn+=nq*f; rebuys+=1
        usdt_bars+=uf
        eq.append(accr+sum((s['qty']*ci if s['state']=='usd1' else s['cash']) for s in sl))
    final=eq[-1]; span=(tsv[-1]-tsv[0])/86400_000
    peak=pd.Series(eq).cummax(); dd=((pd.Series(eq)-peak)/peak).min()
    out=dict(adv=adv, apr=round((final/ALLOC-1)*100*365/span,2),
             ret=round((final/ALLOC-1)*100,3), cap=round(cap_sum/ALLOC*100,3),
             cap_loss=round(cap_loss/ALLOC*100,3), mdd=round(dd*100,4),
             turn_d=turn/span, sells=sells, rebuys=rebuys, tstops=tstops,
             usdt_pct=round(usdt_bars/n*100,2), span=round(span,1),
             open_usdt=sum(1 for s in sl if s['state']=='usdt'))
    if collect: out['cyc']=pd.DataFrame(cyc,columns=['usdt_d','net_bp','sp'])
    return out

def hold(adv=0.0, apr=0.10, sym="USD1USDT"):
    df=bt.load(sym); ypb=apr/365/BPD; qty=ALLOC/(df.open.iloc[0]*(1+adv/1e4)); accr=0.0; eq=[]
    for r in df.itertuples(): accr+=qty*r.close*ypb; eq.append(accr+qty*r.close)
    span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    return round((eq[-1]/ALLOC-1)*100*365/span,2)

if __name__=="__main__":
    HOLD={a:hold(a) for a in [0,0.5,1,1.5,2]}; print("HOLD APR:",HOLD,"\n")
    print(f"{'config':<50}{'adv0':>7}{'adv.5':>7}{'adv1':>7}{'adv2':>7}{'cap%':>7}{'closs':>7}{'USDT%':>7}{'sel':>5}{'turn/d':>8}")
    def show(name,rungs,rp,anchor="ema55_1h",ts=None):
        row=f"{name:<50}"; d={}
        for a in [0,0.5,1,1.5,2]:
            x=run(rungs,adv=a,rp=rp,anchor=anchor,max_usdt_h=ts); d[a]=x
            if a in(0,0.5,1,2):
                w="*" if x['apr']>HOLD[a] else " "; row+=f"{x['apr']:>6}{w}"
        x=d[0.5]; row+=f"{x['cap']:>7}{x['cap_loss']:>7}{x['usdt_pct']:>7}{x['sells']:>5}{x['turn_d']:>8,.0f}"
        print(row)
    base=[(3,.30),(5,.25),(8,.25),(12,.20)]
    for rp in [0,-1,-2,-3]:
        show(f"ema55 +3/5/8/12 .30/.25/.25/.20 rebuy@ema{rp:+d}", base, rp)
    print()
    for rp in [0,-1,-2]:
        show(f"ema21 +3/5/8/12 .30/.25/.25/.20 rebuy@ema{rp:+d}", base, rp, "ema21_1h")
