#!/usr/bin/env python3
"""
dashboard.py — live web console for the tick engine. Zero dependencies.

A stdlib http.server thread embedded in the order-manager process serves a
single-page instrument console:

  * scope-style chart: price trace + fast/slow SMA + BUY/SELL markers
  * an LED strip mirroring the Arty's physical LD4-LD7 semantics
    (parse/divergence trouble, tick activity, link trouble, heartbeat)
  * position, realized/net P&L, fees, round-trip latency, verification
  * signal & event log
  * a guarded KILL SWITCH (arm, then confirm) wired to OrderManager.halt —
    killing is the fail-safe direction, so the dashboard may do it; it can
    do nothing else that touches money

Open http://localhost:<port> on the bench machine, or from a phone on the
same LAN via the machine's IP. The page is fully self-contained (system
fonts, no CDN) because a bench box may have no internet.

Endpoints:
  GET  /            the console page
  GET  /api/state   JSON snapshot for the 500 ms poll
  POST /api/kill    manual kill switch -> OrderManager.halt (latching)
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DashboardServer:
    def __init__(self, bridge, om, port: int = 8000, points: int = 240,
                 scorecards: dict | None = None):
        self.bridge = bridge
        self.om = om
        self.scorecards = scorecards
        self.port = port
        self._lock = threading.Lock()
        self._points = points
        self._series = {}                       # symbol -> deque of 7-tuples
        self.chart_symbol = bridge.symbol
        self._signals = deque(maxlen=20)          # global, chronological
                                                  # across ALL symbols — feeds
                                                  # the SIGNALS table
        self._signals_by_symbol = {}              # symbol -> deque(maxlen=20)
                                                  # — feeds each CHART's own
                                                  # markers. Separate from the
                                                  # global deque above on
                                                  # purpose: with multiple
                                                  # symbols configured, a busy
                                                  # one's signals were crowding
                                                  # a quieter symbol's older
                                                  # signals out of one shared
                                                  # 20-slot buffer, making that
                                                  # symbol's chart markers
                                                  # vanish even though the
                                                  # underlying price point was
                                                  # still visible on screen —
                                                  # a real reported bug.
        self._events = deque(maxlen=30)
        self._last_echo_t = 0.0
        self._last_trouble_t = 0.0
        self.t0 = time.time()

        bridge.on_echo = self._on_echo          # hook added in bridge.py

    # ---- feed hooks (called from bridge threads) ------------------------------
    def _on_echo(self, fr: dict):
        sym = fr["symbol"].strip()
        m = self.bridge.models["sma"].get(sym)
        e = self.bridge.models["ema"].get(sym)
        if m is None:
            return
        with self._lock:
            d = self._series.setdefault(sym, deque(maxlen=self._points))
            d.append((fr["price_e4"], m.sma_fast, m.sma_slow, m.warmed_up,
                      e.ema_fast, e.ema_slow, e.warmed_up))
            self._last_echo_t = time.time()

    def on_signal(self, fr: dict, outcome: str = ""):
        with self._lock:
            rec = {"t": time.strftime("%H:%M:%S"),
                  "strategy": fr.get("strategy", "sma"),
                  "symbol": fr["symbol"].strip(),
                  "side": fr["side"],
                  "price_e4": fr["price_e4"],
                  # v3.19: fabric VWAP signals (0x85) carry {vwap,
                  # eval_skips} instead of {sma_fast, sma_slow}; map the
                  # vwap into the "fast" column so the signals table
                  # shows the cross-check value, and leave "slow" None
                  # (rendered as a dash, not $NaN — see the JS side)
                  "sma_fast": fr.get("sma_fast", fr.get("vwap")),
                  "sma_slow": fr.get("sma_slow"),
                  "outcome": outcome}
            self._signals.appendleft(rec)
            sym = rec["symbol"]
            self._signals_by_symbol.setdefault(
                sym, deque(maxlen=20)).appendleft(rec)

    def on_event(self, text: str, bad: bool = False):
        with self._lock:
            self._events.appendleft({"t": time.strftime("%H:%M:%S"),
                                     "text": text, "bad": bad})
            if bad:
                self._last_trouble_t = time.time()

    # ---- snapshot for the poll ---------------------------------------------------
    def snapshot(self, sym: str | None = None) -> dict:
        br, om = self.bridge, self.om
        sym = (sym or self.chart_symbol).strip().upper()
        if sym not in br.models["sma"]:
            sym = br.symbol
        self.chart_symbol = sym
        m = br.models["sma"][sym]
        now = time.time()
        rtt = sorted(br.rtt_us[-200:]) if br.rtt_us else []
        with self._lock:
            series = list(self._series.get(sym, ()))
            signals = list(self._signals)
            chart_signals = list(self._signals_by_symbol.get(sym, ()))
            events = list(self._events)
            echo_age = now - self._last_echo_t if self._last_echo_t else 1e9
            trouble_age = (now - self._last_trouble_t
                           if self._last_trouble_t else 1e9)
        return {
            "symbol": sym,
            "symbols": list(br.symbols),
            "uptime_s": int(now - self.t0),
            "series": series,
            "signals": signals,       # global, all symbols — feeds the table
            "chart_signals": chart_signals,   # THIS symbol only — chart markers
            "events": events,
            "sent": br.sent, "echoes": br.echoes,
            "resyncs": br.parser.resync_count,
            "fpga_signals": br.fpga_signals,
            "verified": sum(v.verified for v in br.verifiers.values()),
            "divergences": sum(v.divergences
                               for v in br.verifiers.values()),
            "strategies": [
                {"name": c.name, "live": c.live, "signals": c.signals,
                 "trips": c.trips, "wins": c.wins, "blocked": c.blocked,
                 "net": round(c.net_usd, 2),
                 "open": sum(1 for v in c.positions.values() if v) > 0}
                for c in (self.scorecards or {}).values()],
            "warmed_up": m.warmed_up,
            "fill": m.fill, "slow_n": m.slow_n,
            "rtt": {"min": rtt[0], "med": rtt[len(rtt)//2],
                    "max": rtt[-1]} if rtt else None,
            "positions": {k: v for k, v in om.positions.items() if v},
            "orders": om.orders, "blocked": om.blocked,
            "pnl_gross": om.costs.realized_pnl_usd,
            "fees": om.costs.total_fees,
            "pnl_net": om.costs.net_pnl_usd,
            "halted": om.halted, "halt_reason": om.halt_reason,
            "led": {"trouble": trouble_age < 3.0 or om.halted,
                    "activity": echo_age < 1.0,
                    "link": echo_age < 10.0},
        }

    # ---- server -------------------------------------------------------------------
    def start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):           # keep the terminal clean
                pass

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif self.path.startswith("/api/state"):
                    q = self.path.split("sym=")
                    sym = q[1] if len(q) > 1 else None
                    self._send(200, json.dumps(dash.snapshot(sym)).encode(),
                               "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path == "/api/symbols":
                    # GUI symbol editor -> FPGA slot registers over UART.
                    # configure_symbols blocks until all 8 slots ACK (0x90
                    # echoes), so the HTTP response reports ground truth:
                    # what the fabric actually latched, not what we asked.
                    n = int(self.headers.get("Content-Length", 0))
                    try:
                        req = json.loads(self.rfile.read(n) or "{}")
                        syms = [t for t in req.get("symbols", [])
                                if t and t.strip()]
                        from tick_protocol import sym_wire
                        for t in syms:
                            sym_wire(t)            # validate before sending
                        ok = bool(syms) and dash.bridge.configure_symbols(syms)
                    except (ValueError, json.JSONDecodeError) as e:
                        self._send(400, json.dumps(
                            {"ok": False, "error": str(e)}).encode(),
                            "application/json")
                        return
                    if ok:
                        dash.on_event("slots reconfigured: "
                                      + ",".join(dash.bridge.symbols))
                        with dash._lock:
                            dash._series.clear()   # new models = new warmup
                    else:
                        dash.on_event("slot reconfiguration FAILED", True)
                    self._send(200, json.dumps(
                        {"ok": ok,
                         "symbols": list(dash.bridge.symbols)}).encode(),
                        "application/json")
                elif self.path == "/api/kill":
                    dash.om.halt("manual kill from dashboard")
                    dash.on_event("KILL SWITCH — manual, from dashboard",
                                  bad=True)
                    self._send(200, b'{"halted": true}', "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        print(f"[dash] console at http://localhost:{self.port}")
        return self

    def stop(self):
        self.httpd.shutdown()


# -----------------------------------------------------------------------------
# The console page. Self-contained: system fonts, no CDN, no build step.
# -----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TICK ENGINE — console</title>
<style>
:root{
  --bg:#0E1116; --panel:#151B23; --line:#232C38; --ink:#C7D3E2;
  --dim:#66788E; --amber:#E5B454; --green:#4CC38A; --cyan:#5CC8FF;
  --violet:#B18CFF; --red:#E5484D;
}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);
  font:14px/1.5 ui-monospace,'SF Mono','Cascadia Mono',Consolas,Menlo,monospace;
  padding:14px;max-width:1180px;margin:0 auto}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:14px}
h1{font-size:15px;letter-spacing:.28em;font-weight:700;color:var(--amber)}
h1 span{color:var(--dim);font-weight:400}
.leds{display:flex;gap:14px;margin-left:auto;align-items:center}
.led{display:flex;flex-direction:column;align-items:center;gap:3px}
.led i{width:12px;height:12px;border-radius:50%;background:#26303D;
  border:1px solid var(--line);transition:background .15s,box-shadow .15s}
.led small{font-size:9px;letter-spacing:.12em;color:var(--dim)}
.led.on.g i{background:var(--green);box-shadow:0 0 8px var(--green)}
.led.on.a i{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.led.on.r i{background:var(--red);box-shadow:0 0 9px var(--red)}
@keyframes hb{0%,60%{opacity:.25}70%,90%{opacity:1}100%{opacity:.25}}
.led.hb i{background:var(--green);animation:hb 1.4s infinite}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.grid{grid-template-columns:1fr}
  .grid2{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);
  border-radius:6px;padding:12px}
.panel h2{font-size:10px;letter-spacing:.22em;color:var(--dim);
  font-weight:600;margin-bottom:8px}
canvas{width:100%;height:300px;display:block}
.legend{display:flex;gap:16px;font-size:11px;color:var(--dim);margin-top:6px}
.legend b{font-weight:400}
.k-price{color:var(--amber)}.k-f{color:var(--cyan)}.k-s{color:var(--violet)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
  gap:10px;margin:14px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  padding:9px 12px}
.stat small{display:block;font-size:9px;letter-spacing:.18em;color:var(--dim)}
.stat b{font-size:17px;font-weight:600}
.pos b{color:var(--amber)} .good b{color:var(--green)} .bad b{color:var(--red)}
table{width:100%;border-collapse:collapse;font-size:12px}
td.outcome{white-space:nowrap}
td,th{padding:3px 6px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:10px;letter-spacing:.14em}
td:first-child,th:first-child{text-align:left}
.buy{color:var(--green)} .sell{color:var(--red)}
.log{font-size:11px;max-height:180px;overflow-y:auto}
.log div{padding:2px 0;border-bottom:1px solid var(--line);color:var(--dim)}
.log .bad{color:var(--red)}
.slot{background:var(--bg);border:1px solid var(--line);color:var(--amber);
  font:inherit;width:76px;padding:5px 7px;border-radius:4px;
  text-transform:uppercase}
#apply{background:none;border:1px solid var(--green);color:var(--green);
  font:inherit;letter-spacing:.12em;padding:5px 14px;border-radius:4px;
  cursor:pointer}
#cfgmsg{color:var(--dim);font-size:11px;align-self:center}
#csym,#csym2{background:var(--bg);border:1px solid var(--line);color:var(--ink);
  font:inherit;padding:2px 6px;border-radius:4px;margin-left:8px}
.killwrap{margin-top:14px;display:flex;align-items:center;gap:12px}
#kill{background:none;border:2px solid var(--red);color:var(--red);
  font:inherit;font-weight:700;letter-spacing:.2em;padding:10px 22px;
  border-radius:4px;cursor:pointer}
#kill.armed{background:var(--red);color:#fff}
#kill:disabled{border-color:var(--line);color:var(--dim);cursor:default}
#banner{display:none;background:var(--red);color:#fff;font-weight:700;
  letter-spacing:.15em;text-align:center;padding:8px;border-radius:4px;
  margin-bottom:12px}
#warm{color:var(--dim);font-size:11px}
@media(prefers-reduced-motion:reduce){.led.hb i{animation:none;opacity:1}}
</style></head><body>
<header>
  <h1>TICK ENGINE <span id="sym">····</span></h1>
  <span id="warm"></span>
  <div class="leds">
    <div class="led r" id="led0"><i></i><small>TRBL</small></div>
    <div class="led a" id="led1"><i></i><small>TICK</small></div>
    <div class="led g" id="led2"><i></i><small>LINK</small></div>
    <div class="led hb"><i></i><small>HB</small></div>
  </div>
</header>
<div id="banner"></div>
<div class="panel" style="margin-bottom:14px"><h2>SYMBOL SLOTS — WRITTEN TO FPGA OVER UART (8 MAX)</h2>
  <div id="slots" style="display:flex;gap:8px;flex-wrap:wrap">
    <input class="slot" maxlength="6"><input class="slot" maxlength="6">
    <input class="slot" maxlength="6"><input class="slot" maxlength="6">
    <input class="slot" maxlength="6"><input class="slot" maxlength="6">
    <input class="slot" maxlength="6"><input class="slot" maxlength="6">
    <button id="apply">APPLY</button><span id="cfgmsg"></span>
  </div>
</div>
<div class="stats" id="stats"></div>
<div class="grid2">
  <div class="panel"><h2>PRICE / SMA / EMA — LAST 240 TICKS
      <select id="csym"></select></h2>
    <canvas id="chart"></canvas>
    <div class="legend"><b class="k-price">— price</b>
      <b class="k-f">— sma fast</b><b class="k-s">— sma slow</b>
      <b class="k-f">┄ ema fast</b><b class="k-s">┄ ema slow</b>
      <b class="buy">▲ buy</b><b class="sell">▼ sell</b></div>
  </div>
  <div class="panel"><h2>PRICE / SMA / EMA — LAST 240 TICKS
      <select id="csym2"></select></h2>
    <canvas id="chart2"></canvas>
    <div class="legend"><b class="k-price">— price</b>
      <b class="k-f">— sma fast</b><b class="k-s">— sma slow</b>
      <b class="k-f">┄ ema fast</b><b class="k-s">┄ ema slow</b>
      <b class="buy">▲ buy</b><b class="sell">▼ sell</b></div>
  </div>
</div>
<div class="panel" style="margin-top:14px"><div id="cmp"></div></div>
<div class="panel" style="margin-top:14px"><h2>SIGNALS</h2>
  <table><thead><tr><th>t</th><th>sym</th><th>strat</th><th>side</th>
  <th>price</th><th>fast</th><th>slow</th><th>outcome</th></tr></thead>
  <tbody id="sigs"></tbody></table>
</div>
<div class="panel" style="margin-top:14px"><h2>EVENTS</h2>
  <div class="log" id="log"><div>console up — waiting for ticks</div></div>
</div>
</div>
<div class="killwrap">
  <button id="kill">KILL SWITCH</button>
  <span id="killhint" style="color:var(--dim);font-size:11px">
    latching — halts all orders; re-arm requires deleting om.kill</span>
</div>
<script>
"use strict";
const $=id=>document.getElementById(id);
const usd=e4=>'$'+(e4/1e4).toFixed(2);
const money=v=>(v<0?'-':'')+'$'+Math.abs(v).toFixed(2);

function drawChart(canvasId,series,signals){
  const c=$(canvasId),dpr=window.devicePixelRatio||1;
  const W=c.clientWidth,H=c.clientHeight;
  c.width=W*dpr;c.height=H*dpr;
  const g=c.getContext('2d');g.scale(dpr,dpr);g.clearRect(0,0,W,H);
  if(series.length<2)return;
  let lo=1/0,hi=-1/0;
  for(const pt of series){lo=Math.min(lo,pt[0]);hi=Math.max(hi,pt[0]);
    if(pt[3]){lo=Math.min(lo,pt[1],pt[2]);hi=Math.max(hi,pt[1],pt[2]);}
    if(pt[6]){lo=Math.min(lo,pt[4],pt[5]);hi=Math.max(hi,pt[4],pt[5]);}}
  const pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;
  const X=i=>i/(series.length-1)*(W-60),Y=v=>H-8-(v-lo)/(hi-lo)*(H-16);
  g.strokeStyle='#232C38';g.fillStyle='#66788E';
  g.font='10px ui-monospace,monospace';g.textAlign='left';
  for(let k=0;k<4;k++){const v=lo+(hi-lo)*k/3,y=Y(v);
    g.beginPath();g.moveTo(0,y);g.lineTo(W-60,y);g.stroke();
    // usd() (defined above), not a bare toFixed -- matches every other
    // price on the page ($436.72, not 436.72). Gutter is 60px (was 46)
    // specifically so a 4-digit price with cents ("$1234.56", ~48px at
    // this font) has room to fully render instead of clipping against
    // the canvas edge -- confirmed by measuring actual glyph widths,
    // not guessed: the old 46px gutter gave "$436.72" alone only 42px,
    // already tight, and cut off anything priced in four digits.
    g.fillText(usd(v),W-56,y+3);}
  const line=(idx,color,wid,gate,dash)=>{g.strokeStyle=color;g.lineWidth=wid;
    g.setLineDash(dash||[]);g.beginPath();let started=false;
    series.forEach((pt,i)=>{if(gate>=0&&!pt[gate])return;
      const x=X(i),y=Y(pt[idx]);
      started?g.lineTo(x,y):(g.moveTo(x,y),started=true);});
    g.stroke();g.setLineDash([]);};
  line(0,'#E5B454',1.6,-1);line(1,'#5CC8FF',1.1,3);line(2,'#B18CFF',1.1,3);
  line(4,'#5CC8FF',1.0,6,[4,4]);line(5,'#B18CFF',1.0,6,[4,4]);
  // signal markers: match by price on recent points, newest first
  g.textAlign='center';
  for(const s of signals){
    for(let i=series.length-1;i>=0;i--){
      if(series[i][0]===s.price_e4){
        const x=X(i),y=Y(s.price_e4),up=s.side===1;
        g.fillStyle=up?'#4CC38A':'#E5484D';g.beginPath();
        if(up){g.moveTo(x,y+16);g.lineTo(x-5,y+24);g.lineTo(x+5,y+24);}
        else {g.moveTo(x,y-16);g.lineTo(x-5,y-24);g.lineTo(x+5,y-24);}
        g.closePath();g.fill();break;}}}
}

function stat(label,val,cls){return '<div class="stat '+(cls||'')+'">'+
  '<small>'+label+'</small><b>'+val+'</b></div>';}

let slotsSeeded=false,chartSym=null,chartSym2=null;
document.getElementById('csym').onchange=e=>{chartSym=e.target.value};
document.getElementById('csym2').onchange=e=>{chartSym2=e.target.value};
document.getElementById('apply').onclick=async()=>{
  const syms=[...document.querySelectorAll('.slot')]
    .map(i=>i.value.trim().toUpperCase()).filter(v=>v);
  $('cfgmsg').textContent='writing slots + waiting for FPGA acks…';
  try{
    const r=await(await fetch('/api/symbols',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms})})).json();
    $('cfgmsg').textContent=r.ok?'acked: '+r.symbols.join(', ')
      :(r.error||'FAILED — see events');
    if(r.ok){chartSym=r.symbols[0];chartSym2=r.symbols[1]||r.symbols[0];
             slotsSeeded=false;}
  }catch(e){$('cfgmsg').textContent='error: '+e;}
};
let killArmed=false,killTimer=null;
$('kill').onclick=async()=>{
  if(!killArmed){killArmed=true;$('kill').classList.add('armed');
    $('kill').textContent='CONFIRM KILL';
    killTimer=setTimeout(()=>{killArmed=false;
      $('kill').classList.remove('armed');
      $('kill').textContent='KILL SWITCH';},3000);return;}
  clearTimeout(killTimer);
  await fetch('/api/kill',{method:'POST'});
};

async function poll(){
  try{
    const [s,s2]=await Promise.all([
      fetch('/api/state'+(chartSym?'?sym='+chartSym:'')).then(r=>r.json()),
      fetch('/api/state'+(chartSym2?'?sym='+chartSym2:
                          (chartSym?'?sym='+chartSym:''))).then(r=>r.json())
    ]);
    if(!slotsSeeded&&s.symbols){
      const boxes=document.querySelectorAll('.slot');
      s.symbols.forEach((t,i)=>{if(boxes[i])boxes[i].value=t;});
      $('csym').innerHTML=s.symbols.map(t=>'<option'+
        (t===s.symbol?' selected':'')+'>'+t+'</option>').join('');
      $('csym2').innerHTML=s.symbols.map(t=>'<option'+
        (t===(s.symbols[1]||s.symbol)?' selected':'')+'>'+t+'</option>')
        .join('');
      // default the two charts to DIFFERENT symbols when more than one
      // is configured, so you immediately see two different ticks
      // side by side rather than the same one duplicated
      chartSym=s.symbol;
      chartSym2=s.symbols[1]||s.symbol;
      slotsSeeded=true;}
    $('sym').textContent=s.symbol+' · up '+
      Math.floor(s.uptime_s/60)+'m'+(s.uptime_s%60)+'s';
    $('warm').textContent=s.warmed_up?'SMA windows full':
      'warming up '+s.fill+'/'+s.slow_n;
    $('led0').classList.toggle('on',s.led.trouble);
    $('led1').classList.toggle('on',s.led.activity);
    $('led2').classList.toggle('on',s.led.link);
    if(s.halted){$('banner').style.display='block';
      $('banner').textContent='HALTED — '+s.halt_reason;
      $('kill').disabled=true;$('kill').textContent='TRIPPED';
      $('killhint').textContent='delete om.kill and restart to re-arm';}
    $('stats').innerHTML=
      stat('POSITIONS',Object.keys(s.positions).length?
           Object.entries(s.positions).map(([k,v])=>k+':'+v).join(' '):
           'flat','pos')+
      stat('NET P&L',money(s.pnl_net),s.pnl_net>=0?'good':'bad')+
      stat('FEES',money(s.fees))+
      stat('FILLS / BLOCKED',s.orders+' / '+s.blocked)+
      stat('VERIFIED',s.verified+' / '+s.fpga_signals,
           s.divergences?'bad':'good')+
      stat('RTT µs',s.rtt?s.rtt.med+' ('+s.rtt.min+'–'+s.rtt.max+')':'—')+
      stat('ECHO / SENT',s.echoes+' / '+s.sent,
           s.echoes===s.sent?'':'bad');
    drawChart('chart',s.series,s.chart_signals);
    drawChart('chart2',s2.series,s2.chart_signals);
    if(s.strategies&&s.strategies.length)
      $('cmp').innerHTML='<table><thead><tr><th>strategy</th><th>signals'+
        '</th><th>trips</th><th>wins</th><th>blocked/gated</th>'+
        '<th>net $</th></tr></thead><tbody>'+
        s.strategies.map(c=>'<tr><td>'+c.name+(c.live?' [LIVE]':'')+
        (c.open?' *':'')+'</td><td>'+c.signals+'</td><td>'+c.trips+
        '</td><td>'+(c.wins===null?'—':c.wins)+'</td><td>'+c.blocked+
        '</td><td class="'+(c.net>=0?'buy':'sell')+'">'+c.net.toFixed(2)+
        '</td></tr>').join('')+'</tbody></table>';
    $('sigs').innerHTML=s.signals.map(x=>'<tr><td>'+x.t+'</td>'+
      '<td>'+x.symbol+'</td>'+
      '<td>'+x.strategy.toUpperCase()+'</td>'+
      '<td class="'+(x.side===1?'buy">BUY':'sell">SELL')+'</td>'+
      '<td>'+usd(x.price_e4)+'</td>'+
      '<td>'+(x.sma_fast==null?'—':usd(x.sma_fast))+'</td>'+
      '<td>'+(x.sma_slow==null?'—':usd(x.sma_slow))+'</td>'+
      '<td class="outcome '+(x.outcome.startsWith('FILLED')?'buy':
                     x.outcome.startsWith('rejected')?'sell':'')+
      '" style="'+(x.outcome.startsWith('blocked')||
                   x.outcome.startsWith('gated')?'color:var(--amber)':
                   x.outcome.startsWith('ignored')?'color:var(--dim)':'')+
      '">'+(x.outcome||'—')+'</td></tr>').join('');
    if(s.events.length)$('log').innerHTML=s.events.map(e=>
      '<div class="'+(e.bad?'bad':'')+'">'+e.t+'  '+e.text+'</div>').join('');
  }catch(e){$('led2').classList.remove('on');}
  setTimeout(poll,500);
}
poll();
</script></body></html>
"""
