"""
bt_verify_r1_5.py — ADVERSARIAL independent re-implementation of the DRLG variant.

Written from scratch (own structure/flow/naming). Reuses ONLY B.load() for the
data frame and B's adverse/interest *conventions* (buy @ lim*(1+adv/1e4),
sell @ lim*(1-adv/1e4), interest = qty*close*apr/365/288).

DRLG spec being verified:
  R = EMA(close, span=6) on 5m, lagged 1 bar (Rprev[i]=ema[i-1]) -> no lookahead
  CORE  : 45% of $10k, bought once at min(Rprev,peg), HELD FOREVER (carry floor)
  LADDER: 55% in 3 equal rungs. rung j bid = round(min(Rprev - j*1bp, peg+3bp),4)
          fills if low<=bid; each lot rests ask = round(buy+7bp,4), fills if high>=ask.
  10% UTA interest on ALL held USD1. adverse haircut both sides. no stop.

This module ALSO bundles adversarial probes (temporal split, conservative fills,
P&L decomposition, capacity) — the whole point is to break the claimed win.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as B
import numpy as np
import pandas as pd

ALLOC = B.ALLOC      # 10_000
BPD   = B.BPD        # 288  (5m bars/day)
PEG   = 1.0000
USD1_APR = 0.10
MKT_VOL_USD1 = 2_538_200.0   # ~$2.5M/day


def _frame(lo=None, hi=None):
    """Load USD1USDT 5m frame; optional [lo,hi) bar slice for temporal split."""
    df = B.load("USD1USDT")
    if lo is not None or hi is not None:
        df = df.iloc[(lo or 0):(hi if hi is not None else len(df))].reset_index(drop=True)
    return df


def simulate(adv=0.5, *, with_yield=True, ref_span=6, core_frac=0.45,
             n_rungs=3, spacing_bp=1.0, rich_bp=3.0, target_bp=7.0,
             liquidate_end=False,
             # adversarial fill knobs (default = faithful to spec):
             strict_lt=False,      # require low<bid / high>ask (touch is not a fill)
             no_same_bar_recycle=False,  # a rung that sold this bar can't rebuy same bar
             fill_delay=0,         # orders armed at bar i only eligible from bar i+delay
             df=None):
    if df is None:
        df = _frame()
    o = df.open.values.astype(float); h = df.high.values.astype(float)
    l = df.low.values.astype(float);  c = df.close.values.astype(float)
    ts = df.ts.values.astype(np.int64)
    n = len(df)

    # lagged fast EMA reference (causal, then shift 1 bar)
    ema = pd.Series(c).ewm(span=ref_span, adjust=False).mean().values
    Rprev = np.empty(n); Rprev[0] = ema[0]; Rprev[1:] = ema[:-1]   # Rprev[i]=ema[i-1]

    ypb = (USD1_APR if with_yield else 0.0) / 365.0 / BPD
    rich_cap = PEG + rich_bp / 1e4
    sleeve = ALLOC * (1.0 - core_frac)
    rung_cap = sleeve / n_rungs if n_rungs else 0.0
    bm = adv / 1e4   # haircut magnitude per side

    cash = ALLOC
    interest = 0.0
    core = None                 # dict(qty, buy_eff, ft)
    rungs = [None] * n_rungs     # each: dict(qty, buy_eff, ft, armed_i)
    trades = []
    turn_buy = 0.0; turn_sell = 0.0
    eq = np.empty(n)
    deployed_dollars = 0.0       # running sum of (held*close) for TIM/interest-base audit
    held_dollar_sum = 0.0
    idle_cash_bars = 0
    sold_this_bar = [False] * n_rungs

    for i in range(n):
        # ---- 1. interest accrues on USD1 carried INTO this bar (start-of-bar holdings).
        #         conservative: positions opened this bar do NOT earn this bar.
        held_in = (core["qty"] if core else 0.0) + sum(r["qty"] for r in rungs if r)
        if held_in > 0:
            interest += held_in * c[i] * ypb

        # ---- 2. CORE one-time entry (permanent carry floor)
        if core is None and core_frac > 0:
            cp = round(min(Rprev[i], PEG), 4)
            buy_ok = (l[i] < cp) if strict_lt else (l[i] <= cp)
            if buy_ok and cash >= ALLOC * core_frac - 1e-9:
                eff = cp * (1 + bm)
                qty = (ALLOC * core_frac) / eff
                core = dict(qty=qty, buy_eff=eff, ft=ts[i])
                cash -= ALLOC * core_frac; turn_buy += ALLOC * core_frac

        # ---- 3. LADDER sells (existing lots), process before buys
        for j in range(n_rungs):
            sold_this_bar[j] = False
            r = rungs[j]
            if r is None:
                continue
            if fill_delay and (i - r["armed_i"]) < fill_delay:
                continue
            S = round(r["buy_eff"] + target_bp / 1e4, 4)
            sell_ok = (h[i] > S) if strict_lt else (h[i] >= S)
            if sell_ok:
                f = S * (1 - bm)
                proc = r["qty"] * f
                cash += proc; turn_sell += proc
                trades.append(dict(hold_d=(ts[i] - r["ft"]) / 86400_000,
                                   price_bp=(f - r["buy_eff"]) / r["buy_eff"] * 1e4,
                                   buy=r["buy_eff"], sell=f))
                rungs[j] = None
                sold_this_bar[j] = True

        # ---- 4. LADDER buys (re-arm empty rungs)
        for j in range(n_rungs):
            if rungs[j] is not None:
                continue
            if no_same_bar_recycle and sold_this_bar[j]:
                continue
            bid = round(min(Rprev[i] - j * spacing_bp / 1e4, rich_cap), 4)
            if bid <= 0:
                continue
            buy_ok = (l[i] < bid) if strict_lt else (l[i] <= bid)
            if buy_ok and cash >= rung_cap - 1e-9:
                eff = bid * (1 + bm)
                rungs[j] = dict(qty=rung_cap / eff, buy_eff=eff, ft=ts[i], armed_i=i)
                cash -= rung_cap; turn_buy += rung_cap

        # ---- 5. mark equity
        held = (core["qty"] if core else 0.0) + sum(r["qty"] for r in rungs if r)
        dep = held * c[i]
        eq[i] = cash + dep + interest
        held_dollar_sum += dep
        if cash > rung_cap * 0.5:
            idle_cash_bars += 1

    # ---- optional: realize everything at last close (- adverse) to test markup propping
    if liquidate_end:
        held = (core["qty"] if core else 0.0) + sum(r["qty"] for r in rungs if r)
        if held > 0:
            cash += held * c[-1] * (1 - bm)
            eq[-1] = cash + interest
            core = None; rungs = [None] * n_rungs

    final = eq[-1]
    span = (ts[-1] - ts[0]) / 86400_000
    tr = pd.DataFrame(trades)
    eqs = pd.Series(eq); dd = ((eqs - eqs.cummax()) / eqs.cummax()).min()
    open_ages = [(ts[-1] - r["ft"]) / 86400_000 for r in rungs if r]
    n_open_ladder = sum(1 for r in rungs if r)
    max_stuck = max((tr.hold_d.max() if len(tr) else 0.0), max(open_ages, default=0.0))
    turn_per_day = (turn_buy + turn_sell) / span

    return dict(
        adv=adv, span_d=round(span, 1), n=len(tr),
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        interest_apr=round(interest / ALLOC * 100 * 365 / span, 3),
        price_apr=round((final - ALLOC - interest) / ALLOC * 100 * 365 / span, 3),
        avg_deploy_pct=round(held_dollar_sum / n / ALLOC * 100, 2),
        win=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0,
        avg_px_bp=round(tr.price_bp.mean(), 3) if len(tr) else 0,
        avg_hold_d=round(tr.hold_d.mean(), 2) if len(tr) else 0,
        max_hold_d=round(tr.hold_d.max(), 1) if len(tr) else 0,
        n_open_ladder=n_open_ladder, max_open_age=round(max(open_ages, default=0.0), 1),
        max_stuck_d=round(max_stuck, 1),
        mdd_pct=round(dd * 100, 4),
        turn_per_day=round(turn_per_day, 1),
        pct_mkt=round(turn_per_day / MKT_VOL_USD1 * 100, 4),
        idle_cash_pct=round(idle_cash_bars / n * 100, 2),
        core_held=(core is not None or liquidate_end),
        final=round(final, 2),
    )


def buy_hold(adv=0.5, df=None):
    """Honest opportunity cost: 100% into USD1 at bar0, hold, 10% carry, one haircut."""
    if df is None:
        df = _frame()
    c = df.close.values.astype(float); ts = df.ts.values.astype(np.int64)
    ypb = USD1_APR / 365.0 / BPD
    p0 = round(min(c[0], PEG), 4); eff = p0 * (1 + adv / 1e4); qty = ALLOC / eff
    # accrue interest on carried position (bar0 position carried from bar0 onward)
    interest = float(np.sum(qty * c * ypb))
    final = qty * c[-1] + interest
    span = (ts[-1] - ts[0]) / 86400_000
    return round((final / ALLOC - 1) * 100 * 365 / span, 3)


def hdr():
    return (f"{'adv':>4}{'totAPR':>8}{'int':>7}{'px':>7}{'dep%':>6}{'n':>5}{'win':>5}"
            f"{'avgbp':>7}{'avgH':>6}{'maxStk':>7}{'mdd%':>8}{'turn/d':>8}{'%mkt':>7}"
            f"{'LIQ':>7}{'b&h':>7}{'>10':>4}")


def line(r, rl, bh):
    return (f"{r['adv']:>4}{r['apr_pct']:>8.2f}{r['interest_apr']:>7.2f}{r['price_apr']:>7.2f}"
            f"{r['avg_deploy_pct']:>6.1f}{r['n']:>5}{r['win']:>5.0f}{r['avg_px_bp']:>7.2f}"
            f"{r['avg_hold_d']:>6.1f}{r['max_stuck_d']:>7.1f}{r['mdd_pct']:>8.3f}"
            f"{r['turn_per_day']:>8,.0f}{r['pct_mkt']:>7.4f}{rl['apr_pct']:>7.2f}{bh:>7.2f}"
            f"{'Y' if r['apr_pct']>10 else 'N':>4}")


if __name__ == "__main__":
    advs = [0, 0.5, 1.0, 1.5]
    print("=== bt_verify_r1_5: INDEPENDENT adversarial reproduction of DRLG ===")
    df = _frame()
    span = (df.ts.values[-1] - df.ts.values[0]) / 86400_000
    print(f"USD1USDT 5m, {len(df)} bars, span={span:.1f}d, alloc=${ALLOC:.0f}, benchmark=flat 10% APR\n")

    print("[A] FULL WINDOW")
    print(hdr())
    full = {}
    for a in advs:
        r = simulate(adv=a); rl = simulate(adv=a, liquidate_end=True); bh = buy_hold(adv=a)
        full[a] = (r, rl, bh); print(line(r, rl, bh))

    n = len(df); half = n // 2
    df1 = df.iloc[:half].reset_index(drop=True); df2 = df.iloc[half:].reset_index(drop=True)
    print("\n[B] FIRST HALF  (overfit/temporal-stability probe)")
    print(hdr())
    for a in advs:
        r = simulate(adv=a, df=df1); rl = simulate(adv=a, df=df1, liquidate_end=True)
        bh = buy_hold(adv=a, df=df1); print(line(r, rl, bh))
    print("\n[C] SECOND HALF")
    print(hdr())
    for a in advs:
        r = simulate(adv=a, df=df2); rl = simulate(adv=a, df=df2, liquidate_end=True)
        bh = buy_hold(adv=a, df=df2); print(line(r, rl, bh))

    print("\n[D] CONSERVATIVE FILLS @ adv=1.0 (break optimistic fills)")
    print(hdr())
    base = simulate(adv=1.0); print("base       ", line(base, simulate(adv=1.0, liquidate_end=True), buy_hold(1.0)))
    for label, kw in [("strict_lt", dict(strict_lt=True)),
                      ("no_recycle", dict(no_same_bar_recycle=True)),
                      ("delay1bar", dict(fill_delay=1)),
                      ("delay3bar", dict(fill_delay=3)),
                      ("ALL_strict", dict(strict_lt=True, no_same_bar_recycle=True, fill_delay=1))]:
        r = simulate(adv=1.0, **kw)
        print(f"{label:<11}", line(r, simulate(adv=1.0, liquidate_end=True, **kw), buy_hold(1.0)))

    print("\n[E] P&L DECOMPOSITION @ adv=1.0")
    r = simulate(adv=1.0)
    core_only = simulate(adv=1.0, core_frac=1.0, n_rungs=0)   # pure 100% buy-hold via core path
    ladder_only = simulate(adv=1.0, core_frac=0.0)            # ladder alone, 100% sleeve
    no_yield = simulate(adv=1.0, with_yield=False)
    print(f"  full        totAPR={r['apr_pct']:.2f}  int={r['interest_apr']:.2f} px={r['price_apr']:.2f}")
    print(f"  core_only   totAPR={core_only['apr_pct']:.2f}  (45%->100% core = buy&hold proxy)")
    print(f"  ladder_only totAPR={ladder_only['apr_pct']:.2f}  int={ladder_only['interest_apr']:.2f} px={ladder_only['price_apr']:.2f} (no carry floor)")
    print(f"  no_yield    price-only APR={no_yield['price_apr']:.2f}  (grid harvest stripped of carry)")
    print(f"  buy&hold@1  {buy_hold(1.0):.2f}   flat-bench=10.00")

    print("\n[F] CAPACITY @ adv=1.0")
    r1 = full[1.0][0]
    cap = ALLOC * 0.02 * MKT_VOL_USD1 / r1['turn_per_day'] if r1['turn_per_day'] else float('inf')
    print(f"  turnover/day=${r1['turn_per_day']:,.0f} = {r1['pct_mkt']:.4f}% of ${MKT_VOL_USD1:,.0f}/day"
          f"  -> <2% up to size ${cap:,.0f} ({cap/ALLOC:.0f}x base)")
