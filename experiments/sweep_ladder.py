"""Sweep sell-side ladder configs. Key lever: rebuy spread below each sell rung
(tight spread => fast rebuy => low USDT time => preserve 10% interest)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from bt_ladder import run_ladder, run_hold

def cfg(rungs):  # rungs=[(sell_bp, frac, spread_bp)] -> rebuy_bp = sell_bp - spread_bp
    return [(s, f, s-sp) for (s, f, sp) in rungs]

HOLD = {a: run_hold(a)['apr_pct'] for a in [0,0.5,1,1.5,2]}
print("HOLD APR:", HOLD)
print(f"\n{'config':<46}{'adv0':>7}{'adv0.5':>8}{'adv1':>7}{'adv2':>7}{'cap%':>7}{'USDT%':>7}{'sells':>7}{'turn/d':>9}")

def show(name, rungs):
    row=f"{name:<46}"
    caps=usdt=sells=turn=0
    for a in [0,0.5,1,1.5,2]:
        x=run_ladder(cfg(rungs), adv=a)
        win = "*" if x['apr_pct']>HOLD[a] else " "
        if a in (0,0.5,1,2): row+=f"{x['apr_pct']:>6}{win}"
        if a==0.5: caps,usdt,sells,turn=x['price_capture_pct'],x['usdt_time_pct'],x['sells'],x['turn_per_day']
    row+=f"{caps:>7}{usdt:>7}{sells:>7}{turn:>9,.0f}"
    print(row)

# vary uniform spread, uniform-ish fractions, rungs +3/+5/+8/+12
for sp in [2,3,4,5]:
    show(f"rungs3/5/8/12 frac.30/.25/.25/.20 spread{sp}",
         [(3,.30,sp),(5,.25,sp),(8,.25,sp),(12,.20,sp)])
print()
# weight higher rungs (less USDT drag from low rung churn)
for sp in [2,3,4]:
    show(f"weight-hi .15/.20/.30/.35 spread{sp}",
         [(3,.15,sp),(5,.20,sp),(8,.30,sp),(12,.35,sp)])
print()
# drop the churny +3 rung; start at +4/+6/+9/+13
for sp in [3,4,5]:
    show(f"rungs4/6/9/13 .25/.25/.25/.25 spread{sp}",
         [(4,.25,sp),(6,.25,sp),(9,.25,sp),(13,.25,sp)])
print()
# per-rung spread: tight low (fast), wider high (bigger capture on rare spikes)
show("rungs3/5/8/12 graded-spread 2/3/4/6",
     [(3,.30,2),(5,.25,3),(8,.25,4),(12,.20,6)])
show("rungs3/5/8/12 graded-spread 2/2/3/4",
     [(3,.30,2),(5,.25,2),(8,.25,3),(12,.20,4)])
show("weight-hi .15/.20/.30/.35 graded 2/3/4/6",
     [(3,.15,2),(5,.20,3),(8,.30,4),(12,.35,6)])
print()
# finer 6-rung ladder
show("6rung 2/4/6/8/11/15 even spread3",
     [(2,.18,3),(4,.18,3),(6,.18,3),(8,.16,3),(11,.16,3),(15,.14,3)])
show("6rung 3/5/7/9/12/16 graded 2/2/3/3/4/5",
     [(3,.18,2),(5,.18,2),(7,.18,3),(9,.16,3),(12,.16,4),(16,.14,5)])
