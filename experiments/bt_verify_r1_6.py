"""
INDEPENDENT adversarial verification of claimed-winning variant r1_6.
Written from scratch (own structure) — does NOT import the claimed bt_variant_r1_6.py.
Only reuses bt_faithful.load() (the shared no-lookahead data loader) and its constants,
as instructed. Goal: reproduce the adv sweep, then HUNT for fake-win causes.

Variant spec (re-derived from the spec docstring, not copied from the claim's loop):
  - Home = 100% USD1 (10% APR UTA interest while a slice holds USD1; USDT = 0% yield).
  - 5 independent slices, NAV fractions [0.15,0.18,0.20,0.22,0.25] on sell rungs
    [+5,+7,+10,+14,+20] bp above ema21_1h.
  - t0: deploy all capital USDT->USD1 at open[0] with adverse haircut buy=L*(1+adv/1e4).
  - A 'usd1' slice rests a sell limit R = round(ema21_1h + rung_bp/1e4, 4).
       fill (maker) when price reaches R during the bar; recv = R*(1-adv/1e4) -> 'usdt'.
  - A 'usdt' slice rests a buy limit B = round(ema21_1h + rebuy_off/1e4, 4) (rebuy_off=-1).
       fill (maker) when price reaches B during the bar; pay = B*(1+adv/1e4) -> 'usd1'.
  - Re-price every 5m bar (orders float with the EMA). No stop, no time-stop.
  - At most ONE state transition per slice per bar.
  - Interest accrues per bar only on usd1 slices into a separate, non-reinvested bucket.

Fill realism modes (adversarial):
  'touch'  : resting order fills if the bar merely reaches the level (high>=R / low<=B
             or an immediate gap R<=open / B>=open). Matches the claim's convention.
  'strict' : order fills only if price trades THROUGH the level (high>R / low<B), i.e.
             a same-price exact kiss at the back of a thin queue does NOT fill.
  'delay1' : order must rest a full bar; fill is evaluated on the FOLLOWING bar's range
             (latency / queue-position proxy on top of the adverse haircut).
"""
import pandas as pd, numpy as np, os, sys, json
sys.path.insert(0, os.path.dirname(__file__))
import bt_faithful as bt

ALLOC = bt.ALLOC          # 10000.0
BPD = bt.BPD              # 288 5m-bars/day
APR_USD1 = 0.10
SYM = "USD1USDT"
ANCHOR = "ema21_1h"
RUNG_BP = [5, 7, 10, 14, 20]
FRACS = [0.15, 0.18, 0.20, 0.22, 0.25]
REBUY_OFF_BP = -1
MKT_VOL = bt.MKT_VOL[SYM]  # 2,538,200 USD1/day ADV

_CACHE = {}
def _data(sym):
    if sym not in _CACHE:
        df = bt.load(sym)
        _CACHE[sym] = dict(
            o=df.open.values.astype(float), h=df.high.values.astype(float),
            l=df.low.values.astype(float), c=df.close.values.astype(float),
            ts=df.ts.values.astype(np.int64), anc=df[ANCHOR].values.astype(float),
            n=len(df))
    return _CACHE[sym]


def _sell_fill(R, oi, hi, fill_mode):
    """Does a resting sell limit at R fill within this bar?"""
    if fill_mode == 'strict':
        return (R < oi) or (hi > R)          # must trade through
    return (R <= oi) or (hi >= R)            # touch / immediate gap


def _buy_fill(B, oi, li, fill_mode):
    """Does a resting buy limit at B fill within this bar?"""
    if fill_mode == 'strict':
        return (B > oi) or (li < B)
    return (B >= oi) or (li <= B)


def simulate(adv=0.0, with_yield=True, start=0, end=None, fill_mode='touch',
             rebuy_off=REBUY_OFF_BP, rung_bp=RUNG_BP, fracs=FRACS,
             apr=APR_USD1, sym=SYM):
    assert abs(sum(fracs) - 1.0) < 1e-12
    D = _data(sym)
    o, h, l, c, ts, anc, n = D['o'], D['h'], D['l'], D['c'], D['ts'], D['anc'], D['n']
    if end is None:
        end = n
    ypb = (apr / 365.0 / BPD) if with_yield else 0.0

    # deploy at open[start] with entry haircut
    eff0 = o[start] * (1 + adv / 1e4)
    slices = []
    for rb, fr in zip(rung_bp, fracs):
        slices.append(dict(rb=rb, frac=fr, state='usd1',
                           qty=fr * ALLOC / eff0, cash=0.0, sell_px=0.0, t=start))

    accr = 0.0
    turn = ALLOC
    sells = rebuys = 0
    realized = 0.0
    usdt_bar_val = 0.0
    tot_bar_val = 0.0
    max_dwell = 0
    n_touch_only_sell = 0       # fills that occurred via exact-kiss (touch but not through)
    n_touch_only_buy = 0
    eq = []

    for i in range(start, end):
        a = anc[i]; oi, hi, li, ci = o[i], h[i], l[i], c[i]
        bar_usdt = 0.0; bar_tot = 0.0
        for s in slices:
            if s['state'] == 'usd1':
                if ypb:
                    accr += s['qty'] * ci * ypb
                R = round(a + s['rb'] / 1e4, 4)
                if fill_mode == 'delay1':
                    can = (i > s['t']) and _sell_fill(R, oi, hi, 'touch')
                else:
                    can = _sell_fill(R, oi, hi, fill_mode)
                if can:
                    # diagnostic: would a strict (trade-through) test have rejected it?
                    if not _sell_fill(R, oi, hi, 'strict'):
                        n_touch_only_sell += 1
                    f = R * (1 - adv / 1e4)
                    s['cash'] = s['qty'] * f
                    s['sell_px'] = f
                    s['qty'] = 0.0
                    s['state'] = 'usdt'
                    s['t'] = i
                    turn += s['cash']; sells += 1
            else:  # usdt -> rest rebuy
                B = round(a + rebuy_off / 1e4, 4)
                if fill_mode == 'delay1':
                    can = (i > s['t']) and _buy_fill(B, oi, li, 'touch')
                else:
                    can = _buy_fill(B, oi, li, fill_mode)
                if can:
                    if not _buy_fill(B, oi, li, 'strict'):
                        n_touch_only_buy += 1
                    f = B * (1 + adv / 1e4)
                    nq = s['cash'] / f
                    realized += (s['sell_px'] - f) * nq
                    dw = i - s['t']
                    if dw > max_dwell:
                        max_dwell = dw
                    s['qty'] = nq; s['cash'] = 0.0; s['state'] = 'usd1'; s['t'] = i
                    turn += nq * f; rebuys += 1
            v = (s['qty'] * ci) if s['state'] == 'usd1' else s['cash']
            bar_tot += v
            if s['state'] == 'usdt':
                bar_usdt += v
        usdt_bar_val += bar_usdt
        tot_bar_val += bar_tot
        eq.append(accr + bar_tot)

    for s in slices:
        if s['state'] == 'usdt':
            max_dwell = max(max_dwell, (end - 1) - s['t'])

    final = eq[-1]
    span = (ts[end - 1] - ts[start]) / 86400_000
    eqs = pd.Series(eq)
    dd = ((eqs - eqs.cummax()) / eqs.cummax()).min()
    return dict(
        adv=adv, fill_mode=fill_mode,
        apr=round((final / ALLOC - 1) * 100 * 365 / span, 3),
        ret=round((final / ALLOC - 1) * 100, 3),
        price_cap_pct=round(realized / ALLOC * 100, 3),
        mdd=round(dd * 100, 4),
        turn_per_day=turn / span,
        sells=sells, rebuys=rebuys,
        usdt_time_pct=round(usdt_bar_val / tot_bar_val * 100, 3),
        max_stuck_days=round(max_dwell / BPD, 3),
        slices_usdt_end=sum(1 for s in slices if s['state'] == 'usdt'),
        touch_only_sells=n_touch_only_sell, touch_only_buys=n_touch_only_buy,
        span_d=round(span, 2), n=end - start, start=start, end=end)


def hold(adv=0.0, with_yield=True, start=0, end=None, apr=APR_USD1, sym=SYM):
    D = _data(sym)
    o, c, ts, n = D['o'], D['c'], D['ts'], D['n']
    if end is None:
        end = n
    ypb = (apr / 365.0 / BPD) if with_yield else 0.0
    eff0 = o[start] * (1 + adv / 1e4)
    qty = ALLOC / eff0
    accr = 0.0; last = 0.0
    for i in range(start, end):
        accr += qty * c[i] * ypb
        last = accr + qty * c[i]
    span = (ts[end - 1] - ts[start]) / 86400_000
    return round((last / ALLOC - 1) * 100 * 365 / span, 3)


def flat10(start=0, end=None, sym=SYM):
    # the locked benchmark: holding USD1 = 10% APR, by definition
    return 10.0


if __name__ == "__main__":
    ADVS = [0, 0.5, 1.0, 1.5]
    D = _data(SYM); n = D['n']
    print("=" * 90)
    print("INDEPENDENT VERIFY of r1_6 (from-scratch reimpl)  |  bars=%d  span=%.2fd" %
          (n, (D['ts'][-1] - D['ts'][0]) / 86400_000))
    print("=" * 90)

    full = {a: simulate(a, fill_mode='touch') for a in ADVS}
    po = {a: simulate(a, with_yield=False, fill_mode='touch') for a in ADVS}
    print("\n[A] FULL WINDOW, fill_mode=touch (claim's convention)")
    print("   adv:    " + "".join(f"{a:>10}" for a in ADVS))
    print(" strat:    " + "".join(f"{full[a]['apr']:>10.3f}" for a in ADVS))
    print(" vs10%:    " + "".join(f"{full[a]['apr']-10:>+10.3f}" for a in ADVS))
    print("  hold:    " + "".join(f"{hold(a):>10.3f}" for a in ADVS))
    print("vsHold:    " + "".join(f"{full[a]['apr']-hold(a):>+10.3f}" for a in ADVS))
    print("price-only edge vs hold:")
    print(" edge:     " + "".join(f"{po[a]['apr']-hold(a,with_yield=False):>+10.3f}" for a in ADVS))

    print("\n[B] FILL REALISM stress (require trade-THROUGH, no exact-kiss fills)")
    strict = {a: simulate(a, fill_mode='strict') for a in ADVS}
    print(" strat:    " + "".join(f"{strict[a]['apr']:>10.3f}" for a in ADVS))
    print(" vs10%:    " + "".join(f"{strict[a]['apr']-10:>+10.3f}" for a in ADVS))

    print("\n[C] FILL REALISM stress (delay1: order must rest 1 full bar before it can fill)")
    delay = {a: simulate(a, fill_mode='delay1') for a in ADVS}
    print(" strat:    " + "".join(f"{delay[a]['apr']:>10.3f}" for a in ADVS))
    print(" vs10%:    " + "".join(f"{delay[a]['apr']-10:>+10.3f}" for a in ADVS))

    mid = n // 2
    print("\n[D] OVERFIT / REGIME stress: first half vs second half (fresh deploy each)")
    for label, (s0, e0) in [("H1[0:mid]", (0, mid)), ("H2[mid:end]", (mid, n))]:
        r = {a: simulate(a, fill_mode='touch', start=s0, end=e0) for a in ADVS}
        hh = {a: hold(a, start=s0, end=e0) for a in ADVS}
        print(f"  {label} span={r[0]['span_d']}d")
        print("    strat:  " + "".join(f"{r[a]['apr']:>10.3f}" for a in ADVS))
        print("    vs10%:  " + "".join(f"{r[a]['apr']-10:>+10.3f}" for a in ADVS))
        print("    hold:   " + "".join(f"{hh[a]:>10.3f}" for a in ADVS))

    print("\n[E] MECHANICS @adv=0.5 (touch):")
    x = full[0.5]
    print(f"  sells={x['sells']} rebuys={x['rebuys']} usdt_time={x['usdt_time_pct']}% "
          f"max_stuck={x['max_stuck_days']}d end_in_usdt={x['slices_usdt_end']} "
          f"price_cap={x['price_cap_pct']}% mdd={full[1.0]['mdd']}%")
    print(f"  touch-only(kiss) fills: sells={x['touch_only_sells']}/{x['sells']} "
          f"buys={x['touch_only_buys']}/{x['rebuys']}")

    print("\n[F] CAPACITY @adv=0.5:")
    tpd = x['turn_per_day']; pct = tpd / MKT_VOL * 100
    cap = ALLOC * (0.02 * MKT_VOL / tpd)
    print(f"  turnover/day=${tpd:,.0f} = {pct:.4f}% of ${MKT_VOL:,}/day -> 2% cap size <= ${cap:,.0f}")

    out = dict(
        full_touch={a: full[a]['apr'] for a in ADVS},
        full_strict={a: strict[a]['apr'] for a in ADVS},
        full_delay1={a: delay[a]['apr'] for a in ADVS},
        h1_touch={a: simulate(a, start=0, end=mid)['apr'] for a in ADVS},
        h2_touch={a: simulate(a, start=mid, end=n)['apr'] for a in ADVS},
        capacity_pct=round(pct, 4),
        max_stuck_days=x['max_stuck_days'],
        end_in_usdt=x['slices_usdt_end'])
    print("\n[JSON]\n" + json.dumps(out, indent=2, default=str))
