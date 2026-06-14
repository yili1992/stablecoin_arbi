"""
INDEPENDENT adversarial re-implementation of variant r1_1
  "USD1 Taker-Reentry (no-loss-sale + 3bp reversion + 0.5d USDT idle cap)"

Written FROM SCRATCH (own structure, own loop) to verify the claimed WIN without
trusting backtest/bt_variant_r1_1.py. Only the trusted data loader load() from
bt_faithful is reused (it is the engine's no-lookahead data prep, the source of truth).

Variant spec (from the variant docstring, re-derived independently):
  ENTRY (when flat):
    (a) passive maker buy-the-dip: proposed=open; gate proposed-ema55_1h<1bp;
        L=min(open,ema55_1h); if ema21_down -> L-=1tick; L=round(4);
        fills if low<=L, at L*(1+adv/1e4).
    (b) taker re-entry: if (a) did not fill AND idle>=idle_cap_days,
        marketable buy fills at open*(1+taker_cost/1e4) [+adv if adv_on_taker].
  EXIT (when long): maker sell at S=round(buy+sell_bp/1e4,4); NO loss escape, never
    re-priced below buy. Fills if open>=S or high>=S, at S*(1-adv/1e4). reset idle.
  INTEREST: every bar held, accr += qty*close*ypb, ypb=APR/365/288 (UTA, 10%/yr).
  EQUITY each bar: cash + (qty*close + accr) if long.

Decomposition tracked independently: interest_cum vs price_pnl = (final-ALLOC)-interest_cum.
This isolates how much "win" is interest (<=10%*TIM) vs price-leg alpha.
"""
import os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, APR, ALLOC, BPD, MKT_VOL  # trusted data loader + constants

SYM = "USD1USDT"
DAY_MS = 86_400_000


def backtest(df, adv_bp=0.0, with_yield=True,
             sell_bp=3.0, idle_cap_days=0.5, taker_cost_bp=1.0,
             taker_on=True, no_loss=True, adv_on_taker=False):
    """Independent event loop. df must already be load()'d (has ema/trend, no lookahead)."""
    apr = APR[SYM] if with_yield else 0.0
    ypb = apr / 365.0 / BPD

    cash = ALLOC
    long = False
    buy = qty = ft = 0.0
    pos_taker = False
    interest_cum = 0.0          # global running interest (never reset) for decomposition
    accr = 0.0                  # interest of the CURRENT open position
    eq = []
    trades = []                 # each: (entry_ts, exit_ts, hold_d, price_bp, taker, interest)
    turn = 0.0
    inpos_bars = 0

    ts = df.ts.to_numpy()
    op = df.open.to_numpy(); hi = df.high.to_numpy()
    lo = df.low.to_numpy(); cl = df.close.to_numpy()
    ema = df.ema55_1h.to_numpy()
    e21d = df.ema21_down.to_numpy()

    idle_since = float(ts[0])
    n = len(df)
    for i in range(n):
        o, h, l, c = op[i], hi[i], lo[i], cl[i]
        now = ts[i]

        # interest accrues at TOP of bar, before any trade this bar (same as engine)
        if long:
            d = qty * c * ypb
            accr += d
            interest_cum += d
            inpos_bars += 1

        if not long:
            filled = False
            # (a) passive maker buy
            if o - ema[i] < 0.0001:
                L = ema[i] if o > ema[i] else o
                if e21d[i]:
                    L -= 0.0001
                L = round(L, 4)
                if l <= L:
                    eff = L * (1 + adv_bp / 1e4)
                    buy = eff; qty = ALLOC / eff; ft = now; pos_taker = False
                    cash -= ALLOC; turn += ALLOC
                    long = True; accr = 0.0; filled = True
            # (b) taker re-entry on idle cap
            if (not filled) and taker_on and (now - idle_since) >= idle_cap_days * DAY_MS:
                bump = taker_cost_bp / 1e4 + (adv_bp / 1e4 if adv_on_taker else 0.0)
                eff = o * (1 + bump)
                buy = eff; qty = ALLOC / eff; ft = now; pos_taker = True
                cash -= ALLOC; turn += ALLOC
                long = True; accr = 0.0
        else:
            S = round(buy + sell_bp / 1e4, 4)
            if no_loss and S < buy:     # guard: never price below entry (3bp keeps S>buy anyway)
                S = round(buy + sell_bp / 1e4, 4)
            if (o >= S) or (h >= S):
                f = S * (1 - adv_bp / 1e4)
                proc = qty * f
                trades.append((ft, now, (now - ft) / DAY_MS,
                               (f - buy) / buy * 1e4, pos_taker, accr))
                cash += proc + accr
                turn += proc
                long = False; accr = 0.0
                idle_since = float(now)

        eq.append(cash + (qty * c + accr if long else 0.0))

    final = eq[-1]
    span = (ts[-1] - ts[0]) / DAY_MS
    ret = final / ALLOC - 1.0
    price_pnl = (final - ALLOC) - interest_cum     # everything not interest = price leg
    eqs = pd.Series(eq)
    dd = ((eqs - eqs.cummax()) / eqs.cummax()).min()
    tr = pd.DataFrame(trades, columns=["entry_ts", "exit_ts", "hold_d", "price_bp", "taker", "interest"])

    open_hold = (ts[-1] - ft) / DAY_MS if long else 0.0
    open_uw = (cl[-1] / buy - 1) * 1e4 if long else 0.0
    max_stuck = max(tr.hold_d.max() if len(tr) else 0.0, open_hold)

    return dict(
        adv=adv_bp, n=len(tr),
        n_taker=int(tr.taker.sum()) if len(tr) else 0,
        n_passive=int((~tr.taker).sum()) if len(tr) else 0,
        ret_pct=round(ret * 100, 4),
        apr_pct=round(ret * 100 * 365 / span, 4),
        interest_apr=round(interest_cum / ALLOC * 100 * 365 / span, 4),
        price_apr=round(price_pnl / ALLOC * 100 * 365 / span, 4),
        avg_price_bp=round(tr.price_bp.mean(), 3) if len(tr) else 0.0,
        n_loss=int((tr.price_bp < 0).sum()) if len(tr) else 0,
        worst_bp=round(tr.price_bp.min(), 3) if len(tr) else 0.0,
        tim_pct=round(inpos_bars / n * 100, 3),
        idle_days=round((n - inpos_bars) / BPD, 3),
        mdd_pct=round(dd * 100, 4),
        turn_per_day=turn / span,
        open_at_end=long, open_hold_d=round(open_hold, 1), open_uw_bp=round(open_uw, 2),
        max_stuck_d=round(max_stuck, 1),
        span_d=round(span, 2),
        _tr=tr, _eq=eqs, _final=final, _interest=interest_cum, _price=price_pnl,
    )


def hold_bench(df, with_yield=True):
    """True buy-and-hold USD1 on same data + same interest model, single clean entry @ open[0]."""
    apr = APR[SYM] if with_yield else 0.0
    ypb = apr / 365.0 / BPD
    ts = df.ts.to_numpy(); op = df.open.to_numpy(); cl = df.close.to_numpy()
    qty = ALLOC / op[0]
    accr = 0.0
    for i in range(len(df)):
        accr += qty * cl[i] * ypb
    final = qty * cl[-1] + accr
    span = (ts[-1] - ts[0]) / DAY_MS
    ret = final / ALLOC - 1.0
    drift = cl[-1] / op[0] - 1.0
    return dict(apr_pct=round(ret * 100 * 365 / span, 4),
                drift_apr=round(drift * 100 * 365 / span, 4),
                interest_apr=round(accr / ALLOC * 100 * 365 / span, 4),
                p0=op[0], p_last=cl[-1], span_d=round(span, 2))


if __name__ == "__main__":
    df = load(SYM)
    ADVS = [0.0, 0.5, 1.0, 1.5]
    hb = hold_bench(df)

    print("=" * 92)
    print("INDEPENDENT VERIFY r1_1 — USD1 Taker-Reentry (no-loss + 3bp + 0.5d idle cap)")
    print(f"span={hb['span_d']}d  p0={hb['p0']:.4f}->p_last={hb['p_last']:.4f}  ONLY {SYM}, alloc ${ALLOC:.0f}")
    print(f"FLAT benchmark = 10.000% | TRUE buy&hold (same data) = {hb['apr_pct']:.4f}% "
          f"(drift {hb['drift_apr']:.4f} + interest {hb['interest_apr']:.4f})")
    print("=" * 92)
    print(f"{'adv':>4}{'APR':>10}{'vs10%':>9}{'vsHOLD':>9}{'intAPR':>9}{'pxAPR':>9}"
          f"{'n':>4}{'pas':>4}{'tak':>4}{'avgPx':>7}{'loss':>5}{'worst':>7}{'TIM%':>7}{'idleD':>7}{'MDD%':>8}{'stuckD':>7}")
    res = {}
    for a in ADVS:
        r = backtest(df, a)
        res[a] = r
        print(f"{a:>4.1f}{r['apr_pct']:>10.4f}{r['apr_pct']-10:>+9.4f}{r['apr_pct']-hb['apr_pct']:>+9.4f}"
              f"{r['interest_apr']:>9.4f}{r['price_apr']:>9.4f}{r['n']:>4}{r['n_passive']:>4}{r['n_taker']:>4}"
              f"{r['avg_price_bp']:>7.2f}{r['n_loss']:>5}{r['worst_bp']:>7.2f}{r['tim_pct']:>7.2f}"
              f"{r['idle_days']:>7.2f}{r['mdd_pct']:>8.4f}{r['max_stuck_d']:>7.1f}")

    print("\n--- OPEN POSITION AT END (the no-loss trap) ---")
    r1 = res[1.0]
    print(f"  adv=1.0: open_at_end={r1['open_at_end']} held {r1['open_hold_d']}d "
          f"({r1['open_hold_d']/r1['span_d']*100:.1f}% of sample) underwater {r1['open_uw_bp']}bp")

    print("\n--- TRADE TIMELINE @ adv=1.0 (entry day from start, hold, px bp) ---")
    tr = r1["_tr"].copy()
    t0 = df.ts.iloc[0]
    tr["entry_day"] = (tr.entry_ts - t0) / DAY_MS
    for row in tr.itertuples():
        print(f"  day {row.entry_day:6.1f}  hold {row.hold_d:6.2f}d  px {row.price_bp:+6.2f}bp  "
              f"{'TAKER' if row.taker else 'pass '}")

    print("\n--- FIRST-HALF vs SECOND-HALF (independent runs, each starts flat) ---")
    half = len(df) // 2
    d1 = df.iloc[:half].reset_index(drop=True)
    d2 = df.iloc[half:].reset_index(drop=True)
    for a in [0.5, 1.0, 1.5]:
        h1 = backtest(d1, a); h2 = backtest(d2, a)
        hb1 = hold_bench(d1); hb2 = hold_bench(d2)
        print(f"  adv={a}: H1 apr={h1['apr_pct']:8.4f} (vs10 {h1['apr_pct']-10:+.3f}, vsHold {h1['apr_pct']-hb1['apr_pct']:+.3f}, "
              f"n={h1['n']}, pxAPR={h1['price_apr']:+.3f}, TIM={h1['tim_pct']:.1f}, stuck={h1['max_stuck_d']}d) | "
              f"H2 apr={h2['apr_pct']:8.4f} (vs10 {h2['apr_pct']-10:+.3f}, vsHold {h2['apr_pct']-hb2['apr_pct']:+.3f}, "
              f"n={h2['n']}, pxAPR={h2['price_apr']:+.3f}, TIM={h2['tim_pct']:.1f}, stuck={h2['max_stuck_d']}d)")

    print("\n--- WITHIN FULL RUN: APR of pre-trap segment vs trap segment (adv=1.0) ---")
    # the trap = the still-open position. find its entry bar.
    if r1["open_at_end"]:
        eq = r1["_eq"].to_numpy()
        # entry ts of open position = last buy. reconstruct: last trade exit defines idle, then entry.
        # simplest: the open position entered at ts = ts[-1] - open_hold*DAY. find nearest bar.
        ts_arr = df.ts.to_numpy()
        entry_ts = ts_arr[-1] - r1["open_hold_d"] * DAY_MS
        ti = int(np.searchsorted(ts_arr, entry_ts))
        eq_split = eq[ti]
        pre_ret = eq_split / ALLOC - 1
        pre_days = (ts_arr[ti] - ts_arr[0]) / DAY_MS
        trap_ret = eq[-1] / eq_split - 1
        trap_days = (ts_arr[-1] - ts_arr[ti]) / DAY_MS
        print(f"  pre-trap  : days 0..{pre_days:.1f}  ret={pre_ret*100:.4f}%  APR={pre_ret*100*365/pre_days:.4f}%")
        print(f"  trap held : days {pre_days:.1f}..{r1['span_d']:.1f}  ret={trap_ret*100:.4f}%  "
              f"APR={trap_ret*100*365/trap_days:.4f}%  (10% flat would give {10.0:.4f})")

    print("\n--- ROBUSTNESS sweeps @ adv=1.0 ---")
    for sb in [2, 3, 4, 5]:
        v = backtest(df, 1.0, sell_bp=sb)
        print(f"  sell_bp={sb}: apr={v['apr_pct']:.4f} n={v['n']} avgPx={v['avg_price_bp']} stuck={v['max_stuck_d']}d")
    for ic in [0.1, 0.5, 1.0, 2.0]:
        v = backtest(df, 1.0, idle_cap_days=ic)
        print(f"  idle_cap={ic}d: apr={v['apr_pct']:.4f} TIM={v['tim_pct']} takers={v['n_taker']}")
    for tc in [0.5, 1.0, 2.0, 3.0]:
        v = backtest(df, 1.0, taker_cost_bp=tc)
        print(f"  taker_cost={tc}bp: apr={v['apr_pct']:.4f} takers={v['n_taker']}")

    print("\n--- TAKER ISOLATION & CONSERVATIVE adv-on-taker ---")
    for a in [0.5, 1.0]:
        on = backtest(df, a, taker_on=True); off = backtest(df, a, taker_on=False)
        cons = backtest(df, a, adv_on_taker=True)
        print(f"  adv={a}: ON={on['apr_pct']:.4f}(TIM{on['tim_pct']}) OFF={off['apr_pct']:.4f}(TIM{off['tim_pct']}) "
              f"delta={on['apr_pct']-off['apr_pct']:+.4f} | adv-on-taker={cons['apr_pct']:.4f}")

    print("\n--- CAPACITY ---")
    pct = r1['turn_per_day'] / MKT_VOL[SYM] * 100
    print(f"  turnover/day=${r1['turn_per_day']:,.0f} = {pct:.4f}% of ${MKT_VOL[SYM]:,}/day  "
          f"(<2% cap: {pct<2.0}) scalable to ~${ALLOC*0.02*MKT_VOL[SYM]/r1['turn_per_day']:,.0f}")
