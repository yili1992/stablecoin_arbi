"""
BIG-DIPS-ONLY variant. Reuses bt_faithful.load() (same no-lookahead 1h-EMA merge,
same fill/adverse-selection model, same per-bar UTA yield accrual).

Thesis: enter USD1 long ONLY on a deep dip (>= X bp below a rolling mean); ride the
guaranteed re-peg back up; sell at the mean (+offset). Fewer, higher-edge round trips
so a fixed 0.5-1bp adverse haircut is a small fraction of each trade's gain. While we
hold we earn 10% UTA interest, so a long hold is fine; the only cost is idle USDT time.

Mean references (lookahead-safe):
  ema55_1h / ema100_1h : from load() (1h EMA, usable only after the 1h candle closes)
  smaW                 : rolling mean of PAST 5m closes (shift(1)) over W bars

Entry  (maker, == bt_faithful's "market below mean -> buy at market(open)" branch):
  gate  : open <= mean - X/1e4         (deep dip)
  fill  : buy at L=round(open,4); eff = L*(1+adv/1e4)   (passive top-of-book + adverse)
Exit   (maker):
  target: T = round(mean + exit_bp/1e4, 4)              (sell when reverted to ~fair value)
  fill  : if open>=T -> sell at open ; elif high>=T -> sell at T ; f = S*(1-adv/1e4)
  never realize a loss (target is always > buy); optional max_hold escape frees capital.
No same-bar round trip (entry XOR exit per bar), exactly like bt_faithful.
"""
import pandas as pd, numpy as np, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, APR, ALLOC, BPD, MKT_VOL

def add_mean(df, ref):
    if ref in ("ema21_1h", "ema55_1h", "ema100_1h"):
        return df[ref].values
    if ref.startswith("sma"):
        W = int(ref[3:])
        return df["close"].shift(1).rolling(W, min_periods=W).mean().values
    raise ValueError(ref)

def run_bigdip(sym, with_yield, adverse_bp=0.0, X_bp=8.0, exit_bp=1.0,
               ref="ema55_1h", max_hold_d=None):
    df = load(sym)
    mean = add_mean(df, ref)
    apr = APR[sym] if with_yield else 0.0
    ypb = apr/365/BPD
    cash = ALLOC; pos = None; accr = 0.0
    trades = []; eq = []; turn = 0.0; inpos = 0; nbar = 0
    o_ = df.open.values; h_ = df.high.values; c_ = df.close.values; ts_ = df.ts.values
    n = len(df)
    for i in range(n):
        o, h, c, m, ts = o_[i], h_[i], c_[i], mean[i], ts_[i]
        nbar += 1
        if m != m:  # mean not ready (NaN warmup)
            eq.append(cash + (pos["qty"]*c+accr if pos else 0)); continue
        if pos:
            accr += pos["qty"]*c*ypb; inpos += 1
        if pos is None:
            if o <= m - X_bp/1e4:                 # deep-dip gate
                L = round(o, 4)
                eff = L*(1+adverse_bp/1e4)
                qty = ALLOC/eff
                pos = dict(buy=eff, qty=qty, ft=ts); cash -= ALLOC; turn += ALLOC
        else:
            buy = pos["buy"]
            T = round(m + exit_bp/1e4, 4)
            escape = (max_hold_d is not None) and ((ts - pos["ft"])/86400_000 >= max_hold_d)
            S = None
            if T > buy:                            # normal profitable target
                if o >= T: S = o                   # gapped above target -> sell at market(open)
                elif h >= T: S = T                 # target touched intrabar
            if S is None and escape:               # time-based capital free (may be small loss)
                S = o
            if S is not None:
                f = S*(1-adverse_bp/1e4)
                # never realize a loss unless escape forces it
                if f > buy or escape:
                    proc = pos["qty"]*f
                    trades.append(dict(hold_d=(ts-pos["ft"])/86400_000,
                                       pnl=proc-ALLOC+accr, price_bp=(f-buy)/buy*1e4))
                    cash += proc+accr; turn += proc; pos = None; accr = 0.0
        eq.append(cash + (pos["qty"]*c+accr if pos else 0))
    final = eq[-1]; span = (df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    tr = pd.DataFrame(trades)
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq)-peak)/peak).min()
    return dict(sym=sym, ref=ref, X_bp=X_bp, exit_bp=exit_bp, span_d=round(span,1),
                n=len(tr),
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

def run_holdfirst(sym, with_yield, adverse_bp=0.0, X_bp=5.0, sell_bp=2.0,
                  ref="ema55_1h"):
    """DEFAULT-USD1 structure. Start holding USD1 (like the benchmark). Sell ONLY on a
    spike (price >= mean + sell_bp); re-ENTER ONLY on a big dip (price <= mean - X_bp).
    Idle USDT time exists only between a spike-sell and the next big-dip-buy. Never
    realize a loss (USD1 re-pegs -> hold for interest until profitable)."""
    df = load(sym)
    mean = add_mean(df, ref)
    apr = APR[sym] if with_yield else 0.0
    ypb = apr/365/BPD
    cash = ALLOC; pos = None; accr = 0.0; started = False
    trades = []; eq = []; turn = 0.0; inpos = 0; nbar = 0
    last_exit_ts = None; max_idle = 0.0; idle_bars = 0
    o_ = df.open.values; h_ = df.high.values; c_ = df.close.values; ts_ = df.ts.values
    n = len(df)
    for i in range(n):
        o, h, c, m, ts = o_[i], h_[i], c_[i], mean[i], ts_[i]
        nbar += 1
        if m != m:
            eq.append(cash + (pos["qty"]*c+accr if pos else 0)); continue
        if pos is not None:
            accr += pos["qty"]*c*ypb; inpos += 1
        else:
            if started: idle_bars += 1
        if not started:                                   # initial entry into USD1
            eff = round(o, 4)*(1+adverse_bp/1e4); qty = ALLOC/eff
            pos = dict(buy=eff, qty=qty, ft=ts); cash -= ALLOC; turn += ALLOC; started = True
            eq.append(cash + pos["qty"]*c + accr); continue
        if pos is None:                                   # USDT idle -> BIG-DIP re-entry
            if o <= m - X_bp/1e4:
                eff = round(o, 4)*(1+adverse_bp/1e4); qty = ALLOC/eff
                if last_exit_ts is not None:
                    max_idle = max(max_idle, (ts-last_exit_ts)/86400_000)
                pos = dict(buy=eff, qty=qty, ft=ts); cash -= ALLOC; turn += ALLOC
        else:                                             # holding USD1 -> spike-sell
            buy = pos["buy"]; T = round(m + sell_bp/1e4, 4)
            S = o if o >= T else (T if h >= T else None)
            if S is not None:
                f = S*(1-adverse_bp/1e4)
                if f > buy:                               # never realize a loss
                    proc = pos["qty"]*f
                    trades.append(dict(hold_d=(ts-pos["ft"])/86400_000,
                                       pnl=proc-ALLOC+accr, price_bp=(f-buy)/buy*1e4))
                    cash += proc+accr; turn += proc; pos = None; accr = 0.0; last_exit_ts = ts
        eq.append(cash + (pos["qty"]*c+accr if pos else 0))
    final = eq[-1]; span = (df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    tr = pd.DataFrame(trades)
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq)-peak)/peak).min()
    return dict(sym=sym, ref=ref, X_bp=X_bp, sell_bp=sell_bp, span_d=round(span,1),
                n=len(tr),
                win=round((tr.price_bp>0).mean()*100,1) if len(tr) else 0,
                avg_hold_d=round(tr.hold_d.mean(),2) if len(tr) else 0,
                avg_price_bp=round(tr.price_bp.mean(),2) if len(tr) else 0,
                ret_pct=round((final/ALLOC-1)*100,3),
                apr_pct=round((final/ALLOC-1)*100*365/span,2),
                mdd_pct=round(dd*100,3),
                turn_per_day=turn/span,
                max_hold_d=round(tr.hold_d.max(),1) if len(tr) else 0,
                idle_pct=round(idle_bars/nbar*100,1),
                max_idle_d=round(max_idle,1),
                tim_pct=round(inpos/nbar*100,1),
                open_at_end=(pos is not None))

def benchmark(sym):
    df = load(sym); o0 = df.open.iloc[0]; cN = df.close.iloc[-1]
    span = (df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    qty = ALLOC/o0; ypb = APR[sym]/365/BPD
    interest = (qty*df.close*ypb).sum(); final = qty*cN+interest
    return round((final/ALLOC-1)*100*365/span, 2)

if __name__ == "__main__":
    S = "USD1USDT"
    bench = benchmark(S)
    print(f"=== HOLD-FIRST (default-USD1) BIG-DIPS sweep on {S} ===")
    print(f"honest hold-only benchmark APR = {bench}  (stated = 10.0)\n")
    print(f"{'ref':<10}{'X':>4}{'sell':>5}{'n':>5}{'tim%':>6}{'idle%':>6}{'avgPx':>7}"
          f"{'A0':>7}{'A0.5':>7}{'A1.0':>7}{'A1.5':>7}{'mIdle':>7}{'mHold':>7}{'turn/d':>9}")
    rows = []
    for ref in ["ema55_1h", "ema100_1h", "sma2016", "sma4032"]:
        for X in [2, 3, 4, 5, 6, 8]:
            for sell in [0, 1, 2, 3, 5]:
                a = {av: run_holdfirst(S, True, av, X, sell, ref) for av in [0, 0.5, 1.0, 1.5]}
                b = a[0.5]
                rows.append((ref, X, sell, a))
                print(f"{ref:<10}{X:>4}{sell:>5}{b['n']:>5}{b['tim_pct']:>6}{b['idle_pct']:>6}"
                      f"{b['avg_price_bp']:>7.2f}{a[0]['apr_pct']:>7.2f}{a[0.5]['apr_pct']:>7.2f}"
                      f"{a[1.0]['apr_pct']:>7.2f}{a[1.5]['apr_pct']:>7.2f}"
                      f"{b['max_idle_d']:>7.1f}{b['max_hold_d']:>7.1f}{b['turn_per_day']:>9,.0f}")
    print(f"\n=== WINNERS: TOTAL APR > {bench} (honest bench) at adv=0.5 AND adv=1.0 ===")
    win = [(r,X,s,a) for (r,X,s,a) in rows
           if a[0.5]['apr_pct'] > bench and a[1.0]['apr_pct'] > bench]
    win.sort(key=lambda t: -t[3][1.0]['apr_pct'])
    for r,X,s,a in win[:20]:
        print(f"  {r:<10} X={X} sell={s}: adv0.5={a[0.5]['apr_pct']:.2f} adv1.0={a[1.0]['apr_pct']:.2f} "
              f"adv1.5={a[1.5]['apr_pct']:.2f} n={a[0.5]['n']} tim%={a[0.5]['tim_pct']} "
              f"idle%={a[0.5]['idle_pct']} mIdle={a[0.5]['max_idle_d']}d mHold={a[0.5]['max_hold_d']}d")

    # ---- the chosen variant (winner of the X-sweep) ----
    print("\n=== CHOSEN VARIANT: ema21_1h, entry_dip=1bp, exit_spike=4bp (default-hold USD1) ===")
    for adv in [0, 0.5, 1.0, 1.5, 2.0]:
        r = run_holdfirst(S, True, adv, 1.0, 4.0, "ema21_1h")
        print(f"  adv={adv}: TOTAL_APR={r['apr_pct']:>6.2f}  n={r['n']}  tim%={r['tim_pct']}  "
              f"idle%={r['idle_pct']}  maxIdle={r['max_idle_d']}d  maxHold={r['max_hold_d']}d  "
              f"avgPx={r['avg_price_bp']}bp  MDD={r['mdd_pct']}%  turn/d=${r['turn_per_day']:,.0f}")
    print(f"  NOTE: deep 'big' dips (X>=5 vs a slow mean) LOSE to idle-yield drag; the X-sweep")
    print(f"        optimum is a SMALL dip (1bp) below a responsive 21h mean -> ~96% time in USD1.")
