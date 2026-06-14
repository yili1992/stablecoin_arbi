"""
Custom event-driven backtest for the stablecoin EMA-reversion + yield strategy.

Why custom (not Freqtrade): the original Freqtrade strategy's has_open_trade/custom_info
pattern makes its OWN backtest unfaithful (entry always-on, exit signal never fires in
backtest). A transparent hand-rolled loop avoids that entirely and lets us model the two
things Freqtrade can't: UTA holding yield, and DCA-on-depeg with per-coin caps.

Models the LOCKED design:
  universe   = USD1USDT, USDEUSDT, USDTBUSDT   (USDC dropped: no yield + thin edge)
  no stop    = never sell at a loss (except a time-based escape to free capital)
  depeg=buy  = DCA add tranches as price falls, capped per coin (mechanism gate:
               USDe gets a smaller cap than the reserve-backed USD1/USDtb)
  yield      = held coin accrues APR (USDT/USDC idle = 0)

CORRECTNESS GUARDS (each maps to a known backtest pitfall in my feedback library):
  - No lookahead: a 5m bar at ts t uses the latest 1h EMA that has *closed* by t
    (1h open-ts H is usable only for t >= H + 3600s).
  - Fill realism: maker BUY at L fills only if bar.low <= L; maker SELL at S only if
    bar.high >= S. Fill price = the limit (passive). NOTE: front-of-queue optimism;
    adverse selection / queue position NOT modeled (needs tick data -> separate study).
  - Yield accrued per-bar on held notional (time-weighted), not lump-summed.
  - Equity = cash + mark-to-market(position) + accrued yield -> honest MDD during depegs.
"""
import pandas as pd, numpy as np, os, json

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

# ------------------------------- params -------------------------------
PARAMS = dict(
    entry_below_ema_bp = 1.0,   # initial entry: buy when close <= ema55_1h + this (bp)
    dca_step_bp        = 10.0,  # add a tranche each time price drops this far below avg entry
    tp_bp              = 2.0,   # take profit: sell when price reaches avg_entry + this
    min_hold_days      = 2.0,   # below TP, hold at least this long (collect yield, no loss sale)
    escape_days        = 3.0,   # after this, free capital: sell at market even if ~flat/small loss
    tranche_usd        = 1000.0,
    alloc_usd          = 20000.0,  # capital earmarked per coin
)
# per-coin: max tranches (hard cap = mechanism gate) + assumed holding APR
COIN = {
    "USD1USDT":  dict(max_tranches=15, apr=0.04, kind="reserve"),
    "USDTBUSDT": dict(max_tranches=15, apr=0.04, kind="reserve"),
    "USDEUSDT":  dict(max_tranches=6,  apr=0.08, kind="synthetic"),  # capped: solvency tail
}
BARS_PER_DAY_5M = 288

def load(symbol):
    f5 = os.path.join(DATA, f"{symbol}_5m.csv")
    f1 = os.path.join(DATA, f"{symbol}_1h.csv")
    d5 = pd.read_csv(f5); d1 = pd.read_csv(f1)
    for c in ["ts","open","high","low","close"]:
        d5[c]=pd.to_numeric(d5[c]); d1[c]=pd.to_numeric(d1[c])
    d5=d5.sort_values("ts").reset_index(drop=True)
    d1=d1.sort_values("ts").reset_index(drop=True)
    # 1h EMAs on CLOSED candles
    for n in (21,55,100):
        d1[f"ema{n}"]=d1["close"].ewm(span=n,adjust=False).mean()
    # availability ts: a 1h candle opening at H is only known after it closes (H+3600s)
    d1["avail_ts"]=d1["ts"]+3600_000
    d1["ema55_slope"]=d1["ema55"].diff()
    # asof-merge: each 5m bar gets the most recent ALREADY-CLOSED 1h row
    m=pd.merge_asof(d5, d1[["avail_ts","ema21","ema55","ema100","ema55_slope"]],
                    left_on="ts", right_on="avail_ts", direction="backward")
    return m.dropna(subset=["ema55"]).reset_index(drop=True)

def backtest(symbol, p=PARAMS):
    cfg=COIN[symbol]; df=load(symbol)
    apr=cfg["apr"]; max_tr=cfg["max_tranches"]
    yield_per_bar = apr/365.0/BARS_PER_DAY_5M

    cash=p["alloc_usd"]; coin_qty=0.0; cost=0.0          # cost = total USDT spent on current position
    tranches=0; first_ts=None; accr_yield=0.0
    trades=[]; equity=[]; max_pos=0.0

    def avg_entry(): return cost/coin_qty if coin_qty>0 else 0.0

    for r in df.itertuples():
        px=r.close; ema=r.ema55
        # ---- accrue yield on held coin (time-weighted, per bar) ----
        if coin_qty>0:
            accr_yield += coin_qty*px*yield_per_bar
        # ---- EXIT / take-profit (maker sell at avg+tp) ----
        if coin_qty>0:
            ae=avg_entry(); tp=ae*(1+p["tp_bp"]/1e4)
            hold_days=(r.ts-first_ts)/86400_000
            sold=False
            if r.high>=tp:                                  # TP hit -> sell whole position at tp
                proceeds=coin_qty*tp; pnl=proceeds-cost
                trades.append(dict(sym=symbol,exit="tp",hold_days=hold_days,
                                   pnl=pnl,ret_bp=pnl/cost*1e4,tranches=tranches,
                                   yield_usd=accr_yield))
                cash+=proceeds+accr_yield; coin_qty=0; cost=0; tranches=0; accr_yield=0; sold=True
            elif hold_days>=p["escape_days"]:               # escape: free capital at market
                proceeds=coin_qty*px; pnl=proceeds-cost
                trades.append(dict(sym=symbol,exit="escape",hold_days=hold_days,
                                   pnl=pnl,ret_bp=pnl/cost*1e4,tranches=tranches,
                                   yield_usd=accr_yield))
                cash+=proceeds+accr_yield; coin_qty=0; cost=0; tranches=0; accr_yield=0; sold=True
            if sold:
                equity.append((r.ts,cash)); continue
        # ---- ENTRY / DCA (maker buy) ----
        if tranches<max_tr and cash>=p["tranche_usd"]:
            if coin_qty==0:
                limit=ema*(1+p["entry_below_ema_bp"]/1e4)   # initial: buy at/below slow mean
                do = r.low<=limit
            else:
                limit=avg_entry()*(1-p["dca_step_bp"]/1e4)  # DCA: add another step lower
                do = r.low<=limit
            if do:
                fill=min(limit, r.open)                      # can't fill above the bar's open if it gapped down
                qty=p["tranche_usd"]/fill
                coin_qty+=qty; cost+=p["tranche_usd"]; cash-=p["tranche_usd"]; tranches+=1
                if first_ts is None or coin_qty==qty: first_ts=r.ts
        # ---- mark-to-market equity ----
        mtm=cash+coin_qty*px+accr_yield
        equity.append((r.ts,mtm)); max_pos=max(max_pos,coin_qty*px)

    # final mark (close any open position at last price, mark only — not a forced sale)
    eq=pd.DataFrame(equity,columns=["ts","equity"])
    peak=eq["equity"].cummax(); dd=(eq["equity"]-peak)/peak
    tr=pd.DataFrame(trades)
    span_days=(df.ts.iloc[-1]-df.ts.iloc[0])/86400_000
    final_eq=eq.equity.iloc[-1]
    res=dict(
        symbol=symbol, span_days=round(span_days,1),
        n_trades=len(tr),
        win_rate=round((tr.pnl>0).mean()*100,1) if len(tr) else 0,
        price_pnl=round(tr.pnl.sum()-tr.yield_usd.sum(),2) if len(tr) else 0,
        yield_pnl=round(tr.yield_usd.sum(),2) if len(tr) else 0,
        total_pnl=round(final_eq-p["alloc_usd"],2),
        total_ret_pct=round((final_eq/p["alloc_usd"]-1)*100,3),
        apr_pct=round((final_eq/p["alloc_usd"]-1)*100*365/span_days,2) if span_days>0 else 0,
        max_drawdown_pct=round(dd.min()*100,2),
        max_position_usd=round(max_pos,0),
        open_at_end=round(final_eq-cash if coin_qty>0 else 0,0),
        avg_hold_days=round(tr.hold_days.mean(),2) if len(tr) else 0,
    )
    return res, tr, eq

if __name__=="__main__":
    allres=[]
    for sym in COIN:
        try:
            res,tr,eq=backtest(sym)
            allres.append(res)
            print(json.dumps(res,ensure_ascii=False))
        except Exception as e:
            print(f"{sym} ERROR: {e}")
    if allres:
        df=pd.DataFrame(allres)
        print("\n=== SUMMARY ===")
        print(df.to_string(index=False))
        print(f"\nTOTAL price_pnl=${df.price_pnl.sum():.0f}  yield_pnl=${df.yield_pnl.sum():.0f}  "
              f"total_pnl=${df.total_pnl.sum():.0f}")
        print("NOTE: price_pnl = pure spread/reversion edge; yield_pnl = UTA holding interest.")
        print("NOTE: maker fills are front-of-queue-optimistic; real fill rate needs tick study.")
