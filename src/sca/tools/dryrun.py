#!/usr/bin/env python3
"""
Standalone (non-freqtrade) dry-run to MEASURE real maker fill quality on Bybit spot.
Risk-free: places ZERO orders, needs NO API key. Pure live-data measurement.

WHY: the backtest showed the whole strategy's APR (3% vs 12%) hinges on ONE unknown —
adverse selection (`adv`): when your passive limit order fills, how does the price move
right after? 5m OHLC can't measure it. This tool measures it from the live trade stream.

HOW (markout method, queue-independent & robust):
  - Every public trade that HITS THE BID (taker sell) is a moment a passive top-of-book
    BUY would have filled at the bid. We then look at the mid-price 5s & 30s later.
  - markout_buy = (mid_future - fill_bid)  -> POSITIVE = a passive buy there made money
    (you captured half-spread and price didn't run away); NEGATIVE = adverse selection.
  - Same for the ask (taker buy -> passive SELL fills at ask).
  - Round-trip maker edge ≈ buy_markout + sell_markout. If that sum (in bp) is positive,
    买低卖高 has a real edge; if negative, adverse selection eats it. THIS is your `adv`.

This measures the MARKET's structural adverse selection for a top-of-book maker right now.
It is NOT your account's exact number (real queue position / latency need real orders) —
a real-tiny-order mode would need your API key + explicit go-ahead and is intentionally
NOT included here (real money). Default = measurement only.

Usage:  python3 dryrun.py --symbol USD1USDT --seconds 600
        python3 dryrun.py --symbol USD1USDT --seconds 600 --csv out.csv
"""
import asyncio, json, argparse, time, statistics, csv, bisect, os, urllib.request
import websockets

try:
    from sca.config import CFG as _CFG
    _D = _CFG.get("dryrun", {})
except Exception:
    _D = {}
WS_URL = _D.get("ws_url", "wss://stream.bybit.com/v5/public/spot")
HORIZONS = list(_D.get("horizons_sec", [5, 30]))
MID_RETAIN = 90                 # keep mid history this many seconds

def rest_ctx(symbol, span=55):
    """Bootstrap ema55(1h) + tickSize for strategy context."""
    u=f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}&interval=60&limit=200"
    rows=json.load(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'M'}),timeout=15))['result']['list'][::-1]
    closes=[float(r[4]) for r in rows]; ema=closes[0]; k=2/(span+1)
    for c in closes[1:]: ema=c*k+ema*(1-k)
    return ema

def mid_at(mids_t, mids_v, target):
    """mid value at-or-just-before target time (mids_t sorted)."""
    i=bisect.bisect_right(mids_t, target)-1
    return mids_v[i] if i>=0 else None

async def run(symbol, seconds, csv_path):
    ema=rest_ctx(symbol)
    bid=ask=None
    mids_t=[]; mids_v=[]          # parallel sorted arrays of (t, mid)
    pending=[]                    # [t, side, fill_price]
    done=[]                       # [side, fill_price, {h: markout_bp}]
    spreads=[]
    start=time.time(); t_end=start+seconds; last_print=start
    history=[]; out_dir=os.path.dirname(csv_path) if csv_path else os.environ.get("SCA_OUT_DIR", ".")
    print(f"[dryrun] {symbol}  measuring {seconds}s, ema55(1h)≈{ema:.5f}  (no orders, no key)")

    def flush(now):
        # mature any pending event older than max horizon; compute markout at each horizon
        maxh=max(HORIZONS)
        while pending and now - pending[0][0] >= maxh:
            t0, side, fp = pending.pop(0)
            mo={}
            for h in HORIZONS:
                mv=mid_at(mids_t, mids_v, t0+h)
                if mv is None: mo[h]=None; continue
                mo[h]=( (mv-fp) if side=="buy" else (fp-mv) )/fp*1e4   # bp, +=maker profit
            done.append([side, fp, mo])
        # trim old mids
        cut=now-MID_RETAIN
        c=bisect.bisect_left(mids_t, cut)
        if c>0: del mids_t[:c]; del mids_v[:c]

    while time.time() < t_end:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_queue=None) as ws:
                await ws.send(json.dumps({"op":"subscribe","args":[f"orderbook.1.{symbol}", f"publicTrade.{symbol}"]}))
                while time.time() < t_end:
                    try:
                        msg=await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        flush(time.time()); continue
                    d=json.loads(msg); topic=d.get("topic",""); now=time.time()
                    if topic.startswith("orderbook.1"):
                        ob=d["data"]
                        if ob.get("b"): bid=float(ob["b"][0][0])
                        if ob.get("a"): ask=float(ob["a"][0][0])
                        if bid and ask and ask>bid:
                            mids_t.append(now); mids_v.append((bid+ask)/2)
                            spreads.append((ask-bid)/((ask+bid)/2)*1e4)
                    elif topic.startswith("publicTrade"):
                        for tr in d["data"]:
                            if not (bid and ask): continue
                            s=tr["S"]
                            if s=="Sell": pending.append([now,"buy",bid])    # hit bid -> passive buy fills
                            elif s=="Buy": pending.append([now,"sell",ask])  # hit ask -> passive sell fills
                    flush(now)
                    if now-last_print>=30:
                        _summary(done, spreads, partial=True); last_print=now
                        _write_status(out_dir, symbol, start, now, done, spreads, history)
        except Exception as e:
            print(f"[dryrun] reconnect ({type(e).__name__}: {e})"); await asyncio.sleep(2)

    flush(time.time()+max(HORIZONS))
    _summary(done, spreads, partial=False)
    _write_status(out_dir, symbol, start, time.time(), done, spreads, history)
    if csv_path:
        with open(csv_path,"w",newline="") as f:
            w=csv.writer(f); w.writerow(["side","fill_price"]+[f"mo{h}_bp" for h in HORIZONS])
            for side,fp,mo in done: w.writerow([side,fp]+[mo.get(h) for h in HORIZONS])
        print(f"[dryrun] wrote {len(done)} events -> {csv_path}")

def _med(xs): xs=[x for x in xs if x is not None]; return statistics.median(xs) if xs else None
def _mean(xs): xs=[x for x in xs if x is not None]; return statistics.fmean(xs) if xs else None
def _fmt(x): return f"{x:.2f}" if x is not None else " n/a"

def _aggregate(done, spreads):
    """Median markout per horizon + counts/spread. None (not NaN) when empty -> JSON-safe."""
    buys=[mo for s,_,mo in done if s=="buy"]; sells=[mo for s,_,mo in done if s=="sell"]
    mk={}
    for h in HORIZONS:
        b=_med([mo[h] for mo in buys]); s=_med([mo[h] for mo in sells])
        mk[h]={"buy":b, "sell":s, "round_trip":(b+s) if (b is not None and s is not None) else None}
    return {"n_buy":len(buys), "n_sell":len(sells), "avg_spread_bp":_mean(spreads), "markout":mk}

def _summary(done, spreads, partial):
    a=_aggregate(done, spreads); tag="[partial]" if partial else "[FINAL]"
    print(f"\n{tag} events: {a['n_buy']} buy-fills, {a['n_sell']} sell-fills, "
          f"avg spread {_fmt(a['avg_spread_bp'])}bp")
    print(f"  {'horizon':<8}{'buy_markout':>13}{'sell_markout':>14}{'ROUND-TRIP':>13}  (median bp, +=maker profit)")
    for h in HORIZONS:
        m=a["markout"][h]
        print(f"  {str(h)+'s':<8}{_fmt(m['buy']):>13}{_fmt(m['sell']):>14}{_fmt(m['round_trip']):>13}")
    if not partial:
        rt=a["markout"].get(30, {}).get("round_trip")
        if rt is not None:
            print(f"\n  => implied per-round-trip maker edge ≈ {rt:.2f} bp (30s markout).")
            print(f"     >0  : 买低卖高 has a real edge -> map to backtest adv ≈ {max(0,(1.8-rt)/2):.2f}bp/side")
            print(f"     <=0 : adverse selection eats the spread -> strategy ≈ just hold (or worse).")

def _write_status(out_dir, symbol, start, now, done, spreads, history):
    """Emit out_dir/status_<symbol>.json for the dashboard (atomic, best-effort)."""
    try:
        a=_aggregate(done, spreads)
        history.append({"t":round(now-start), "rt30":a["markout"].get(30, {}).get("round_trip")})
        history[:]=history[-600:]
        doc={"symbol":symbol, "updated_utc":time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
             "elapsed_sec":round(now-start), "ws_url":WS_URL, **a, "history":list(history)}
        os.makedirs(out_dir, exist_ok=True)
        p=os.path.join(out_dir, f"status_{symbol}.json"); tmp=p+".tmp"
        with open(tmp, "w") as f: json.dump(doc, f)
        os.replace(tmp, p)
    except Exception:
        pass

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbol", default=_D.get("symbol", "USD1USDT"))
    ap.add_argument("--seconds", type=int, default=int(_D.get("seconds", 600)))
    ap.add_argument("--csv", default=None)
    a=ap.parse_args()
    asyncio.run(run(a.symbol, a.seconds, a.csv))
