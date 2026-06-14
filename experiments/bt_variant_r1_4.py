"""
PARK-AND-COMPOUND  (variant r1_4)
=================================
USDe-parked idle sleeve on top of the FAITHFUL USD1 core.

Reuses bt_faithful.load() EXACTLY (open[i]=live market; ema55_1h / ema21_down /
ema55_up from the last CLOSED 1h candle; no lookahead) and the same adverse-
selection fill conventions (buy at limit*(1+adv/1e4), sell at limit*(1-adv/1e4)).

One state machine over 5m bars. 100% of capital is always in exactly ONE sleeve:
    {USD1-long (10% APR), USDe-parked (3.5% APR), transient idle-USDT (0%)}

Three on-theme levers vs the faithful base (which LOSES to 10% hold @adv>=0.5):
  (1) COMPOUND  : each USD1 buy redeploys 100% of current cash (principal + all
                  accrued USD1 interest + park yield). bt_faithful instead always
                  deploys the fixed ALLOC and leaves earned interest idle at 0%.
  (2) PARK      : after being continuously flat >= park_delay_h, deploy idle USDT
                  into park_asset (USDe) at 3.5% APR; unwind on the bar a USD1 buy
                  fills (marketable) or on an optional passive park take-profit.
  (3) REDEPLOY  : widen entry_gate_bp 1.0->1.5 and redeploy_band_bp 0->0.5 to
                  redeploy into 10% USD1 faster after each exit / catch more dips.

BASE EQUIVALENCE (verified in __main__): with
    compound=False, park=False, entry_gate_bp=1.0, redeploy_band_bp=0.0,
    dip_extra_bp=1.0, tp_bp=1, tp_up_bp=2, min_hold_d=2, force_exit_d=3
this reduces EXACTLY to bt_faithful.run("USD1USDT", with_yield=True, adv).

ADVERSE SELECTION: the swept `adv` is applied to EVERY fill, USD1 *and* park
(park_adv_bp defaults to adv). This is the honest reading of the win criterion
("adverse-selection adv >= 0.5 bp/side") -- no cherry-picked low park friction.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as B
import pandas as pd, numpy as np

ALLOC = B.ALLOC            # 10000.0
BPD = B.BPD                # 288
MKT_VOL = B.MKT_VOL        # {"USD1USDT":2_538_200,"USDEUSDT":17_278_905,"USDTBUSDT":878_295}
midnight_after = B.midnight_after


def run(adv=0.5, *, compound=True,
        entry_gate_bp=1.5, redeploy_band_bp=0.5, dip_extra_bp=1.0,
        tp_bp=1.0, tp_up_bp=2.0, min_hold_d=2, force_exit_d=3,
        park=True, park_asset="USDEUSDT", park_apr=0.035, park_adv_bp=None,
        park_delay_h=6.0, park_tp_bp=2.0, with_yield=True):
    """
    adv          : adverse-selection bp/side applied to every USD1 fill.
    park_adv_bp  : adverse-selection bp/side on park fills; None -> = adv.
    Returns a dict of metrics (APR decomposed into interest vs price, capacity,
    max stuck days for USD1 and park, etc.).
    """
    df = B.load("USD1USDT")
    pk = pd.read_csv(f"{B.DATA}/{park_asset}_5m.csv").sort_values("ts").reset_index(drop=True)
    m = df.merge(pk[["ts", "high", "low", "close"]].rename(
        columns={"high": "pH", "low": "pL", "close": "pC"}), on="ts", how="left")
    assert m["pC"].isna().sum() == 0, f"{park_asset} misaligned to USD1 grid"

    padv = adv if park_adv_bp is None else park_adv_bp
    u_ypb = (0.10 if with_yield else 0.0) / 365 / BPD
    p_ypb = (park_apr if with_yield else 0.0) / 365 / BPD
    EG = entry_gate_bp * 1e-4; RB = redeploy_band_bp * 1e-4; DE = dip_extra_bp * 1e-4
    TP = tp_bp * 1e-4; TPU = tp_up_bp * 1e-4; PTP = park_tp_bp * 1e-4
    park_delay_ms = park_delay_h * 3600_000.0

    cash = ALLOC
    upos = None; u_accr = 0.0          # USD1 sleeve
    ppos = None; p_accr = 0.0          # park sleeve
    flat_start = m.ts.iloc[0]          # we begin flat (idle USDT) at bar 0

    trades = []; ptrades = []; eq = []
    u_turn = 0.0; p_turn = 0.0
    tot_int = 0.0                      # cumulative interest (USD1 + park) for decomposition
    nbar = 0; usd1_bars = 0; park_bars = 0; idle_bars = 0
    park_durs = []

    for r in m.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close
        ema = r.ema55_1h; now = r.ts
        pC = r.pC; pH = r.pH
        nbar += 1

        # ---- accrue interest at bar start (mark on close), exactly like bt_faithful ----
        if upos is not None:
            bi = upos["qty"] * c * u_ypb
            u_accr += bi; tot_int += bi; usd1_bars += 1
        elif ppos is not None:
            bi = ppos["qty"] * pC * p_ypb
            p_accr += bi; tot_int += bi; park_bars += 1
        else:
            idle_bars += 1

        if upos is not None:
            # ============ USD1 EXIT (faithful, parameterized) ============
            buy = upos["buy"]
            target = buy + (TPU if r.ema55_up else TP)
            if now > midnight_after(upos["ft"], min_hold_d) and target > buy + TP:
                target = buy + TP
            if buy > o:
                target = o
            S = None if (buy - target >= 1.9e-4) else round(target, 4)
            if S is not None:
                if S - buy >= 1.9e-4:
                    allow = True
                elif now < midnight_after(upos["ft"], min_hold_d):
                    allow = False
                elif now > midnight_after(upos["ft"], force_exit_d) and S <= buy:
                    allow = True
                elif S <= buy:
                    allow = False
                else:
                    allow = True
                if allow:
                    filled = (S, True) if S <= o else ((S, True) if h >= S else (None, False))
                    if filled[1]:
                        f = filled[0] * (1 - adv / 1e4)
                        proc = upos["qty"] * f
                        trades.append(dict(hold_d=(now - upos["ft"]) / 86400_000.0,
                                           price_bp=(f - buy) / buy * 1e4))
                        cash += proc + u_accr; u_turn += proc
                        upos = None; u_accr = 0.0; flat_start = now
        else:
            # ============ NOT USD1-long: try USD1 entry, else manage park ============
            filled_usd1 = False
            if o - ema < EG:                                    # entry gate
                L = min(o, ema + RB)                            # passive limit
                if r.ema21_down:
                    L -= DE
                L = round(L, 4)
                if l <= L:                                      # passive buy fills
                    fill = L * (1 + adv / 1e4)
                    if ppos is not None:                        # SAME bar: unwind park first
                        pf = pC * (1 - padv / 1e4)
                        pproc = ppos["qty"] * pf
                        cash += pproc + p_accr; p_turn += pproc
                        ptrades.append(dict(dur_d=(now - ppos["ft"]) / 86400_000.0,
                                            price_bp=(pf - ppos["buy"]) / ppos["buy"] * 1e4,
                                            kind="unwind"))
                        park_durs.append((now - ppos["ft"]) / 86400_000.0)
                        ppos = None; p_accr = 0.0
                    deploy = cash if compound else ALLOC       # COMPOUND vs fixed ALLOC
                    qty = deploy / fill
                    upos = dict(buy=fill, qty=qty, ft=now)
                    cash -= deploy; u_turn += deploy
                    flat_start = None
                    filled_usd1 = True

            if not filled_usd1:
                if ppos is not None:
                    # optional passive park take-profit (captures USDe oscillation)
                    if PTP > 0:
                        tp_level = ppos["buy"] + PTP
                        if pH >= tp_level:
                            pf = tp_level * (1 - padv / 1e4)
                            pproc = ppos["qty"] * pf
                            cash += pproc + p_accr; p_turn += pproc
                            ptrades.append(dict(dur_d=(now - ppos["ft"]) / 86400_000.0,
                                                price_bp=(pf - ppos["buy"]) / ppos["buy"] * 1e4,
                                                kind="tp"))
                            park_durs.append((now - ppos["ft"]) / 86400_000.0)
                            ppos = None; p_accr = 0.0
                elif park and flat_start is not None and (now - flat_start) >= park_delay_ms \
                        and cash > 1e-9:
                    # park idle USDT (only long gaps clear this gate)
                    pbuy = pC * (1 + padv / 1e4)
                    ppos = dict(buy=pbuy, qty=cash / pbuy, ft=now)
                    p_turn += cash; cash = 0.0; p_accr = 0.0

        uval = (upos["qty"] * c + u_accr) if upos else 0.0
        pval = (ppos["qty"] * pC + p_accr) if ppos else 0.0
        eq.append(cash + uval + pval)

    final = eq[-1]
    span = (m.ts.iloc[-1] - m.ts.iloc[0]) / 86400_000.0
    tr = pd.DataFrame(trades); ptr = pd.DataFrame(ptrades)
    eqs = pd.Series(eq); peak = eqs.cummax(); dd = ((eqs - peak) / peak).min()

    # park max duration includes a still-open park at the end
    pmax = max(park_durs) if park_durs else 0.0
    if ppos is not None:
        pmax = max(pmax, (m.ts.iloc[-1] - ppos["ft"]) / 86400_000.0)

    ret = final / ALLOC - 1.0
    apr = ret * 365.0 / span
    int_apr = (tot_int / ALLOC) * 365.0 / span * 100.0
    price_apr = (final - ALLOC - tot_int) / ALLOC * 365.0 / span * 100.0

    return dict(
        adv=adv, park_adv=padv, compound=compound, park=park, park_asset=park_asset,
        span_d=round(span, 1),
        apr_raw=apr * 100, final_raw=final,        # unrounded, for base-equivalence check
        apr_pct=round(apr * 100, 3),
        int_apr_pct=round(int_apr, 3),
        price_apr_pct=round(price_apr, 3),
        ret_pct=round(ret * 100, 3),
        n_usd1=len(tr),
        n_park=len(ptr),
        n_park_tp=int((ptr["kind"] == "tp").sum()) if len(ptr) else 0,
        win_pct=round((tr.price_bp > 0).mean() * 100, 1) if len(tr) else 0.0,
        avg_px_bp=round(tr.price_bp.mean(), 3) if len(tr) else 0.0,
        avg_hold_d=round(tr.hold_d.mean(), 2) if len(tr) else 0.0,
        max_usd1_hold_d=round(tr.hold_d.max(), 1) if len(tr) else 0.0,
        max_park_dur_d=round(pmax, 2),
        n_loss=int((tr.price_bp < 0).sum()) if len(tr) else 0,
        worst_bp=round(tr.price_bp.min(), 2) if len(tr) else 0.0,
        mdd_pct=round(dd * 100, 4),
        usd1_tim_pct=round(usd1_bars / nbar * 100, 1),
        park_tim_pct=round(park_bars / nbar * 100, 1),
        idle_tim_pct=round(idle_bars / nbar * 100, 1),
        usd1_turn_per_day=round(u_turn / span, 0),
        park_turn_per_day=round(p_turn / span, 0),
        open_at_end=("USD1" if upos else ("PARK" if ppos else "idle")),
    )


# ----- base-equivalence param set: must reproduce bt_faithful exactly -----
BASE_EQUIV = dict(compound=False, park=False, entry_gate_bp=1.0, redeploy_band_bp=0.0,
                  dip_extra_bp=1.0, tp_bp=1.0, tp_up_bp=2.0, min_hold_d=2, force_exit_d=3,
                  park_tp_bp=0.0)

# ----- locked headline variant config (from SPEC params) -----
VARIANT = dict(compound=True, entry_gate_bp=1.5, redeploy_band_bp=0.5, dip_extra_bp=1.0,
               tp_bp=1.0, tp_up_bp=2.0, min_hold_d=2, force_exit_d=3,
               park=True, park_asset="USDEUSDT", park_apr=0.035, park_adv_bp=None,
               park_delay_h=6.0, park_tp_bp=2.0)
ADV_SWEEP = [0, 0.5, 1.0, 1.5]


def _faithful_raw(adv):
    """Inline UNROUNDED copy of bt_faithful.run loop -> (final_equity, apr%).
    bt_faithful.run rounds apr to 2dp, so we recompute raw here to prove EXACT
    equality (not just 2dp agreement)."""
    df = B.load("USD1USDT"); ypb = 0.10 / 365 / B.BPD
    cash = B.ALLOC; pos = None; accr = 0.0; eq = []
    for r in df.itertuples():
        o, h, l, c = r.open, r.high, r.low, r.close; ema = r.ema55_1h
        if pos: accr += pos["qty"] * c * ypb
        if pos is None:
            if o - ema < 0.0001:
                L = ema if o > ema else o
                if r.ema21_down: L -= 0.0001
                L = round(L, 4)
                if l <= L:
                    eff = L * (1 + adv / 1e4)
                    pos = dict(buy=eff, qty=B.ALLOC / eff, ft=r.ts); cash -= B.ALLOC
        else:
            buy = pos["buy"]; now = r.ts
            base = buy + 0.0002 if r.ema55_up else buy + 0.0001
            if now > B.midnight_after(pos["ft"], 2) and base > buy + 0.0001: base = buy + 0.0001
            if buy > o: base = o
            S = None if (buy - base >= 0.00019) else round(base, 4)
            if S is not None:
                if S - buy >= 0.00019: allow = True
                elif now < B.midnight_after(pos["ft"], 2): allow = False
                elif now > B.midnight_after(pos["ft"], 3) and S <= buy: allow = True
                elif S <= buy: allow = False
                else: allow = True
                if allow:
                    filled = (S, True) if S <= o else ((S, True) if h >= S else (None, False))
                    if filled[1]:
                        f = filled[0] * (1 - adv / 1e4); proc = pos["qty"] * f
                        cash += proc + accr; pos = None; accr = 0.0
        eq.append(cash + (pos["qty"] * c + accr if pos else 0))
    final = eq[-1]; span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400_000
    return final, (final / B.ALLOC - 1) * 100 * 365 / span


def _check_base_equivalence():
    print("=== BASE EQUIVALENCE CHECK (variant base_equiv == bt_faithful, UNROUNDED) ===")
    ok = True
    for a in ADV_SWEEP:
        ff, fa = _faithful_raw(a)
        r = run(a, **BASE_EQUIV)
        match = abs(ff - r["final_raw"]) < 1e-7
        ok = ok and match
        print(f"  adv={a:>4}: faithful=${ff:.6f} ({fa:.4f}%)  variant=${r['final_raw']:.6f} "
              f"({r['apr_raw']:.4f}%)  dFinal={abs(ff-r['final_raw']):.1e}  "
              f"{'EXACT' if match else 'DIFF'}")
    print(f"  -> {'PASS - exact superset of faithful base' if ok else 'FAIL'}\n")
    return ok


if __name__ == "__main__":
    print("PARK-AND-COMPOUND variant r1_4  (USD1USDT core + USDe park sleeve)")
    print(f"data ~6.7mo 5m, alloc ${ALLOC:.0f}, benchmark hold USD1 = 10.00% APR\n")

    _check_base_equivalence()

    print("=== HEADLINE VARIANT  (compound + USDe park + redeploy), park_adv = adv ===")
    print(f"config: {VARIANT}\n")
    hdr = (f"{'adv':>5}{'APR%':>8}{'int%':>7}{'px%':>7}{'vsHold':>8}"
           f"{'nU':>4}{'nP':>4}{'nTP':>4}{'win%':>6}{'avgPx':>7}{'mxUhold':>8}"
           f"{'mxPark':>7}{'U-tim%':>7}{'P-tim%':>7}{'idle%':>6}{'end':>6}")
    print(hdr)
    head = {}
    for a in ADV_SWEEP:
        r = run(a, **VARIANT); head[a] = r
        print(f"{a:>5}{r['apr_pct']:>8.3f}{r['int_apr_pct']:>7.2f}{r['price_apr_pct']:>7.2f}"
              f"{r['apr_pct']-10:>+8.3f}{r['n_usd1']:>4}{r['n_park']:>4}{r['n_park_tp']:>4}"
              f"{r['win_pct']:>6.0f}{r['avg_px_bp']:>7.2f}{r['max_usd1_hold_d']:>8.1f}"
              f"{r['max_park_dur_d']:>7.2f}{r['usd1_tim_pct']:>7.1f}{r['park_tim_pct']:>7.1f}"
              f"{r['idle_tim_pct']:>6.1f}{r['open_at_end']:>6}")

    print("\n=== CAPACITY  (turnover/day vs 2% of market daily volume) ===")
    for a in [0.5, 1.0]:
        r = head[a]
        u_pct = r['usd1_turn_per_day'] / MKT_VOL['USD1USDT'] * 100
        p_pct = r['park_turn_per_day'] / MKT_VOL[VARIANT['park_asset']] * 100
        print(f"  adv={a}: USD1 turn/d=${r['usd1_turn_per_day']:>9,.0f} = {u_pct:>5.3f}% of $2.5M "
              f"(<2%? {'Y' if u_pct < 2 else 'N'}) | park turn/d=${r['park_turn_per_day']:>8,.0f} = "
              f"{p_pct:>6.4f}% of ${MKT_VOL[VARIANT['park_asset']]:,} (<2%? {'Y' if p_pct < 2 else 'N'})")

    print("\n=== ATTRIBUTION: peel back each lever @adv=0.5 (park_adv=adv) ===")
    cfgs = {
        "base faithful          ": BASE_EQUIV,
        "+compound only         ": {**BASE_EQUIV, "compound": True},
        "+compound +redeploy    ": {**BASE_EQUIV, "compound": True, "entry_gate_bp": 1.5, "redeploy_band_bp": 0.5},
        "+compound +park(no tp) ": {**BASE_EQUIV, "compound": True, "park": True, "park_delay_h": 6.0, "park_tp_bp": 0.0},
        "FULL variant           ": VARIANT,
    }
    for name, cfg in cfgs.items():
        r = run(0.5, **cfg)
        print(f"  {name} APR={r['apr_pct']:>7.3f}%  (int {r['int_apr_pct']:>5.2f} + px {r['price_apr_pct']:>5.2f})"
              f"  nP={r['n_park']} maxParkD={r['max_park_dur_d']}")

    print("\n=== PARAM SWEEPS (APR%, park_adv=adv) ===")
    print("park_delay_h:")
    for pd_h in [0, 6, 24]:
        row = " ".join(f"adv{a}={run(a, **{**VARIANT,'park_delay_h':pd_h})['apr_pct']:.3f}" for a in ADV_SWEEP)
        print(f"   delay={pd_h:>4}h: {row}")
    print("redeploy_band_bp:")
    for rb in [0, 0.5, 1.0]:
        row = " ".join(f"adv{a}={run(a, **{**VARIANT,'redeploy_band_bp':rb})['apr_pct']:.3f}" for a in ADV_SWEEP)
        print(f"   band={rb:>4}bp: {row}")
    print("entry_gate_bp:")
    for eg in [1.0, 1.5, 2.0]:
        row = " ".join(f"adv{a}={run(a, **{**VARIANT,'entry_gate_bp':eg})['apr_pct']:.3f}" for a in ADV_SWEEP)
        print(f"   gate={eg:>4}bp: {row}")
    print("park_asset (with its realistic park_adv):")
    for pa, padv in [("USDEUSDT", None), ("USDTBUSDT", 1.0)]:
        row = " ".join(f"adv{a}={run(a, **{**VARIANT,'park_asset':pa,'park_adv_bp':padv})['apr_pct']:.3f}" for a in ADV_SWEEP)
        print(f"   {pa:<10} (park_adv={padv}): {row}")
    print("park_tp_bp on/off @full variant:")
    for ptp in [0.0, 2.0]:
        row = " ".join(f"adv{a}={run(a, **{**VARIANT,'park_tp_bp':ptp})['apr_pct']:.3f}" for a in ADV_SWEEP)
        print(f"   park_tp={ptp:>4}bp: {row}")

    print("\n=== SENSITIVITY: keep park_adv FIXED at 0.5 (USDe is liquid, $17M/day) ===")
    for a in ADV_SWEEP:
        r = run(a, **{**VARIANT, "park_adv_bp": 0.5})
        print(f"  adv={a:>4} (park_adv=0.5): APR={r['apr_pct']:>7.3f}%  vsHold {r['apr_pct']-10:>+.3f}")
