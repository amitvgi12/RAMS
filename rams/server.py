"""
Local web dashboard for the RAMS deterioration engine.

Pure standard-library HTTP server (no Flask/FastAPI). Serves a single
self-contained SPA and a small JSON API backed by rams.api.

Run:
    python -m rams.server            # http://127.0.0.1:8000
    python -m rams.server --port 8080

Security:
    * Binds to 127.0.0.1 only (loopback) -- never exposed to the network.
    * Request bodies are size-capped (MAX_BODY) to bound memory.
    * JSON is parsed defensively; ValueError -> HTTP 400 with a safe message,
      unexpected errors -> HTTP 500 with a generic message (no stack leak).
    * Response headers set nosniff + a restrictive CSP. The page is fully
      self-contained (inline CSS/JS, no external origins).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict

from . import api

MAX_BODY = 4 * 1024 * 1024  # 4 MB request cap

# POST routes -> pure handler functions in rams.api.
_ROUTES: Dict[str, Callable[[dict], dict]] = {
    "/api/forecast": api.forecast_single,
    "/api/network": api.network_and_budget,
}


class RAMSHandler(BaseHTTPRequestHandler):
    server_version = "RAMS/1.0"

    # --- helpers -----------------------------------------------------------

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:",
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large.")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("request body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object.")
        return data

    def log_message(self, fmt, *args):  # quieter, single-line logging
        print(f"[rams] {self.address_string()} {fmt % args}")

    # --- routing -----------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/sample":
            self._send_json(200, api.default_network())
        elif self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        handler = _ROUTES.get(self.path)
        if handler is None:
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = self._read_json()
            self._send_json(200, handler(payload))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001 - never leak internals to the client
            self._send_json(500, {"error": "internal error"})


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    httpd = ThreadingHTTPServer((host, port), RAMSHandler)
    print(f"RAMS dashboard running at http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rams-server", description="RAMS web dashboard")
    p.add_argument("--host", default="127.0.0.1", help="bind host (loopback only by default)")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    serve(args.host, args.port)
    return 0


# --- Embedded single-page app (self-contained: no external origins) --------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAMS &mdash; Pavement Deterioration & Budget</title>
<style>
  :root { --green:#1a9850; --amber:#f0a000; --red:#d73027; --ink:#1f2a36;
          --line:#1f3b57; --bg:#eef2f6; --card:#fff; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0;
         color:var(--ink); background:var(--bg); }
  header { background:var(--line); color:#fff; padding:16px 24px; }
  header h1 { margin:0; font-size:18px; }
  header p { margin:4px 0 0; font-size:12px; opacity:.8; }
  .tabs { display:flex; gap:4px; padding:0 24px; background:var(--line); }
  .tab { padding:10px 16px; color:#cdd8e3; cursor:pointer; border:none;
         background:none; font-size:14px; border-bottom:3px solid transparent; }
  .tab.active { color:#fff; border-bottom-color:var(--amber); font-weight:600; }
  main { max-width:1000px; margin:0 auto; padding:24px; }
  .panel { display:none; } .panel.active { display:block; }
  .card { background:var(--card); border-radius:8px; padding:18px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:12px; }
  label { font-size:12px; color:#556; display:block; margin-bottom:4px; }
  input,select { width:100%; padding:7px 8px; border:1px solid #cdd5dd; border-radius:6px;
                 font-size:14px; }
  button.go { background:var(--line); color:#fff; border:none; padding:10px 18px;
              border-radius:6px; font-size:14px; cursor:pointer; margin-top:12px; }
  button.go:hover { background:#16334d; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th,td { padding:6px 9px; border-bottom:1px solid #eee; text-align:right; }
  th:first-child,td:first-child { text-align:left; }
  th { background:#f0f3f7; color:#445; }
  .banner { padding:12px 16px; border-radius:8px; color:#fff; font-weight:600; }
  .legend span { font-size:12px; margin-right:14px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .kpi { background:#f6f9fc; border-radius:8px; padding:12px; }
  .kpi b { display:block; font-size:22px; }
  .kpi span { font-size:11px; color:#667; text-transform:uppercase; letter-spacing:.04em; }
  .muted { color:#778; font-size:13px; }
  .err { color:var(--red); font-weight:600; }
  .dot { font-size:11px; }
</style></head>
<body>
<header>
  <h1>RAMS &mdash; Indian Pavement Deterioration Engine</h1>
  <p>Deterministic IRC:82 forecasting &middot; MoRTH treatment reset &middot; multi-year budget optimisation</p>
</header>
<div class="tabs">
  <button class="tab active" data-tab="seg">Segment Forecast</button>
  <button class="tab" data-tab="net">Network &amp; Budget</button>
</div>
<main>

  <section class="panel active" id="seg">
    <div class="card">
      <div class="grid">
        <div><label>Initial IRI (mm/m)</label><input id="iri" type="number" step="0.1" value="1.5"></div>
        <div><label>Initial Rut (mm)</label><input id="rut" type="number" step="0.1" value="2.0"></div>
        <div><label>Initial Crack (%)</label><input id="crack" type="number" step="0.1" value="0.0"></div>
        <div><label>Annual MSA</label><input id="msa" type="number" step="0.1" value="4.5"></div>
        <div><label>Traffic growth</label><input id="growth" type="number" step="0.01" value="0.06"></div>
        <div><label>Monsoon zone</label><select id="zone">
          <option>HIGH</option><option>MEDIUM</option><option>LOW</option></select></div>
        <div><label>Horizon (yrs)</label><input id="years" type="number" value="10"></div>
      </div>
      <button class="go" onclick="runForecast()">Forecast</button>
      <span id="segErr" class="err"></span>
    </div>
    <div id="segOut"></div>
  </section>

  <section class="panel" id="net">
    <div class="card">
      <div class="grid">
        <div><label>Annual budget (&#8377; lakh)</label><input id="budget" type="number" value="600"></div>
        <div><label>Unit cost (&#8377; lakh/km)</label><input id="unit" type="number" value="30"></div>
        <div><label>Horizon (yrs)</label><input id="nyears" type="number" value="10"></div>
      </div>
      <button class="go" onclick="runNetwork()">Optimise budget</button>
      <span id="netErr" class="err"></span>
      <p class="muted">Demo network (8 segments) loaded from the server. Cost units are &#8377; lakh.</p>
    </div>
    <div id="netOut"></div>
  </section>

</main>
<script>
const BANDC = {ROUTINE:'#1a9850', PREVENTIVE:'#f0a000', STRUCTURAL:'#d73027'};
function $(id){return document.getElementById(id);}
function flagFor(pci,b){ if(pci>=b.preventive_upper)return'ROUTINE';
  if(pci>=b.structural_lower)return'PREVENTIVE'; return'STRUCTURAL'; }

document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  t.classList.add('active'); $(t.dataset.tab).classList.add('active');
});

async function postJSON(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||'request failed'); return j;
}

// ---- PCI chart (untreated vs treated) -------------------------------------
function pciChart(untreated, treated, b){
  const W=720,H=300,pl=44,pr=16,pt=14,pb=28, pw=W-pl-pr, ph=H-pt-pb;
  const years=untreated.map(r=>r.Year), ymin=1, ymax=4;
  const xmin=Math.min(...years), xmax=Math.max(...years), xs=Math.max(1,xmax-xmin);
  const px=y=>pl+(y-xmin)/xs*pw, py=v=>pt+(ymax-v)/(ymax-ymin)*ph;
  const bandRect=(lo,hi,c)=>`<rect x="${pl}" y="${py(hi)}" width="${pw}" height="${py(lo)-py(hi)}" fill="${c}" opacity="0.12"/>`;
  const bands=bandRect(ymin,b.structural_lower,BANDC.STRUCTURAL)
    +bandRect(b.structural_lower,b.preventive_upper,BANDC.PREVENTIVE)
    +bandRect(b.preventive_upper,ymax,BANDC.ROUTINE);
  const line=(rows,col,dash)=>{ const pts=rows.map(r=>`${px(r.Year)},${py(r.IRC82_PCI)}`).join(' ');
    return `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" ${dash?'stroke-dasharray="5 4"':''}/>`+
      rows.map(r=>`<circle cx="${px(r.Year)}" cy="${py(r.IRC82_PCI)}" r="3" fill="${col}"/>`).join(''); };
  const ticks=[ymin,b.structural_lower,b.preventive_upper,ymax];
  const yl=ticks.map(t=>`<line x1="${pl}" y1="${py(t)}" x2="${W-pr}" y2="${py(t)}" stroke="#ccc" stroke-width="0.5"/>`+
    `<text x="${pl-6}" y="${py(t)+4}" font-size="10" text-anchor="end" fill="#666">${t.toFixed(2)}</text>`).join('');
  const xl=years.map(y=>`<text x="${px(y)}" y="${H-pb+16}" font-size="10" text-anchor="middle" fill="#666">${y}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">${bands}${yl}${xl}`+
    line(untreated,'#d73027',true)+line(treated,'#1a9850',false)+`</svg>`+
    `<div class="legend"><span style="color:#d73027">&#9644; Untreated (do nothing)</span>`+
    `<span style="color:#1a9850">&#9644; Managed (MoRTH treatments applied)</span></div>`;
}

function timelineTable(rows,b){
  let h='<table><thead><tr><th>Year</th><th>Cum MSA</th><th>IRI</th><th>Rut</th><th>Crack%</th><th>PCI</th><th>Flag</th></tr></thead><tbody>';
  for(const r of rows){ const f=flagFor(r.IRC82_PCI,b); const c=BANDC[f];
    h+=`<tr><td>${r.Year}</td><td>${r.Cumulative_MSA}</td><td>${r.IRI}</td><td>${r.Rutting_mm}</td>`+
       `<td>${r.Cracking_Pct}</td><td style="color:${c};font-weight:600">${r.IRC82_PCI}</td>`+
       `<td><span class="dot" style="color:${c}">&#9679;</span> ${f}</td></tr>`; }
  return h+'</tbody></table>';
}

async function runForecast(){
  $('segErr').textContent=''; $('segOut').innerHTML='<p class="muted">computing&hellip;</p>';
  try{
    const body={iri:+$('iri').value,rut:+$('rut').value,crack:+$('crack').value,
      msa:+$('msa').value,growth:+$('growth').value,zone:$('zone').value,years:+$('years').value};
    const d=await postJSON('/api/forecast',body); const b=d.bands;
    const p=d.plan; const bc = p.window_expired_year?BANDC.STRUCTURAL:(p.preventive_window_year?BANDC.PREVENTIVE:BANDC.ROUTINE);
    let iv='';
    if(d.interventions.length){ iv='<div class="card"><b>Treatments applied (managed scenario)</b><table><thead><tr><th>Year</th><th>Treatment</th><th>PCI before</th><th>PCI after</th><th>Cost (&#8377;L)</th></tr></thead><tbody>'+
      d.interventions.map(i=>`<tr><td>${i.year}</td><td>${i.treatment}</td><td>${i.pci_before}</td><td>${i.pci_after}</td><td>${i.cost}</td></tr>`).join('')+
      `</tbody></table><p class="muted">Managed lifecycle cost: &#8377;${d.managed_total_cost} lakh</p></div>`; }
    $('segOut').innerHTML=
      `<div class="card"><div class="banner" style="background:${bc}">${p.rationale}</div></div>`+
      `<div class="card"><div class="legend"><span style="color:${BANDC.ROUTINE}">&#9679; Routine</span>`+
        `<span style="color:${BANDC.PREVENTIVE}">&#9679; Preventive window</span>`+
        `<span style="color:${BANDC.STRUCTURAL}">&#9679; Structural</span></div>`+
        pciChart(d.untreated,d.treated,b)+`</div>`+
      iv+
      `<div class="card"><b>Untreated forecast</b>${timelineTable(d.untreated,b)}</div>`;
  }catch(e){ $('segErr').textContent=e.message; $('segOut').innerHTML=''; }
}

async function runNetwork(){
  $('netErr').textContent=''; $('netOut').innerHTML='<p class="muted">optimising&hellip;</p>';
  try{
    const sample=await (await fetch('/api/sample')).json();
    const body={segments:sample.segments, annual_budget:+$('budget').value,
      base_unit_cost:+$('unit').value, years:+$('nyears').value};
    const d=await postJSON('/api/network',body); const bud=d.budget;
    // KPI cards
    let kp=`<div class="card"><div class="kpis">`+
      `<div class="kpi"><b>&#8377;${bud.total_spend}</b><span>Total spend (L)</span></div>`+
      `<div class="kpi"><b style="color:#1a9850">&#8377;${bud.net_savings}</b><span>Avoided structural cost (funded)</span></div>`+
      `<div class="kpi"><b>${bud.scheduled.length}</b><span>Segments funded</span></div>`+
      `<div class="kpi"><b style="color:${bud.unfunded.length?'#d73027':'#1a9850'}">${bud.unfunded.length}</b><span>Unfunded (&rarr; structural)</span></div>`+
      `</div><p class="muted">${bud.rationale}</p></div>`;
    // spend-by-year bars
    const yrs=Object.keys(bud.spend_by_year).map(Number).sort((a,b)=>a-b);
    const maxS=Math.max(bud.annual_budget,...yrs.map(y=>bud.spend_by_year[y]),1);
    let bars='<div class="card"><b>Spend by year (vs annual budget &#8377;'+bud.annual_budget+'L)</b><svg viewBox="0 0 720 200" width="100%" style="max-width:720px">';
    const bw=680/yrs.length;
    yrs.forEach((y,i)=>{ const h=bud.spend_by_year[y]/maxS*150; const x=30+i*bw;
      bars+=`<rect x="${x}" y="${170-h}" width="${bw*0.6}" height="${h}" fill="#1f3b57"/>`+
        `<text x="${x+bw*0.3}" y="186" font-size="10" text-anchor="middle" fill="#666">Y${y}</text>`; });
    const by=170-bud.annual_budget/maxS*150;
    bars+=`<line x1="30" y1="${by}" x2="710" y2="${by}" stroke="#d73027" stroke-dasharray="4 3"/></svg></div>`;
    // schedule table
    let sched='<div class="card"><b>Treatment schedule</b><table><thead><tr><th>Year</th><th>Segment</th><th>Treatment</th><th>Cost (&#8377;L)</th><th>Avoided premium (&#8377;L)</th></tr></thead><tbody>'+
      bud.scheduled.map(s=>`<tr><td>${s.year}</td><td>${s.segment_id}</td><td>${s.treatment}</td><td>${s.cost}</td><td>${s.avoided_premium}</td></tr>`).join('')+
      '</tbody></table>'+(bud.unfunded.length?`<p class="err">Unfunded: ${bud.unfunded.join(', ')}</p>`:'')+'</div>';
    // network risk table
    let risk='<div class="card"><b>Network risk profile</b><table><thead><tr><th>Segment</th><th>Zone</th><th>Len km</th><th>MSA</th><th>Prev. window</th><th>Expiry</th><th>Final PCI</th></tr></thead><tbody>'+
      d.segments.map(s=>`<tr><td>${s.segment_id}</td><td>${s.monsoon_zone}</td><td>${s.length_km}</td><td>${s.annual_msa}</td>`+
        `<td>${s.preventive_window_year||'-'}</td><td>${s.window_expired_year||'-'}</td><td>${s.final_pci}</td></tr>`).join('')+
      '</tbody></table></div>';
    $('netOut').innerHTML=kp+bars+sched+risk;
  }catch(e){ $('netErr').textContent=e.message; $('netOut').innerHTML=''; }
}

// auto-run the segment forecast on load so the page isn't empty
runForecast();
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
