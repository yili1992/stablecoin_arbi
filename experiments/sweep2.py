import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from bt_ladder2 import run, hold

HOLD={a:hold(a) for a in [0,0.5,1,1.5,2]}
print("HOLD APR:",HOLD,"\n")
print(f"{'config':<52}{'adv0':>7}{'adv.5':>7}{'adv1':>7}{'adv2':>7}{'cap%':>7}{'closs':>7}{'USDT%':>7}{'sel':>5}{'tstop':>6}{'turn/d':>8}")
def show(name,rungs,ts):
    row=f"{name:<52}"; d={}
    for a in [0,0.5,1,1.5,2]:
        x=run(rungs,adv=a,max_usdt_h=ts); d[a]=x
        if a in(0,0.5,1,2):
            w="*" if x['apr']>HOLD[a] else " "; row+=f"{x['apr']:>6}{w}"
    x=d[0.5]
    row+=f"{x['cap']:>7}{x['cap_loss']:>7}{x['usdt_pct']:>7}{x['sells']:>5}{x['tstops']:>6}{x['turn_d']:>8,.0f}"
    print(row)

# rebuy @ peg (1.0000) i.e. rebuy_bp=0 for all slices, sweep time-stop
R_peg=lambda: [(3,.30,0),(5,.25,0),(8,.25,0),(12,.20,0)]
for ts in [None,24,12,8,6,4,3,2]:
    show(f"rebuy@peg .30/.25/.25/.20  tstop={ts}", R_peg(), ts)
print()
# rebuy @ 0.9999 (-1bp), sweep tstop
R_m1=lambda:[(3,.30,-1),(5,.25,-1),(8,.25,-1),(12,.20,-1)]
for ts in [12,8,6,4,3]:
    show(f"rebuy@-1bp .30/.25/.25/.20  tstop={ts}", R_m1(), ts)
print()
# weight-hi, rebuy@peg, sweep tstop
R_wh=lambda:[(3,.15,0),(5,.20,0),(8,.30,0),(12,.35,0)]
for ts in [12,8,6,4,3]:
    show(f"weight-hi .15/.20/.30/.35 rebuy@peg tstop={ts}", R_wh(), ts)
print()
# weight-LOW (more on near rungs that revert fast), rebuy@peg
R_wl=lambda:[(3,.40,0),(5,.30,0),(8,.20,0),(12,.10,0)]
for ts in [8,6,4,3]:
    show(f"weight-lo .40/.30/.20/.10 rebuy@peg tstop={ts}", R_wl(), ts)
print()
# higher first rung to avoid churn, rebuy@peg
for ts in [8,6,4]:
    show(f"rungs5/8/12/18 .30/.30/.25/.15 rebuy@peg ts={ts}",
         [(5,.30,0),(8,.30,0),(12,.25,0),(18,.15,0)], ts)
