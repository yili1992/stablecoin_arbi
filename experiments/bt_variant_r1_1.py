"""
VARIANT r1_1 — "USD1 Taker-Reentry (no-loss-sale + 3bp reversion + 0.5d USDT idle cap)"

Event-driven backtest of ONE variant, trading ONLY USD1USDT, built as a thin extension
of the FAITHFUL engine (backtest/bt_faithful.py). We REUSE its load() + constants and do
NOT edit any shared file.

Three coordinated mechanisms vs the base strategy:
  (1) NO-LOSS-SALE   : maker sell limit is ALWAYS buy + sell_bp (default 3bp); never lowered
                       to market, never below buy. The base strategy's 3-day forced
                       loss-escape is REMOVED — underwater USD1 is just held (holding USD1
                       IS the 10% benchmark, so waiting for the +3bp pop has ~0 opp cost).
  (2) 3bp SELL TARGET: wider than base's 1-2bp so the price leg stays net-positive after
                       adverse selection (nets ~2bp at adv=1.0 where the base nets <=0).
  (3) TAKER RE-ENTRY : if sitting in USDT for >= idle_cap_days (default 0.5d) without the
                       passive buy filling, cross the spread with a marketable buy
                       (fill = open*(1+taker_cost_bp/1e4)) to immediately resume 10% APR.

ADVERSE SELECTION is applied on EVERY MAKER fill:
    passive buy  -> L * (1 + adv/1e4)
    maker  sell  -> S * (1 - adv/1e4)
The TAKER buy pays an explicit spread cross (taker_cost_bp), NOT maker adverse selection,
because a taker is not resting in the book and is not adversely selected. A conservative
sensitivity (adv_on_taker=True, adv ADDED on top of taker_cost) is also reported — its
effect is negligible (~6-11 taker fills over 6.7 months).

UTA interest (10%/yr) accrues every bar that USD1 is held, on the position's market value
(qty*close*ypb), exactly like the faithful engine. Idle USDT earns 0%.

No-lookahead: identical to bt_faithful — open[i] is the live market, ema/trend come from the
already-CLOSED 1h candle merged by load().
"""
import os, sys
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, APR, ALLOC, BPD, MKT_VOL, run as base_run  # reuse shared engine

SYM = "USD1USDT"
DAY_MS = 86400_000


def run_variant(adverse_bp=0.0, with_yield=True,
                sell_bp=3.0, idle_cap_days=0.5, taker_cost_bp=1.0,
                no_loss=True, taker_on=True, adv_on_taker=False, detail=False):
    """Faithful single-position USD1USDT backtest of the variant. Returns a result dict."""
    df = load(SYM)
    apr = APR[SYM] if with_yield else 0.0
    ypb = apr / 365 / BPD
    sell_delta = sell_bp / 1e4            # 3bp -> 0.0003

    cash = ALLOC; pos = None; accr = 0.0
    trades = []; eq = []; turn = 0.0
    inpos = 0; nbar = 0
    n_taker = 0; n_passive = 0
    idle_since = float(df.ts.iloc[0])     # strategy start anchors the first idle window

    for r in df.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close
        ema = r.ema55_1h
        now = r.ts
        nbar += 1
        if pos:
            accr += pos["qty"] * c * ypb
            inpos += 1

        if pos is None:
            filled = False
            # (1) PRIMARY: passive maker buy-the-dip (same geometry as base)
            proposed = o
            if proposed - ema < 0.0001:                       # 1bp entry gate
                L = ema if proposed > ema else proposed        # min(open, ema55_1h)
                if r.ema21_down:
                    L -= 0.0001                                # downtrend nudge (1 tick)
                L = round(L, 4)                                # 1 tick = 1bp price floor
                if l <= L:                                     # passive fill
                    eff = L * (1 + adverse_bp / 1e4)           # adverse haircut, maker buy
                    pos = dict(buy=eff, qty=ALLOC / eff, ft=now, taker=False)
                    cash -= ALLOC; turn += ALLOC
                    filled = True; n_passive += 1
            # (2) THE ANGLE: taker re-entry idle cap
            if (not filled) and taker_on and (now - idle_since) >= idle_cap_days * DAY_MS:
                bump = taker_cost_bp / 1e4
                if adv_on_taker:
                    bump += adverse_bp / 1e4                   # conservative add-on (sensitivity)
                eff = o * (1 + bump)                           # marketable buy, always fills
                pos = dict(buy=eff, qty=ALLOC / eff, ft=now, taker=True)
                cash -= ALLOC; turn += ALLOC
                n_taker += 1
        else:
            # EXIT: maker limit sell at buy + sell_bp. NO-LOSS-SALE, no forced escape.
            buy = pos["buy"]
            S = round(buy + sell_delta, 4)
            if no_loss and S <= buy:                          # never price a losing sell
                S = round(buy + sell_delta, 4)
            hit = (S <= o) or (h >= S)                         # open already above, or high tags it
            if hit:
                f = S * (1 - adverse_bp / 1e4)                 # adverse haircut, maker sell
                proc = pos["qty"] * f
                trades.append(dict(hold_d=(now - pos["ft"]) / DAY_MS,
                                   pnl=proc - ALLOC + accr,
                                   price_bp=(f - buy) / buy * 1e4,
                                   taker=pos["taker"]))
                cash += proc + accr
                pos = None; accr = 0.0
                idle_since = float(now)                        # reset idle timer at the sell

        eq.append(cash + (pos["qty"] * c + accr if pos else 0))

    final = eq[-1]
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / DAY_MS
    tr = pd.DataFrame(trades)
    peak = pd.Series(eq).cummax(); dd = ((pd.Series(eq) - peak) / peak).min()

    # max stuck = longest single time in USD1 (closed trades + the still-open position)
    open_hold = (df.ts.iloc[-1] - pos["ft"]) / DAY_MS if pos else 0.0
    max_stuck = max((tr.hold_d.max() if len(tr) else 0.0), open_hold)

    return dict(
        adv=adverse_bp,
        n=len(tr), n_passive=n_passive, n_taker=n_taker,
        win=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0,
        avg_hold_d=round(tr.hold_d.mean(), 2) if len(tr) else 0,
        avg_price_bp=round(tr.price_bp.mean(), 2) if len(tr) else 0,
        ret_pct=round((final / ALLOC - 1) * 100, 3),
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        mdd_pct=round(dd * 100, 3),
        turn_per_day=turn / span,
        max_stuck_d=round(max_stuck, 1),
        avg_hold=round(tr.hold_d.mean(), 2) if len(tr) else 0,
        n_loss=int((tr.price_bp < 0).sum()) if len(tr) else 0,
        worst_bp=round(tr.price_bp.min(), 2) if len(tr) else 0,
        open_at_end=(pos is not None),
        open_underwater_bp=round((c / pos["buy"] - 1) * 1e4, 2) if pos else 0.0,
        tim_pct=round(inpos / nbar * 100, 2),
        idle_days=round((nbar - inpos) / BPD, 2),
        span_d=round(span, 1),
        _trades=tr if detail else None,
        _open=(dict(buy=pos["buy"], ft=pos["ft"], taker=pos["taker"], hold_d=round(open_hold, 1))
               if (detail and pos) else None),
    )


def hold_bench(with_yield=True):
    """True buy-and-hold USD1 benchmark on the SAME data + same interest model.
       One clean entry at the first open (no haircut -> strongest possible benchmark)."""
    df = load(SYM)
    apr = APR[SYM] if with_yield else 0.0
    ypb = apr / 365 / BPD
    p0 = df.open.iloc[0]
    qty = ALLOC / p0
    accr = 0.0; eq = []
    for r in df.itertuples():
        accr += qty * r.close * ypb
        eq.append(qty * r.close + accr)
    final = eq[-1]
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / DAY_MS
    ret = final / ALLOC - 1
    drift = df.close.iloc[-1] / df.open.iloc[0] - 1
    return dict(apr_pct=round(ret * 100 * 365 / span, 3),
                ret_pct=round(ret * 100, 3),
                drift_pct=round(drift * 100, 4),
                drift_apr=round(drift * 100 * 365 / span, 3),
                span_d=round(span, 1), p0=p0, p_last=df.close.iloc[-1])


if __name__ == "__main__":
    ADVS = [0.0, 0.5, 1.0, 1.5, 2.0]
    print("=" * 78)
    print("VARIANT r1_1: USD1 Taker-Reentry (no-loss-sale + 3bp + 0.5d idle cap)")
    print(f"data span, alloc ${ALLOC:.0f}, single position, trade ONLY {SYM}")
    print("=" * 78)

    hb = hold_bench(True)
    print(f"\nHOLD BENCHMARK (true buy-and-hold USD1, same data+interest):")
    print(f"  span={hb['span_d']}d  p0={hb['p0']:.4f} -> p_last={hb['p_last']:.4f}")
    print(f"  price drift = {hb['drift_pct']:.4f}% over span  (= {hb['drift_apr']:.3f}% APR)")
    print(f"  HOLD total APR (drift + 10% interest) = {hb['apr_pct']:.3f}%")
    print(f"  STATED benchmark = 10.000% APR")

    print(f"\n{'adv bp/side':<14}{'VARIANT APR':>12}{'vs10%':>9}{'vsHOLD':>9}"
          f"{'BASE APR':>10}{'trades':>8}{'avgPx_bp':>9}{'TIM%':>7}{'idleD':>7}{'MDD%':>8}{'stuckD':>8}")
    rows = {}
    for a in ADVS:
        v = run_variant(a, with_yield=True)
        b = base_run(SYM, True, a)
        rows[a] = v
        print(f"{a:<14.1f}{v['apr_pct']:>12.3f}{v['apr_pct']-10.0:>+9.3f}"
              f"{v['apr_pct']-hb['apr_pct']:>+9.3f}{b['apr_pct']:>10.2f}"
              f"{v['n']:>8}{v['avg_price_bp']:>9}{v['tim_pct']:>7}{v['idle_days']:>7}"
              f"{v['mdd_pct']:>8}{v['max_stuck_d']:>8}")

    print(f"\nTrade breakdown @ adv=1.0: passive={rows[1.0]['n_passive']} taker={rows[1.0]['n_taker']} "
          f"losses={rows[1.0]['n_loss']} worst={rows[1.0]['worst_bp']}bp "
          f"open_at_end={rows[1.0]['open_at_end']} open_uw={rows[1.0]['open_underwater_bp']}bp")

    print("\n--- TAKER ISOLATION (identical params, taker ON vs OFF) ---")
    for a in [0.5, 1.0]:
        on = run_variant(a, taker_on=True)
        off = run_variant(a, taker_on=False)
        print(f"  adv={a}: ON apr={on['apr_pct']:.3f} TIM={on['tim_pct']}% idle={on['idle_days']}d  |  "
              f"OFF apr={off['apr_pct']:.3f} TIM={off['tim_pct']}% idle={off['idle_days']}d  |  "
              f"delta={on['apr_pct']-off['apr_pct']:+.3f}")

    print("\n--- CONSERVATIVE: adverse selection ALSO applied to the taker leg ---")
    for a in [0.5, 1.0, 1.5]:
        cons = run_variant(a, adv_on_taker=True)
        norm = rows[a]
        print(f"  adv={a}: spec-model apr={norm['apr_pct']:.3f}  adv-on-taker apr={cons['apr_pct']:.3f}  "
              f"delta={cons['apr_pct']-norm['apr_pct']:+.4f}")

    print("\n--- ROBUSTNESS: taker_cost_bp sweep (adv=1.0) ---")
    for tc in [0.5, 1.0, 2.0, 3.0]:
        v = run_variant(1.0, taker_cost_bp=tc)
        print(f"  taker_cost={tc}bp: apr={v['apr_pct']:.3f}  takers={v['n_taker']}")
    print("--- ROBUSTNESS: idle_cap_days sweep (adv=1.0) ---")
    for ic in [0.1, 0.5, 1.0, 2.0]:
        v = run_variant(1.0, idle_cap_days=ic)
        print(f"  idle_cap={ic}d: apr={v['apr_pct']:.3f}  TIM={v['tim_pct']}%  takers={v['n_taker']}")
    print("--- ROBUSTNESS: sell_bp sweep (adv=1.0) ---")
    for sb in [2.0, 3.0, 4.0, 5.0]:
        v = run_variant(1.0, sell_bp=sb)
        print(f"  sell_bp={sb}: apr={v['apr_pct']:.3f}  trades={v['n']}  avgPx={v['avg_price_bp']}bp  stuckD={v['max_stuck_d']}")

    print("\n--- CAPACITY (@ $10k alloc) ---")
    v1 = rows[1.0]
    pct = v1['turn_per_day'] / MKT_VOL[SYM] * 100
    scale = ALLOC * (0.02 * MKT_VOL[SYM] / v1['turn_per_day'])
    print(f"  turnover/day = ${v1['turn_per_day']:,.0f} = {pct:.3f}% of ${MKT_VOL[SYM]:,}/day mkt")
    print(f"  to stay <2% of daily volume -> scalable to ~${scale:,.0f} notional")
    print(f"  capacity_ok (turnover < 2% of $2.5M/day): {pct < 2.0}")
