"""
VARIANT R1.2 — DIP-GATED DEFAULT-HOLD (independent faithful reimplementation).

This is a clean, self-contained re-implementation of the spec'd variant, written
from the SPEC text (not copied from bt_bigdip.run_holdfirst) so it doubles as an
independent cross-check. It reuses ONLY bt_faithful.load() for the no-lookahead
1h-EMA merge and the shared constants (APR/ALLOC/BPD/MKT_VOL). No shared file edited.

STRATEGY (USD1USDT only):
  Default state = LONG USD1 (the yield asset earns 10% APR UTA interest while held).
  - INIT: buy the full alloc into USD1 at the very first bar (like the 10% hold
          benchmark). Defaulting to the yield asset instead of idle USDT is the
          single change that makes the angle viable.
  - EXIT (only while LONG): rest a maker SELL at round(ema21_1h + exit_spike_bp/1e4, 4).
          Fills at the bar open if it gaps above, else at the target if high >= target.
          NEVER sell below cost: if the post-haircut sell price <= entry price we keep
          holding USD1 (it re-pegs) and keep earning interest. -> realized losses = 0.
  - RE-ENTRY (only while idle in USDT): buy the full alloc back into USD1 on the first
          5m bar whose open <= ema21_1h - entry_dip_bp/1e4 (price dipped below the
          responsive 21h mean). Idle USDT (0% yield) exists only between a spike-sell
          and the next dip-buy; minimizing it is the whole game.

FILL MODEL (faithful to bt_faithful, no idealization beyond the swept adv knob):
  - passive top-of-book at the bar open; adverse-selection haircut on EVERY fill:
        buy  eff = round(price,4) * (1 + adv/1e4)   (you pay more)
        sell f   = limit          * (1 - adv/1e4)   (you receive less)
  - adv=0 is the perfect-fill CEILING (front-of-queue), not real. The strict win bar
    is adv >= 0.5 bp/side.
  - one position max; entry XOR exit per bar (no same-bar round trip), exactly like
    bt_faithful (the if/elif on pos state structurally guarantees this).

NO LOOKAHEAD: ema21_1h comes from load()'s merge_asof on avail_ts = 1h_ts + 3600000,
so a 5m bar only ever sees a 1h EMA whose candle already CLOSED. Decisions use the bar
OPEN as the live price. Interest accrues per-bar on the close, identical to bt_faithful.

Params (locked for this variant): entry_dip_bp=1, exit_spike_bp=4, ref=ema21_1h.
Reproduce: python3 backtest/bt_variant_r1_2.py
"""
import os, sys
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from bt_faithful import load, APR, ALLOC, BPD, MKT_VOL  # reuse loader + constants


def run_variant(sym="USD1USDT", with_yield=True, adverse_bp=0.0,
                entry_dip_bp=1.0, exit_spike_bp=4.0, ref="ema21_1h"):
    df = load(sym)
    mean = df[ref].values
    o_ = df["open"].values
    h_ = df["high"].values
    c_ = df["close"].values
    ts_ = df["ts"].values
    n = len(df)

    apr = APR[sym] if with_yield else 0.0
    ypb = apr / 365.0 / BPD                       # interest fraction per 5m bar

    cash = ALLOC
    pos = None                                    # dict(buy=eff_price, qty, ft=fill_ts)
    accr = 0.0                                    # accrued (unsettled) interest for open pos
    started = False
    eq = []
    trades = []
    turn = 0.0
    inpos_bars = 0
    idle_bars = 0
    nbar = 0
    last_exit_ts = None
    max_idle_d = 0.0

    for i in range(n):
        o, h, c, m, ts = o_[i], h_[i], c_[i], mean[i], ts_[i]
        nbar += 1
        if m != m:                                # NaN guard (no-op post-dropna; defensive)
            eq.append(cash + (pos["qty"] * c + accr if pos else 0.0))
            continue

        # --- per-bar UTA interest accrual, based on state at the START of the bar ---
        if pos is not None:
            accr += pos["qty"] * c * ypb
            inpos_bars += 1
        elif started:
            idle_bars += 1

        # --- INIT: default into the yield asset on the first usable bar ---
        if not started:
            eff = round(o, 4) * (1 + adverse_bp / 1e4)   # haircut on EVERY fill
            pos = dict(buy=eff, qty=ALLOC / eff, ft=ts)
            cash -= ALLOC
            turn += ALLOC
            started = True
            eq.append(cash + pos["qty"] * c + accr)
            continue

        if pos is None:
            # --- RE-ENTRY (idle in USDT): gated on a dip below the responsive mean ---
            if o <= m - entry_dip_bp / 1e4:
                eff = round(o, 4) * (1 + adverse_bp / 1e4)
                if last_exit_ts is not None:
                    max_idle_d = max(max_idle_d, (ts - last_exit_ts) / 86400_000)
                pos = dict(buy=eff, qty=ALLOC / eff, ft=ts)
                cash -= ALLOC
                turn += ALLOC
        else:
            # --- EXIT (holding USD1): maker sell at spike target, never below cost ---
            buy = pos["buy"]
            T = round(m + exit_spike_bp / 1e4, 4)
            if o >= T:                            # gapped above -> fill at the open
                S = o
            elif h >= T:                          # target touched intrabar
                S = T
            else:
                S = None
            if S is not None:
                f = S * (1 - adverse_bp / 1e4)    # haircut on EVERY fill
                if f > buy:                       # NEVER realize a loss (USD1 re-pegs)
                    proc = pos["qty"] * f
                    trades.append(dict(
                        hold_d=(ts - pos["ft"]) / 86400_000,
                        price_bp=(f - buy) / buy * 1e4,
                        pnl=proc - ALLOC + accr,
                    ))
                    cash += proc + accr
                    turn += proc
                    pos = None
                    accr = 0.0
                    last_exit_ts = ts

        eq.append(cash + (pos["qty"] * c + accr if pos else 0.0))

    final = eq[-1]
    span = (ts_[-1] - ts_[0]) / 86400_000
    tr = pd.DataFrame(trades)
    eqs = pd.Series(eq)
    peak = eqs.cummax()
    mdd = ((eqs - peak) / peak).min()

    return dict(
        sym=sym, adv=adverse_bp, entry_dip_bp=entry_dip_bp, exit_spike_bp=exit_spike_bp,
        ref=ref, span_d=round(span, 1), n=len(tr),
        apr_pct=round((final / ALLOC - 1) * 100 * 365 / span, 2),
        ret_pct=round((final / ALLOC - 1) * 100, 3),
        mdd_pct=round(mdd * 100, 3),
        tim_pct=round(inpos_bars / nbar * 100, 1),       # time-in-USD1 %
        idle_pct=round(idle_bars / nbar * 100, 1),       # idle-USDT %
        max_idle_d=round(max_idle_d, 2),                 # longest 0-yield USDT stretch
        max_hold_d=round(tr.hold_d.max(), 1) if len(tr) else 0.0,  # longest USD1 hold (earning)
        avg_price_bp=round(tr.price_bp.mean(), 2) if len(tr) else 0.0,
        n_loss=int((tr.price_bp < 0).sum()) if len(tr) else 0,
        turn_per_day=turn / span,
        open_at_end=(pos is not None),
    )


def benchmark_hold(sym="USD1USDT", with_yield=True, adverse_bp=0.0):
    """Honest realized buy-and-hold: buy full alloc at the first open (optionally
    haircut), accrue interest on every close, mark final value at the last close.
    With adv=0 and the observed +15bp re-peg drift this lands ~10.28% (> the stated
    10% bar). The STATED benchmark is a flat 10% (pure interest, no drift)."""
    df = load(sym)
    o0 = df["open"].iloc[0]
    cN = df["close"].iloc[-1]
    span = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 86400_000
    eff = o0 * (1 + adverse_bp / 1e4)
    qty = ALLOC / eff
    ypb = (APR[sym] if with_yield else 0.0) / 365.0 / BPD
    interest = (qty * df["close"] * ypb).sum()
    final = qty * cN + interest
    return round((final / ALLOC - 1) * 100 * 365 / span, 2)


def half_sample(adv):
    """Regime check: split the 5m frame in two and run each half independently."""
    df = load("USD1USDT")
    mid = df["ts"].iloc[len(df) // 2]
    out = {}
    for tag, sub in (("H1", df[df["ts"] < mid]), ("H2", df[df["ts"] >= mid])):
        m = sub[ref_global].values
        o_ = sub["open"].values; h_ = sub["high"].values
        c_ = sub["close"].values; ts_ = sub["ts"].values
        n = len(sub)
        ypb = APR["USD1USDT"] / 365.0 / BPD
        cash = ALLOC; pos = None; accr = 0.0; started = False; eq = []
        for i in range(n):
            o, h, c, mm, ts = o_[i], h_[i], c_[i], m[i], ts_[i]
            if mm != mm:
                eq.append(cash + (pos["qty"] * c + accr if pos else 0.0)); continue
            if pos is not None:
                accr += pos["qty"] * c * ypb
            if not started:
                eff = round(o, 4) * (1 + adv / 1e4)
                pos = dict(buy=eff, qty=ALLOC / eff, ft=ts)
                cash -= ALLOC; started = True
                eq.append(cash + pos["qty"] * c + accr); continue
            if pos is None:
                if o <= mm - 1.0 / 1e4:
                    eff = round(o, 4) * (1 + adv / 1e4)
                    pos = dict(buy=eff, qty=ALLOC / eff, ft=ts); cash -= ALLOC
            else:
                buy = pos["buy"]; T = round(mm + 4.0 / 1e4, 4)
                S = o if o >= T else (T if h >= T else None)
                if S is not None:
                    f = S * (1 - adv / 1e4)
                    if f > buy:
                        proc = pos["qty"] * f
                        cash += proc + accr; pos = None; accr = 0.0
            eq.append(cash + (pos["qty"] * c + accr if pos else 0.0))
        span = (ts_[-1] - ts_[0]) / 86400_000
        strat = round((eq[-1] / ALLOC - 1) * 100 * 365 / span, 2)
        # realized hold over this half
        o0 = sub["open"].iloc[0]; cN = sub["close"].iloc[-1]
        qty = ALLOC / o0; interest = (qty * sub["close"] * ypb).sum()
        hold = round((( qty * cN + interest) / ALLOC - 1) * 100 * 365 / span, 2)
        out[tag] = (strat, hold)
    return out


ref_global = "ema21_1h"

if __name__ == "__main__":
    S = "USD1USDT"
    advs = [0, 0.5, 1.0, 1.5, 2.0]
    print("=" * 78)
    print("VARIANT R1.2 — DIP-GATED DEFAULT-HOLD  (USD1USDT, entry_dip=1bp, exit_spike=4bp)")
    print("=" * 78)
    bench_real = benchmark_hold(S, True, 0.0)
    print(f"STATED benchmark (hold USD1)          : 10.00% APR")
    print(f"HONEST realized hold (adv0, +15bp drift): {bench_real:.2f}% APR")
    print()
    print(f"{'adv bp/side':>12}{'TOTAL_APR':>11}{'vs 10%':>9}{'vs realHold':>12}"
          f"{'n':>4}{'tim%':>7}{'idle%':>7}{'mIdle_d':>9}{'mHold_d':>9}"
          f"{'avgPx_bp':>10}{'MDD%':>8}{'turn/day':>11}")
    res = {}
    for a in advs:
        r = run_variant(S, True, a, 1.0, 4.0, "ema21_1h")
        res[a] = r
        d10 = r["apr_pct"] - 10.0
        dH = r["apr_pct"] - bench_real
        print(f"{a:>12}{r['apr_pct']:>11.2f}{d10:>+9.2f}{dH:>+12.2f}"
              f"{r['n']:>4}{r['tim_pct']:>7}{r['idle_pct']:>7}{r['max_idle_d']:>9.2f}"
              f"{r['max_hold_d']:>9.1f}{r['avg_price_bp']:>10.2f}{r['mdd_pct']:>8.3f}"
              f"{r['turn_per_day']:>11,.0f}")

    # price-only (no interest) to isolate the overlay edge
    print("\nPRICE-ONLY APR (no interest, isolates the reversion overlay edge):")
    for a in [0, 0.5, 1.0]:
        rp = run_variant(S, False, a, 1.0, 4.0, "ema21_1h")
        print(f"  adv={a}: price_only_APR={rp['apr_pct']:.2f}%  n={rp['n']}  avgPx={rp['avg_price_bp']}bp")

    # capacity (headroom = how far we can scale before turnover hits 2% of volume)
    r1 = res[1.0]
    headroom_x = 0.02 * MKT_VOL[S] / r1["turn_per_day"]
    cap_size = ALLOC * headroom_x
    print(f"\nCAPACITY @ ${ALLOC:,.0f} alloc:")
    print(f"  turnover/day = ${r1['turn_per_day']:,.0f}  = "
          f"{r1['turn_per_day'] / MKT_VOL[S] * 100:.3f}% of ${MKT_VOL[S]:,}/day mkt vol")
    print(f"  to stay < 2% of volume: max size ~= ${cap_size:,.0f} "
          f"({headroom_x:.0f}x headroom)  capacity_ok={r1['turn_per_day'] < 0.02 * MKT_VOL[S]}")

    # regime / half-sample robustness
    print("\nHALF-SAMPLE (regime) at adv=0.5 and adv=1.0  [strat vs realized hold]:")
    for a in [0.5, 1.0]:
        hs = half_sample(a)
        print(f"  adv={a}:  H1 strat={hs['H1'][0]:.2f} (hold {hs['H1'][1]:.2f})   "
              f"H2 strat={hs['H2'][0]:.2f} (hold {hs['H2'][1]:.2f})")

    print("\nCROSS-CHECK vs bt_bigdip.run_holdfirst (should match this impl):")
    try:
        from bt_bigdip import run_holdfirst
        for a in advs:
            mine = res[a]["apr_pct"]
            ref = run_holdfirst(S, True, a, 1.0, 4.0, "ema21_1h")["apr_pct"]
            flag = "OK" if abs(mine - ref) < 0.01 else "MISMATCH"
            print(f"  adv={a}: mine={mine:.2f}  ref={ref:.2f}  [{flag}]")
    except Exception as e:
        print(f"  (cross-check skipped: {e})")
