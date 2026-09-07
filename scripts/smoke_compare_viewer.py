#!/usr/bin/env python3
"""Tiny local viewer for smoke_mt5_vs_cache.json (port 8791)."""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = ROOT / "data" / "runs" / "smoke_mt5_vs_cache.json"
STATIC = Path(__file__).resolve().parent / "smoke_compare_static"


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>alexg7 smoke — cache vs MT5</title>
<style>
  :root { --bg:#0f1419; --panel:#1a222c; --fg:#e7ecf1; --muted:#8b9aab; --a:#5b9fd4; --b:#d4a05b; --win:#3d9a6a; --loss:#c45c5c; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:1rem 1.25rem; border-bottom:1px solid #2a3440; }
  h1 { margin:0; font-size:1.15rem; font-weight:600; letter-spacing:.02em; }
  .sub { color:var(--muted); font-size:.85rem; margin-top:.35rem; }
  main { display:grid; grid-template-columns: 1fr 1fr; gap:1rem; padding:1rem; }
  @media (max-width:1100px) { main { grid-template-columns:1fr; } }
  .card { background:var(--panel); border-radius:8px; padding:1rem; }
  .card h2 { margin:0 0 .75rem; font-size:.95rem; }
  table { width:100%; border-collapse:collapse; font-size:.8rem; }
  th, td { text-align:left; padding:.35rem .4rem; border-bottom:1px solid #2a3440; }
  th { color:var(--muted); font-weight:500; }
  canvas { width:100%; height:220px; background:#121820; border-radius:6px; }
  .row { display:flex; gap:.75rem; flex-wrap:wrap; margin-bottom:.75rem; }
  .pill { background:#243040; padding:.25rem .55rem; border-radius:999px; font-size:.75rem; color:var(--muted); }
  .ops { grid-column:1 / -1; }
  .ops ol { margin:.5rem 0 0; padding-left:1.2rem; color:var(--muted); font-size:.85rem; }
  .ops li { margin:.4rem 0; }
  .ops strong { color:var(--fg); }
  select { background:#243040; color:var(--fg); border:1px solid #3a4654; border-radius:6px; padding:.3rem .5rem; }
</style>
</head>
<body>
<header>
  <h1>alexg7 smoke — Dukascopy cache vs MT5 H1</h1>
  <div class="sub" id="meta">loading…</div>
  <div class="row" style="margin-top:.75rem">
    <label class="pill">mode
      <select id="mode">
        <option value="off">same_bar_exit OFF (+xxx% path)</option>
        <option value="on">same_bar_exit ON (closer to live intrabar stop)</option>
      </select>
    </label>
  </div>
</header>
<main>
  <section class="card">
    <h2>Cache run</h2>
    <div id="stats-cache"></div>
    <canvas id="eq-cache"></canvas>
    <canvas id="px-cache" style="margin-top:.5rem"></canvas>
  </section>
  <section class="card">
    <h2>MT5 OHLC run</h2>
    <div id="stats-mt5"></div>
    <canvas id="eq-mt5"></canvas>
    <canvas id="px-mt5" style="margin-top:.5rem"></canvas>
  </section>
  <section class="card ops">
    <h2>Live order of operations</h2>
    <div id="ops"></div>
  </section>
</main>
<script>
let DATA = null;

function fmt(n, d=2){ return Number(n).toLocaleString(undefined,{maximumFractionDigits:d}); }
function pct(n){ return (100*Number(n)).toFixed(2)+'%'; }

function statsHtml(run){
  if(!run) return '<p>missing run</p>';
  return `<table>
    <tr><th>trades</th><td>${run.trades}</td><th>WR</th><td>${pct(run.win_rate)}</td></tr>
    <tr><th>window PnL</th><td>${fmt(run.window_pnl)}</td><th>PF</th><td>${fmt(run.profit_factor,3)}</td></tr>
    <tr><th>final eq (full hist)</th><td>${fmt(run.final_equity)}</td><th>max DD</th><td>${pct(run.max_drawdown_pct)}</td></tr>
  </table>`;
}

function drawEquity(canvas, run, color){
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,w,h);
  const eq = (run.equity_curve||[]).map(p=>p.equity);
  if(eq.length<2) return;
  const min = Math.min(...eq), max = Math.max(...eq);
  const pad = 8*devicePixelRatio;
  ctx.strokeStyle = color; ctx.lineWidth = 1.5*devicePixelRatio; ctx.beginPath();
  eq.forEach((v,i)=>{
    const x = pad + (i/(eq.length-1))*(w-2*pad);
    const y = h-pad - ((v-min)/Math.max(1e-9,max-min))*(h-2*pad);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);
  });
  ctx.stroke();
  ctx.fillStyle = '#8b9aab'; ctx.font = `${10*devicePixelRatio}px sans-serif`;
  ctx.fillText('equity', pad, 12*devicePixelRatio);
}

function drawPrice(canvas, ohlc, trades, color){
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,w,h);
  if(!ohlc || ohlc.length<2) return;
  const lows = ohlc.map(b=>b.l), highs = ohlc.map(b=>b.h);
  const min = Math.min(...lows), max = Math.max(...highs);
  const pad = 8*devicePixelRatio;
  const xAt = i => pad + (i/(ohlc.length-1))*(w-2*pad);
  const yAt = v => h-pad - ((v-min)/Math.max(1e-9,max-min))*(h-2*pad);
  ctx.strokeStyle = color; ctx.lineWidth = 1*devicePixelRatio; ctx.beginPath();
  ohlc.forEach((b,i)=>{ const y=yAt(b.c); i?ctx.lineTo(xAt(i),y):ctx.moveTo(xAt(i),y); });
  ctx.stroke();
  const tIndex = {};
  ohlc.forEach((b,i)=>{ tIndex[b.t.slice(0,13)] = i; });
  (trades||[]).forEach(tr=>{
    const key = (tr.entry_time||'').replace(' ','T').slice(0,13);
    let i = tIndex[key];
    if(i==null){
      // nearest
      const et = Date.parse(tr.entry_time);
      let best=0, bd=1e99;
      ohlc.forEach((b,j)=>{ const d=Math.abs(Date.parse(b.t)-et); if(d<bd){bd=d;best=j;} });
      i=best;
    }
    const x = xAt(i), y = yAt(tr.entry_price);
    ctx.fillStyle = tr.pnl>=0 ? '#3d9a6a' : '#c45c5c';
    ctx.beginPath();
    if((tr.side||'').toLowerCase().includes('long') || tr.side==='buy'){
      ctx.moveTo(x,y+6*devicePixelRatio); ctx.lineTo(x-5*devicePixelRatio,y-4*devicePixelRatio); ctx.lineTo(x+5*devicePixelRatio,y-4*devicePixelRatio);
    } else {
      ctx.moveTo(x,y-6*devicePixelRatio); ctx.lineTo(x-5*devicePixelRatio,y+4*devicePixelRatio); ctx.lineTo(x+5*devicePixelRatio,y+4*devicePixelRatio);
    }
    ctx.fill();
  });
  ctx.fillStyle = '#8b9aab'; ctx.font = `${10*devicePixelRatio}px sans-serif`;
  ctx.fillText('price + trade markers', pad, 12*devicePixelRatio);
}

function render(){
  const mode = document.getElementById('mode').value;
  const cache = DATA.runs['cache_samebar_'+mode];
  const mt5 = DATA.runs['mt5_samebar_'+mode];
  document.getElementById('stats-cache').innerHTML = statsHtml(cache);
  document.getElementById('stats-mt5').innerHTML = statsHtml(mt5);
  drawEquity(document.getElementById('eq-cache'), cache, '#5b9fd4');
  drawEquity(document.getElementById('eq-mt5'), mt5, '#d4a05b');
  drawPrice(document.getElementById('px-cache'), DATA.ohlc.cache, cache.trade_list, '#5b9fd4');
  drawPrice(document.getElementById('px-mt5'), DATA.ohlc.mt5, mt5.trade_list, '#d4a05b');
}

async function boot(){
  const res = await fetch('/data.json');
  DATA = await res.json();
  document.getElementById('meta').textContent =
    `${DATA.symbol} · ${DATA.interval} · window ${DATA.window_start.slice(0,10)} → ${DATA.window_end.slice(0,10)} · ${DATA.live_order_of_ops.strategy_tester_note}`;
  const ops = DATA.live_order_of_ops;
  document.getElementById('ops').innerHTML =
    `<p class="sub">${ops.title}</p><ol>` +
    ops.steps.map(s=>`<li><strong>${s.when}</strong><br/>live: ${s.live}<br/>backtest same_bar OFF: ${s.backtest_no_same_bar}<br/>backtest same_bar ON: ${s.backtest_same_bar}</li>`).join('') +
    `</ol>`;
  document.getElementById('mode').addEventListener('change', render);
  render();
}
boot();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    if not args.json.is_file():
        print(f"Missing {args.json} — run scripts/smoke_mt5_vs_cache.py first")
        return 1

    data_bytes = args.json.read_bytes()
    html_bytes = HTML.encode("utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = html_bytes
                ctype = "text/html; charset=utf-8"
            elif self.path.startswith("/data.json"):
                body = data_bytes
                ctype = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            print("[%s] %s" % (self.log_date_time_string(), fmt % a))

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Smoke compare viewer: http://127.0.0.1:{args.port}/")
    print(f"Data: {args.json}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
