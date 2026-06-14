"""
bt_variant_r1_5.py  —  INDEPENDENT reproduction + backtest of the DRLG variant.

Dynamic-Reference Laddered Grid (DRLG) for USD1USDT.
UNIQUE file. Reuses ONLY bt_faithful.load() for data + its exact adverse/interest
conventions (buy @ limit*(1+adv/1e4), sell @ limit*(1-adv/1e4), interest = qty*close*apr/365/288).

SPEC implemented exactly:
  reference R = EMA(close, span=6) on 5m; R_prev = EMA of the PREVIOUS CLOSED bar (no lookahead)
  peg=1.0000 ; round every order to 4dp (tickSize=1bp)
  (1) CORE  : first bar, 45% of capital -> USD1 at min(R_prev, peg); HELD PERMANENTLY (never sold)
  (2) LADDER: other 55% split into 3 equal rungs. rung j passive bid =
              round(min(R_prev - j*1bp, peg+3bp), 4); fills if low<=bid.
              each open lot rests ask = round(buy+7bp, 4); fills if high>=ask; then re-arm.
  NO stop-loss (USD1 always re-pegs, locked). 10% UTA interest on ALL held USD1.

This is a clean-room implementation from the SPEC (not an import of grid_variant.py),
so it serves as an independent cross-check of the prior analysis.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as B
import pandas as pd, numpy as np

ALLOC = B.ALLOC          # 10000
BPD   = B.BPD            # 288 bars/day (5m)
PEG   = 1.0000
APR   = 0.10             # USD1 UTA interest

# ---- LOCKED SPEC params ----
REF_SPAN    = 6
CORE_FRAC   = 0.45
N_RUNGS     = 3
SPACING_BP  = 1.0
MAX_RICH_BP = 3.0
TARGET_BP   = 7.0
MKT_VOL_USD1 = 2_538_200  # ~$2.5M/day


def run_variant(adv=0.5, with_yield=True, liquidate_end=False,
                ref_span=REF_SPAN, core_frac=CORE_FRAC, n_rungs=N_RUNGS,
                spacing_bp=SPACING_BP, max_rich_bp=MAX_RICH_BP, target_bp=TARGET_BP,
                relax_days=None, relax_target_bp=2.0):
    df = B.load("USD1USDT")
    apr = APR if with_yield else 0.0
    ypb = apr / 365 / BPD
    # FAST reference, lagged 1 bar (uses previous CLOSED 5m candle -> no lookahead)
    ema = df["close"].ewm(span=ref_span, adjust=False).mean()
    Rprev = ema.shift(1).bfill().values
    rich_cap = PEG + max_rich_bp / 1e4
    o = df.open.values; h = df.high.values; l = df.low.values; c = df.close.values; ts = df.ts.values
    n = len(df)

    sleeve = ALLOC * (1.0 - core_frac)
    rung_cap = sleeve / n_rungs if n_rungs > 0 else 0.0

    cash = ALLOC; interest = 0.0
    core = None
    lots = [None] * n_rungs
    trades = []; turn = 0.0; eq = []
    tim_num = 0.0; dep_bars = 0; idle_bars = 0
    raw_buys = []; raw_sells = []

    for i in range(n):
        # --- CORE: permanent, first fill at min(R_prev, peg) ---
        if core is None and core_frac > 0:
            cp = round(min(Rprev[i], PEG), 4)
            if l[i] <= cp:
                eff = cp * (1 + adv / 1e4)
                core = dict(qty=(ALLOC * core_frac) / eff, buy=eff, ft=ts[i])
                cash -= ALLOC * core_frac; turn += ALLOC * core_frac

        # --- interest on all held USD1 (position at start of bar) ---
        held = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        if held > 0:
            interest += held * c[i] * ypb

        # --- LADDER SELLS: each open lot at buy+target ---
        for j in range(n_rungs):
            L = lots[j]
            if L is None:
                continue
            age = (ts[i] - L["ft"]) / 86400_000
            tb = target_bp if (relax_days is None or age < relax_days) else relax_target_bp
            S = round(L["buy"] + tb / 1e4, 4)
            if h[i] >= S:
                f = S * (1 - adv / 1e4)
                proc = L["qty"] * f
                cash += proc; turn += proc
                trades.append(dict(hold_d=age, price_bp=(f - L["buy"]) / L["buy"] * 1e4))
                raw_sells.append(f)
                lots[j] = None

        # --- LADDER BUYS: re-arm bids = min(R_prev - j*spacing, peg+rich) ---
        for j in range(n_rungs):
            if lots[j] is not None:
                continue
            bid = round(min(Rprev[i] - j * spacing_bp / 1e4, rich_cap), 4)
            if bid <= 0:
                continue
            if l[i] <= bid and cash >= rung_cap - 1e-9:
                eff = bid * (1 + adv / 1e4)
                lots[j] = dict(qty=rung_cap / eff, buy=eff, ft=ts[i])
                cash -= rung_cap; turn += rung_cap
                raw_buys.append(eff)

        # --- equity / time-in-market ---
        held = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        dep = held * c[i]
        equity = cash + dep + interest
        eq.append(equity)
        if equity > 0:
            tim_num += dep / equity
        if dep > 1e-6:
            dep_bars += 1
        if cash > rung_cap * 0.5:
            idle_bars += 1

    # sanity: realize ALL holdings at last close - adverse (confirms not propped by markup)
    if liquidate_end and (core or any(lots)):
        lf = c[-1] * (1 - adv / 1e4)
        held = (core["qty"] if core else 0.0) + sum(L["qty"] for L in lots if L)
        cash += held * lf
        eq[-1] = cash + interest
        core = None; lots = [None] * n_rungs

    final = eq[-1]; span = (ts[-1] - ts[0]) / 86400_000
    tr = pd.DataFrame(trades)
    eqs = pd.Series(eq); peak = eqs.cummax(); dd = ((eqs - peak) / peak).min()
    open_lot_ages = [(ts[-1] - L["ft"]) / 86400_000 for L in lots if L]
    max_open_lot_age = max(open_lot_ages, default=0.0)
    # "max stuck" = worst-case time a LADDER unit of capital is tied up (closed hold or still-open age)
    max_stuck = max(
        (tr.hold_d.max() if len(tr) else 0.0),
        max_open_lot_age,
    )
    return dict(
        adv=adv, n=len(tr),
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        interest_apr=round(interest / ALLOC * 100 * 365 / span, 3),
        price_apr=round((final - ALLOC - interest) / ALLOC * 100 * 365 / span, 3),
        tim_pct=round(tim_num / n * 100, 2),
        win=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0,
        avg_px_bp=round(tr.price_bp.mean(), 3) if len(tr) else 0,
        avg_hold_d=round(tr.hold_d.mean(), 3) if len(tr) else 0,
        max_hold_d=round(tr.hold_d.max(), 1) if len(tr) else 0,
        max_open_lot_age=round(max_open_lot_age, 1),
        max_stuck_d=round(max_stuck, 1),
        mdd_pct=round(dd * 100, 4),
        turn_per_day=round(turn / span, 0),
        n_open_end=sum(1 for L in lots if L) + (1 if core else 0),
        span_d=round(span, 1),
        idle_pct=round(idle_bars / n * 100, 2),
        raw_buys=np.array(raw_buys), raw_sells=np.array(raw_sells),
    )


def buy_hold(adv=0.5):
    """True opportunity cost: put ALL $10k into USD1 at the first bar and hold w/ interest.
    Single entry takes one adverse haircut; marked at last close."""
    df = B.load("USD1USDT"); ypb = APR / 365 / BPD
    c = df.close.values; ts = df.ts.values
    p0 = round(min(df.close.values[0], PEG), 4)
    eff = p0 * (1 + adv / 1e4); qty = ALLOC / eff
    interest = float(np.sum(qty * c * ypb))
    final = qty * c[-1] + interest
    span = (ts[-1] - ts[0]) / 86400_000
    return round((final / ALLOC - 1) * 100 * 365 / span, 3)


if __name__ == "__main__":
    df0 = B.load("USD1USDT")
    span = (df0.ts.values[-1] - df0.ts.values[0]) / 86400_000
    sub = (df0.close < PEG).mean() * 100
    at = (df0.low <= PEG).mean() * 100  # bars whose low touches/crosses peg
    print("=== DRLG variant (bt_variant_r1_5.py) — INDEPENDENT reproduction ===")
    print(f"data: USD1USDT 5m, {len(df0)} bars, span={span:.1f}d (~{span/30.4:.1f}mo), alloc=${ALLOC:.0f}")
    print(f"USD1 < peg: {sub:.1f}% of bars | low<=peg: {at:.1f}% of bars")
    print(f"config: ref_span={REF_SPAN} core={CORE_FRAC} rungs={N_RUNGS} spacing={SPACING_BP}bp "
          f"rich_cap=peg+{MAX_RICH_BP}bp target={TARGET_BP}bp interest={APR*100:.0f}%/yr")
    print("benchmark: hold USD1 = 10.00%  |  buy-and-hold(bar0, w/ adverse) shown per adv\n")

    print(f"{'adv':>5}{'totAPR':>8}{'int':>7}{'px':>7}{'tim%':>6}{'n':>5}{'win%':>6}"
          f"{'avgPx':>7}{'avgH':>6}{'maxH':>6}{'mdd%':>8}{'turn/d$':>9}{'%mkt':>6}{'LIQ':>7}{'b&h':>7}{'>10?':>5}{'>b&h?':>6}")
    rows = {}
    for a in [0, 0.5, 1.0, 1.5]:
        r = run_variant(adv=a)
        rl = run_variant(adv=a, liquidate_end=True)
        bh = buy_hold(adv=a)
        rows[a] = (r, rl, bh)
        print(f"{a:>5}{r['apr_pct']:>8.2f}{r['interest_apr']:>7.2f}{r['price_apr']:>7.2f}"
              f"{r['tim_pct']:>6.1f}{r['n']:>5}{r['win']:>6.0f}{r['avg_px_bp']:>7.2f}"
              f"{r['avg_hold_d']:>6.1f}{r['max_hold_d']:>6.0f}{r['mdd_pct']:>8.3f}"
              f"{r['turn_per_day']:>9,.0f}{r['turn_per_day']/MKT_VOL_USD1*100:>6.3f}"
              f"{rl['apr_pct']:>7.2f}{bh:>7.2f}"
              f"{'Y' if r['apr_pct']>10 else 'N':>5}{'Y' if r['apr_pct']>bh else 'N':>6}")

    r1 = rows[1.0][0]
    cap = ALLOC * 0.02 * MKT_VOL_USD1 / r1['turn_per_day']
    print(f"\ncapacity @adv1: turn/day=${r1['turn_per_day']:,.0f} = {r1['turn_per_day']/MKT_VOL_USD1*100:.3f}% of "
          f"${MKT_VOL_USD1:,}/day -> stay <2% up to size ${cap:,.0f} (~{cap/ALLOC:.0f}x base)")
    print(f"max stuck (ladder): adv0.5={rows[0.5][0]['max_stuck_d']}d  adv1.0={rows[1.0][0]['max_stuck_d']}d  "
          f"adv1.5={rows[1.5][0]['max_stuck_d']}d  (core is permanent-by-design, earning 10%, not 'stuck')")

    # raw-fill realism: where do buys actually land vs peg?
    rb = rows[1.0][0]['raw_buys']
    if len(rb):
        bp_vs_peg = (rb / PEG - 1) * 1e4
        print(f"raw buy fills @adv1: n={len(rb)}  median={np.median(bp_vs_peg):+.2f}bp vs peg  "
              f"frac>peg={np.mean(bp_vs_peg>0)*100:.0f}%  max={bp_vs_peg.max():+.2f}bp")

    print("\n--- mini robustness sweep (total APR @ adv=1.0) ---")
    print("ref_span :", {s: run_variant(adv=1.0, ref_span=s)['apr_pct'] for s in (3, 6, 12)})
    print("core_frac:", {cf: run_variant(adv=1.0, core_frac=cf)['apr_pct'] for cf in (0.35, 0.45, 0.55)})
    print("target_bp:", {t: run_variant(adv=1.0, target_bp=t)['apr_pct'] for t in (5, 6, 7)})
