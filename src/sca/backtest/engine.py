"""
FAITHFUL port of the user's Freqtrade ArbiStrategy (buy-low-sell-high), event-driven.
Every rule mirrors a specific callback in the pasted strategy — no idealization.

ENTRY (confirm_trade_entry + custom_entry_price):
  - only enter when  market - ema55_1h < 0.0001   (else 拒单)
  - limit buy = min(market, ema55_1h);  if ema21_downtrend: -= 0.0001 ; round(4)
EXIT (custom_exit_price + confirm_trade_exit):
  - target = buy + (0.0002 if ema55_uptrend else 0.0001)
  - if now > fill_day+2d (midnight): cap target at buy + 0.0001
  - if market < buy: target = market
  - if buy - target >= 0.00019: place NO sell order
  - confirm gate: profit>=0.00019 -> SELL ; elif now<2d -> HOLD ;
                  elif now>3d and rate<=buy -> SELL ; elif rate<=buy -> HOLD ; else SELL
  - stoploss = -0.99 (none); position_adjustment = False (no DCA, single position)

No-lookahead: decisions use open[i] as live market + ema/trend from already-CLOSED 1h candle.
Fill: buy fills if low[i]<=L (passive); sell fills if high[i]>=S (or at market when S<=open).
"""
import pandas as pd, numpy as np, os

from sca.config import DATA_DIR as _DD, CFG as _CFG
DATA = str(_DD)
APR = _CFG.get("baseline", {}).get("apr", {"USD1USDT":0.10,"USDEUSDT":0.035,"USDTBUSDT":0.035})
ALLOC = float(_CFG.get("backtest", {}).get("alloc_usd", 10000)); BPD = int(_CFG.get("market", {}).get("bars_per_day_5m", 288))
TW = int(_CFG.get("baseline", {}).get("trend_window", 7)); MID = float(_CFG.get("baseline", {}).get("mid_trend_threshold", 0.015))

def load(sym):
    d5=pd.read_csv(f"{DATA}/{sym}_5m.csv"); d1=pd.read_csv(f"{DATA}/{sym}_1h.csv")
    for c in ["ts","open","high","low","close","volume"]:
        if c in d5: d5[c]=pd.to_numeric(d5[c])
    for c in ["ts","close"]: d1[c]=pd.to_numeric(d1[c])
    d5=d5.sort_values("ts").reset_index(drop=True); d1=d1.sort_values("ts").reset_index(drop=True)
    for n in (21,55,100): d1[f"ema{n}"]=d1["close"].ewm(span=n,adjust=False).mean()
    d1["avail_ts"]=d1["ts"]+3600_000   # 1h usable only after it closes
    m=pd.merge_asof(d5, d1[["avail_ts","ema21","ema55","ema100"]].rename(
        columns={"ema21":"ema21_1h","ema55":"ema55_1h","ema100":"ema100_1h"}),
        left_on="ts", right_on="avail_ts", direction="backward")
    # trend flags computed on the merged 5m frame, exactly as the strategy does
    for k in (21,55):
        e=m[f"ema{k}_1h"]
        trend=e.diff(); chg=e.pct_change(TW)
        m[f"ema{k}_down"]=(trend.rolling(TW).mean()<0)|(chg.rolling(TW).sum()<-MID)
        m[f"ema{k}_up"]=(trend.rolling(TW).mean()>0)|(chg.rolling(TW).sum()>MID)
    return m.dropna(subset=["ema55_1h"]).reset_index(drop=True)

def midnight_after(ts_ms, days): return (ts_ms//86400_000 + days)*86400_000

def run(sym, with_yield, adverse_bp=0.0):
    df=load(sym); apr=APR[sym] if with_yield else 0.0; ypb=apr/365/BPD
    cash=ALLOC; pos=None; accr=0.0; trades=[]; eq=[]; turn=0.0; inpos=0; nbar=0
    for r in df.itertuples():
        o,h,l,c=r.open,r.high,r.low,r.close; ema=r.ema55_1h
        nbar+=1
        if pos: accr+=pos["qty"]*c*ypb; inpos+=1
        if pos is None:
            proposed=o
            if proposed-ema < 0.0001:                       # confirm_trade_entry gate
                L = ema if proposed>ema else proposed        # custom_entry_price
                if r.ema21_down: L-=0.0001
                L=round(L,4)
                if l<=L:                                     # passive buy fills (+adverse haircut)
                    eff=L*(1+adverse_bp/1e4)
                    qty=ALLOC/eff; pos=dict(buy=eff,qty=qty,ft=r.ts); cash-=ALLOC; turn+=ALLOC
        else:
            buy=pos["buy"]; now=r.ts
            base = buy+0.0002 if r.ema55_up else buy+0.0001  # custom_exit_price
            if now>midnight_after(pos["ft"],2) and base>buy+0.0001: base=buy+0.0001
            if buy>o: base=o
            S = None if (buy-base>=0.00019) else round(base,4)
            if S is not None:
                if S-buy>=0.00019: allow=True                # confirm_trade_exit
                elif now<midnight_after(pos["ft"],2): allow=False
                elif now>midnight_after(pos["ft"],3) and S<=buy: allow=True
                elif S<=buy: allow=False
                else: allow=True
                if allow:
                    filled = (S,True) if S<=o else ((S,True) if h>=S else (None,False))
                    if filled[1]:
                        f=filled[0]*(1-adverse_bp/1e4); proc=pos["qty"]*f   # sell -adverse haircut
                        trades.append(dict(hold_d=(now-pos["ft"])/86400_000,
                                           pnl=proc-ALLOC+accr, price_bp=(f-buy)/buy*1e4))
                        cash+=proc+accr; turn+=proc; pos=None; accr=0.0
        eq.append(cash + (pos["qty"]*c+accr if pos else 0))
    final=eq[-1]; span=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    tr=pd.DataFrame(trades)
    peak=pd.Series(eq).cummax(); dd=((pd.Series(eq)-peak)/peak).min()
    return dict(sym=sym, span_d=round(span,1), n=len(tr),
                win=round((tr.price_bp>0).mean()*100,1) if len(tr) else 0,
                avg_hold_d=round(tr.hold_d.mean(),2) if len(tr) else 0,
                avg_price_bp=round(tr.price_bp.mean(),2) if len(tr) else 0,
                ret_pct=round((final/ALLOC-1)*100,3),
                apr_pct=round((final/ALLOC-1)*100*365/span,2),
                mdd_pct=round(dd*100,3),
                turn_per_day=turn/span,
                max_hold_d=round(tr.hold_d.max(),1) if len(tr) else 0,
                n_loss=int((tr.price_bp<0).sum()) if len(tr) else 0,
                worst_bp=round(tr.price_bp.min(),2) if len(tr) else 0,
                pct_stuck_gt2d=round((tr.hold_d>2).mean()*100,1) if len(tr) else 0,
                open_at_end=(pos is not None),
                tim_pct=round(inpos/nbar*100,1))

MKT_VOL = _CFG.get("market", {}).get("market_volume_per_day", {"USD1USDT":2_538_200,"USDEUSDT":17_278_905,"USDTBUSDT":878_295})

if __name__=="__main__":
    print("FAITHFUL backtest of your Freqtrade ArbiStrategy (buy-low-sell-high)")
    print(f"data: ~6.7 months 5m, alloc ${ALLOC:.0f}/symbol, single position, no stop")
    print("hold-only benchmark: USD1=10%  USDe=3.5%  USDtb=3.5%\n")

    print("=== PRICE-ONLY APR (pure 低买高卖), swept over adverse-selection per fill ===")
    print(f"{'symbol':<11}{'adv=0':>8}{'adv=0.5':>9}{'adv=1.0':>9}{'adv=1.5':>9}{'adv=2.0':>9}{'trades':>8}{'avgPx_bp':>9}")
    for s in APR:
        po=[run(s,False,a)['apr_pct'] for a in [0,0.5,1,1.5,2]]
        base=run(s,False,0)
        print(f"{s:<11}"+"".join(f"{x:>{9 if i else 8}.2f}" for i,x in enumerate(po))+f"{base['n']:>8}{base['avg_price_bp']:>9}")

    print("\n=== FULL STRATEGY APR (低买高卖 + UTA持有利息), swept over adverse-selection ===")
    print(f"{'symbol':<11}{'hold%':>7}{'adv=0':>8}{'adv=0.5':>9}{'adv=1.0':>9}{'adv=1.5':>9}{'adv=2.0':>9}{'MDD%':>8}")
    for s in APR:
        ty=[run(s,True,a)['apr_pct'] for a in [0,0.5,1,1.5,2]]
        mdd=run(s,True,1.0)['mdd_pct']
        print(f"{s:<11}{APR[s]*100:>7.1f}"+"".join(f"{x:>{9 if i else 8}.2f}" for i,x in enumerate(ty))+f"{mdd:>8}")

    print("\n=== CAPACITY (@ $10k alloc): strategy turnover as % of market daily volume ===")
    for s in APR:
        r=run(s,False,0); pct=r['turn_per_day']/MKT_VOL[s]*100
        cap=ALLOC*min(1,0.02*MKT_VOL[s]/r['turn_per_day'])
        print(f"  {s:<11} turnover/day=${r['turn_per_day']:>10,.0f} = {pct:>4.1f}% of mkt  -> stay <2%: size <= ${cap:>8,.0f}")

    print("\nNOTE: adv=0 is the PERFECT-FILL ceiling (front-of-queue, no adverse selection) — not real.")
    print("Real thin-book stablecoin maker adverse-selection is ~1-2bp -> read adv=1.0~2.0 columns.")
