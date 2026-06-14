"""
VARIANT r1_0 — "Base-Hold-90 + Thin Peg-Band Scalp" (USD1USDT ONLY).
Reuses bt_faithful.load() (same data, same no-lookahead 1h merge, same adverse conventions).
Self-contained NEW file; does not edit any shared module.

STRUCTURE
  INCEPTION (t0, once): deploy 100% of capital into USD1 at the first available open.
    Split BASE (90%) + SCALP (10%); BOTH start fully in USD1 -> zero idle USDT at launch.
    Inception is a market/taker deploy -> adverse haircut applied to the fill (eff = open0*(1+adv/1e4)),
    identical to the realized-hold benchmark below, so the marginal comparison is fair.
  BASE (90%): never sells. Permanent USD1, accrues 10% APR forever. The engine of the return.
  SCALP (10%): 2-state machine (usd1 <-> usdt).
    usd1 -> place passive limit SELL at S=1.0002. fills if high>=S.
    usdt -> place passive limit BUY  at L=0.9995. fills if low<=L.  Then waits in USDT.
  NO stop-loss, NO time-stop (re-peg assumed; below-peg USD1 still earns 10% while it waits).

FILL MODEL (HEADLINE = engine-faithful, conservative):
  A passive resting limit order fills AT ITS LIMIT PRICE — buy at L, sell at S — NOT at a
  gapped-through open. This mirrors bt_faithful (`eff=L`, sell at S even on gap) and the task's
  explicit instruction "buy at limit*(1+adv/1e4), sell at limit*(1-adv/1e4)". A resting bid that
  the market gaps down through still fills at the bid (you provide liquidity at your price), so
  awarding the gapped-open price would be optimistic. `gap_fill=True` reproduces the SPEC's
  optimistic "fill at open if gapped" convention as a SENSITIVITY only.
  Adverse haircut on EVERY fill: buy eff = fill*(1+adv/1e4); sell eff = fill*(1-adv/1e4).

INTEREST MODEL (HEADLINE = engine-faithful dollar accrual, matches bt_faithful & bt_ladder):
  accr += held_USD1_qty * close * ypb  each bar,  ypb = APR/365/288.  Held separately, added to
  equity. `compound=True` instead grows the USD1 qty in place (spec's interest_routing); the two
  differ by < 0.01% APR over a ~0.55yr sample (verified), so the headline uses accrual for clean
  apples-to-apples vs the in-repo hold benchmark.

Conservative: at most ONE scalp transition per bar (no free intra-bar round trip).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_faithful as bt
import pandas as pd, numpy as np

SYM = "USD1USDT"
ALLOC = bt.ALLOC                 # 10000
BPD = bt.BPD                     # 288 bars/day (5m)
APR_USD1 = bt.APR[SYM]           # 0.10
MKT_VOL = bt.MKT_VOL[SYM]        # ~2,538,200 USD/day


def run_variant(adv=0.0, with_yield=True, base_frac=0.90, scalp_frac=0.10,
                buy_level=0.9995, sell_level=1.0002, apr=APR_USD1, sym=SYM,
                gap_fill=False, compound=False):
    df = bt.load(sym)
    ypb = (apr / 365 / BPD) if with_yield else 0.0
    o = df.open.values; h = df.high.values; l = df.low.values
    c = df.close.values; ts = df.ts.values
    n = len(df)
    L = round(buy_level, 4); S = round(sell_level, 4)   # tickSize=1bp -> 4dp grid

    # --- INCEPTION: 100% into USD1 at first open (adverse haircut on the taker fill) ---
    eff0 = o[0] * (1 + adv / 1e4)
    base_qty = (ALLOC * base_frac) / eff0      # permanent, never sells
    scalp_qty = (ALLOC * scalp_frac) / eff0    # starts in USD1
    scalp_cash = 0.0
    scalp_state = 'usd1'
    scalp_sell_px = eff0                        # px of last scalp sell (cost-basis bookkeeping)

    accr = 0.0
    turn = ALLOC                               # inception deploy counted once
    eq = []
    n_sell = 0; n_buy = 0
    cycle_bp = []                              # per-completed-cycle realized capture (bp)
    realized_capture = 0.0                     # $ price pnl harvested by scalp round trips
    scalp_usdt_bars = 0
    idle_start_ts = None                       # start of current USDT (idle) stretch
    max_idle_days = 0.0
    # informational: longest stretch scalp holds USD1 below its sell target (NOT "stuck"-bad: earns 10%)
    hold_start_ts = ts[0]
    max_hold_days = 0.0

    for i in range(n):
        oi, hi, li, ci, ti = o[i], h[i], l[i], c[i], ts[i]

        # interest on all USD1 held at START of bar
        held_qty = base_qty + (scalp_qty if scalp_state == 'usd1' else 0.0)
        if compound:
            base_qty *= (1 + ypb)
            if scalp_state == 'usd1':
                scalp_qty *= (1 + ypb)
        else:
            accr += held_qty * ci * ypb

        # one scalp transition per bar
        if scalp_state == 'usd1':
            if hi >= S:                                    # passive ASK fills
                fill_px = (oi if (gap_fill and oi > S) else S)
                f = fill_px * (1 - adv / 1e4)
                scalp_cash = scalp_qty * f
                scalp_sell_px = f
                turn += scalp_cash; n_sell += 1
                d = (ti - hold_start_ts) / 86400_000
                if d > max_hold_days: max_hold_days = d
                scalp_qty = 0.0
                scalp_state = 'usdt'
                idle_start_ts = ti
        else:                                              # in USDT
            scalp_usdt_bars += 1
            if li <= L:                                    # passive BID fills
                fill_px = (oi if (gap_fill and oi < L) else L)
                f = fill_px * (1 + adv / 1e4)
                newqty = scalp_cash / f
                realized_capture += (scalp_sell_px - f) * newqty
                cycle_bp.append((scalp_sell_px - f) / f * 1e4)
                scalp_qty = newqty
                turn += scalp_cash; n_buy += 1
                scalp_cash = 0.0
                scalp_state = 'usd1'
                hold_start_ts = ti
                if idle_start_ts is not None:
                    d = (ti - idle_start_ts) / 86400_000
                    if d > max_idle_days: max_idle_days = d
                    idle_start_ts = None

        scalp_val = (scalp_qty * ci if scalp_state == 'usd1' else scalp_cash)
        base_val = base_qty * ci
        eq.append(base_val + scalp_val + accr)             # accr=0 when compound=True

    # trailing idle stretch (ended in USDT)
    if idle_start_ts is not None:
        d = (ts[-1] - idle_start_ts) / 86400_000
        if d > max_idle_days: max_idle_days = d
    if scalp_state == 'usd1':
        d = (ts[-1] - hold_start_ts) / 86400_000
        if d > max_hold_days: max_hold_days = d

    final = eq[-1]
    span = (ts[-1] - ts[0]) / 86400_000
    eqs = pd.Series(eq); peak = eqs.cummax(); dd = ((eqs - peak) / peak).min()
    # interest/price split only meaningful in accrual mode (compound embeds interest in qty)
    int_apr = round(accr / ALLOC * 100 * 365 / span, 3) if not compound else None
    px_apr = round((final - ALLOC - accr) / ALLOC * 100 * 365 / span, 3) if not compound else None
    return dict(
        adv=adv,
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        ret_pct=round((final / ALLOC - 1) * 100, 3),
        interest_apr=int_apr,
        price_apr=px_apr,
        n_sell=n_sell, n_buy=n_buy, n_cycles=len(cycle_bp),
        avg_cycle_bp=round(float(np.mean(cycle_bp)), 3) if cycle_bp else 0.0,
        realized_capture_pct=round(realized_capture / ALLOC * 100, 4),
        scalp_idle_pct=round(scalp_usdt_bars / n * 100, 2),
        max_idle_days=round(max_idle_days, 2),
        max_scalp_hold_days=round(max_hold_days, 2),
        mdd_pct=round(dd * 100, 4),
        turn_per_day=turn / span,
        scalp_state_end=scalp_state,
        span_d=round(span, 1),
    )


def run_hold(adv=0.0, with_yield=True, apr=APR_USD1, sym=SYM):
    """Realized buy-and-hold USD1: 100% at open0 (same inception adverse), never sell, accrue 10%.
       Same dollar-accrual interest -> fair marginal benchmark for beats_hold."""
    df = bt.load(sym); ypb = (apr / 365 / BPD) if with_yield else 0.0
    o = df.open.values; c = df.close.values; ts = df.ts.values
    eff0 = o[0] * (1 + adv / 1e4); qty = ALLOC / eff0; accr = 0.0; eq = []
    for i in range(len(df)):
        accr += qty * c[i] * ypb
        eq.append(qty * c[i] + accr)
    final = eq[-1]; span = (ts[-1] - ts[0]) / 86400_000
    return dict(apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 3),
                ret_pct=round((final / ALLOC - 1) * 100, 3),
                interest_apr=round(accr / ALLOC * 100 * 365 / span, 3),
                price_apr=round((final - ALLOC - accr) / ALLOC * 100 * 365 / span, 3))


if __name__ == "__main__":
    import json
    ADVS = [0, 0.5, 1.0, 1.5]
    print("=" * 96)
    print("VARIANT r1_0: Base-Hold-90 + Thin Peg-Band Scalp  (USD1USDT only, full ~6.7mo)")
    print("benchmarks: literal hold = 10.000% APR | realized buy&hold (drift-incl) computed below")
    print(f"alloc=${ALLOC:.0f}  base/scalp = 90/10  buy@0.9995 sell@1.0002  yield=10%APR")
    print("=" * 96)

    print("\n--- REALIZED BUY&HOLD USD1 benchmark (same inception adverse + 10% accrual) ---")
    for a in ADVS:
        rh = run_hold(adv=a)
        print(f"  adv={a:<3}: APR={rh['apr_pct']:>7.3f}%  (interest {rh['interest_apr']:.3f} + price {rh['price_apr']:.3f})")

    print("\n--- VARIANT (HEADLINE: fill-at-limit, dollar-accrual interest) ---")
    hdr = (f"{'adv':>4}{'APR%':>9}{'int%':>8}{'px%':>7}{'vsHold':>8}{'vs10%':>8}"
           f"{'cyc':>5}{'avgBp':>7}{'idle%':>7}{'mxIdle_d':>9}{'MDD%':>8}{'turn/d$':>10}{'%mkt':>7}")
    print(hdr)
    rows = {}
    for a in ADVS:
        r = run_variant(adv=a)
        rh = run_hold(adv=a)
        rows[a] = r
        print(f"{a:>4}{r['apr_pct']:>9.3f}{r['interest_apr']:>8.3f}{r['price_apr']:>7.3f}"
              f"{r['apr_pct'] - rh['apr_pct']:>8.3f}{r['apr_pct'] - 10.0:>8.3f}"
              f"{r['n_cycles']:>5}{r['avg_cycle_bp']:>7.2f}{r['scalp_idle_pct']:>7.1f}"
              f"{r['max_idle_days']:>9.2f}{r['mdd_pct']:>8.3f}{r['turn_per_day']:>10,.0f}"
              f"{r['turn_per_day'] / MKT_VOL * 100:>7.3f}")

    print("\n--- SENSITIVITY: optimistic gap-fill (spec convention: fill at open if gapped) ---")
    for a in ADVS:
        r = run_variant(adv=a, gap_fill=True)
        print(f"  adv={a:<3}: APR={r['apr_pct']:>7.3f}%  (cycles {r['n_cycles']}, avgBp {r['avg_cycle_bp']:.2f})")

    print("\n--- SENSITIVITY: compound-into-USD1 interest (spec interest_routing) vs accrual ---")
    for a in [0.5]:
        ra = run_variant(adv=a, compound=False)
        rc = run_variant(adv=a, compound=True)
        print(f"  adv={a}: accrual APR={ra['apr_pct']:.3f}%  compound APR={rc['apr_pct']:.3f}%  "
              f"delta={rc['apr_pct'] - ra['apr_pct']:+.4f}%")

    print("\n--- CONSERVATISM: more drift-robust base fractions (fill-at-limit, adv=0.5) ---")
    for bf in [0.90, 0.92, 0.95]:
        r = run_variant(adv=0.5, base_frac=bf, scalp_frac=round(1 - bf, 2))
        print(f"  base={bf:.2f}: APR={r['apr_pct']:.3f}%  cycles={r['n_cycles']}  idle%={r['scalp_idle_pct']}")

    print("\n--- CAPACITY ---")
    r1 = run_variant(adv=1.0)
    tpd = r1['turn_per_day']
    pct = tpd / MKT_VOL * 100
    cap = ALLOC * 0.02 * MKT_VOL / tpd
    print(f"  @ ${ALLOC:,.0f} alloc: turnover/day = ${tpd:,.0f} = {pct:.4f}% of ${MKT_VOL:,}/day market")
    print(f"  scales linearly -> stays < 2% of market up to allocation ${cap:,.0f}")
    print(f"  at recommended $2.0M: turnover/day = ${2_000_000 / ALLOC * tpd:,.0f} = "
          f"{2_000_000 / ALLOC * tpd / MKT_VOL * 100:.3f}% of market")
    print(f"  max idle (USDT) stretch @adv1.0: {r1['max_idle_days']:.2f} days; "
          f"max scalp USD1-hold stretch: {r1['max_scalp_hold_days']:.2f} days (earns 10% while held)")

    # machine-readable summary
    out = {"variant": "Base-Hold-90 + Thin Peg-Band Scalp (USD1USDT)",
           "apr_adv0": rows[0]['apr_pct'], "apr_adv05": rows[0.5]['apr_pct'],
           "apr_adv10": rows[1.0]['apr_pct'], "apr_adv15": rows[1.5]['apr_pct'],
           "hold_adv05": run_hold(adv=0.5)['apr_pct'],
           "beats_10_adv05": bool(rows[0.5]['apr_pct'] > 10.0),
           "beats_hold_adv05": bool(rows[0.5]['apr_pct'] > run_hold(adv=0.5)['apr_pct']),
           "turn_pct_mkt_at_2M": round(2_000_000 / ALLOC * rows[1.0]['turn_per_day'] / MKT_VOL * 100, 3),
           "max_idle_days": rows[1.0]['max_idle_days'], "mdd_pct": rows[0.5]['mdd_pct']}
    print("\nJSON:", json.dumps(out))
