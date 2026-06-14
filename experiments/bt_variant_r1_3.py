"""
bt_variant_r1_3.py  --  INDEPENDENT backtest of:
  "Asymmetric Core-Grid Harvester (USD1USDT)"  (variant r1_3)

Self-contained re-implementation (NOT importing bt_asym) so the result is an
independent cross-check of the spec's claimed numbers. Reuses ONLY load()/APR/
ALLOC/BPD from bt_faithful (no shared file is edited).

THESIS (verbatim intent):
  - Anchor an ABSOLUTE price grid on peg=1.0 (USD1 always re-pegs -> absolute
    levels are meaningful; no EMA gate -> zero lookahead).
  - Capital splits into a permanent CORE (core_frac, default 80%) bought once at
    t0 and NEVER sold, and a trading SLEEVE (1-core_frac, default 20%) in R=8
    equal rungs.
  - START FULLY INVESTED: at t0 market-buy 100% of capital into USD1 (the HOLD
    benchmark does the same, so the cheap re-peg is not an unfair edge).
  - PASSIVE DIP BUYS: each EMPTY sleeve rung r rests a limit BUY at
        buy_line[r]  = 1.0000 - (buy0_bp  + buy_step_bp *r)/1e4
    Fills (EMPTY->HELD) when bar low  <= line; fill px = line*(1+adv/1e4).
  - ASYMMETRIC SCALED-OUT SELLS: each HELD sleeve rung r rests a limit SELL at
        sell_line[r] = 1.0000 + (sell0_bp + sell_step_bp*r)/1e4
    Fills (HELD->EMPTY) when bar high >= line; fill px = line*(1-adv/1e4).
  - Each rung does AT MOST ONE transition per bar (no intrabar round-trip).
  - No stop-loss (re-peg assumption). In a hard depeg the buy ladder fills out,
    runs out of cash, and the book simply HOLDS through to re-peg.

FILL / INTEREST model mirrors bt_faithful exactly:
  - passive buy fills if low<=line at line*(1+adv/1e4);
    passive sell fills if high>=line at line*(1-adv/1e4).
  - UTA interest 10%/yr accrues per bar on USD1 *value* held (core+held rungs);
    cash (idle USDT) earns 0%.  (accr += usd1_value * apr/365/BPD)
  - equity marked-to-close each bar.

WIN BAR (orchestrator, strict): TOTAL APR (price+interest) > 10% at adv>=0.5/side.
Reported BOTH mark-to-close (engine convention) and mark-to-peg (terminal USD1
re-marked to 1.0 -- the fair metric under the locked "always re-pegs" rule).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import pandas as pd
from bt_faithful import load, APR, ALLOC, BPD

PEG = 1.0000


def run_variant(sym, with_yield, adverse_bp,
                core_frac=0.80, R=8,
                buy0_bp=2, buy_step_bp=1, sell0_bp=4, sell_step_bp=3):
    """Event-driven backtest of the Asymmetric Core-Grid Harvester.

    Returns a dict with both mark-to-close and mark-to-peg total APR plus
    capacity / stuck-capital diagnostics.
    """
    df = load(sym)
    apr = APR[sym] if with_yield else 0.0
    ypb = apr / 365 / BPD                       # interest per 5m bar (on USD1 value)
    adv = adverse_bp / 1e4

    O = df.open.values; H = df.high.values; L = df.low.values; C = df.close.values
    nbar = len(df)
    bar_ms = int(df.ts.iloc[1] - df.ts.iloc[0])
    span = (df.ts.iloc[-1] - df.ts.iloc[0]) / 86400_000
    bar_days = bar_ms / 86400_000

    # ---- grid lines (absolute, anchored on peg) ----
    rung_dollar = (1 - core_frac) * ALLOC / R if R > 0 else 0.0
    buy_line  = [round(PEG - (buy0_bp  + r * buy_step_bp)  / 1e4, 4) for r in range(R)]
    sell_line = [round(PEG + (sell0_bp + r * sell_step_bp) / 1e4, 4) for r in range(R)]

    # ---- t0: start FULLY invested at first open (+adverse on the buy) ----
    eff0 = O[0] * (1 + adv)
    core_qty = core_frac * ALLOC / eff0
    held = [True] * R
    qty  = [rung_dollar / eff0 for _ in range(R)]
    cash = 0.0
    turn = ALLOC                                # one-time initial deployment

    accr = 0.0
    eq = []
    n_buy = 0; n_sell = 0
    inpos_value_sum = 0.0                       # for value-weighted time-in-USD1
    cur_empty = [0] * R                         # contiguous bars a rung sits in cash
    max_empty = [0] * R                         # longest such contiguous span

    for i in range(nbar):
        o, h, l, c = O[i], H[i], L[i], C[i]

        # interest on all USD1 currently held (core + held rungs); cash earns 0%
        usd1_qty = core_qty + sum(qty[r] for r in range(R) if held[r])
        accr += usd1_qty * c * ypb
        inpos_value_sum += usd1_qty * c

        for r in range(R):
            if held[r]:
                # resting SELL; fills if bar high reaches the line
                if h >= sell_line[r]:
                    f = sell_line[r] * (1 - adv)
                    proc = qty[r] * f
                    cash += proc; turn += proc
                    held[r] = False; qty[r] = 0.0; n_sell += 1
                    cur_empty[r] = 0
            else:
                # in cash -> resting BUY; fills if bar low reaches the line
                cur_empty[r] += 1
                max_empty[r] = max(max_empty[r], cur_empty[r])
                if l <= buy_line[r]:
                    cost = rung_dollar
                    if cash + 1e-9 >= cost:
                        eff = buy_line[r] * (1 + adv)
                        cash -= cost; turn += cost
                        qty[r] = cost / eff; held[r] = True; n_buy += 1
                        cur_empty[r] = 0

        eq.append(cash + core_qty * c + sum(qty[r] * c for r in range(R) if held[r]) + accr)

    c_last = C[-1]
    held_qty_end = core_qty + sum(qty[r] for r in range(R) if held[r])
    final_close = cash + held_qty_end * c_last + accr     # engine convention
    final_peg   = cash + held_qty_end * PEG   + accr       # terminal USD1 re-marked to peg

    eqs = pd.Series(eq); peak = eqs.cummax(); mdd = ((eqs - peak) / peak).min()
    tim = inpos_value_sum / nbar / ALLOC * 100             # value-weighted time-in-USD1

    # longest a single rung sat idle in USDT (incl. rungs still empty at end)
    max_stuck_d = max(max_empty) * bar_days if R else 0.0

    def apr_of(final):
        return (final / ALLOC - 1) * 100 * 365 / span

    return dict(
        sym=sym, adv=adverse_bp, core_frac=core_frac, R=R, span_d=round(span, 1),
        apr_close=round(apr_of(final_close), 3),
        apr_peg=round(apr_of(final_peg), 3),
        ret_close_pct=round((final_close / ALLOC - 1) * 100, 4),
        mdd_pct=round(mdd * 100, 3),
        tim_pct=round(tim, 1),
        n_buy=n_buy, n_sell=n_sell,
        turn_per_day=round(turn / span, 1),
        max_stuck_d=round(max_stuck_d, 1),
        end_cash_pct=round(cash / ALLOC * 100, 1),
        n_rungs_empty_end=sum(1 for r in range(R) if not held[r]),
    )


MKT_VOL = 2_538_200.0   # USD1USDT measured daily volume (USDT)


def main():
    ADV = [0, 0.5, 1.0, 1.5]
    print("=" * 78)
    print("Asymmetric Core-Grid Harvester (USD1USDT) -- variant r1_3  [INDEPENDENT bt]")
    print("core_frac=0.80  R=8  buy 2/1bp  sell 4/3bp   alloc=$%.0f" % ALLOC)
    print("=" * 78)

    # ---- hold benchmark (100% invested, never trades) ----
    hold = {a: run_variant('USD1USDT', True, a, core_frac=1.0, R=0) for a in ADV}
    h0 = hold[0.5]
    print("\nHOLD benchmark (core=1.0, R=0; same start-fully-invested):")
    print(f"  mark-to-close APR = {h0['apr_close']:.3f}%   mark-to-peg APR = {h0['apr_peg']:.3f}%"
          f"   (adv-independent: hold never trades)   stated bar = 10.00%")

    # ---- strategy sweep ----
    res = {a: run_variant('USD1USDT', True, a) for a in ADV}
    print("\nSTRATEGY total APR (price + 10% interest), swept over adverse-selection:")
    print(f"{'adv/side':>9}{'APR(close)':>12}{'APR(peg)':>11}{'>10%?':>7}{'>hold(cl)':>10}{'>hold(peg)':>11}")
    for a in ADV:
        r = res[a]
        beat10 = 'Y' if r['apr_close'] > 10 else 'N'
        beathc = 'Y' if r['apr_close'] > hold[a]['apr_close'] else 'N'
        beathp = 'Y' if r['apr_peg']   > hold[a]['apr_peg']   else 'N'
        print(f"{a:>9}{r['apr_close']:>12.3f}{r['apr_peg']:>11.3f}{beat10:>7}{beathc:>10}{beathp:>11}")

    # ---- capacity ----
    r1 = res[1.0]
    pct = r1['turn_per_day'] / MKT_VOL * 100
    cap = ALLOC * (0.02 * MKT_VOL / r1['turn_per_day'])
    print("\nCAPACITY (@ $%.0f alloc, adv=1.0):" % ALLOC)
    print(f"  turnover/day = ${r1['turn_per_day']:,.0f} = {pct:.4f}% of ${MKT_VOL:,.0f}/day  "
          f"(<2% -> {'OK' if pct < 2 else 'FAIL'})")
    print(f"  max scalable alloc keeping turnover<2% of vol = ${cap:,.0f}")

    # ---- stuck capital ----
    print("\nSTUCK-CAPITAL / IDLE diagnostics (adv=1.0):")
    print(f"  time-in-USD1 = {r1['tim_pct']:.1f}%   end idle cash = {r1['end_cash_pct']:.1f}%   "
          f"rungs empty@end = {r1['n_rungs_empty_end']}/8")
    print(f"  longest single rung idle in USDT = {r1['max_stuck_d']:.1f} days   "
          f"MDD = {r1['mdd_pct']:.3f}%   n_buy={r1['n_buy']} n_sell={r1['n_sell']}")

    # ---- robustness: core_frac sensitivity at adv0.5 ----
    print("\nCORE_FRAC sensitivity (APR close / APR peg) at adv=0.5:")
    for cf in [0.75, 0.80, 0.85]:
        x = run_variant('USD1USDT', True, 0.5, core_frac=cf)
        hb = hold[0.5]
        print(f"  core={cf:.2f}: close={x['apr_close']:.3f}%  peg={x['apr_peg']:.3f}%  "
              f"(hold close={hb['apr_close']:.3f} peg={hb['apr_peg']:.3f})  tim={x['tim_pct']:.1f}%")

    return res, hold


if __name__ == "__main__":
    main()
