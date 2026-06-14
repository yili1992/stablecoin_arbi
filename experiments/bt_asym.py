"""
ASYMMETRIC-EXIT variant (per-rung grid harvester) for USD1USDT.
Angle: passive-buy dips on a ladder; scale-out / widen take-profit on up-moves;
keep a permanent USD1 CORE so carry (10% APR) never stops -> minimize idle USDT.

Reuses bt_faithful.load() for data. Fill model mirrors the faithful engine:
  buy fills if low<=line (+adverse haircut); sell fills if high>=line (-adverse haircut).
Absolute grid lines anchored on peg=1.0 (USD1 always re-pegs), no ema -> no lookahead.
Each rung does at most ONE transition per bar (conservative; no intrabar round-trip).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import pandas as pd, numpy as np
from bt_faithful import load, APR, ALLOC, BPD

PEG = 1.0000

def run_asym(sym, with_yield, adverse_bp,
             core_frac, R, buy0_bp, buy_step_bp, sell0_bp, sell_step_bp,
             verbose=False):
    df = load(sym)
    apr = APR[sym] if with_yield else 0.0
    ypb = apr/365/BPD
    adv = adverse_bp/1e4

    # ---- build rungs ----
    rung_dollar = (1-core_frac)*ALLOC / R if R>0 else 0.0
    buy_line  = [round(PEG - (buy0_bp + r*buy_step_bp)/1e4, 4) for r in range(R)]
    sell_line = [round(PEG + (sell0_bp + r*sell_step_bp)/1e4, 4) for r in range(R)]

    # ---- initial state: 100% in USD1 at first open (start invested) ----
    o0 = df.open.iloc[0]
    eff0 = o0*(1+adv)
    core_qty = core_frac*ALLOC / eff0
    held = [True]*R                      # rung state: True=hold USD1, False=in cash
    qty  = [rung_dollar/eff0 for _ in range(R)]   # USD1 qty per rung
    cash = 0.0
    turn = ALLOC                         # initial deployment counts as turnover once
    # idle tracking: per-rung bars spent EMPTY (in cash) + cash $-days idle
    rung_empty_bars = [0]*R
    max_empty_bars  = [0]*R
    cur_empty       = [0]*R
    cash_idle_dollardays = 0.0

    accr = 0.0
    eq = []
    n_buy = 0; n_sell = 0; gross_price_pnl = 0.0
    nbar = len(df); inpos_dollars = 0.0
    spanbar_ms = (df.ts.iloc[1]-df.ts.iloc[0])

    O=df.open.values; H=df.high.values; L=df.low.values; C=df.close.values
    for i in range(nbar):
        o,h,l,c = O[i],H[i],L[i],C[i]
        # interest accrues on all USD1 currently held (core + held rungs)
        usd1_qty = core_qty + sum(qty[r] for r in range(R) if held[r])
        accr += usd1_qty * c * ypb
        inpos_dollars += usd1_qty * c   # for time-in-market (value-weighted)

        for r in range(R):
            if held[r]:
                # resting SELL at sell_line[r]; fills if high>=line
                if h >= sell_line[r]:
                    f = sell_line[r]*(1-adv)
                    proc = qty[r]*f
                    cash += proc; turn += proc
                    gross_price_pnl += proc - rung_dollar  # vs notional in
                    held[r] = False; qty[r] = 0.0; n_sell += 1
                    cur_empty[r] = 0
            else:
                # in cash for this rung -> resting BUY at buy_line[r]; fills if low<=line
                cur_empty[r] += 1
                if l <= buy_line[r]:
                    eff = buy_line[r]*(1+adv)
                    cost = rung_dollar
                    if cash + 1e-9 >= cost:
                        cash -= cost; turn += cost
                        qty[r] = cost/eff; held[r] = True; n_buy += 1
                        max_empty_bars[r] = max(max_empty_bars[r], cur_empty[r])
                        rung_empty_bars[r] += cur_empty[r]; cur_empty[r] = 0
        # idle cash carrying (cash that is NOT earmarked... here all cash is idle USDT)
        cash_idle_dollardays += cash * (spanbar_ms/86400_000)
        eq.append(cash + core_qty*c + sum(qty[r]*c for r in range(R) if held[r]) + accr)

    final = eq[-1]
    span = (df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    eqs = pd.Series(eq); peak=eqs.cummax(); mdd=((eqs-peak)/peak).min()
    # value-weighted time-in-market = avg fraction of equity in USD1
    avg_usd1_value = inpos_dollars/nbar
    tim = avg_usd1_value/ALLOC*100
    turn_per_day = turn/span
    # worst stuck: max bars any rung sat in cash (idle USDT)
    max_stuck_d = max(max_empty_bars)*spanbar_ms/86400_000 if R else 0
    return dict(sym=sym, adv=adverse_bp,
                apr_pct=round((final/ALLOC-1)*100*365/span,2),
                ret_pct=round((final/ALLOC-1)*100,3),
                mdd_pct=round(mdd*100,3),
                tim_pct=round(tim,1),
                n_buy=n_buy, n_sell=n_sell,
                turn_per_day=round(turn_per_day,0),
                max_stuck_d=round(max_stuck_d,1),
                core_frac=core_frac, R=R)

def sweep():
    print("=== ASYM grid sweep on USD1USDT (full strategy: price+interest), benchmark hold=10% ===")
    print("params: core_frac / R / buy0 / buy_step / sell0 / sell_step (bp)")
    hdr=f"{'core':>5}{'R':>3}{'b0':>4}{'bs':>4}{'s0':>4}{'ss':>4} | {'adv0':>7}{'adv0.5':>8}{'adv1.0':>8}{'adv1.5':>8}{'adv2.0':>8} | {'tim%':>6}{'turn/d':>9}{'stuckD':>7}{'MDD%':>7}"
    print(hdr); print('-'*len(hdr))
    configs = []
    # core_frac, R, buy0, buy_step, sell0, sell_step
    for core in [0.0,0.3,0.5,0.6,0.7]:
        for R in [4,6,8]:
            for (b0,bs,s0,ss) in [(2,2,2,2),(3,2,3,2),(3,2,4,3),(2,1.5,3,2),(3,3,5,3)]:
                configs.append((core,R,b0,bs,s0,ss))
    rows=[]
    for (core,R,b0,bs,s0,ss) in configs:
        res={a:run_asym('USD1USDT',True,a,core,R,b0,bs,s0,ss) for a in [0,0.5,1,1.5,2]}
        r05=res[0.5]
        rows.append((core,R,b0,bs,s0,ss,res))
        flag = ' <== WIN@adv0.5' if res[0.5]['apr_pct']>10 and res[1.0]['apr_pct']>10 else (' <- adv0.5 ok' if res[0.5]['apr_pct']>10 else '')
        print(f"{core:>5}{R:>3}{b0:>4}{bs:>4}{s0:>4}{ss:>4} | "
              f"{res[0]['apr_pct']:>7.2f}{res[0.5]['apr_pct']:>8.2f}{res[1.0]['apr_pct']:>8.2f}"
              f"{res[1.5]['apr_pct']:>8.2f}{res[2.0]['apr_pct']:>8.2f} | "
              f"{r05['tim_pct']:>6.1f}{r05['turn_per_day']:>9.0f}{r05['max_stuck_d']:>7.1f}{r05['mdd_pct']:>7.2f}{flag}")
    return rows

if __name__=="__main__":
    sweep()

def focused():
    print("\n=== PURE HOLD benchmark in-engine (core=1.0, R=0): interest + endpoint drift ===")
    for a in [0,0.5,1,2]:
        h=run_asym('USD1USDT',True,a,1.0,0,0,0,0,0)
        print(f"  adv={a}: APR={h['apr_pct']:.2f}  tim={h['tim_pct']:.1f}")
    hold = run_asym('USD1USDT',True,0.5,1.0,0,0,0,0,0)['apr_pct']
    print(f"  -> honest hold bar (adv irrelevant) = {hold:.2f}%\n")

    print("=== FINE sweep: high core, shallow frequent buys, scaled wide sells ===")
    hdr=f"{'core':>5}{'R':>3}{'b0':>4}{'bs':>5}{'s0':>4}{'ss':>4} | {'adv0':>7}{'adv0.5':>8}{'adv1.0':>8}{'adv1.5':>8}{'adv2.0':>8} | {'tim%':>6}{'turn/d':>8}{'stuckD':>7}{'dHOLD05':>8}"
    print(hdr); print('-'*len(hdr))
    best=None
    for core in [0.70,0.75,0.80,0.85]:
        for R in [4,5,6,8]:
            for (b0,bs,s0,ss) in [(2,1,3,2),(2,1,4,2),(2,1.5,3,2),(2,1.5,4,3),(3,2,4,3),(3,2,5,3),(2,1,4,3),(3,3,6,4)]:
                res={a:run_asym('USD1USDT',True,a,core,R,b0,bs,s0,ss) for a in [0,0.5,1,1.5,2]}
                r=res[0.5]
                margin=res[0.5]['apr_pct']-hold
                win = res[0.5]['apr_pct']>10 and res[1.0]['apr_pct']>10
                cand=(res[1.0]['apr_pct'],core,R,b0,bs,s0,ss,res)
                if win and (best is None or cand[0]>best[0]): best=cand
                flag=' WIN' if win else ''
                flag+=(' >HOLD' if margin>0 else '')
                print(f"{core:>5}{R:>3}{b0:>4}{bs:>5}{s0:>4}{ss:>4} | "
                      f"{res[0]['apr_pct']:>7.2f}{res[0.5]['apr_pct']:>8.2f}{res[1.0]['apr_pct']:>8.2f}"
                      f"{res[1.5]['apr_pct']:>8.2f}{res[2.0]['apr_pct']:>8.2f} | "
                      f"{r['tim_pct']:>6.1f}{r['turn_per_day']:>8.0f}{r['max_stuck_d']:>7.1f}{margin:>+8.2f}{flag}")
    if best:
        _,core,R,b0,bs,s0,ss,res=best
        print(f"\nBEST(by adv1.0): core={core} R={R} buy0={b0} buy_step={bs} sell0={s0} sell_step={ss}")
        for a in [0,0.5,1,1.5,2]:
            x=res[a]; print(f"  adv={a}: APR={x['apr_pct']:.2f} tim={x['tim_pct']:.1f} turn/d=${x['turn_per_day']:.0f} stuck={x['max_stuck_d']}d MDD={x['mdd_pct']} nbuy={x['n_buy']} nsell={x['n_sell']}")
