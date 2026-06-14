import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from bt_ladder3 import run, hold
HOLD={a:hold(a) for a in [0,0.5,1,1.5,2]}
def line(name,rungs,rp,anchor="ema21_1h"):
    cells=[]
    for a in [0,0.5,1,1.5,2]:
        x=run(rungs,adv=a,rp=rp,anchor=anchor)
        cells.append((a,x))
    mm=min(round(x['apr']-HOLD[a],2) for a,x in cells if a in [0.5,1,1.5])
    mm2=min(round(x['apr']-HOLD[a],2) for a,x in cells if a in [0.5,1,1.5,2])
    s=f"{name:<46}"
    for a,x in cells:
        w="*" if x['apr']>HOLD[a] else " "; s+=f"{x['apr']:>6}{w}"
    x05=dict(cells)[0.5]
    s+=f"  mm(.5-1.5)={mm:>5} mm(.5-2)={mm2:>5} cap{x05['cap']:>6} USDT{x05['usdt_pct']:>5} sel{x05['sells']:>4} td{x05['turn_d']:>7,.0f}"
    print(s)

print("HOLD:",{k:float(v) for k,v in HOLD.items()})
print(f"{'config':<46}{'adv0':>7}{'adv.5':>7}{'adv1':>7}{'adv1.5':>7}{'adv2':>7}")
print("--- fine sweep around winner (ema21, rb-1) ---")
CANDS={
 "5/7/10/14/20 equal":[(5,.2),(7,.2),(10,.2),(14,.2),(20,.2)],
 "5/8/11/15/21 equal":[(5,.2),(8,.2),(11,.2),(15,.2),(21,.2)],
 "6/8/11/15/20 equal":[(6,.2),(8,.2),(11,.2),(15,.2),(20,.2)],
 "5/7/10/14/20 hiW .15/.18/.22/.22/.23":[(5,.15),(7,.18),(10,.22),(14,.22),(20,.23)],
 "5/7/10/14/20 loW .24/.22/.20/.18/.16":[(5,.24),(7,.22),(10,.20),(14,.18),(20,.16)],
 "5/7/9/12/16/22 6rung":[(5,1/6),(7,1/6),(9,1/6),(12,1/6),(16,1/6),(22,1/6)],
 "6/9/13/18 4rung .25e":[(6,.25),(9,.25),(13,.25),(18,.25)],
}
for nm,rg in CANDS.items(): line(nm,rg,-1)
print("--- rb=0 vs rb=-1 vs rb=-2 for the winner shape ---")
W=[(5,.2),(7,.2),(10,.2),(14,.2),(20,.2)]
for rp in [0,-1,-2]: line(f"5/7/10/14/20 equal rb{rp:+d}",W,rp)

print("\n=== DIAGNOSTICS for WINNER: ema21 5/7/10/14/20 equal rb-1 ===")
for a in [0,0.5,1,1.5,2]:
    x=run(W,adv=a,rp=-1)
    print(f"  adv={a}: APR={x['apr']:>6} ret={x['ret']:>6} cap={x['cap']:>6} cap_loss={x['cap_loss']:>6} "
          f"MDD={x['mdd']:>7} USDT={x['usdt_pct']:>5}% sells={x['sells']} rebuys={x['rebuys']} "
          f"turn/d=${x['turn_d']:>7,.0f} open_usdt_end={x['open_usdt']}")
# price-only (no yield) sanity: is the price edge itself positive after adv?
print("  price-only (yield OFF):")
for a in [0,0.5,1,2]:
    x=run(W,adv=a,rp=-1,with_yield=False)
    print(f"    adv={a}: priceAPR={x['apr']:>6} cap={x['cap']:>6}")
# capacity
x=run(W,adv=0.5,rp=-1); mkt=2_538_200
print(f"\n  CAPACITY: turn/day=${x['turn_d']:,.0f} at $10k. Scale to 2% of ${mkt:,}/day=${0.02*mkt:,.0f}/day "
      f"-> max capital ~${10000*0.02*mkt/x['turn_d']:,.0f}")
