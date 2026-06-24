"""Lightweight, zero-dependency Chinese web dashboard for the paper-trading engine.

Reads ``<out-dir>/status_<symbol>.json`` (written by the paper engine every ~10-15s,
atomic tmp+rename) and serves a self-contained single-page dashboard. NO external JS /
NO CDN — the K-line candlestick chart and the markout sparkline are drawn on a <canvas>
with vanilla JS, so it works fully air-gapped. All UI text is in Chinese (中文).

Per symbol it renders: top quote bar (symbol / mode / bid·ask·mid / anchor / spread /
uptime / update time), a candlestick chart (status.klines) overlaid with the floating
EMA anchor line, the 5 sell-rung lines, the rebuy line and the buy/sell event markers,
a slice position table, a deployment-rate bar, a PnL card, a fill-quality (markout) card
with a round-trip sparkline, and a recent-events log. It tolerates ``null`` everywhere
(shows ``n/a`` instead of crashing) and never assumes a field is present.

Run:  sca dashboard --port 3015 --out-dir ./out
      (port default: config runtime.dashboard_port=3015, or env DASHBOARD_PORT)
"""
from __future__ import annotations
import argparse
import glob
import gzip
import json
import os
import http.server

try:
    from sca.config import (CFG as _CFG, out_dir as _cfg_out_dir,
                            resolve_mode as _cfg_resolve_mode, runtime as _cfg_runtime)
    _PORT = _cfg_runtime()["dashboard_port"]
except Exception:
    _CFG = {}
    _PORT = 3015
    def _cfg_out_dir(fallback=".", cfg=None):
        return os.environ.get("SCA_OUT_DIR") or fallback

    def _cfg_resolve_mode(cfg=None, env=None):
        env = os.environ if env is None else env
        return env.get("MODE") if env.get("MODE") in ("dryrun", "live") else "dryrun"

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>稳定币套利 — 模拟盘看板</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0d1117;--card:#161b22;--card2:#0f141a;--mut:#8b949e;--fg:#e6edf3;
   --grn:#3fb950;--red:#f85149;--bd:#30363d;--acc:#e3b341;--blu:#58a6ff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,Helvetica,Arial,sans-serif}
 header.page{padding:16px 22px;border-bottom:1px solid var(--bd)}
 header.page h1{margin:0;font-size:18px}
 .psub{color:var(--mut);font-size:12px;margin-top:4px}
 #wrap{max-width:1180px;margin:0 auto;padding:18px 16px;display:flex;flex-direction:column;gap:22px}
 .empty{color:var(--mut);padding:48px;text-align:center}
 section.sym{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px}
 .top{display:flex;flex-wrap:wrap;align-items:center;gap:14px;border-bottom:1px solid var(--bd);padding-bottom:12px}
 .title{display:flex;align-items:center;gap:8px}
 .name{font-size:18px;font-weight:700}
 .badge{font-size:11px;padding:2px 9px;border-radius:20px;font-weight:600}
 .badge.paper{background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb66}
 .badge.live{background:#f8514933;color:#f85149;border:1px solid #f8514966}
 .badge.dryrun{background:#8b949e33;color:#8b949e;border:1px solid #8b949e55}
 .quotes{display:flex;flex-wrap:wrap;gap:16px;margin-left:auto}
 .q{display:flex;flex-direction:column;align-items:flex-end}
 .ql{color:var(--mut);font-size:10px;letter-spacing:.04em}
 .qv{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
 .grid{display:grid;grid-template-columns:1.9fr 1fr;gap:16px;margin-top:14px}
 @media(max-width:860px){.grid{grid-template-columns:1fr}}
 .chartbox{background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:8px}
 canvas.kchart{width:100%;height:360px;display:block}
 .side{display:flex;flex-direction:column;gap:12px}
 .panel{background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:12px}
 .ph{font-size:12px;color:var(--mut);margin-bottom:8px;font-weight:600;letter-spacing:.03em}
 .prow{display:flex;justify-content:space-between;padding:3px 0;font-variant-numeric:tabular-nums}
 .prow.tot{border-top:1px solid var(--bd);margin-top:4px;padding-top:6px;font-weight:700;font-size:15px}
 .sub2{color:var(--mut);font-size:11px;margin-top:6px}
 .bar{height:14px;border-radius:7px;background:#21262d;overflow:hidden;border:1px solid var(--bd)}
 .barfill{height:100%;background:linear-gradient(90deg,#238636,#3fb950)}
 table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
 th,td{padding:4px 6px;text-align:right;border-top:1px solid var(--bd)}
 th{color:var(--mut);font-weight:500}
 td:first-child,th:first-child{text-align:left}
 canvas.mkchart{width:100%;height:64px;display:block;margin-top:8px}
 .note{color:var(--mut);font-size:11px;margin-top:6px}
 .tables{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;margin-top:14px}
 @media(max-width:860px){.tables{grid-template-columns:1fr}}
 .pos{color:var(--grn)} .neg{color:var(--red)} .mut{color:var(--mut)}
 .evbuy{color:var(--grn);font-weight:600} .evsell{color:var(--red);font-weight:600}
 .legend{color:var(--mut);font-size:12px;text-align:center;padding:0 16px 28px;max-width:1180px;margin:0 auto}
</style></head><body>
<header class="page"><h1 id="ptitle">稳定币套利 · 实时看板</h1>
<div class="psub" id="psub">EMA 锚定分片卖出阶梯策略 · 成交质量 <b>往返 markout</b> 是真实每笔边际:<span class="pos">&gt;0</span> 才有真 edge;<span class="neg">&le;0</span> 时策略 ≈ 单纯持有(本看板不暗示稳赚)。</div></header>
<div id="wrap"><div class="empty">等待 status_*.json …</div></div>
<div class="legend"><button onclick="tick()" style="background:#238636;color:#fff;border:0;border-radius:6px;padding:6px 16px;cursor:pointer;font-size:13px">🔄 刷新</button> &nbsp; 每 60 秒自动刷新 · 点按钮立即刷新 · 价格轴已放大到 bp 级别 · 多标的各占一个区块。<br>K线指标线：<span style="color:#e3b341">━━ EMA锚</span> &nbsp; <span style="color:#f85149">┄┄ 卖出档位(锚上各档)</span> &nbsp; <span style="color:#3fb950">┄┄ 买回线(min(锚,买一)-1bp)</span> &nbsp; <span style="color:#3fb950">▲买</span>/<span style="color:#f85149">▼卖</span> 成交点</div>
<script src="/dashboard.js"></script></body></html>"""

DASHBOARD_JS = '\n// ---- JSON-safe helpers: treat null / NaN / Infinity uniformly as "no value" ----\nfunction num(x){return (typeof x===\'number\' && isFinite(x))?x:null;}\nfunction fp(x,d){x=num(x);return x==null?\'n/a\':x.toFixed(d==null?5:d);}      // price\nfunction fnum(x,d){x=num(x);return x==null?\'n/a\':x.toFixed(d==null?2:d);}    // qty/value\nfunction fb(x){x=num(x);return x==null?\'n/a\':(x>0?\'+\':\'\')+x.toFixed(2);}     // signed bp\nfunction fpct(x){x=num(x);return x==null?\'n/a\':(x>0?\'+\':\'\')+x.toFixed(2)+\'%\';}\nfunction fusd(x){x=num(x);return x==null?\'n/a\':(x<0?\'-\':\'\')+\'$\'+Math.abs(x).toLocaleString(\'en-US\',{minimumFractionDigits:2,maximumFractionDigits:2});}\nfunction cls(x){x=num(x);return x==null?\'\':(x>0?\'pos\':(x<0?\'neg\':\'\'));}\nfunction esc(s){return String(s==null?\'\':s).replace(/[&<>]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\'}[c]));}\nfunction dur(s){s=num(s);if(s==null)return \'n/a\';const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);\n  return (d?d+\'天 \':\'\')+(h?h+\'时 \':\'\')+m+\'分\';}\nfunction pad2(n){return String(n).padStart(2,\'0\');}\nfunction hhmm(ms){const d=new Date(ms);return pad2(d.getHours())+\':\'+pad2(d.getMinutes());}\nfunction hhmmss(ms){const d=new Date(ms);return pad2(d.getHours())+\':\'+pad2(d.getMinutes())+\':\'+pad2(d.getSeconds());}\nfunction utctime(s){if(!s)return \'n/a\';const i=s.indexOf(\'T\');return i>=0?s.slice(i+1).replace(\'Z\',\' UTC\'):s;}\nconst MODE={paper:\'模拟盘\',live:\'实盘\',dryrun:\'试运行\'};\n\nfunction tri(ctx,x,y,sz,up){const w=sz*0.62;ctx.beginPath();\n  if(up){ctx.moveTo(x,y);ctx.lineTo(x-w,y+sz);ctx.lineTo(x+w,y+sz);}        // ▲ apex at price\n  else  {ctx.moveTo(x,y);ctx.lineTo(x-w,y-sz);ctx.lineTo(x+w,y-sz);}        // ▼ apex at price\n  ctx.closePath();ctx.fill();}\n\n// ---- candlestick K-line chart with strategy overlays ----\nfunction drawChart(cv,s){\n  const dpr=window.devicePixelRatio||1, cssW=cv.clientWidth||760, cssH=360;\n  cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr);\n  const ctx=cv.getContext(\'2d\'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,cssW,cssH);\n  const padL=8,padR=70,padT=10,padB=22, W=cssW-padL-padR, H=cssH-padT-padB;\n  const ks=(s.klines||[]).filter(k=>k&&num(k.t)!=null);\n  ctx.textBaseline=\'middle\'; ctx.font=\'10px monospace\';\n  if(ks.length<1){ctx.fillStyle=\'#8b949e\';ctx.font=\'13px sans-serif\';\n    ctx.fillText(\'暂无 K线数据\',padL+12,padT+26);return;}\n  let bar=ks.length>1?(ks[1].t-ks[0].t):300000; if(!(bar>0))bar=300000;\n  const tMin=ks[0].t, tMax=ks[ks.length-1].t+bar, tSpan=(tMax-tMin)||1;\n  const ind=s.indicators||{};\n  let lo=Infinity,hi=-Infinity;\n  ks.forEach(k=>{const l=num(k.l),h=num(k.h); if(l!=null)lo=Math.min(lo,l); if(h!=null)hi=Math.max(hi,h);});\n  [num(ind.anchor),num(ind.rebuy_price)].forEach(v=>{if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v);}});\n  (ind.sell_rungs||[]).forEach(r=>{const p=num(r&&r.price); if(p!=null){lo=Math.min(lo,p);hi=Math.max(hi,p);}});\n  (s.events||[]).forEach(e=>{const p=num(e&&e.price),t=num(e&&e.ts); if(p!=null&&t!=null&&t>=tMin&&t<=tMax){lo=Math.min(lo,p);hi=Math.max(hi,p);}});\n  if(!isFinite(lo)||!isFinite(hi)){ctx.fillStyle=\'#8b949e\';ctx.font=\'13px sans-serif\';ctx.fillText(\'暂无价格区间\',padL+12,padT+26);return;}\n  if(hi<=lo){const m=Math.abs(lo)||1; hi=lo+m*1e-4; lo=lo-m*1e-4;}\n  const pd=(hi-lo)*0.08; lo-=pd; hi+=pd; const pSpan=(hi-lo)||1;\n  const X=t=>padL+(t-tMin)/tSpan*W, Y=p=>padT+(hi-p)/pSpan*H;\n  // price grid + right-axis labels\n  ctx.lineWidth=1;\n  for(let g=0;g<=5;g++){const p=lo+pSpan*g/5,y=Y(p);\n    ctx.strokeStyle=\'#21262d\';ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(padL+W,y);ctx.stroke();\n    ctx.fillStyle=\'#8b949e\';ctx.textAlign=\'left\';ctx.fillText(p.toFixed(5),padL+W+5,y);}\n  // time labels\n  ctx.textAlign=\'center\';ctx.fillStyle=\'#8b949e\';\n  for(let g=0;g<=4;g++){const t=tMin+tSpan*g/4,x=padL+(t-tMin)/tSpan*W;\n    ctx.fillText(hhmm(t),Math.min(Math.max(x,padL+16),padL+W-16),padT+H+12);}\n  ctx.textAlign=\'left\';\n  // candles\n  const cw=Math.max(1,Math.min(14,W/ks.length*0.62));\n  ks.forEach(k=>{const o=num(k.o),h=num(k.h),l=num(k.l),c=num(k.c); if(o==null||c==null)return;\n    const xc=X(k.t+bar/2), up=c>=o; ctx.strokeStyle=ctx.fillStyle=up?\'#3fb950\':\'#f85149\';\n    if(h!=null&&l!=null){ctx.beginPath();ctx.moveTo(xc,Y(h));ctx.lineTo(xc,Y(l));ctx.stroke();}\n    const y1=Y(Math.max(o,c)),y2=Y(Math.min(o,c)); ctx.fillRect(xc-cw/2,y1,cw,Math.max(1,y2-y1));});\n  // overlay: sell rungs (dashed RED — 卖出档位)\n  (ind.sell_rungs||[]).forEach(r=>{const p=num(r&&r.price); if(p==null)return; const y=Y(p);\n    ctx.save();ctx.setLineDash([5,4]);ctx.strokeStyle=\'#f85149\';ctx.lineWidth=1;\n    ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(padL+W,y);ctx.stroke();\n    ctx.fillStyle=\'#f85149\';ctx.fillText(\'卖\'+(num(r.bp)!=null?(\'+\'+r.bp+\'bp\'):\'\'),padL+3,y-5);ctx.restore();});\n  // overlay: rebuy/BUY line (dashed GREEN, thicker — distinct from red sells & gold anchor)\n  const rb=num(ind.rebuy_price); if(rb!=null){const y=Y(rb);\n    ctx.save();ctx.setLineDash([6,3]);ctx.strokeStyle=\'#3fb950\';ctx.lineWidth=1.5;\n    ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(padL+W,y);ctx.stroke();\n    ctx.fillStyle=\'#3fb950\';ctx.fillText(\'买回 \'+rb.toFixed(5),padL+3,y+12);ctx.restore();}\n  // overlay: EMA anchor (solid gold)\n  const an=num(ind.anchor); if(an!=null){const y=Y(an);\n    ctx.strokeStyle=\'#e3b341\';ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(padL,y);ctx.lineTo(padL+W,y);ctx.stroke();\n    ctx.fillStyle=\'#e3b341\';ctx.fillText(\'锚 \'+an.toFixed(5),padL+3,y-5);}\n  // event markers (▲ buy green / ▼ sell red)\n  (s.events||[]).forEach(e=>{const p=num(e&&e.price),t=num(e&&e.ts); if(p==null||t==null)return;\n    const x=X(t); if(x<padL-2||x>padL+W+2)return; const y=Y(p);\n    if(e.side===\'buy\'){ctx.fillStyle=\'#3fb950\';tri(ctx,x,y,7,true);}\n    else if(e.side===\'sell\'){ctx.fillStyle=\'#f85149\';tri(ctx,x,y,7,false);}});\n}\n\n// ---- round-trip markout sparkline (history.rt30) ----\nfunction miniChart(cv,hist){\n  const dpr=window.devicePixelRatio||1, cssW=cv.clientWidth||300, cssH=64;\n  cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr);\n  const ctx=cv.getContext(\'2d\'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,cssW,cssH);\n  const pts=(hist||[]).filter(p=>num(p&&p.rt30)!=null);\n  if(pts.length<2){ctx.fillStyle=\'#8b949e\';ctx.font=\'10px sans-serif\';ctx.textBaseline=\'middle\';\n    ctx.fillText(\'往返 markout 历史不足\',6,cssH/2);return;}\n  const ys=pts.map(p=>p.rt30), mn=Math.min(0,...ys), mx=Math.max(0,...ys), rng=(mx-mn)||1;\n  const X=i=>i/(pts.length-1)*cssW, Y=v=>cssH-6-(v-mn)/rng*(cssH-12);\n  ctx.strokeStyle=\'#30363d\';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(0,Y(0));ctx.lineTo(cssW,Y(0));ctx.stroke();\n  ctx.strokeStyle=ys[ys.length-1]>0?\'#3fb950\':\'#f85149\';ctx.lineWidth=1.5;ctx.beginPath();\n  pts.forEach((p,i)=>{const x=X(i),y=Y(p.rt30); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();\n}\n\nfunction mkRow(label,m){m=m||{};\n  return `<tr><td>${label}</td>\n    <td class="${cls(m.buy)}">${fb(m.buy)}</td>\n    <td class="${cls(m.sell)}">${fb(m.sell)}</td>\n    <td class="${cls(m.round_trip)}">${fb(m.round_trip)}</td></tr>`;}\n\nfunction card(s){\n  const px=s.price||{}, ind=s.indicators||{}, pos=s.position||{}, pnl=s.pnl||{}, mk=s.markout||{};\n  const mode=(s.mode||\'paper\'); const modeC=(MODE[mode]?mode:\'dryrun\');\n  const bid=num(px.bid), ask=num(px.ask), mid=num(px.mid);\n  let spread=null;\n  if(bid!=null&&ask!=null&&mid){spread=(ask-bid)/mid*1e4;}\n  else if(num(s.avg_spread_bp)!=null){spread=num(s.avg_spread_bp);}\n  const span=num(ind.anchor_ema_span)!=null?ind.anchor_ema_span:21;\n  const usd1pct=num(pos.usd1_pct), usd1w=usd1pct==null?0:Math.max(0,Math.min(100,usd1pct));\n\n  // position rows\n  const slices=(pos.slices||[]);\n  const posRows = slices.length? slices.map(sl=>{\n      const st=sl.state===\'usd1\';\n      return `<tr><td>#${esc(sl.i)}</td>\n        <td class="${st?\'pos\':\'mut\'}">${st?\'USD1 持有\':\'USDT 空闲\'}</td>\n        <td>${num(sl.frac)!=null?(sl.frac*100).toFixed(0)+\'%\':\'n/a\'}</td>\n        <td>${fnum(sl.qty,2)}</td>\n        <td>${fp(sl.entry_price)}</td>\n        <td>${fp(sl.sell_target)}</td>\n        <td>${fusd(sl.value_usd)}</td></tr>`;}).join(\'\')\n    : `<tr><td colspan="7" class="mut" style="text-align:center">暂无仓位数据</td></tr>`;\n\n  // events (newest first)\n  const evs=(s.events||[]).slice().reverse().slice(0,14);\n  const evRows = evs.length? evs.map(e=>{\n      const buy=e.side===\'buy\';\n      return `<tr><td>${num(e.ts)!=null?hhmmss(e.ts):esc(e.utc)}</td>\n        <td class="${buy?\'evbuy\':\'evsell\'}">${buy?\'买入 ▲\':\'卖出 ▼\'}</td>\n        <td>#${esc(e.slice)}</td>\n        <td>${fp(e.price)}</td></tr>`;}).join(\'\')\n    : `<tr><td colspan="4" class="mut" style="text-align:center">暂无成交</td></tr>`;\n\n  return `<section class="sym">\n    <div class="top">\n      <div class="title"><span class="name">${esc(s.symbol)}</span>\n        <span class="badge ${modeC}">${MODE[modeC]||mode}</span></div>\n      <div class="quotes">\n        <div class="q"><span class="ql">中间价</span><span class="qv">${fp(mid)}</span></div>\n        <div class="q"><span class="ql">买一 / 卖一</span><span class="qv">${fp(bid)} / ${fp(ask)}</span></div>\n        <div class="q"><span class="ql">最新</span><span class="qv">${fp(px.last)}</span></div>\n        <div class="q"><span class="ql">锚 EMA${span}(1h)</span><span class="qv">${fp(num(ind.anchor)!=null?ind.anchor:s.anchor)}</span></div>\n        <div class="q"><span class="ql">价差</span><span class="qv">${fb(spread)} bp</span></div>\n        <div class="q"><span class="ql">运行时长</span><span class="qv">${dur(s.elapsed_sec)}</span></div>\n        <div class="q"><span class="ql">更新</span><span class="qv">${utctime(s.updated_utc)}</span></div>\n      </div>\n    </div>\n    <div class="grid">\n      <div class="chartbox"><canvas class="kchart"></canvas></div>\n      <div class="side">\n        <div class="panel"><div class="ph">盈亏</div>\n          <div class="prow"><span>价差实现</span><span class="${cls(pnl.realized_price)}">${fusd(pnl.realized_price)}</span></div>\n          <div class="prow"><span>已结算利息</span><span class="${cls(pnl.accrued_interest)}">${fusd(pnl.accrued_interest)}</span></div>\n          <div class="prow"><span class="mut">本日待结(估)</span><span class="mut">${fusd(pnl.pending_interest)}</span></div>\n          <div class="prow"><span>浮动盈亏</span><span class="${cls(pnl.unrealized)}">${fusd(pnl.unrealized)}</span></div>\n          <div class="prow tot"><span>合计</span><span class="${cls(pnl.total)}">${fusd(pnl.total)}</span></div>\n          <div class="prow"><span>估算年化</span><span class="${cls(pnl.apr_est)}">${fpct(pnl.apr_est)}</span></div>\n          <div class="sub2">初始 ${fusd(pnl.start_value)} → 当前 ${fusd(pos.total_value)}</div>\n          <div class="sub2">利息按 Bybit 规则：UTC 日 · 每小时快照取<b>最小持有量</b>日结。\n            合计只计<b>已结算</b>；本日待结为上限估值,未满整 UTC 日不入账(故首日为 0)。</div>\n        </div>\n        <div class="panel"><div class="ph">部署率</div>\n          <div class="bar"><div class="barfill" style="width:${usd1w}%"></div></div>\n          <div class="prow"><span class="pos">USD1 持有 ${fpct0(usd1pct)}</span>\n            <span class="mut">USDT 空闲 ${fpct0(usd1pct==null?null:100-usd1pct)}</span></div>\n          <div class="sub2">${num(pos.n_in_usd1)!=null?pos.n_in_usd1:\'?\'} 片持有 ·\n            ${num(pos.n_in_usdt)!=null?pos.n_in_usdt:\'?\'} 片空闲 · 总值 ${fusd(pos.total_value)}</div>\n        </div>\n        <div class="panel"><div class="ph">成交质量 (markout, bp)</div>\n          <table><tr><th>时窗</th><th>买</th><th>卖</th><th>往返</th></tr>\n            ${mkRow(\'5s\',mk[\'5\'])}${mkRow(\'30s\',mk[\'30\'])}</table>\n          <canvas class="mkchart"></canvas>\n          <div class="note">往返 &gt;0 才有真 edge,否则 ≈ 持有 · 样本 买${num(s.n_buy)!=null?s.n_buy:0}/卖${num(s.n_sell)!=null?s.n_sell:0}</div>\n        </div>\n      </div>\n    </div>\n    <div class="tables">\n      <div class="panel"><div class="ph">仓位 (${slices.length} 片)</div>\n        <table><tr><th>序号</th><th>状态</th><th>占比</th><th>数量</th><th>成本</th><th>卖出目标</th><th>市值</th></tr>\n          ${posRows}</table></div>\n      <div class="panel"><div class="ph">最近成交</div>\n        <table><tr><th>时间</th><th>方向</th><th>档位</th><th>价格</th></tr>\n          ${evRows}</table></div>\n    </div>\n  </section>`;\n}\nfunction fpct0(x){x=num(x);return x==null?\'n/a\':x.toFixed(1)+\'%\';}\n\nasync function tick(){\n  try{\n    const r=await fetch(\'/api/status\',{cache:\'no-store\'});\n    const d=await r.json();\n    window._last=d;\n    render(d);\n  }catch(e){/* keep last good render */}\n}\n// page-level header reflects the ACTUAL mode(s) present (the per-card badge already shows\n// each symbol\'s mode). CRITICAL in live: never claim "模拟盘 / 不下真单" while real PostOnly\n// orders are trading real money.\nfunction setHeader(syms){\n  const modes=new Set((syms||[]).map(s=>s&&s.mode).filter(Boolean));\n  const t=document.getElementById(\'ptitle\'), p=document.getElementById(\'psub\');\n  if(!t||!p)return;\n  if(modes.has(\'live\')){\n    t.innerHTML=\'稳定币套利 · <span style="color:var(--red)">实盘看板 ⚠️</span>\';\n    p.innerHTML=\'<b style="color:var(--red)">LIVE · 真实 PostOnly 挂单(真金,会下真单)</b> · 实时 Bybit · 往返 markout <span class="pos">&gt;0</span> 才有真 edge,<span class="neg">&le;0</span> ≈ 持有。\';\n  }else{\n    t.textContent=\'稳定币套利 · \'+(modes.has(\'dryrun\')?\'试运行看板 (dryrun)\':\'模拟盘看板 (paper)\');\n    p.innerHTML=\'实时 Bybit 公共数据模拟撮合,<b>不下真单</b> · 往返 markout <span class="pos">&gt;0</span> 才有真 edge,<span class="neg">&le;0</span> ≈ 持有(不暗示稳赚)。\';\n  }\n}\nfunction render(d){\n  const w=document.getElementById(\'wrap\');\n  const syms=Object.values(d||{}).filter(x=>x&&typeof x===\'object\');\n  if(!syms.length){w.innerHTML=\'<div class="empty">等待 status_*.json …</div>\';return;}\n  syms.sort((a,b)=>String(a.symbol).localeCompare(String(b.symbol)));\n  setHeader(syms);\n  w.innerHTML=syms.map(card).join(\'\');\n  syms.forEach((s,i)=>{const sec=w.children[i]; if(!sec)return;\n    drawChart(sec.querySelector(\'.kchart\'),s);\n    miniChart(sec.querySelector(\'.mkchart\'),s.history||[]);});\n}\nwindow.addEventListener(\'resize\',()=>{if(window._last)render(window._last);});\ntick();                                                  // initial load\n// auto-refresh ~every 12s (matches the engine\'s status write cadence) so a page opened\n// during a restart / empty window recovers on its own; paused while the tab is hidden. The\n// manual 🔄 button still forces an immediate refresh.\nsetInterval(function(){if(!document.hidden)tick();},60000);\n'

DASHBOARD_JS = DASHBOARD_JS.replace(
    "  }catch(e){/* keep last good render */}\n",
    "  }catch(e){\n"
    "    console.warn('dashboard status fetch failed', e);\n"
    "    if(!window._last){\n"
    "      const w=document.getElementById('wrap');\n"
    "      if(w)w.innerHTML='<div class=\"empty neg\">数据加载失败,请稍后刷新。</div>';\n"
    "    }\n"
    "  }\n",
)


def _read_status(out_dir: str, mode: str | None = None) -> dict:
    out = {}
    active_mode = mode or _cfg_resolve_mode(_CFG)
    for f in sorted(glob.glob(os.path.join(out_dir, "status_*.json"))):
        try:
            with open(f) as fh:
                doc = json.load(fh)
            if doc.get("mode") != active_mode:
                continue
            stem = os.path.basename(f)[len("status_"):-len(".json")]
            out[stem] = doc
        except Exception:
            pass
    return out


def _maybe_gzip(body: bytes, headers: dict) -> tuple[bytes, dict]:
    resp_headers = {}
    if "gzip" in (headers.get("Accept-Encoding") or ""):
        body = gzip.compress(body)
        resp_headers["Content-Encoding"] = "gzip"
        resp_headers["Vary"] = "Accept-Encoding"
    return body, resp_headers


def _asset_response(path: str, headers: dict) -> tuple[bytes, str, dict]:
    if path != "/dashboard.js":
        raise FileNotFoundError(path)
    body, resp_headers = _maybe_gzip(DASHBOARD_JS.encode("utf-8"), headers)
    return body, "application/javascript; charset=utf-8", resp_headers


def _status_response(out_dir: str, headers: dict, mode: str | None = None) -> tuple[bytes, str, dict]:
    body = json.dumps(_read_status(out_dir, mode=mode)).encode("utf-8")
    body, resp_headers = _maybe_gzip(body, headers)
    return body, "application/json; charset=utf-8", resp_headers


class _Handler(http.server.BaseHTTPRequestHandler):
    out_dir = "."

    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, extra_headers: dict | None = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.startswith("/api/status"):
            body, ctype, headers = _status_response(self.out_dir, self.headers)
            self._send(body, ctype, headers)
        elif self.path.startswith("/dashboard.js"):
            body, ctype, headers = _asset_response("/dashboard.js", self.headers)
            self._send(body, ctype, headers)
        else:
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="稳定币套利模拟盘看板 (Chinese paper-trading dashboard)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", _PORT)))
    ap.add_argument("--out-dir", default=_cfg_out_dir("./out"))
    a = ap.parse_args(argv)
    _Handler.out_dir = a.out_dir
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", a.port), _Handler)
    print(f"[dashboard] serving http://0.0.0.0:{a.port}  (reading {a.out_dir}/status_*.json)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
