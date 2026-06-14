"""Lightweight, zero-dependency web dashboard for dryrun results.

Reads ``<out-dir>/status_<symbol>.json`` (written by dryrun every ~30s) and serves a live
single-page dashboard: per-symbol round-trip markout (the real per-trade edge), buy/sell
markout @5s/30s, event counts, spread, uptime, and a markout-over-time chart. No external
deps, no CDN (works air-gapped). Serves on 0.0.0.0:<port>.

Run:  sca dashboard --port 3015 --out-dir ./out
      (or via:  docker compose --profile dryrun up -d  -> http://<host>:3015)
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import http.server

try:
    from sca.config import CFG as _CFG
    _PORT = int(_CFG.get("dryrun", {}).get("dashboard_port", 3015))
except Exception:
    _PORT = 3015

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>stablecoin_arbi — dryrun dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0e1116;--card:#161b22;--mut:#8b949e;--fg:#e6edf3;--grn:#3fb950;--red:#f85149;--bd:#30363d}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
 header{padding:16px 22px;border-bottom:1px solid var(--bd)}
 h1{margin:0;font-size:17px} .sub{color:var(--mut);font-size:12px;margin-top:4px}
 .wrap{display:flex;flex-wrap:wrap;gap:16px;padding:20px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;width:340px}
 .sym{font-weight:600;font-size:15px} .upd{color:var(--mut);font-size:11px;float:right}
 .big{font-size:34px;font-weight:700;margin:10px 0 2px} .biglbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
 table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
 td,th{padding:4px 6px;text-align:right;border-top:1px solid var(--bd)} th{color:var(--mut);font-weight:500;text-align:right}
 td:first-child,th:first-child{text-align:left}
 .meta{color:var(--mut);font-size:12px;margin-top:10px;display:flex;justify-content:space-between}
 canvas{margin-top:12px;width:100%;height:70px;display:block}
 .pos{color:var(--grn)} .neg{color:var(--red)}
 .legend{color:var(--mut);font-size:12px;padding:0 22px 18px}
 .empty{color:var(--mut);padding:24px}
</style></head><body>
<header><h1>stablecoin_arbi — dryrun dashboard</h1>
<div class="sub">Live maker fill-quality (adverse-selection markout). No orders, public data. The
<b>ROUND-TRIP</b> 30s markout is your real per-trade edge: <span class="pos">&gt;0</span> = a real edge;
<span class="neg">&le;0</span> = strategy ≈ just holding.</div></header>
<div class="wrap" id="wrap"><div class="empty">waiting for status_*.json … start a dryrun.</div></div>
<div class="legend">Auto-refreshes every 4s · run dryrun for days spanning active periods for a stable read.</div>
<script>
const F=(x)=>x==null?'n/a':(x>0?'+':'')+x.toFixed(2);
const cls=(x)=>x==null?'':(x>0?'pos':(x<0?'neg':''));
function dur(s){if(s==null)return'';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return (d?d+'d ':'')+(h?h+'h ':'')+m+'m';}
function chart(cv,hist){const ctx=cv.getContext('2d');const W=cv.width=cv.clientWidth*2,H=cv.height=140;
  ctx.clearRect(0,0,W,H);const pts=hist.filter(p=>p.rt30!=null);if(pts.length<2)return;
  const ys=pts.map(p=>p.rt30),mn=Math.min(0,...ys),mx=Math.max(0,...ys),rng=(mx-mn)||1;
  const X=i=>i/(pts.length-1)*W, Y=v=>H-8-(v-mn)/rng*(H-16);
  ctx.strokeStyle='#30363d';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,Y(0));ctx.lineTo(W,Y(0));ctx.stroke();
  ctx.strokeStyle=ys[ys.length-1]>0?'#3fb950':'#f85149';ctx.lineWidth=2;ctx.beginPath();
  pts.forEach((p,i)=>{const x=X(i),y=Y(p.rt30);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();}
function card(s){
  const rt=s.markout&&s.markout['30']?s.markout['30'].round_trip:null;
  const m5=s.markout&&s.markout['5']||{},m30=s.markout&&s.markout['30']||{};
  return `<div class="card"><div><span class="sym">${s.symbol}</span>
    <span class="upd">updated ${s.updated_utc||''}</span></div>
    <div class="biglbl">round-trip markout (30s)</div>
    <div class="big ${cls(rt)}">${F(rt)} <span style="font-size:14px;color:var(--mut)">bp</span></div>
    <table><tr><th>horizon</th><th>buy</th><th>sell</th><th>round-trip</th></tr>
    <tr><td>5s</td><td class="${cls(m5.buy)}">${F(m5.buy)}</td><td class="${cls(m5.sell)}">${F(m5.sell)}</td><td class="${cls(m5.round_trip)}">${F(m5.round_trip)}</td></tr>
    <tr><td>30s</td><td class="${cls(m30.buy)}">${F(m30.buy)}</td><td class="${cls(m30.sell)}">${F(m30.sell)}</td><td class="${cls(m30.round_trip)}">${F(m30.round_trip)}</td></tr></table>
    <div class="meta"><span>buys ${s.n_buy||0} · sells ${s.n_sell||0}</span><span>spread ${F(s.avg_spread_bp)}bp · ${dur(s.elapsed_sec)}</span></div>
    <canvas></canvas></div>`;}
async function tick(){
  try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();
    const syms=Object.values(d);const w=document.getElementById('wrap');
    if(!syms.length){return;}
    w.innerHTML=syms.map(card).join('');
    syms.forEach((s,i)=>chart(w.children[i].querySelector('canvas'),s.history||[]));
  }catch(e){}
}
tick();setInterval(tick,4000);
</script></body></html>"""


def _read_status(out_dir: str) -> dict:
    out = {}
    for f in sorted(glob.glob(os.path.join(out_dir, "status_*.json"))):
        try:
            with open(f) as fh:
                out[os.path.basename(f)[len("status_"):-len(".json")]] = json.load(fh)
        except Exception:
            pass
    return out


class _Handler(http.server.BaseHTTPRequestHandler):
    out_dir = "."

    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            self._send(json.dumps(_read_status(self.out_dir)).encode(), "application/json")
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", _PORT)))
    ap.add_argument("--out-dir", default=os.environ.get("SCA_OUT_DIR", "./out"))
    a = ap.parse_args(argv)
    _Handler.out_dir = a.out_dir
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", a.port), _Handler)
    print(f"[dashboard] serving http://0.0.0.0:{a.port}  (reading {a.out_dir}/status_*.json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
