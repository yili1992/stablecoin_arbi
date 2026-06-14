#!/usr/bin/env python3
"""
Regenerate the historical kline CSVs in data/ from Bybit's public spot API (no key).
The repo ships with data already, but run this to refresh or extend the window.

Usage:  python3 fetch_data.py [--days 210]
"""
import urllib.request, json, time, csv, os, argparse

SYMBOLS = ["USD1USDT", "USDEUSDT", "USDTBUSDT"]
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

def _get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return json.load(urllib.request.urlopen(req, timeout=25))
        except Exception:
            if attempt == 3:
                raise
            time.sleep(0.5)

def fetch(symbol, interval, days):
    """Page backwards `days` of klines. interval '5' (5m) or '60' (1h)."""
    ms_now = int(time.time() * 1000)
    target = ms_now - days * 86400 * 1000
    out, end, calls = {}, ms_now, 0
    while True:
        d = _get(f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}"
                 f"&interval={interval}&limit=1000&end={end}")
        rows = d["result"]["list"]
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = r
        oldest = min(int(r[0]) for r in rows)
        if oldest <= target or len(rows) < 1000 or calls > 300:
            break
        end = oldest - 1
        calls += 1
        time.sleep(0.05)
    return [out[k] for k in sorted(out)]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=210, help="how many days of 5m to pull (1h pulls >=420)")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    for s in SYMBOLS:
        for interval, tf, days in [("5", "5m", a.days), ("60", "1h", max(a.days, 420))]:
            rows = fetch(s, interval, days)
            fn = os.path.join(DATA, f"{s}_{tf}.csv")
            with open(fn, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
                for r in rows:
                    w.writerow(r[:7])
            print(f"{s} {tf}: {len(rows)} rows -> {fn}")
