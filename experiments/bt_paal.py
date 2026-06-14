"""
PAAL = Peg-Anchored Asymmetric Ladder (default-long).
Reuses bt_faithful.load() (same EMAs/trend/no-lookahead merge) and the SAME
fill + adverse + interest mechanics, so results are directly comparable.

State machine (single full position, like the base engine):
  LONG (holding USD1, earning 10% APR on qty*price each bar):
    - sell limit S = round(max(peg + Ps_bp/1e4, buy + m_bp/1e4), 4)   (only skim premiums)
    - if Ps_bp is None -> NEVER sell (pure buy & hold)
    - fill: passive sell at S if high>=S (capped at S, conservative); go FLAT, record last_sell
  FLAT (holding USDT, 0% yield -> bleed):
    - buy limit B = round(peg + Pb_bp/1e4, 4); optionally gated <= last_sell - gap
    - fill: passive buy at B if low<=B; go LONG
    - idle timeout: if flat for > T_bars, MARKET buy at open (cap the interest bleed)
  Start: FLAT with cash=ALLOC; first eligible bar buys (dip-buy entry).

Adverse: buy eff = px*(1+adv/1e4); sell f = px*(1-adv/1e4).  tickSize: round(.,4).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, ALLOC, BPD
import pandas as pd, numpy as np

PEG = 1.0

def run_paal(sym, with_yield, adv, Ps_bp, Pb_bp, m_bp=0.0, T_days=None,
             rebuy_gap_bp=None, entry_dip_bp=None, apr=0.10):
    """entry_dip_bp: first buy only fills at/below peg+entry_dip_bp (else None=buy bar0 market).
       rebuy_gap_bp: after a sell, rebuy limit also capped at last_sell - rebuy_gap_bp."""
    df = load(sym)
    ypb = (apr/365/BPD) if with_yield else 0.0
    cash = ALLOC; pos = None; accr = 0.0; trades = []; eq = []
    turn = 0.0; inpos = 0; nbar = 0
    flat_bars = 0; last_sell = None; max_flat = 0
    T_bars = int(T_days*BPD) if T_days is not None else None
    started = False
    for r in df.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close
        nbar += 1
        if pos:
            accr += pos["qty"]*c*ypb; inpos += 1
        if pos is None:
            flat_bars += 1; max_flat = max(max_flat, flat_bars)
            # buy limit
            B = PEG + Pb_bp/1e4
            if rebuy_gap_bp is not None and last_sell is not None:
                B = min(B, last_sell - rebuy_gap_bp/1e4)
            B = round(B, 4)
            buy_px = None
            if not started and entry_dip_bp is not None:
                # initial dip-buy: only fill at/below peg+entry_dip_bp
                Lim = round(PEG + entry_dip_bp/1e4, 4)
                if l <= Lim:
                    buy_px = Lim if Lim <= o else (Lim if l <= Lim else None)
            elif not started:
                buy_px = o  # market buy bar0
            else:
                # normal rebuy
                if l <= B:
                    buy_px = B
                if buy_px is None and T_bars is not None and flat_bars >= T_bars:
                    buy_px = o  # forced market re-entry to stop bleed
            if buy_px is not None:
                eff = buy_px*(1+adv/1e4); notional = cash; qty = notional/eff
                pos = dict(buy=eff, qty=qty, ft=r.ts)
                turn += notional; cash = 0.0; started = True; flat_bars = 0
        else:
            buy = pos["buy"]; now = r.ts
            if Ps_bp is None:
                S = None  # never sell (buy&hold)
            else:
                S = round(max(PEG + Ps_bp/1e4, buy + m_bp/1e4), 4)
            if S is not None:
                # passive sell, capped at S (conservative)
                filled = (S <= o) or (h >= S)
                if filled:
                    px = S
                    f = px*(1-adv/1e4); proc = pos["qty"]*f
                    trades.append(dict(hold_d=(now-pos["ft"])/86400e3,
                                       pnl=proc-pos["qty"]*buy+accr, price_bp=(f-buy)/buy*1e4))
                    cash += proc+accr; turn += proc; last_sell = px
                    pos = None; accr = 0.0; flat_bars = 0
        eq.append(cash + (pos["qty"]*c+accr if pos else 0))
    final = eq[-1]; span = (df.ts.iloc[-1]-df.ts.iloc[0])/86400e3
    tr = pd.DataFrame(trades)
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq)-peak)/peak).min()
    return dict(n=len(tr),
                apr=round((final/ALLOC-1)*100*365/span, 2),
                ret=round((final/ALLOC-1)*100, 3),
                mdd=round(dd*100, 3),
                tim=round(inpos/nbar*100, 1),
                turn_day=round(turn/span, 0),
                max_flat_d=round(max_flat/BPD, 2),
                avg_hold_d=round(tr.hold_d.mean(), 2) if len(tr) else 0,
                win=round((tr.price_bp > 0).mean()*100, 1) if len(tr) else 0,
                open_end=(pos is not None))

if __name__ == "__main__":
    print("PAAL grid search on USD1USDT (full strategy: price + 10% UTA interest)")
    print("benchmark: hold = 10.00%   |   buy&hold(engine) = 10.27%\n")
    advs = [0, 0.5, 1, 2]
    print("=== A) pure buy & hold (Ps=None) ===")
    for adv in advs:
        x = run_paal("USD1USDT", True, adv, None, 0)
        print(f"  adv={adv}: APR={x['apr']:.2f} TIM={x['tim']} n={x['n']} turn/d=${x['turn_day']:.0f}")

    print("\n=== B) asymmetric ladder grid (sell premium Ps / rebuy Pb / timeout T) ===")
    print(f"{'Ps':>4}{'Pb':>4}{'m':>3}{'T_d':>5} | {'APR@0':>7}{'@0.5':>7}{'@1':>7}{'@2':>7} | {'n':>4}{'TIM':>6}{'maxFlat':>8}{'turn/d':>9}")
    grids = []
    for Ps in [6, 8, 10, 12, 15]:
        for Pb in [-2, 0, 2]:
            for T in [0.25, 0.5, 1, 3, None]:
                grids.append((Ps, Pb, 0, T))
    rows = []
    for Ps, Pb, m, T in grids:
        res = {a: run_paal("USD1USDT", True, a, Ps, Pb, m, T) for a in advs}
        r05 = res[0.5]
        rows.append((Ps, Pb, m, T, res))
    # sort by APR@0.5 desc
    rows.sort(key=lambda x: -x[4][0.5]['apr'])
    for Ps, Pb, m, T, res in rows[:20]:
        Ts = f"{T}" if T is not None else "inf"
        b = res[0.5]
        print(f"{Ps:>4}{Pb:>4}{m:>3}{Ts:>5} | "
              f"{res[0]['apr']:>7.2f}{res[0.5]['apr']:>7.2f}{res[1]['apr']:>7.2f}{res[2]['apr']:>7.2f} | "
              f"{b['n']:>4}{b['tim']:>6}{b['max_flat_d']:>8}{b['turn_day']:>9.0f}")
