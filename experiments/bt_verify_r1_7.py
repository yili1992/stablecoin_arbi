"""
bt_verify_r1_7.py — INDEPENDENT adversarial re-implementation of variant r1_7
"PAAL — Peg-Anchored Asymmetric Ladder (default-long, extreme-premium skim)".

Written from scratch (own structure, own fill helpers, explicit trade log + ts).
Reuses ONLY load() from bt_faithful for the identical no-lookahead 5m frame.
Does NOT import bt_variant_r1_7 or bt_paal — this is a clean cross-check.

Logic being verified (from the variant spec):
  Anchor peg=1.0. Two states, single all-in position.
  LONG  (earns 10% APR on qty*close per bar):
     resting SELL limit S = round(1.0 + 15bp, 4) = 1.0015
       wick   fill: (S<=open) or (high>=S)
       strict fill: close>=S
     on fill -> sell at S*(1-adv/1e4), go FLAT.  No stop, no forced sell.
  FLAT  (0% yield):
     initial bar -> MARKET buy at open
     resting BUY limit B = round(1.0 + 4bp, 4) = 1.0004
       wick   fill: low<=B
       strict fill: close<=B
     idle-timeout: flat >= 2.0d (576 bars) -> MARKET buy at open
     on fill -> go LONG.
  Adverse haircut on EVERY fill: buy*(1+adv/1e4), sell*(1-adv/1e4).
  tick: round limits to 4dp.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, ALLOC, BPD
import pandas as pd

PEG = 1.0
S_LIMIT = round(PEG + 15 / 1e4, 4)   # 1.0015
B_LIMIT = round(PEG + 4 / 1e4, 4)    # 1.0004
TIMEOUT_BARS = 576                    # 2.0 days * 288
MKT_VOL_USD1 = 2_538_200.0
APR_USD1 = 0.10


def sell_fills(o, h, c, wick):
    return ((S_LIMIT <= o) or (h >= S_LIMIT)) if wick else (c >= S_LIMIT)


def buy_fills(o, l, c, wick):
    return (l <= B_LIMIT) if wick else (c <= B_LIMIT)


def simulate(rows, wick=True, adv=0.0, with_yield=True, enable_sell=True):
    """rows: list of (ts,o,h,l,c). Returns metrics dict + trade log.
    Each run starts FLAT and market-buys on its first bar (so a sub-slice is a
    self-contained 'run the strategy only over this window' experiment)."""
    ypb = (APR_USD1 / 365 / BPD) if with_yield else 0.0
    cash = ALLOC
    long = False
    qty = 0.0
    buy_eff = 0.0
    ft = None
    accr = 0.0
    started = False
    flat_run = 0          # consecutive flat bars
    turn = 0.0
    inpos_bars = 0
    n_bars = 0
    eq = []
    trades = []
    max_flat_bars = 0
    max_hold_d = 0.0

    for (ts, o, h, l, c) in rows:
        n_bars += 1
        if long:
            accr += qty * c * ypb
            inpos_bars += 1
            if enable_sell and sell_fills(o, h, c, wick):
                f = S_LIMIT * (1 - adv / 1e4)
                proc = qty * f
                cash = proc + accr
                hold_d = (ts - ft) / 86400e3
                max_hold_d = max(max_hold_d, hold_d)
                trades.append(dict(buy_ts=ft, sell_ts=ts, hold_d=hold_d,
                                   buy=buy_eff, sell=f,
                                   price_bp=(f - buy_eff) / buy_eff * 1e4,
                                   forced_buy=False))
                long = False
                qty = 0.0
                accr = 0.0
                flat_run = 0
        else:
            flat_run += 1
            max_flat_bars = max(max_flat_bars, flat_run)
            do_buy = None     # (price, forced?)
            if not started:
                do_buy = (o, False)
            elif buy_fills(o, l, c, wick):
                do_buy = (B_LIMIT, False)
            elif flat_run >= TIMEOUT_BARS:
                do_buy = (o, True)
            if do_buy is not None:
                px, forced = do_buy
                buy_eff = px * (1 + adv / 1e4)
                qty = cash / buy_eff
                ft = ts
                turn += cash
                cash = 0.0
                long = True
                started = True
                flat_run = 0
                # tag the most-recent (open) leg's forced status via a sentinel trade
                if forced:
                    trades.append(dict(buy_ts=ts, sell_ts=None, hold_d=None,
                                       buy=buy_eff, sell=None, price_bp=None,
                                       forced_buy=True))
        eq.append(cash + (qty * c + accr if long else 0.0))

    last_ts = rows[-1][0]
    if long:
        max_hold_d = max(max_hold_d, (last_ts - ft) / 86400e3)
        turn += 0.0   # final hold never sells -> no extra turnover
    final = eq[-1]
    span = (last_ts - rows[0][0]) / 86400e3
    eqs = pd.Series(eq)
    dd = ((eqs - eqs.cummax()) / eqs.cummax()).min()
    sells = [t for t in trades if t["sell"] is not None]
    forced_buys = sum(1 for t in trades if t.get("forced_buy"))
    bp = pd.Series([t["price_bp"] for t in sells]) if sells else pd.Series(dtype=float)
    return dict(
        n_sells=len(sells),
        forced_buys=forced_buys,
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        ret=round((final / ALLOC - 1) * 100, 4),
        mdd=round(dd * 100, 3),
        tim=round(inpos_bars / n_bars * 100, 2),
        turn_day=round(turn / span, 1),
        max_flat_d=round(max_flat_bars / BPD, 2),
        max_hold_d=round(max_hold_d, 1),
        win=round((bp > 0).mean() * 100, 1) if len(bp) else None,
        worst_bp=round(bp.min(), 3) if len(bp) else None,
        avg_bp=round(bp.mean(), 3) if len(bp) else None,
        span_d=round(span, 1),
        final=round(final, 2),
    ), trades


def get_rows(sym, ts_lo=None, ts_hi=None):
    df = load(sym)
    if ts_lo is not None:
        df = df[df.ts >= ts_lo]
    if ts_hi is not None:
        df = df[df.ts < ts_hi]
    df = df.reset_index(drop=True)
    return list(zip(df.ts, df.open, df.high, df.low, df.close)), df


if __name__ == "__main__":
    SYM = "USD1USDT"
    ADVS = [0, 0.5, 1.0, 1.5]
    rows, df = get_rows(SYM)
    print("=" * 80)
    print("INDEPENDENT VERIFY of r1_7 (PAAL S=+15bp B=+4bp timeout=2d)")
    print(f"span {len(rows)} bars / {(rows[-1][0]-rows[0][0])/86400e3:.1f}d  alloc ${ALLOC:.0f}")
    print("=" * 80)

    print("\n[A] FULL WINDOW — buy&hold (do-nothing) total APR")
    for a in ADVS:
        r, _ = simulate(rows, wick=True, adv=a, enable_sell=False)
        print(f"   adv={a:<4} APR={r['apr']:>7.3f}  TIM={r['tim']}  ret={r['ret']}")

    print("\n[B] FULL WINDOW — strategy, WICK fill")
    for a in ADVS:
        r, _ = simulate(rows, wick=True, adv=a)
        print(f"   adv={a:<4} APR={r['apr']:>7.3f}  vs10={r['apr']-10:+.3f}  "
              f"n={r['n_sells']} forcedBuy={r['forced_buys']} TIM={r['tim']} "
              f"maxFlat={r['max_flat_d']}d maxHold={r['max_hold_d']}d "
              f"turn/d=${r['turn_day']:.0f} MDD={r['mdd']} worst={r['worst_bp']}bp")

    print("\n[C] FULL WINDOW — strategy, STRICT close-through fill")
    for a in ADVS:
        r, _ = simulate(rows, wick=False, adv=a)
        print(f"   adv={a:<4} APR={r['apr']:>7.3f}  vs10={r['apr']-10:+.3f}  n={r['n_sells']} forcedBuy={r['forced_buys']}")

    # ---- trade log (wick adv0.5) ----
    print("\n[D] TRADE LOG (wick, adv0.5) — timestamps prove regime concentration")
    r, trades = simulate(rows, wick=True, adv=0.5)
    for t in trades:
        if t["sell"] is not None:
            bt = pd.to_datetime(t["buy_ts"], unit="ms")
            st = pd.to_datetime(t["sell_ts"], unit="ms")
            print(f"   BUY {bt}  SELL {st}  hold={t['hold_d']:.2f}d  "
                  f"px {t['buy']:.5f}->{t['sell']:.5f}  {t['price_bp']:+.2f}bp")
        else:
            bt = pd.to_datetime(t["buy_ts"], unit="ms")
            print(f"   FORCED MKT-BUY {bt}  px {t['buy']:.5f}")

    # ---- half split (regime / overfit test) ----
    mid_ts = df.ts.iloc[len(df) // 2]
    print(f"\n[E] HALF-SPLIT at {pd.to_datetime(mid_ts,unit='ms')} (overfit/regime stability)")
    rows1, _ = get_rows(SYM, ts_hi=mid_ts)
    rows2, _ = get_rows(SYM, ts_lo=mid_ts)
    for label, rr in [("H1 (1st half)", rows1), ("H2 (2nd half)", rows2)]:
        print(f"  {label}  {len(rr)} bars / {(rr[-1][0]-rr[0][0])/86400e3:.1f}d")
        for a in [0.5, 1.0]:
            st, _ = simulate(rr, wick=True, adv=a)
            bh, _ = simulate(rr, wick=True, adv=a, enable_sell=False)
            print(f"     adv={a:<4} strat APR={st['apr']:>7.3f}  b&h APR={bh['apr']:>7.3f}  "
                  f"skim_alpha={st['apr']-bh['apr']:+.3f}  n_sells={st['n_sells']}")

    # ---- second-half ONLY out-of-spike-regime test ----
    last_spike = df[df.high >= S_LIMIT].ts.max()
    print(f"\n[F] POST-SPIKE-REGIME (after last spike {pd.to_datetime(last_spike,unit='ms')}) — true OOS")
    rows_oos, _ = get_rows(SYM, ts_lo=last_spike)
    for a in [0.5, 1.0]:
        st, _ = simulate(rows_oos, wick=True, adv=a)
        bh, _ = simulate(rows_oos, wick=True, adv=a, enable_sell=False)
        print(f"     adv={a:<4} strat APR={st['apr']:>7.3f}  b&h APR={bh['apr']:>7.3f}  "
              f"n_sells={st['n_sells']}  ({(rows_oos[-1][0]-rows_oos[0][0])/86400e3:.1f}d)")

    # ---- capacity, peak-window turnover ----
    print("\n[G] CAPACITY")
    r05, _ = simulate(rows, wick=True, adv=0.5)
    pct = r05['turn_day'] / MKT_VOL_USD1 * 100
    print(f"   span-avg turnover/d=${r05['turn_day']:.0f} = {pct:.4f}% of ${MKT_VOL_USD1:,.0f}/day mkt")
    # peak window turnover (active spike window)
    active = df[(df.ts >= 1766534400000)]   # ~2025-12-24
    active = active[active.ts < 1770336000000]  # ~2026-02-06+
    rows_act, _ = get_rows(SYM, ts_lo=1766534400000, ts_hi=1770336000000)
    if rows_act:
        ract, _ = simulate(rows_act, wick=True, adv=0.5)
        print(f"   ACTIVE-WINDOW (Dec24-Feb6) turnover/d=${ract['turn_day']:.0f} "
              f"= {ract['turn_day']/MKT_VOL_USD1*100:.4f}% of mkt  (n_sells={ract['n_sells']})")
        cap_active = ALLOC * (0.02 * MKT_VOL_USD1 / ract['turn_day']) if ract['turn_day'] else float('inf')
        print(f"   max capital under 2% cap using ACTIVE-window turnover: ${cap_active:,.0f}")
    cap_span = ALLOC * (0.02 * MKT_VOL_USD1 / r05['turn_day'])
    print(f"   max capital under 2% cap using SPAN-avg turnover (claim's method): ${cap_span:,.0f}")
