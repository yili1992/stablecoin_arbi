"""
Parameter sweep for r1_6 (EMA-anchored slice ladder): slice COUNT (N) x per-slice NAV
FRACTIONS x rung range, to find max backtest return — WITH out-of-sample validation so we
don't just hand back an overfit config (the exact trap that produced the fake PAAL win).

Reuses strategy.backtest(df=...) verbatim (overrides its FRACS/RUNG_BP globals).
Reports IN-SAMPLE (first half) vs OUT-OF-SAMPLE (second half), at adv 0.5 & 1.0, STRICT +
20% liquidity gate (the conservative, realistic fill model). Ranks by ROBUST metric
(min of IS/OOS at adv1.0), NOT full-sample max.
"""
import numpy as np
from sca.backtest import strategy as S
from sca.config import CFG as _CFG
_SW = _CFG.get("sweep", {})

df = S.load()
n = len(df); mid = n // 2
df_full, df_is, df_oos = df, df.iloc[:mid].reset_index(drop=True), df.iloc[mid:].reset_index(drop=True)

def rungs_for(N, lo, hi):
    if N == 1: return [round((lo + hi) / 2, 1)]
    return [round(x, 1) for x in np.linspace(lo, hi, N)]

def fracs_for(N, shape):
    if shape == "equal":  w = np.ones(N)
    if shape == "front":  w = np.arange(N, 0, -1)      # more weight in LOW rungs (trade more)
    if shape == "back":   w = np.arange(1, N + 1)      # more weight in HIGH rungs (hold more)
    return list(w / w.sum())

def run_cfg(fracs, rungs, dfx, adv):
    S.FRACS = fracs; S.RUNG_BP = rungs
    return S.backtest(adv, fill_mode="strict", liq_gate=0.2, df=dfx)["apr"]

# hold benchmark per slice (realized) for reference
hold_full = S.hold_benchmark(df=df_full); hold_is = S.hold_benchmark(df=df_is); hold_oos = S.hold_benchmark(df=df_oos)

CONFIGS = []
for N in _SW.get("slice_counts", [1, 3, 5, 7, 10]):
    for lo, hi in [tuple(r) for r in _SW.get("rung_ranges", [[3,12],[5,20],[5,30]])]:
        rg = rungs_for(N, lo, hi)
        for shape in (["equal"] if N == 1 else ["equal", "front", "back"]):
            CONFIGS.append((N, shape, lo, hi, fracs_for(N, shape), rg))
# add the verified r1_6 original explicitly
CONFIGS.append(("orig", "r1_6", 5, 20, [0.15,0.18,0.20,0.22,0.25], [5,7,10,14,20]))

rows = []
for (N, shape, lo, hi, fracs, rg) in CONFIGS:
    try:
        f05, f10 = run_cfg(fracs, rg, df_full, 0.5), run_cfg(fracs, rg, df_full, 1.0)
        is10, oos10 = run_cfg(fracs, rg, df_is, 1.0), run_cfg(fracs, rg, df_oos, 1.0)
        rows.append(dict(N=N, shape=shape, rng=f"{lo}-{hi}", fracs=fracs, rungs=rg,
                         full05=f05, full10=f10, is10=is10, oos10=oos10,
                         robust=min(is10, oos10)))
    except Exception as e:
        print(f"ERR {N}/{shape}/{lo}-{hi}: {e}")

print(f"Hold benchmark (realized): full={hold_full:.2f}  IS={hold_is:.2f}  OOS={hold_oos:.2f}  (LOCKED bar=10.0)\n")

print("=== TOP 12 by FULL-SAMPLE APR @adv0.5 (the naive 'max return' the request asks for) ===")
print(f"{'N':>4}{'shape':>7}{'rng':>7}{'full@0.5':>10}{'full@1.0':>10}{'IS@1.0':>9}{'OOS@1.0':>9}")
for r in sorted(rows, key=lambda x: -x["full05"])[:12]:
    print(f"{str(r['N']):>4}{r['shape']:>7}{r['rng']:>7}{r['full05']:>10.2f}{r['full10']:>10.2f}{r['is10']:>9.2f}{r['oos10']:>9.2f}")

print("\n=== TOP 12 by ROBUST metric min(IS,OOS)@adv1.0 (the HONEST, overfit-resistant rank) ===")
print(f"{'N':>4}{'shape':>7}{'rng':>7}{'robust':>8}{'IS@1.0':>9}{'OOS@1.0':>9}{'full@0.5':>10}  fracs")
for r in sorted(rows, key=lambda x: -x["robust"])[:12]:
    print(f"{str(r['N']):>4}{r['shape']:>7}{r['rng']:>7}{r['robust']:>8.2f}{r['is10']:>9.2f}{r['oos10']:>9.2f}{r['full05']:>10.2f}  {r['fracs']}")

# diagnostics
best_full = max(rows, key=lambda x: x["full05"])
best_rob  = max(rows, key=lambda x: x["robust"])
print(f"\nIN-SAMPLE-MAX cfg:  N={best_full['N']} {best_full['shape']} {best_full['rng']} -> full@0.5={best_full['full05']:.2f}, but OOS@1.0={best_full['oos10']:.2f}")
print(f"ROBUST-MAX cfg:     N={best_rob['N']} {best_rob['shape']} {best_rob['rng']} -> robust={best_rob['robust']:.2f} (IS={best_rob['is10']:.2f}/OOS={best_rob['oos10']:.2f})")
print(f"\nOOS spread across ALL cfgs @adv1.0: min={min(r['oos10'] for r in rows):.2f}  max={max(r['oos10'] for r in rows):.2f}  (vs OOS hold {hold_oos:.2f})")
