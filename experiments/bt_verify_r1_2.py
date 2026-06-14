"""
INDEPENDENT ADVERSARIAL VERIFICATION of variant r1_2:
  "DIP-GATED DEFAULT-HOLD" (USD1USDT)
   default = LONG USD1 (earns 10% APR while held)
   sell  when bar shows price >= ema21_1h + 4bp   (spike), never below cost
   rebuy when bar open      <= ema21_1h - 1bp      (dip)

Written from the SPEC, my own structure (explicit two-state machine + pluggable
fill model). Reuses ONLY bt_faithful.load() for the no-lookahead 1h-EMA merge and
shared constants. Does NOT import the file under test.

The point is not to re-print their number; it is to ATTACK it:
  fill_mode='market' : their model (sell at max(o,T), buy at o)         <- reproduce
  fill_mode='limit'  : SAME triggers but fills capped at the limit      <- kills gap bonus
  fill_mode='maker'  : pure passive (trigger low<=Lb / high>=T, fill@lim)<- realistic maker
plus: realized-hold benchmark, exits-disabled identity (interest sanity),
      half-sample regime split, param-sensitivity grid, capacity, sell instrumentation.
"""
import os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bt_faithful import load, APR, ALLOC, BPD, MKT_VOL

DAY_MS = 86_400_000


def simulate(o_, h_, l_, c_, m_, ts_, adv, dip_bp=1.0, spike_bp=4.0,
             with_yield=True, fill_mode="market", disable_exit=False,
             default_hold=True):
    """Two-state machine over pre-extracted numpy arrays.

    States: HOLD (pos!=None, earning interest) / IDLE (pos==None, 0 yield).
    Interest accrues on the state at the START of each bar (matches bt_faithful).
    """
    apr = APR["USD1USDT"] if with_yield else 0.0
    ypb = apr / 365.0 / BPD
    n = len(o_)

    cash = ALLOC
    pos = None              # (entry_eff_price, qty, fill_ts)
    accr = 0.0
    started = False
    eq = np.empty(n)
    turn = 0.0
    hold_bars = 0
    idle_bars = 0
    last_exit_ts = None
    max_idle_d = 0.0
    trades = []
    sell_gap = 0           # instrumentation: sells that gapped above target (o>=T)
    sell_touch = 0         # sells that only touched intrabar (o<T<=h)
    gap_excess_bp = []     # how much extra (o-T)/T the gap fills harvested

    for i in range(n):
        o, h, l, c, m, ts = o_[i], h_[i], l_[i], c_[i], m_[i], ts_[i]

        # --- interest on state at START of bar ---
        if pos is not None:
            accr += pos[1] * c * ypb
            hold_bars += 1
        elif started:
            idle_bars += 1

        # --- INIT: default into USD1 on the very first usable bar ---
        if not started:
            started = True
            if default_hold:
                eff = round(o, 4) * (1 + adv / 1e4)
                pos = (eff, ALLOC / eff, ts)
                cash -= ALLOC
                turn += ALLOC
            eq[i] = cash + (pos[1] * c + accr if pos else 0.0)
            continue

        if pos is None:
            # IDLE: rebuy on a dip below the responsive mean
            Lb = m - dip_bp / 1e4
            if o <= Lb:
                if fill_mode == "maker":
                    # pure passive: only fills if the LOW reached the limit; fill AT limit
                    if l <= Lb:
                        px = Lb
                    else:
                        px = None
                elif fill_mode == "limit":
                    # same trigger as 'market' (open<=Lb) but no gap-down bonus: pay Lb
                    px = Lb
                else:  # market
                    px = o
                if px is not None:
                    eff = round(px, 4) * (1 + adv / 1e4)
                    if last_exit_ts is not None:
                        max_idle_d = max(max_idle_d, (ts - last_exit_ts) / DAY_MS)
                    pos = (eff, ALLOC / eff, ts)
                    cash -= ALLOC
                    turn += ALLOC
            elif fill_mode == "maker":
                # even if open>Lb, a resting maker buy fills if low pierces Lb
                if l <= Lb:
                    eff = round(Lb, 4) * (1 + adv / 1e4)
                    if last_exit_ts is not None:
                        max_idle_d = max(max_idle_d, (ts - last_exit_ts) / DAY_MS)
                    pos = (eff, ALLOC / eff, ts)
                    cash -= ALLOC
                    turn += ALLOC
        elif not disable_exit:
            # HOLD: spike-sell, never below cost
            buy = pos[0]
            T = round(m + spike_bp / 1e4, 4)
            S = None
            is_gap = False
            if o >= T:
                S = o if fill_mode == "market" else T   # gap bonus only in 'market'
                is_gap = True
            elif h >= T:
                S = T                                    # intrabar touch: maker price T
            if S is not None:
                f = S * (1 - adv / 1e4)
                if f > buy:                              # NEVER realize a loss
                    proc = pos[1] * f
                    trades.append((( ts - pos[2]) / DAY_MS, (f - buy) / buy * 1e4))
                    if is_gap:
                        sell_gap += 1
                        gap_excess_bp.append((o - T) / T * 1e4)
                    else:
                        sell_touch += 1
                    cash += proc + accr
                    turn += proc
                    pos = None
                    accr = 0.0
                    last_exit_ts = ts

        eq[i] = cash + (pos[1] * c + accr if pos else 0.0)

    final = eq[-1]
    span = (ts_[-1] - ts_[0]) / DAY_MS
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    holds = np.array([t[0] for t in trades]) if trades else np.array([])
    pxbp = np.array([t[1] for t in trades]) if trades else np.array([])
    return dict(
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 2),
        ret=round((final / ALLOC - 1) * 100, 3),
        n=len(trades),
        n_loss=int((pxbp < 0).sum()) if len(pxbp) else 0,
        avg_px_bp=round(float(pxbp.mean()), 2) if len(pxbp) else 0.0,
        mdd=round(float(mdd) * 100, 3),
        tim=round(hold_bars / n * 100, 1),
        idle=round(idle_bars / n * 100, 1),
        max_idle_d=round(max_idle_d, 2),
        max_hold_d=round(float(holds.max()), 1) if len(holds) else 0.0,
        turn_day=turn / span,
        open_end=(pos is not None),
        sell_gap=sell_gap, sell_touch=sell_touch,
        gap_excess_bp=round(float(np.mean(gap_excess_bp)), 2) if gap_excess_bp else 0.0,
        final=final, span=span,
    )


def realized_hold(o_, c_, ts_, adv=0.0, with_yield=True):
    """Honest buy-and-hold: buy at first open (optional haircut), accrue interest on
    every close, mark at last close. This is the TRUE opportunity cost of running the
    strategy (you would otherwise just hold USD1)."""
    span = (ts_[-1] - ts_[0]) / DAY_MS
    eff = o_[0] * (1 + adv / 1e4)
    qty = ALLOC / eff
    ypb = (APR["USD1USDT"] if with_yield else 0.0) / 365.0 / BPD
    interest = float((qty * c_ * ypb).sum())
    final = qty * c_[-1] + interest
    return round((final / ALLOC - 1) * 100 * 365 / span, 2)


def arrays(df):
    return (df["open"].values, df["high"].values, df["low"].values,
            df["close"].values, df["ema21_1h"].values, df["ts"].values)


if __name__ == "__main__":
    df = load("USD1USDT")
    o_, h_, l_, c_, m_, ts_ = arrays(df)
    advs = [0, 0.5, 1.0, 1.5, 2.0]

    print("=" * 92)
    print("INDEPENDENT VERIFY — DIP-GATED DEFAULT-HOLD (USD1USDT, dip=1bp, spike=4bp, ref=ema21_1h)")
    print("=" * 92)
    print(f"span={ (ts_[-1]-ts_[0])/DAY_MS:.1f}d  open0={o_[0]}  closeN={c_[-1]}  "
          f"(+{(c_[-1]/o_[0]-1)*1e4:.1f}bp one-time price drift over window)")
    h0 = realized_hold(o_, c_, ts_, 0.0)
    print(f"STATED benchmark = 10.00% (flat interest).  HONEST realized hold (adv0) = {h0:.2f}% "
          f"(= interest + drift; the real opportunity cost)\n")

    # ---- 1) reproduce their 'market' fill, plus stricter fills ----
    print("TOTAL APR by adverse selection and fill model:")
    print(f"{'adv':>5} | {'market(claim)':>14} {'limit(no-gap)':>14} {'maker(passive)':>15} "
          f"| {'realizedHold':>13}")
    table = {}
    for a in advs:
        rm = simulate(o_, h_, l_, c_, m_, ts_, a, fill_mode="market")
        rl = simulate(o_, h_, l_, c_, m_, ts_, a, fill_mode="limit")
        rk = simulate(o_, h_, l_, c_, m_, ts_, a, fill_mode="maker")
        hh = realized_hold(o_, c_, ts_, a)
        table[a] = (rm, rl, rk, hh)
        print(f"{a:>5} | {rm['apr']:>14.2f} {rl['apr']:>14.2f} {rk['apr']:>15.2f} | {hh:>13.2f}")

    print("\nDetail @ market fill:")
    print(f"{'adv':>5}{'apr':>8}{'n':>4}{'nloss':>6}{'avgPx':>8}{'tim%':>7}{'idle%':>7}"
          f"{'mIdle_d':>9}{'mHold_d':>9}{'gapSell':>8}{'touchSell':>10}{'gapXSbp':>9}{'turn/d':>10}")
    for a in advs:
        r = table[a][0]
        print(f"{a:>5}{r['apr']:>8.2f}{r['n']:>4}{r['n_loss']:>6}{r['avg_px_bp']:>8.2f}"
              f"{r['tim']:>7}{r['idle']:>7}{r['max_idle_d']:>9.2f}{r['max_hold_d']:>9.1f}"
              f"{r['sell_gap']:>8}{r['sell_touch']:>10}{r['gap_excess_bp']:>9.2f}{r['turn_day']:>10,.0f}")

    # ---- 2) interest sanity: exits disabled must equal realized hold ----
    dis = simulate(o_, h_, l_, c_, m_, ts_, 0.0, disable_exit=True)
    print(f"\nINTEREST SANITY (no double-count): exits-disabled APR = {dis['apr']:.2f}  vs "
          f"realized-hold = {h0:.2f}  -> diff {abs(dis['apr']-h0):.3f} (should be ~0)")

    # ---- 3) price-only (isolate overlay, strip interest) ----
    print("\nPRICE-ONLY APR (no interest) — isolates the reversion/overlay+drift edge:")
    for a in [0, 0.5, 1.0, 1.5]:
        pm = simulate(o_, h_, l_, c_, m_, ts_, a, with_yield=False, fill_mode="market")
        pl = simulate(o_, h_, l_, c_, m_, ts_, a, with_yield=False, fill_mode="limit")
        ph = realized_hold(o_, c_, ts_, a, with_yield=False)
        print(f"  adv={a}: market={pm['apr']:.2f}  limit={pl['apr']:.2f}  (price-only hold={ph:.2f})")

    # ---- 4) half-sample regime split ----
    print("\nHALF-SAMPLE (overfit/regime). strat = market fill; hold = realized hold:")
    half = len(df) // 2
    for tag, sl in (("H1", slice(0, half)), ("H2", slice(half, None))):
        oo, hh2, ll, cc, mm, tt = (o_[sl], h_[sl], l_[sl], c_[sl], m_[sl], ts_[sl])
        print(f"  {tag} ({(tt[-1]-tt[0])/DAY_MS:.0f}d, "
              f"{oo[0]:.4f}->{cc[-1]:.4f}, drift {(cc[-1]/oo[0]-1)*1e4:+.1f}bp):")
        for a in [0.5, 1.0]:
            sM = simulate(oo, hh2, ll, cc, mm, tt, a, fill_mode="market")
            sL = simulate(oo, hh2, ll, cc, mm, tt, a, fill_mode="limit")
            hd = realized_hold(oo, cc, tt, a)
            print(f"     adv={a}: strat_market={sM['apr']:>6.2f}  strat_limit={sL['apr']:>6.2f}  "
                  f"hold={hd:>6.2f}  beats10={sM['apr']>10}/{sL['apr']>10}  "
                  f"beatsHold={sM['apr']>hd}")

    # ---- 5) param sensitivity at adv=0.5 (overfit probe) ----
    print("\nPARAM SENSITIVITY @ adv=0.5 (market fill). TOTAL APR over (dip_bp, spike_bp):")
    dips = [0.5, 1.0, 2.0, 3.0, 5.0]
    spikes = [2.0, 3.0, 4.0, 5.0, 8.0]
    hdr = "  dip\\spike" + "".join(f"{s:>8.0f}" for s in spikes)
    print(hdr)
    for d in dips:
        row = f"  {d:>8.1f} "
        for s in spikes:
            r = simulate(o_, h_, l_, c_, m_, ts_, 0.5, dip_bp=d, spike_bp=s, fill_mode="market")
            row += f"{r['apr']:>8.2f}"
        print(row)

    # ---- 6) capacity ----
    r1 = table[1.0][0]
    cap = ALLOC * (0.02 * MKT_VOL["USD1USDT"] / r1["turn_day"])
    print(f"\nCAPACITY @ ${ALLOC:,.0f}: turnover=${r1['turn_day']:,.0f}/day = "
          f"{r1['turn_day']/MKT_VOL['USD1USDT']*100:.3f}% of ${MKT_VOL['USD1USDT']:,}/day "
          f"-> stay<2%: size<=${cap:,.0f}  capacity_ok={r1['turn_day']<0.02*MKT_VOL['USD1USDT']}")

    # ---- 7) verdict summary ----
    print("\n" + "=" * 92)
    print("VERDICT INPUTS (strict criterion = TOTAL APR beats 10% at adv>=0.5):")
    for a in [0.5, 1.0, 1.5]:
        rm, rl, rk, hh = table[a]
        print(f"  adv={a}: market={rm['apr']:.2f} (>10:{rm['apr']>10}) | "
              f"limit={rl['apr']:.2f} (>10:{rl['apr']>10}) | "
              f"maker={rk['apr']:.2f} (>10:{rk['apr']>10}) | hold={hh:.2f} | "
              f"edge_vs_hold(market)={rm['apr']-hh:+.2f}")
