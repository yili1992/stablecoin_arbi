import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from bt_ladder3 import run, hold
HOLD={a:hold(a) for a in [0,0.5,1,1.5,2]}
print("HOLD APR:",{k:float(v) for k,v in HOLD.items()},"\n")
print(f"{'config':<54}{'adv0':>7}{'adv.5':>7}{'adv1':>7}{'adv1.5':>7}{'adv2':>7}{'min_marg':>9}{'cap%':>6}{'USDT%':>6}{'sel':>5}{'turn/d':>8}")
def marg(x,a): return round(x-HOLD[a],2)
def show(name,rungs,rp,anchor):
    row=f"{name:<54}"; aprs={}
    for a in [0,0.5,1,1.5,2]:
        x=run(rungs,adv=a,rp=rp,anchor=anchor); aprs[a]=x
        w="*" if x['apr']>HOLD[a] else " "; row+=f"{x['apr']:>6}{w}"
    # robustness metric: min margin over hold across the realistic band adv in {0.5,1,1.5}
    mm=min(marg(aprs[a]['apr'],a) for a in [0.5,1,1.5])
    x=aprs[0.5]
    row+=f"{mm:>9}{x['cap']:>6}{x['usdt_pct']:>6}{x['sells']:>5}{x['turn_d']:>8,.0f}"
    print(row)
# anchor x rebuy-offset x rung-set grid, focused on adv-robustness
RUNGS={
 "3/5/8/12 .30/.25/.25/.20":[(3,.30),(5,.25),(8,.25),(12,.20)],
 "4/6/9/13 .25/.25/.25/.25":[(4,.25),(6,.25),(9,.25),(13,.25)],
 "4/6/9/13 .20/.25/.30/.25":[(4,.20),(6,.25),(9,.30),(13,.25)],
 "5/8/12/18 .25/.30/.25/.20":[(5,.25),(8,.30),(12,.25),(18,.20)],
 "4/7/11/16 .25/.25/.25/.25":[(4,.25),(7,.25),(11,.25),(16,.25)],
 "5/7/10/14/20 .2/.2/.2/.2/.2":[(5,.2),(7,.2),(10,.2),(14,.2),(20,.2)],
 "4/6/8/11/15 .2/.2/.2/.2/.2":[(4,.2),(6,.2),(8,.2),(11,.2),(15,.2)],
}
for anchor in ["ema21_1h","ema55_1h"]:
    for rp in [0,-1,-2]:
        for nm,rg in RUNGS.items():
            show(f"{anchor[:5]} {nm} rb{rp:+d}", rg, rp, anchor)
    print()
