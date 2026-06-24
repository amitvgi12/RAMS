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
import os
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict
from urllib.parse import parse_qs, urlparse

from . import api

# Request-body cap. Sized to admit the ingest layer's 16 MB blob limit even after
# base64 encoding (PDF uploads inflate ~1.37x) plus JSON overhead. The server is
# loopback-only, so this larger ceiling does not widen the network attack surface.
MAX_BODY = 32 * 1024 * 1024  # 32 MB cap for JSON API requests

# Large-file uploads (CSV/XLSX/PDF surveys) stream to a temp file via /api/upload
# as a raw body -- no base64, no full-in-memory JSON -- so big NSV exports fit.
UPLOAD_MAX = 128 * 1024 * 1024  # 128 MB
_UPLOAD_CHUNK = 1024 * 1024     # 1 MB read chunks

# POST routes -> pure handler functions in rams.api.
_ROUTES: Dict[str, Callable[[dict], dict]] = {
    "/api/forecast": api.forecast_single,
    "/api/network": api.network_and_budget,
    "/api/ingest": api.ingest_data,
    "/api/residual": api.residual_life,
    "/api/calibrate": api.calibrate,
    "/api/traffic": api.traffic_msa,
    "/api/design": api.pavement_design,
    "/api/pbmc": api.pbmc,
    "/api/iitpave": api.iitpave_evaluate,
    "/api/fwd": api.fwd_overlay,
    "/api/sections": api.survey_sections,
    "/api/ingest_multi": api.ingest_multi,
    "/api/lca": api.lca,
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
            raise ValueError(
                f"request body too large ({length // (1024 * 1024)} MB; "
                f"limit {MAX_BODY // (1024 * 1024)} MB). For a big scanned PDF, "
                f"export the survey table as CSV/XLSX instead."
            )
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
        if urlparse(self.path).path == "/api/upload":
            self._handle_upload()
            return
        if urlparse(self.path).path == "/api/export":
            self._handle_export()
            return
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

    def _handle_export(self) -> None:
        """Build a downloadable homogeneous-section report (xlsx | pdf)."""
        fmt = (parse_qs(urlparse(self.path).query).get("format", [""])[0] or "").lower()
        try:
            payload = self._read_json()
            data, mime, name = api.export_report(payload, fmt)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            self._send_json(500, {"error": "internal error"})

    def _handle_upload(self) -> None:
        """Stream a raw file body to a temp file, then ingest it (large files)."""
        fmt = (parse_qs(urlparse(self.path).query).get("format", [""])[0] or "").lower()
        if fmt not in ("csv", "xlsx", "pdf"):
            self._send_json(400, {"error": "query ?format= must be csv, xlsx or pdf"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self._send_json(400, {"error": "empty upload"})
            return
        if length > UPLOAD_MAX:
            self._send_json(413, {"error": (
                f"file too large ({length // (1024 * 1024)} MB; limit "
                f"{UPLOAD_MAX // (1024 * 1024)} MB).")})
            return
        fd, tmp = tempfile.mkstemp(suffix="." + fmt)
        try:
            remaining = length
            with os.fdopen(fd, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(_UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            self._send_json(200, api.ingest_file(tmp, fmt))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            self._send_json(500, {"error": "internal error"})
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


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
    # On a PaaS (Render / Cloud Run / Heroku / Fly) the platform injects $PORT and
    # expects a public bind, so default to 0.0.0.0:$PORT there; locally keep the
    # secure loopback default. Explicit --host/--port always win.
    env_port = os.environ.get("PORT")
    default_port = int(env_port) if env_port and env_port.isdigit() else 8000
    default_host = os.environ.get("HOST", "0.0.0.0" if env_port else "127.0.0.1")
    p = argparse.ArgumentParser(prog="rams-server", description="RAMS web dashboard")
    p.add_argument("--host", default=default_host, help="bind host (loopback only by default)")
    p.add_argument("--port", type=int, default=default_port)
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
  button.pg { background:#e7edf3; color:var(--ink); border:1px solid #cdd5dd; border-radius:5px;
              padding:3px 9px; font-size:12px; cursor:pointer; }
  button.pg:hover { background:#d7e0e9; }
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
  <button class="tab" data-tab="cal">Calibrate &amp; Residual Life</button>
  <button class="tab" data-tab="dsn">Design &amp; PBMC</button>
</div>
<main>

  <section class="panel active" id="seg">
    <div class="card">
      <b>Load a segment from a survey / FWD file</b>
      <p class="muted">Upload an NSV / FWD condition file (.csv / .xlsx / .pdf) with the standard
        columns (segment_id, base_iri, base_rut, base_crack, annual_msa, traffic_growth_rate,
        monsoon_zone, and optionally deflection_mm / structural_number) in the first worksheet.
        The first segment fills the form below and forecasts automatically. A multi-row chainage
        survey (e.g. an NSV per-100&nbsp;m export) is grouped into <b>homogeneous sections</b>
        (IRC:115 cumulative-difference) with a per-section table you can download as XLSX / PDF.
        <b>Select several files at once</b> (rutting + roughness + cracking + potholes, or a
        multi-sheet workbook) and they are <b>merged by chainage</b> into the full condition.
        If only deflection is present, SNP is derived.</p>
      <div class="grid">
        <div style="grid-column:1/-2"><label>Survey file(s)</label><input id="segFile" type="file" accept=".csv,.xlsx,.pdf" multiple></div>
        <div style="align-self:end"><button class="go" style="margin:0" onclick="importSegment()">Load &amp; forecast</button></div>
      </div>
      <span id="segImpErr" class="err"></span>
      <p class="muted" id="segImpMsg"></p>
      <div id="segSections"></div>
    </div>
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
      <div class="grid" style="margin-top:12px">
        <div><label>Rut model</label><select id="model" onchange="toggleHdm4()">
          <option value="default">Default (IRC:82 law)</option>
          <option value="hdm4">HDM-4 (mechanistic)</option></select></div>
        <div class="hdm4f"><label>Pavement (HDM-4)</label><select id="pavement">
          <option value="dense">Dense-graded AC</option>
          <option value="porous">Porous AC</option></select></div>
        <div class="hdm4f"><label>FWD deflection (mm)</label><input id="deflection" type="number" step="0.05" value="0.85"></div>
        <div class="hdm4f"><label>Structural No. (SNP)</label><input id="snp" type="number" step="0.1" value="4.2"></div>
        <div class="hdm4f"><label>&nbsp;</label><label style="font-weight:normal;font-size:12px">
          <input type="checkbox" id="derive_snp" style="width:auto;margin-right:4px" onchange="$('snp').disabled=this.checked">derive SNP from FWD</label></div>
        <div><label>Crack model</label><select id="crack_model">
          <option value="default">Default (IRC:82 S-curve)</option>
          <option value="mlit">MLIT recursion (paper)</option></select></div>
        <div><label>Roughness model</label><select id="roughness_model">
          <option value="default">Default (IRI law)</option>
          <option value="hdm4">HDM-4 (coupled to rut/crack)</option></select></div>
        <div><label>Skid model</label><select id="skid_model">
          <option value="none">Not modelled</option>
          <option value="hdm4">HDM-4 (SFC polishing)</option></select></div>
        <div><label>Pothole model</label><select id="pothole_model">
          <option value="none">Not modelled</option>
          <option value="hdm4">HDM-4 (crack-initiated)</option></select></div>
        <div><label>Design traffic (MSA)</label><input id="design_msa" type="number" step="1" value="30"></div>
      </div>
      <button class="go" onclick="runForecast()">Forecast</button>
      <span id="segErr" class="err"></span>
    </div>
    <div id="segOut"></div>
  </section>

  <section class="panel" id="net">
    <div class="card">
      <b>Import pavement-databank network</b>
      <p class="muted">Load a network from a <b>CSV</b>, an <b>XLSX</b> survey/condition
        workbook, or a (text-based) <b>PDF</b> condition report. Parsed segments replace the
        demo network used by the optimiser below.</p>
      <div class="grid">
        <div><label>Data file (.csv / .xlsx / .pdf)</label><input id="impFile" type="file" accept=".csv,.xlsx,.pdf"></div>
        <div style="align-self:end"><button class="go" style="margin:0" onclick="importNetwork()">Import</button></div>
      </div>
      <span id="impErr" class="err"></span>
      <div id="impOut"></div>
    </div>
    <div class="card">
      <div class="grid">
        <div><label>Annual budget (&#8377; lakh)</label><input id="budget" type="number" value="600"></div>
        <div><label>Unit cost (&#8377; lakh/km)</label><input id="unit" type="number" value="30"></div>
        <div><label>Horizon (yrs)</label><input id="nyears" type="number" value="10"></div>
        <div><label>Rut model</label><select id="nmodel">
          <option value="default">Default (IRC:82 law)</option>
          <option value="hdm4">HDM-4 (per-segment FWD)</option></select></div>
        <div><label>Pavement (HDM-4)</label><select id="npavement">
          <option value="dense">Dense-graded AC</option>
          <option value="porous">Porous AC</option></select></div>
        <div><label>Design MSA (IRC:37)</label><input id="ndesign" type="number" step="1" value="30"></div>
        <div><label>Handback reqd (MSA)</label><input id="nreq" type="number" step="1" value="10"></div>
      </div>
      <button class="go" onclick="runNetwork()">Optimise budget</button>
      <button class="go reuse" style="display:none;margin-left:8px;background:#1a9850" onclick="useUploadedNetwork()">Use uploaded survey</button>
      <span id="netErr" class="err"></span>
      <p class="muted" id="netSrc">Source: demo network (8 segments) loaded from the server. Cost units are &#8377; lakh.</p>
    </div>
    <div id="netOut"></div>
  </section>

  <section class="panel" id="cal">
    <div class="card">
      <b>Remaining structural (fatigue) life &mdash; IRC:81 / IRC:37</b>
      <p class="muted">Governing of the FWD-deflection capacity (IRC:81) and the design
        traffic budget (IRC:37). Enter a handback requirement to get a PASS/FAIL verdict
        for a BOT/HAM concession.</p>
      <div class="grid">
        <div><label>FWD deflection (mm)</label><input id="r_def" type="number" step="0.05" value="1.10"></div>
        <div><label>Annual MSA</label><input id="r_msa" type="number" step="0.1" value="4.5"></div>
        <div><label>Traffic growth</label><input id="r_growth" type="number" step="0.01" value="0.06"></div>
        <div><label>Cumulative MSA carried</label><input id="r_cmsa" type="number" step="1" value="12"></div>
        <div><label>Design MSA (IRC:37)</label><input id="r_design" type="number" step="1" value="30"></div>
        <div><label>Handback reqd (MSA)</label><input id="r_req" type="number" step="1" value="20"></div>
      </div>
      <button class="go" onclick="runResidual()">Assess residual life</button>
      <span id="resErr" class="err"></span>
      <div id="resOut"></div>
    </div>
    <div class="card">
      <b>Calibrate a deterioration model to your field data</b>
      <p class="muted">Fit the rut, cracking, roughness, skid, or potholes model by OLS regression
        (the paper's method). Pick the model, then paste or upload the matching observations
        (<b>.csv, .xlsx or .pdf</b>). Missing optional columns fall back to defaults; only the
        measured target and its primary driver are required.</p>
      <div class="grid">
        <div><label>Model</label><select id="c_kind" onchange="calHint()">
          <option value="rut">Rutting (Krid/Krst/Krpd)</option>
          <option value="cracking">Cracking (MLIT a,b)</option>
          <option value="roughness">Roughness (HDM-4)</option>
          <option value="skid">Skid (decay_k)</option>
          <option value="potholes">Potholes (rate)</option></select></div>
        <div style="grid-column:2/-1;align-self:end"><label>Observations file (.csv / .xlsx / .pdf)</label><input id="c_file" type="file" accept=".csv,.xlsx,.pdf"></div>
      </div>
      <p class="muted" id="c_hint"></p>
      <textarea id="c_csv" rows="6" style="width:100%;font-family:monospace;font-size:12px;border:1px solid #cdd5dd;border-radius:6px;padding:8px"></textarea>
      <button class="go" onclick="runCalibrate()">Calibrate</button>
      <button class="go" id="calReuse" style="display:none;margin-left:8px;background:#1a9850" onclick="useUploadedCalibrate()">Use uploaded CSV</button>
      <span id="calErr" class="err"></span>
      <div id="calOut"></div>
    </div>
  </section>

  <section class="panel" id="dsn">
    <div class="card">
      <b>1. IRC:37 pavement design (CBR &rarr; layer thicknesses)</b>
      <p class="muted">The design stage, before any field data. Enter the subgrade CBR and the
        design traffic &mdash; either directly as design MSA, or as commercial vehicles/day
        (CVPD) and VDF to derive it via IRC:37. <b>Catalogue</b> returns the IRC:37-2018
        catalogue section; <b>IITPAVE (mechanistic)</b> sizes the lowest-cost section whose
        Odemark&ndash;Boussinesq fatigue and rutting strains both meet the design traffic.</p>
      <div class="grid">
        <div><label>Method</label><select id="d_method"><option value="catalogue">Catalogue (IRC:37-2018)</option><option value="iitpave">IITPAVE (mechanistic)</option></select></div>
        <div><label>Subgrade CBR (%)</label><input id="d_cbr" type="number" step="0.5" value="8"></div>
        <div><label>Design MSA (blank to derive)</label><input id="d_msa" type="number" step="1" placeholder="from CVPD"></div>
        <div><label>CVPD (if no MSA)</label><input id="d_cvpd" type="number" step="100" value="4500"></div>
        <div><label>VDF</label><input id="d_vdf" type="number" step="0.1" value="4.5"></div>
        <div><label>Design life (yrs)</label><input id="d_life" type="number" value="15"></div>
        <div><label>Carriageway</label><select id="d_cway">
          <option value="two_lane">Two-lane</option><option value="four_lane">Four-lane</option>
          <option value="single">Single</option><option value="six_lane">Six-lane</option></select></div>
      </div>
      <button class="go" onclick="runDesign()">Design pavement</button>
      <span id="dsnErr" class="err"></span>
      <div id="dsnOut"></div>
    </div>
    <div class="card">
      <b>1b. IITPAVE section check (FWD back-calculated moduli &rarr; residual life)</b>
      <p class="muted">Evaluate an in-service section mechanistically from its layer moduli and
        thicknesses &mdash; e.g. the 15th-percentile design moduli from an FWD back-calculation
        report. Returns the critical strains, the IRC:37 fatigue/rutting capacity, and the
        remaining structural life against the design traffic.</p>
      <div class="grid">
        <div><label>E bituminous (MPa)</label><input id="i_ebt" type="number" step="50" value="977"></div>
        <div><label>E granular (MPa)</label><input id="i_egr" type="number" step="10" value="200"></div>
        <div><label>E subgrade (MPa)</label><input id="i_esg" type="number" step="5" value="70"></div>
        <div><label>Bituminous (mm)</label><input id="i_hbt" type="number" step="5" value="300"></div>
        <div><label>Granular (mm)</label><input id="i_hgr" type="number" step="5" value="350"></div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div><label>Annual MSA</label><input id="i_msa" type="number" step="0.5" value="10"></div>
        <div><label>Traffic growth</label><input id="i_growth" type="number" step="0.01" value="0.05"></div>
        <div><label>Cumulative MSA carried</label><input id="i_cmsa" type="number" step="1" value="20"></div>
        <div><label>Design MSA</label><input id="i_dmsa" type="number" step="1" value="150"></div>
        <div><label>Fatigue model</label><select id="i_standard">
          <option value="irc115">IRC:115-2014 (FWD)</option>
          <option value="irc37">IRC:37-2018 (design)</option></select></div>
      </div>
      <button class="go" onclick="runIITPAVE()">Assess section</button>
      <span id="iitErr" class="err"></span>
      <div id="iitOut"></div>
    </div>
    <div class="card">
      <b>1c. FWD remaining-life &amp; overlay (15th-percentile moduli &rarr; overlay)</b>
      <p class="muted">Paste an FWD report's homogeneous sub-sections (the corrected
        15th-percentile back-calculated moduli + crust thickness), or upload the report
        (<b>.csv / .xlsx / .pdf</b>). RAMS computes the IRC:115-2014 remaining life per section
        and flags where an overlay is needed for the design traffic. Screening-grade
        (Odemark&ndash;Boussinesq, ~&plusmn;10% of IITPAVE); sections within 15% of the threshold
        are marked to confirm with IITPAVE. Columns: section_id, e_bituminous, e_granular,
        e_subgrade, h_bituminous, h_granular (optional chainage_from, chainage_to).
        Missing optional columns default; only e_bituminous is required.</p>
      <div class="grid">
        <div><label>Design MSA</label><input id="f_design" type="number" step="10" value="300"></div>
        <div style="grid-column:2/-1;align-self:end"><label>FWD report file (.csv / .xlsx / .pdf)</label><input id="f_file" type="file" accept=".csv,.xlsx,.pdf"></div>
      </div>
      <textarea id="f_csv" rows="6" style="width:100%;font-family:monospace;font-size:12px;border:1px solid #cdd5dd;border-radius:6px;padding:8px;margin-top:8px">section_id,e_bituminous,e_granular,e_subgrade,h_bituminous,h_granular
Sec-1,870,348,77,300,350
Sec-2,1581,235,77,300,350
Sec-3,801,352,77,300,350
Sec-4,865,335,77,300,350</textarea>
      <button class="go" onclick="runFWD()">Assess overlay</button>
      <span id="fwdErr" class="err"></span>
      <div id="fwdOut"></div>
    </div>
    <div class="card">
      <b>2. Performance-Based Maintenance Contract estimate (5&ndash;7 yr)</b>
      <p class="muted">The financial-forecast stage. Prices a fixed-term contract to keep the
        road above a service-level PCI: handover rectification + routine + periodic renewals
        (from the deterioration forecast) + escalation, contingency and overhead. Reports the
        contract value, NPV and per-year cash flow.</p>
      <div class="grid">
        <div><label>Initial IRI (mm/m)</label><input id="p_iri" type="number" step="0.1" value="2.6"></div>
        <div><label>Initial Rut (mm)</label><input id="p_rut" type="number" step="0.1" value="5.0"></div>
        <div><label>Initial Crack (%)</label><input id="p_crack" type="number" step="0.1" value="5.0"></div>
        <div><label>Annual MSA</label><input id="p_msa" type="number" step="0.1" value="3.5"></div>
        <div><label>Traffic growth</label><input id="p_growth" type="number" step="0.01" value="0.05"></div>
        <div><label>Monsoon zone</label><select id="p_zone">
          <option>HIGH</option><option selected>MEDIUM</option><option>LOW</option></select></div>
        <div><label>Length (km)</label><input id="p_len" type="number" step="0.5" value="15"></div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div><label>Term (yrs)</label><input id="p_term" type="number" value="5"></div>
        <div><label>Service level PCI</label><input id="p_pci" type="number" step="0.1" value="3.0"></div>
        <div><label>Routine /km/yr</label><input id="p_rate" type="number" step="0.1" value="1.5"></div>
        <div><label>Escalation</label><input id="p_esc" type="number" step="0.01" value="0.05"></div>
        <div><label>Contingency</label><input id="p_cont" type="number" step="0.01" value="0.10"></div>
        <div><label>Overhead</label><input id="p_oh" type="number" step="0.01" value="0.10"></div>
        <div><label>Discount (NPV)</label><input id="p_disc" type="number" step="0.01" value="0.08"></div>
      </div>
      <button class="go" onclick="runPBMC()">Estimate PBMC</button>
      <button class="go reuse" style="display:none;margin-left:8px;background:#1a9850" onclick="useUploadedPBMC()">Use uploaded survey</button>
      <span id="pbmcErr" class="err"></span>
      <div id="pbmcOut"></div>
    </div>
    <div class="card">
      <b>3. Life-Cycle Analysis (LCA) &amp; decision matrix &mdash; MoRTH cost</b>
      <p class="muted">Project the deterioration over the years you choose and get the
        year-by-year <b>decision</b> it triggers &mdash; routine &rarr; preventive &rarr; structural
        overlay &rarr; reconstruction &mdash; with each action costed from the <b>MoRTH Standard
        Data Book</b> (indicative rates, editable). Reports total cost, NPV and the equivalent
        uniform annual cost (EUAC). Download the matrix as XLSX / PDF.</p>
      <div class="grid">
        <div><label>Initial IRI (mm/m)</label><input id="l_iri" type="number" step="0.1" value="2.6"></div>
        <div><label>Initial Rut (mm)</label><input id="l_rut" type="number" step="0.1" value="5.0"></div>
        <div><label>Initial Crack (%)</label><input id="l_crack" type="number" step="0.1" value="5.0"></div>
        <div><label>Annual MSA</label><input id="l_msa" type="number" step="0.1" value="4.5"></div>
        <div><label>Traffic growth</label><input id="l_growth" type="number" step="0.01" value="0.06"></div>
        <div><label>Monsoon zone</label><select id="l_zone">
          <option>HIGH</option><option selected>MEDIUM</option><option>LOW</option></select></div>
      </div>
      <div class="grid" style="margin-top:12px">
        <div><label>Horizon (years)</label><input id="l_years" type="number" value="20"></div>
        <div><label>Length (km)</label><input id="l_len" type="number" step="0.5" value="10"></div>
        <div><label>Carriageway width (m)</label><input id="l_width" type="number" step="0.5" value="7"></div>
        <div><label>Discount rate</label><input id="l_disc" type="number" step="0.01" value="0.08"></div>
      </div>
      <button class="go" onclick="runLCA()">Build LCA matrix</button>
      <button class="go reuse" style="display:none;margin-left:8px;background:#1a9850" onclick="useUploadedLCA()">Use uploaded survey</button>
      <span id="lcaErr" class="err"></span>
      <div id="lcaOut"></div>
    </div>
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

// ---- client-side pagination for long result tables ------------------------
const PAGES={};
function renderPaged(outId, headerRow, rowsArr, extraHtml){
  PAGES[outId]={head:headerRow, rows:rowsArr, page:0, size:25, extra:extraHtml||''};
  _drawPage(outId);
}
function _drawPage(outId){
  const st=PAGES[outId], n=st.rows.length;
  const size=(st.size==='all')?Math.max(n,1):st.size;
  const pages=Math.max(1, Math.ceil(n/size));
  if(st.page>=pages) st.page=pages-1; if(st.page<0) st.page=0;
  const start=st.page*size, end=Math.min(n, start+size);
  const tbl='<div style="overflow-x:auto"><table>'+st.head+st.rows.slice(start,end).join('')+'</table></div>';
  let ctrl='';
  if(n>25){
    const opts=['25','50','100','all'].map(s=>'<option'+(String(st.size)===s?' selected':'')+'>'+s+'</option>').join('');
    ctrl='<div class="muted" style="margin:8px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'+
      'Rows '+(start+1)+'–'+end+' of '+n+
      ' <button class="pg" onclick="_pg(\''+outId+'\',-1)">&#8249; Prev</button>'+
      ' page '+(st.page+1)+'/'+pages+
      ' <button class="pg" onclick="_pg(\''+outId+'\',1)">Next &#8250;</button>'+
      ' &middot; <select onchange="_pgSize(\''+outId+'\',this.value)">'+opts+'</select> per page</div>';
  }
  $(outId).innerHTML=st.extra+ctrl+tbl;
}
function _pg(outId,d){ PAGES[outId].page+=d; _drawPage(outId); }
function _pgSize(outId,v){ const st=PAGES[outId]; st.size=(v==='all'?'all':+v); st.page=0; _drawPage(outId); }

// Deferred pagination: build a card with a placeholder div now, paginate after
// the card is inserted into the DOM (controls only appear when >25 rows).
let PENDING_PAGED=[];
function pagedCard(id, titleHtml, descHtml, header, rows){
  PENDING_PAGED.push({id, header, rows});
  return '<div class="card">'+titleHtml+(descHtml||'')+'<div id="'+id+'"></div></div>';
}
function flushPaged(){ for(const p of PENDING_PAGED){ renderPaged(p.id, p.header, p.rows, ''); } PENDING_PAGED=[]; }

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

// HDM-4 per-year rut increment breakdown (densification / structural / plastic).
function hdm4Table(bk){
  if(!bk || !bk.length) return '';
  const header='<tr><th>Year</th><th>Densif.</th><th>Structural</th><th>Plastic</th><th>Total</th></tr>';
  const rows=bk.map(r=>`<tr><td>${r.year}</td><td>${r.densification}</td><td>${r.structural}</td><td>${r.plastic}</td><td><b>${r.total}</b></td></tr>`);
  return pagedCard('fcHdm4','<b>HDM-4 rut increment breakdown (mm/yr)</b>',
    '<p class="muted">&Delta;RDM = K<sub>rid</sub>&middot;densification + K<sub>rst</sub>&middot;structural + K<sub>rpd</sub>&middot;plastic. '+
    'Densification is a one-off in year 1; structural/plastic accrue with traffic (driven by FWD deflection &amp; structural number).</p>',
    header, rows);
}

// Indian intervention triggers (first crossing of each).
const SEVC={FUNCTIONAL:'#f0a000', STRUCTURAL:'#d73027'};
function triggerTable(tr){
  if(!tr) return '';
  const seen={}; const raw=[];
  for(const yt of tr){ for(const t of yt.fired){ const k=t.name+'|'+t.severity;
    if(seen[k]) continue; seen[k]=1; raw.push([yt.year,t]); } }
  if(!raw.length) return '';
  raw.sort((a,b)=>a[0]-b[0]);
  const header='<tr><th>Year</th><th>Severity</th><th>Trigger</th><th>IRC ref</th><th>Reason</th></tr>';
  const rows=raw.map(([y,t])=>{ const c=SEVC[t.severity]||'#445';
    return `<tr><td>${y}</td><td style="color:${c};font-weight:600">${t.severity}</td><td>${t.name}</td>`+
       `<td>${t.irc_reference}</td><td>${t.reason}</td></tr>`; });
  return pagedCard('fcTrigger','<b>Intervention triggers (Indian IRC thresholds &mdash; first crossing)</b>','',header,rows);
}

// Skid resistance (SFC) trajectory -- decreases with traffic (aggregate polishing).
function skidTable(sk){
  if(!sk || !sk.length) return '';
  const header='<tr><th>Year</th><th>SFC</th><th>Below 0.40?</th></tr>';
  const rows=sk.map(r=>{ const c=r.below_limit?'#d73027':'#1a9850';
    return `<tr><td>${r.year}</td><td style="color:${c};font-weight:600">${r.skid}</td>`+
       `<td>${r.below_limit?'<span class="err">YES</span>':'no'}</td></tr>`; });
  return pagedCard('fcSkid','<b>Skid resistance (side-force coefficient)</b>',
    '<p class="muted">HDM-4 aggregate-polishing decay toward a terminal SFC. Below 0.40 triggers a '+
    'skid-restoring surface treatment (IRC:SP:16 / safety).</p>', header, rows);
}

// Potholing (area %) -- crack-initiated, grows with traffic.
function potholeTable(pt){
  if(!pt || !pt.length) return '';
  const header='<tr><th>Year</th><th>Potholes %</th><th>Over 2%?</th></tr>';
  const rows=pt.map(r=>{ const c=r.over_limit?'#d73027':'#1a9850';
    return `<tr><td>${r.year}</td><td style="color:${c};font-weight:600">${r.potholes}</td>`+
       `<td>${r.over_limit?'<span class="err">YES</span>':'no'}</td></tr>`; });
  return pagedCard('fcPothole','<b>Potholing (area %)</b>',
    '<p class="muted">HDM-4 crack-initiated potholing: starts once cracking passes 20% area, then grows '+
    'with traffic. Above 2% triggers immediate patching (IRC:82 / MoRTH).</p>', header, rows);
}

// MLIT-PMS MCI cross-reference (Taniguchi & Yoshida). Colour by management band.
const MCIC={DESIRABLE:'#1a9850', NEEDS_REPAIR:'#f0a000', IMMEDIATE_REPAIR:'#d73027'};
function mciTable(mci){
  if(!mci || !mci.length) return '';
  const header='<tr><th>Year</th><th>MCI</th><th>Management band</th><th>Rut &gt; 30mm</th></tr>';
  const rows=mci.map(r=>{ const c=MCIC[r.band]||'#445';
    return `<tr><td>${r.year}</td><td style="color:${c};font-weight:600">${r.mci}</td>`+
       `<td><span class="dot" style="color:${c}">&#9679;</span> ${r.band.replace('_',' ')}</td>`+
       `<td>${r.rut_over_30mm?'<span class="err">YES</span>':'no'}</td></tr>`; });
  return pagedCard('fcMci','<b>MLIT-PMS Maintenance Control Index (MCI) &mdash; cross-reference</b>',
    '<p class="muted">Japanese integrated index <code>MCI = 10 &minus; 1.48&middot;C<sup>0.3</sup> &minus; 0.29&middot;D<sup>0.7</sup> &minus; 0.47&middot;&sigma;<sup>0.2</sup></code>. '+
    'Bands: &gt;5 desirable, 3&ndash;5 needs repair, &lt;3 immediate. Rut overlay trigger = 30&nbsp;mm. '+
    '<i>IRI is used as the roughness &sigma; proxy here &mdash; an approximation.</i></p>', header, rows);
}

function timelineCard(rows,b,modelDesc){
  const header='<tr><th>Year</th><th>Cum MSA</th><th>IRI</th><th>Rut</th><th>Crack%</th><th>PCI</th><th>Flag</th></tr>';
  const rr=rows.map(r=>{ const f=flagFor(r.IRC82_PCI,b); const c=BANDC[f];
    return `<tr><td>${r.Year}</td><td>${r.Cumulative_MSA}</td><td>${r.IRI}</td><td>${r.Rutting_mm}</td>`+
       `<td>${r.Cracking_Pct}</td><td style="color:${c};font-weight:600">${r.IRC82_PCI}</td>`+
       `<td><span class="dot" style="color:${c}">&#9679;</span> ${f}</td></tr>`; });
  return pagedCard('fcUntreated','<b>Untreated forecast</b>', modelDesc, header, rr);
}

function toggleHdm4(){ const on=$('model').value==='hdm4';
  document.querySelectorAll('.hdm4f').forEach(e=>e.style.display=on?'':'none'); }

// Load one segment's condition (incl. FWD deflection/SNP) from a survey file.
let SURVEY=null;     // segments from the most recent survey upload (shared across tabs)
let UPLOAD_RAW=null;  // raw text of an uploaded CSV (for the Calibrate tab)

// Representative (most-distressed) segment of the uploaded survey, for prefilling
// the single-segment tabs (PBMC / LCA).
function worstSeg(){
  if(!SURVEY||!SURVEY.length) return null;
  return SURVEY.reduce((a,b)=> ((b.base_rut+b.base_crack+b.base_iri) > (a.base_rut+a.base_crack+a.base_iri) ? b : a));
}
function _setVal(id,v){ const e=$(id); if(e&&v!=null) e.value=v; }

// Which columns come from the survey file vs. are filled with defaults.
const provenanceNote='<p class="muted" style="margin:6px 0 10px">'+
  '<b>From the file:</b> Segment/chainage, IRI, Rut, Crack% and length (100&nbsp;m bins). '+
  '<b>Defaults (not in an NSV survey):</b> Zone = MEDIUM, MSA = 4.5, traffic growth = 5%/yr '+
  '&mdash; override per segment, or upload a CSV that carries <code>monsoon_zone</code>, '+
  '<code>annual_msa</code>, <code>traffic_growth_rate</code> to use real values.</p>';

function readB64(file){
  return new Promise((res,rej)=>{
    const r=new FileReader();
    r.onload=()=>res(String(r.result).split(',')[1]||'');
    r.onerror=()=>rej(new Error('could not read '+file.name));
    r.readAsDataURL(file);
  });
}

async function importSegment(){
  $('segImpErr').textContent=''; $('segImpMsg').textContent=''; $('segSections').innerHTML='';
  SURVEY=null; UPLOAD_RAW=null;
  const fs=$('segFile').files;
  if(!fs.length){ $('segImpErr').textContent='choose one or more .csv / .xlsx / .pdf file(s).'; return; }
  $('segImpMsg').textContent='Uploading & parsing '+fs.length+' file(s)…';
  try{
    const files=[];
    for(const f of fs){
      files.push({name:f.name, content_b64:await readB64(f)});
      if(f.name.toLowerCase().endsWith('.csv') && !UPLOAD_RAW){ UPLOAD_RAW=await f.text(); }
    }
    const d=await postJSON('/api/ingest_multi',{files:files});
    if(!d.segments.length) throw new Error('no segments found (sheets: '+d.files.map(x=>x.name+' ['+x.status+']').join('; ')+')');
    // Share the uploaded data with every tab (no re-upload needed).
    SURVEY=d.segments; IMPORTED=d.segments;
    const s=d.segments[0];
    $('iri').value=s.base_iri; $('rut').value=s.base_rut; $('crack').value=s.base_crack;
    $('msa').value=s.annual_msa; $('growth').value=s.traffic_growth_rate; $('zone').value=s.monsoon_zone;
    if(s.deflection_mm!=null){ $('deflection').value=s.deflection_mm; }
    if(s.structural_number!=null){ $('snp').value=s.structural_number; }
    await runForecast();   // forecast the first segment for the chart below
    refreshReuse();
    const fileList=d.files.map(x=>'<li>'+x.name+' &mdash; '+x.status+'</li>').join('');
    if(d.count>1){
      SURVEY=d.segments;
      $('segImpMsg').innerHTML='Merged <b>'+d.count+'</b> chainage points from '+fs.length+
        ' file(s). First segment ('+(s.segment_id||'')+') forecast below; homogeneous sections:'+
        '<ul class="muted" style="margin:6px 0">'+fileList+'</ul>'+provenanceNote;
      runSections();
    }else{
      $('segImpMsg').innerHTML='Loaded '+(s.segment_id||'segment')+'. Forecast below.'+
        '<ul class="muted" style="margin:6px 0">'+fileList+'</ul>'+provenanceNote;
    }
  }catch(e){ $('segImpErr').textContent=e.message; $('segImpMsg').textContent=''; }
}

function _sectionBody(){
  return {segments:SURVEY, years:+$('years').value, min_length_km:0.5, key:'pci'};
}

async function runSections(){
  $('segSections').innerHTML='<p class="muted">building homogeneous sections&hellip;</p>';
  try{
    const d=await postJSON('/api/sections',_sectionBody());
    const rows=d.sections.map(s=>{
      const bc=BANDC[s.band]||BANDC.ROUTINE;
      return '<tr><td>'+s.section_id+'</td><td>'+(s.chainage_from!=null?s.chainage_from:'-')+'</td>'+
        '<td>'+(s.chainage_to!=null?s.chainage_to:'-')+'</td><td>'+s.length_km+'</td><td>'+s.n_points+'</td>'+
        '<td>'+s.mean_iri+'</td><td>'+s.mean_rut+'</td><td>'+s.mean_crack+'</td><td>'+s.base_pci+'</td>'+
        '<td style="text-align:left;color:'+bc+';font-weight:600">'+s.band+'</td>'+
        '<td style="text-align:left">'+s.treatment+'</td><td>'+(s.preventive_window_year!=null?s.preventive_window_year:'-')+'</td></tr>';
    });
    const b=d.band_counts;
    const header='<tr><th>Section</th><th>From</th><th>To</th><th>km</th><th>pts</th>'+
      '<th>IRI</th><th>Rut</th><th>Crack%</th><th>PCI</th><th style="text-align:left">Band</th>'+
      '<th style="text-align:left">Treatment</th><th>Prev.Yr</th></tr>';
    const extra='<div class="kpis" style="margin:12px 0">'+
        kpi(d.n_sections,'homogeneous sections')+
        kpi(d.total_length_km+' km','total length')+
        kpi(b.PREVENTIVE+b.STRUCTURAL,'sections needing action')+
        kpi(d.n_points,'survey points')+'</div>'+
      '<div style="margin-bottom:6px">'+
        '<button class="go" style="margin:0 8px 0 0" onclick="downloadReport(\'xlsx\')">Download XLSX</button>'+
        '<button class="go" style="margin:0" onclick="downloadReport(\'pdf\')">Download PDF</button></div>';
    renderPaged('segSections', header, rows, extra);
  }catch(e){ $('segSections').innerHTML='<span class="err">'+e.message+'</span>'; }
}

async function downloadReport(fmt){
  if(!SURVEY){ return; }
  try{
    const r=await fetch('/api/export?format='+fmt,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(_sectionBody())});
    if(!r.ok){ const j=await r.json(); throw new Error(j.error||'export failed'); }
    const blob=await r.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='rams_sections.'+fmt;
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  }catch(e){ $('segImpErr').textContent=e.message; }
}

async function runForecast(){
  $('segErr').textContent=''; $('segOut').innerHTML='<p class="muted">computing&hellip;</p>';
  try{
    const body={iri:+$('iri').value,rut:+$('rut').value,crack:+$('crack').value,
      msa:+$('msa').value,growth:+$('growth').value,zone:$('zone').value,years:+$('years').value,
      model:$('model').value, pavement:$('pavement').value,
      crack_model:$('crack_model').value, roughness_model:$('roughness_model').value,
      skid_model:$('skid_model').value, pothole_model:$('pothole_model').value,
      deflection:+$('deflection').value, snp:+$('snp').value, design_msa:+$('design_msa').value,
      derive_snp:$('derive_snp').checked};
    const d=await postJSON('/api/forecast',body); const b=d.bands;
    if(d.model && d.model.snp_derived_from_fwd){ $('snp').value=d.model.structural_number; }
    const p=d.plan; const bc = p.window_expired_year?BANDC.STRUCTURAL:(p.preventive_window_year?BANDC.PREVENTIVE:BANDC.ROUTINE);
    let iv='';
    if(d.interventions.length){
      const ivHeader='<tr><th>Year</th><th>Treatment</th><th>PCI before</th><th>PCI after</th><th>Cost (&#8377;L)</th></tr>';
      const ivRows=d.interventions.map(i=>`<tr><td>${i.year}</td><td>${i.treatment}</td><td>${i.pci_before}</td><td>${i.pci_after}</td><td>${i.cost}</td></tr>`);
      iv=pagedCard('fcIv','<b>Treatments applied (managed scenario)</b>',
        `<p class="muted">Managed lifecycle cost: &#8377;${d.managed_total_cost} lakh &middot; costed from MoRTH Standard Data Book rates (&times; carriageway area)</p>`,
        ivHeader, ivRows);
    }
    const modelDesc=`<p class="muted">Rut: ${d.model.label} &middot; Crack: ${d.model.crack_label} &middot; Roughness: ${d.model.roughness_label} &middot; Skid: ${d.model.skid_label} &middot; Potholes: ${d.model.pothole_label}</p>`;
    $('segOut').innerHTML=
      `<div class="card"><div class="banner" style="background:${bc}">${p.rationale}</div></div>`+
      `<div class="card"><div class="legend"><span style="color:${BANDC.ROUTINE}">&#9679; Routine</span>`+
        `<span style="color:${BANDC.PREVENTIVE}">&#9679; Preventive window</span>`+
        `<span style="color:${BANDC.STRUCTURAL}">&#9679; Structural</span></div>`+
        pciChart(d.untreated,d.treated,b)+`</div>`+
      iv+
      timelineCard(d.untreated,b,modelDesc)+
      hdm4Table(d.model.rut_breakdown)+
      skidTable(d.skid)+
      potholeTable(d.potholes)+
      triggerTable(d.triggers)+
      mciTable(d.mci);
    flushPaged();
  }catch(e){ $('segErr').textContent=e.message; $('segOut').innerHTML=''; }
}

// Imported network (null => use the server demo network).
let IMPORTED=null;

function readFile(file){ return new Promise((res,rej)=>{
  const r=new FileReader(); r.onerror=()=>rej(new Error('could not read file'));
  r.onload=()=>res(r.result);
  if(file.name.toLowerCase().endsWith('.pdf')) r.readAsDataURL(file); else r.readAsText(file);
}); }

async function importNetwork(){
  $('impErr').textContent=''; $('impOut').innerHTML='';
  const f=$('impFile').files[0];
  if(!f){ $('impErr').textContent='choose a .csv, .xlsx or .pdf file first.'; return; }
  const name=f.name.toLowerCase();
  const fmt=name.endsWith('.xlsx')?'xlsx':name.endsWith('.pdf')?'pdf':name.endsWith('.csv')?'csv':null;
  if(!fmt){ $('impErr').textContent='unsupported file type (use .csv, .xlsx or .pdf).'; return; }
  $('impOut').innerHTML='<p class="muted">uploading &amp; parsing&hellip;</p>';
  try{
    // Stream the raw file to /api/upload (no base64) so large NSV files fit.
    const r=await fetch('/api/upload?format='+fmt,{method:'POST',body:f});
    const d=await r.json(); if(!r.ok) throw new Error(d.error||'upload failed');
    IMPORTED=d.segments;
    $('netSrc').innerHTML=`Source: <b>imported ${fmt.toUpperCase()}</b> (${d.count} segment(s)) from <b>${f.name}</b>.`;
    let errHtml = d.errors.length
      ? `<p class="err">${d.errors.length} row(s) skipped: `+
        d.errors.slice(0,5).map(e=>`row ${e.row}: ${e.message}`).join('; ')+'</p>' : '';
    const header='<tr><th>Segment</th><th>Zone</th><th>IRI</th><th>Rut</th><th>Crack%</th><th>MSA</th><th>Len km</th></tr>';
    const rows=d.segments.map(s=>`<tr><td>${s.segment_id}</td><td>${s.monsoon_zone}</td><td>${s.base_iri}</td><td>${s.base_rut}</td><td>${s.base_crack}</td><td>${s.annual_msa}</td><td>${s.length_km}</td></tr>`);
    $('impOut').innerHTML=
      `<p class="muted">Imported <b>${d.count}</b> segment(s). Click <b>Optimise budget</b> to run.</p>`+errHtml+
      provenanceNote+'<div id="netImp"></div>';
    renderPaged('netImp', header, rows, '');
  }catch(e){ $('impErr').textContent=e.message; $('impOut').innerHTML=''; IMPORTED=null; }
}

async function runNetwork(){
  $('netErr').textContent=''; $('netOut').innerHTML='<p class="muted">optimising&hellip;</p>';
  try{
    const segments = IMPORTED || (await (await fetch('/api/sample')).json()).segments;
    const body={segments:segments, annual_budget:+$('budget').value,
      base_unit_cost:+$('unit').value, years:+$('nyears').value,
      model:$('nmodel').value, pavement:$('npavement').value,
      design_msa:+$('ndesign').value, required_residual_msa:+$('nreq').value};
    const d=await postJSON('/api/network',body); const bud=d.budget;
    // KPI cards
    let kp=`<div class="card"><div class="kpis">`+
      `<div class="kpi"><b>&#8377;${bud.total_spend}</b><span>Total spend (L)</span></div>`+
      `<div class="kpi"><b style="color:#1a9850">&#8377;${bud.net_savings}</b><span>Avoided structural cost (funded)</span></div>`+
      `<div class="kpi"><b>${bud.scheduled.length}</b><span>Segments funded</span></div>`+
      `<div class="kpi"><b style="color:${bud.unfunded.length?'#d73027':'#1a9850'}">${bud.unfunded.length}</b><span>Unfunded (&rarr; structural)</span></div>`+
      (d.handback?`<div class="kpi"><b style="color:${d.handback.counts.FAIL?'#d73027':'#1a9850'}">${d.handback.counts.FAIL}</b><span>Fail handback (&ge;${d.handback.required_residual_msa} MSA)</span></div>`:'')+
      `</div><p class="muted">${bud.rationale}</p>`+
      (bud.unfunded.length
        ? `<p class="muted" style="background:#fff6e5;border-left:3px solid #f0a000;padding:8px 10px;border-radius:4px">`+
          `<b>Why &ldquo;unfunded&rdquo;?</b> ${bud.unfunded.length} of ${bud.n_at_risk} at-risk segment(s) need preventive work, `+
          `but &#8377;${bud.annual_budget}L/yr &times; ${$('nyears').value}yr covers only the highest-priority ones &mdash; the rest miss their window and fall to costlier structural repair. `+
          `<b>To clear the backlog</b>, raise the annual budget to &asymp; <b>&#8377;${bud.recommended_annual_budget}L/yr</b> `+
          `(total preventive need &#8377;${bud.total_preventive_need}L over the horizon), or accept the deferral.</p>`
        : `<p class="muted" style="color:#1a9850">All ${bud.n_at_risk} at-risk segment(s) funded within budget &mdash; no backlog.</p>`)+
      (d.handback&&d.handback.failing.length?`<p class="err">Handback FAIL: ${d.handback.failing.join(', ')} &mdash; need structural strengthening before handback.</p>`:'')+
      `</div>`;
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
    // schedule table (paginated)
    const schedHeader='<tr><th>Year</th><th>Segment</th><th>Treatment</th><th>Cost (&#8377;L)</th><th>Avoided premium (&#8377;L)</th></tr>';
    const schedRows=bud.scheduled.map(s=>`<tr><td>${s.year}</td><td>${s.segment_id}</td><td>${s.treatment}</td><td>${s.cost}</td><td>${s.avoided_premium}</td></tr>`);
    const unf = bud.unfunded.length
      ? `<details><summary class="err" style="cursor:pointer;font-weight:600">`+
        `${bud.unfunded.length} segment(s) unfunded &mdash; will require structural mill &amp; overlay (click to list)</summary>`+
        `<div class="muted" style="max-height:160px;overflow:auto;margin:6px 0;font-size:12px;line-height:1.6">`+
        `${bud.unfunded.join(', ')}</div></details>`
      : '';
    const sched=pagedCard('netSched','<b>Treatment schedule</b>', unf, schedHeader, schedRows);
    // network risk table (structural columns shown under HDM-4)
    const hdm4 = d.model && d.model.rut_model==='HDM4';
    const hb = !!d.handback;
    const modelNote = `<div class="card"><p class="muted">Rut model: <b>${d.model?d.model.label:'default'}</b>${hdm4?' &mdash; each segment forecast from its own FWD deflection / structural number.':''}</p></div>`;
    const riskHeader='<tr><th>Segment</th><th>Zone</th><th>Len km</th><th>MSA</th>'+
      (hdm4?'<th>Defl. (mm)</th><th>SNP</th>':'')+'<th>Prev. window</th><th>Expiry</th><th>Final PCI</th>'+
      '<th>Residual MSA</th><th>Resid. yr</th>'+(hb?'<th>Handback</th>':'')+'</tr>';
    const riskRows=d.segments.map(s=>{ const hc=VERDC[s.handback]||'#445';
        return `<tr><td>${s.segment_id}</td><td>${s.monsoon_zone}</td><td>${s.length_km}</td><td>${s.annual_msa}</td>`+
        (hdm4?`<td>${s.deflection_mm}</td><td>${s.structural_number}</td>`:'')+
        `<td>${s.preventive_window_year||'-'}</td><td>${s.window_expired_year||'-'}</td><td>${s.final_pci}</td>`+
        `<td>${s.residual_msa}</td><td>${s.residual_years==null?'&infin;':s.residual_years}</td>`+
        (hb?`<td style="color:${hc};font-weight:600">${s.handback||'-'}</td>`:'')+`</tr>`; });
    const riskCard='<div class="card"><b>Network risk &amp; residual-life profile</b><div id="netRisk"></div></div>';
    $('netOut').innerHTML=modelNote+kp+bars+sched+riskCard;
    flushPaged();   // schedule table
    renderPaged('netRisk', riskHeader, riskRows,
      '<p class="muted" style="margin:6px 0">Residual MSA = governing of IRC:81 deflection capacity vs IRC:37 design budget.</p>');
  }catch(e){ $('netErr').textContent=e.message; $('netOut').innerHTML=''; }
}

// ---- Residual life & calibration (3rd tab) --------------------------------
const VERDC={PASS:'#1a9850', MARGINAL:'#f0a000', FAIL:'#d73027'};
async function runResidual(){
  $('resErr').textContent=''; $('resOut').innerHTML='<p class="muted">assessing&hellip;</p>';
  try{
    const body={deflection:+$('r_def').value, msa:+$('r_msa').value, growth:+$('r_growth').value,
      cumulative_msa:+$('r_cmsa').value, design_msa:+$('r_design').value, required_residual_msa:+$('r_req').value};
    const d=await postJSON('/api/residual',body); const r=d.residual;
    let h='<div class="kpis" style="margin-top:8px">'+
      `<div class="kpi"><b>${r.governing_remaining_msa}</b><span>Governing remaining MSA</span></div>`+
      `<div class="kpi"><b>${r.residual_years==null?'&infin;':r.residual_years}</b><span>Residual years</span></div>`+
      `<div class="kpi"><b>${r.allowable_msa_deflection}</b><span>IRC:81 deflection capacity</span></div>`+
      `<div class="kpi"><b>${r.remaining_msa_traffic==null?'&mdash;':r.remaining_msa_traffic}</b><span>IRC:37 budget left</span></div></div>`+
      `<p class="muted">Governing basis: <b>${r.governing_basis}</b>. ${r.rationale}</p>`;
    if(d.handback){ const v=d.handback; const c=VERDC[v.verdict]||'#445';
      h+=`<div class="banner" style="background:${c};margin-top:8px">Handback: ${v.verdict} &mdash; ${v.rationale}</div>`; }
    $('resOut').innerHTML=h;
  }catch(e){ $('resErr').textContent=e.message; $('resOut').innerHTML=''; }
}

const CAL_HINTS={
  rut:'CSV header: ye4,age,deflection_mm,structural_number,measured_rut_increment_mm (optional: compaction_pct,cds,heavy_speed_kmh,surfacing_thickness_mm)',
  cracking:'CSV header: crack_prev,crack_next (consecutive yearly cracking % on the same segment)',
  roughness:'CSV header: measured_iri_increment,iri,structural_number,age,d_msa,d_crack_pct,d_rut_mm',
  skid:'CSV header: measured_sfc_decrement,sfc,d_msa (yearly SFC change <= 0, SFC at year start, MSA that year)',
  potholes:'CSV header: measured_pothole_increment,cracking_pct,d_msa (yearly potholes-area change, cracking % at year start, MSA that year)'};
const CAL_PH={
  rut:'ye4,age,deflection_mm,structural_number,measured_rut_increment_mm\n4.5,1,0.85,4.2,3.8\n4.8,2,0.85,4.2,1.1',
  cracking:'crack_prev,crack_next\n0.0,0.40\n0.40,0.86\n0.86,1.40',
  roughness:'measured_iri_increment,iri,structural_number,age,d_msa,d_crack_pct,d_rut_mm\n0.18,1.5,4.2,1,4.5,0.4,3.5',
  skid:'measured_sfc_decrement,sfc,d_msa\n-0.0135,0.55,4.5\n-0.0128,0.537,4.77',
  potholes:'measured_pothole_increment,cracking_pct,d_msa\n0.23,26.4,5.5\n0.66,37.1,5.8'};
function calHint(){ const k=$('c_kind').value; $('c_hint').textContent=CAL_HINTS[k]; $('c_csv').placeholder=CAL_PH[k]; }

function calKpi(label,val){ return `<div class="kpi"><b>${val}</b><span>${label}</span></div>`; }
async function runCalibrate(){
  $('calErr').textContent=''; $('calOut').innerHTML='<p class="muted">fitting&hellip;</p>';
  try{
    const kind=$('c_kind').value;
    const txt=$('c_csv').value.trim();
    const f=$('c_file').files[0];
    let body={kind:kind};
    if(txt){ body.csv=txt; }
    else if(f){
      const n=f.name.toLowerCase();
      if(n.endsWith('.xlsx')||n.endsWith('.pdf')){ body.content_b64=await readB64(f); body.format=n.endsWith('.xlsx')?'xlsx':'pdf'; }
      else { body.csv=await f.text(); }
    } else {
      $('calErr').textContent='paste observations, or upload a .csv / .xlsx / .pdf file.'; $('calOut').innerHTML=''; return;
    }
    const d=await postJSON('/api/calibrate',body);
    let cards='';
    if(kind==='rut'){
      let zero=d.fixed_to_zero.length?`<p class="err">Forced to 0 (physically inadmissible): ${d.fixed_to_zero.join(', ')}</p>`:'';
      cards='<div class="kpis" style="margin-top:8px">'+calKpi('K_rid (densification)',d.k_rid)+calKpi('K_rst (structural)',d.k_rst)+
        calKpi('K_rpd (plastic)',d.k_rpd)+calKpi('R&sup2; ('+d.n+' obs)',d.r_squared)+'</div>'+
        `<p class="muted">RMSE ${d.rmse_before} &rarr; <b>${d.rmse_after}</b> after calibration. ${d.label}</p>`+zero;
    } else if(kind==='cracking'){
      cards='<div class="kpis" style="margin-top:8px">'+calKpi('a (intercept)',d.a)+calKpi('b (slope)',d.b)+
        calKpi('R&sup2; ('+d.n+' pairs)',d.r_squared)+'</div>'+
        `<p class="muted">Fitted recursion: C<sub>i+1</sub> = ${d.a} + ${d.b}&middot;C<sub>i</sub></p>`;
    } else if(kind==='roughness'){
      cards='<div class="kpis" style="margin-top:8px">'+calKpi('env',d.env_coeff)+calKpi('struct a0',d.struct_a0)+
        calKpi('K_crack',d.crack_coeff)+calKpi('K_rut',d.rut_coeff)+calKpi('R&sup2; ('+d.n+' obs)',d.r_squared)+'</div>'+
        `<p class="muted">RMSE ${d.rmse}. ${d.label}</p>`;
    } else if(kind==='skid'){
      cards='<div class="kpis" style="margin-top:8px">'+calKpi('decay_k (polishing rate)',d.decay_k)+
        calKpi('SFC_min (terminal)',d.sfc_min)+calKpi('R&sup2; ('+d.n+' obs)',d.r_squared)+'</div>'+
        `<p class="muted">${d.label}</p>`;
    } else {
      cards='<div class="kpis" style="margin-top:8px">'+calKpi('rate (progression)',d.rate)+
        calKpi('crack threshold %',d.crack_threshold_pct)+calKpi('R&sup2; ('+d.n+' obs)',d.r_squared)+'</div>'+
        `<p class="muted">${d.label}</p>`;
    }
    if(d.skipped){ cards+=`<p class="muted">${d.skipped} row(s) skipped (missing a required column); fitted on ${d.n}.</p>`; }
    $('calOut').innerHTML=cards;
  }catch(e){ $('calErr').textContent=e.message; $('calOut').innerHTML=''; }
}

// ---- Design & PBMC --------------------------------------------------------
async function runDesign(){
  $('dsnErr').textContent=''; $('dsnOut').innerHTML='<p class="muted">designing&hellip;</p>';
  try{
    const body={cbr:+$('d_cbr').value, design_life_years:+$('d_life').value,
      carriageway:$('d_cway').value, vdf:+$('d_vdf').value, method:$('d_method').value};
    if($('d_msa').value!=='') body.design_msa=+$('d_msa').value;
    else body.cvpd=+$('d_cvpd').value;
    const d=await postJSON('/api/design',body); const L=d.layers;
    const t=d.traffic? ' (CVPD &times; VDF via IRC:37)':'';
    const s=d.strains;
    const kpis=
      kpi(d.total_mm+' mm','total above subgrade')+
      kpi(L.bituminous_mm+' mm','bituminous (BC+DBM)')+
      kpi(L.granular_mm+' mm','granular (WMM+GSB)')+
      kpi(Math.round(d.subgrade_modulus_mpa)+' MPa','subgrade modulus')+
      (s? kpi(Math.round(s.governing_life_msa)+' MSA','mechanistic capacity'):'');
    const strainTbl = s?
      '<p class="muted" style="margin-top:12px"><b>Mechanistic check (IITPAVE-style)</b></p>'+
      '<table><tr><th>Criterion</th><th>Strain (&micro;&epsilon;)</th><th>Life (MSA)</th></tr>'+
        '<tr><td>Fatigue (eps_t, bottom of bituminous)</td><td>'+s.tensile_microstrain+'</td><td>'+Math.round(s.fatigue_life_msa)+'</td></tr>'+
        '<tr><td>Rutting (eps_v, top of subgrade)</td><td>'+s.vertical_microstrain+'</td><td>'+Math.round(s.rutting_life_msa)+'</td></tr>'+
        '<tr><th>Governing</th><th>'+s.governing_mode+'</th><th>'+Math.round(s.governing_life_msa)+'</th></tr>'+
      '</table>' : '';
    $('dsnOut').innerHTML=
      '<div class="kpis" style="margin:14px 0">'+kpis+'</div>'+
      pavementSection(L,d)+
      '<table><tr><th>Layer</th><th>Thickness (mm)</th></tr>'+
        row2('BC &mdash; bituminous concrete (wearing)',L.bc_mm)+
        row2('DBM &mdash; dense bituminous macadam (binder)',L.dbm_mm)+
        row2('WMM &mdash; wet-mix macadam (base)',L.wmm_mm)+
        row2('GSB &mdash; granular sub-base',L.gsb_mm)+
        '<tr><th>Total</th><th>'+d.total_mm+'</th></tr></table>'+strainTbl+
      '<p class="muted" style="margin-top:10px">CBR '+d.cbr+'% &middot; design '+
        Math.round(d.design_msa)+' MSA / '+d.design_life_years+'y &middot; method: '+
        d.method+t+'.<br>'+d.rationale+'</p>';
  }catch(e){ $('dsnOut').innerHTML=''; $('dsnErr').textContent=e.message; }
}
function row2(label,val){ return '<tr><td>'+label+'</td><td>'+val+'</td></tr>'; }
function kpi(big,small){ return '<div class="kpi"><b>'+big+'</b><span>'+small+'</span></div>'; }

// To-scale IRC:37 pavement cross-section (BC/DBM/WMM/GSB over subgrade).
function pavementSection(L,d){
  const layers=[
    {n:'BC — bituminous concrete (wearing)', mm:L.bc_mm,  c:'#23262b', t:'#fff'},
    {n:'DBM — dense bituminous macadam (binder)', mm:L.dbm_mm, c:'#414b54', t:'#fff'},
    {n:'WMM — wet-mix macadam (base)', mm:L.wmm_mm, c:'#b5712f', t:'#fff'},
    {n:'GSB — granular sub-base', mm:L.gsb_mm, c:'#d8ad72', t:'#1f2a36'},
  ].filter(x=>x.mm>0);
  const total=d.total_mm||1;
  const W=580, padL=60, padR=16, padT=26, padB=14, stackH=300, subH=66;
  const innerW=W-padL-padR, scale=stackH/total, H=padT+stackH+subH+padB;
  let y=padT, cum=0;
  let body=`<text x="${padL+innerW/2}" y="${padT-9}" font-size="11" text-anchor="middle" fill="#666">Pavement surface</text>`+
    `<line x1="${padL-6}" y1="${padT}" x2="${padL}" y2="${padT}" stroke="#888"/>`+
    `<text x="${padL-9}" y="${padT+4}" font-size="10" text-anchor="end" fill="#666">0</text>`;
  for(const ly of layers){
    const h=Math.max(16, ly.mm*scale); cum+=ly.mm;
    body+=`<rect x="${padL}" y="${y}" width="${innerW}" height="${h}" fill="${ly.c}" stroke="#0d0f12" stroke-width="0.6"/>`+
      `<text x="${padL+innerW/2}" y="${y+h/2+4}" font-size="12" font-weight="600" text-anchor="middle" fill="${ly.t}">${ly.n} &middot; ${ly.mm} mm</text>`+
      `<line x1="${padL-6}" y1="${y+h}" x2="${padL}" y2="${y+h}" stroke="#888"/>`+
      `<text x="${padL-9}" y="${y+h+4}" font-size="10" text-anchor="end" fill="#666">${cum}</text>`;
    y+=h;
  }
  body+=`<rect x="${padL}" y="${y}" width="${innerW}" height="${subH}" fill="#9a9b6f" stroke="#0d0f12" stroke-width="0.6"/>`+
    `<text x="${padL+innerW/2}" y="${y+subH/2+4}" font-size="12" font-weight="600" text-anchor="middle" fill="#1f2a36">Subgrade &middot; CBR ${d.cbr}% &middot; M_R ${Math.round(d.subgrade_modulus_mpa)} MPa</text>`+
    `<text x="16" y="${padT+stackH/2}" font-size="11" text-anchor="middle" fill="#666" transform="rotate(-90,16,${padT+stackH/2})">Depth (mm)</text>`;
  return `<div class="card"><b>Pavement cross-section (IRC:37-2018)</b>`+
    `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;margin-top:8px">${body}</svg>`+
    `<p class="muted">${d.total_mm} mm above subgrade &mdash; ${L.bituminous_mm} mm bituminous over ${L.granular_mm} mm granular. Layer heights drawn to scale.</p></div>`;
}

async function runIITPAVE(){
  $('iitErr').textContent=''; $('iitOut').innerHTML='<p class="muted">analysing&hellip;</p>';
  try{
    const body={e_bituminous:+$('i_ebt').value, e_granular:+$('i_egr').value,
      e_subgrade:+$('i_esg').value, h_bituminous:+$('i_hbt').value, h_granular:+$('i_hgr').value,
      annual_msa:+$('i_msa').value, growth:+$('i_growth').value,
      cumulative_msa:+$('i_cmsa').value, design_msa:+$('i_dmsa').value,
      standard:$('i_standard').value};
    const d=await postJSON('/api/iitpave',body); const s=d.strains;
    const ok=d.adequate==null? '' : (d.adequate?
      '<span style="color:var(--green)">adequate</span>' : '<span class="err">deficient &mdash; overlay needed</span>');
    $('iitOut').innerHTML=
      '<div class="kpis" style="margin:14px 0">'+
        kpi(Math.round(s.governing_life_msa)+' MSA','capacity ('+s.governing_mode+')')+
        kpi((d.remaining_msa!=null?Math.round(d.remaining_msa):'-')+' MSA','remaining')+
        kpi((d.residual_years!=null?d.residual_years:'-')+' yr','residual life')+
        kpi(s.tensile_microstrain+' / '+s.vertical_microstrain,'eps_t / eps_v (&micro;&epsilon;)')+'</div>'+
      '<table><tr><th>Criterion</th><th>Strain (&micro;&epsilon;)</th><th>Life (MSA)</th></tr>'+
        '<tr><td>Fatigue (eps_t)</td><td>'+s.tensile_microstrain+'</td><td>'+Math.round(s.fatigue_life_msa)+'</td></tr>'+
        '<tr><td>Rutting (eps_v)</td><td>'+s.vertical_microstrain+'</td><td>'+Math.round(s.rutting_life_msa)+'</td></tr>'+
      '</table>'+
      '<p class="muted" style="margin-top:10px">'+ok+(ok?' &middot; ':'')+d.rationale+'</p>';
  }catch(e){ $('iitOut').innerHTML=''; $('iitErr').textContent=e.message; }
}

async function runFWD(){
  $('fwdErr').textContent=''; $('fwdOut').innerHTML='<p class="muted">assessing&hellip;</p>';
  try{
    const body={design_msa:+$('f_design').value};
    const f=$('f_file').files[0];
    const txt=$('f_csv').value.trim();
    if(f){
      const n=f.name.toLowerCase();
      if(n.endsWith('.xlsx')||n.endsWith('.pdf')){ body.content_b64=await readB64(f); body.format=n.endsWith('.xlsx')?'xlsx':'pdf'; }
      else { body.csv=await f.text(); }
    } else if(txt){ body.csv=txt; }
    else { throw new Error('paste sections, or upload a .csv / .xlsx / .pdf file.'); }
    const d=await postJSON('/api/fwd',body);
    const rows=d.sections.map(r=>{
      const flag=r.overlay_required? '<span class="err">YES</span>':'<span style="color:var(--green)">no</span>';
      const star=r.confirm_with_iitpave? ' <span style="color:var(--amber)">*</span>':'';
      return '<tr><td>'+r.section_id+'</td><td>'+r.tensile_microstrain+'</td><td>'+r.vertical_microstrain+
        '</td><td>'+Math.round(r.remaining_fatigue_msa)+'</td><td>'+Math.round(r.remaining_rutting_msa)+
        '</td><td>'+Math.round(r.remaining_life_msa)+'</td><td style="text-align:left">'+flag+star+'</td></tr>';
    });
    const header='<tr><th>Section</th><th>eps_t</th><th>eps_v</th><th>Fatigue</th><th>Rutting</th>'+
        '<th>Remaining</th><th style="text-align:left">Overlay</th></tr>';
    const extra='<div class="kpis" style="margin:14px 0">'+
        kpi(d.n_sections,'sub-sections')+
        kpi(d.n_overlay_required,'overlay required')+
        kpi(d.borderline_sections.length,'confirm w/ IITPAVE')+
        kpi(Math.round(d.min_remaining_msa)+' MSA','min remaining life')+'</div>'+
      '<p class="muted" style="margin:4px 0">'+d.verdict+
        (d.skipped?` &middot; ${d.skipped} row(s) skipped (no e_bituminous)`:'')+'</p>';
    renderPaged('fwdOut', header, rows, extra);
  }catch(e){ $('fwdOut').innerHTML=''; $('fwdErr').textContent=e.message; }
}

async function runPBMC(){
  $('pbmcErr').textContent=''; $('pbmcOut').innerHTML='<p class="muted">pricing&hellip;</p>';
  try{
    const body={iri:+$('p_iri').value, rut:+$('p_rut').value, crack:+$('p_crack').value,
      msa:+$('p_msa').value, growth:+$('p_growth').value, zone:$('p_zone').value,
      length_km:+$('p_len').value, id:'SEGMENT', term_years:+$('p_term').value,
      performance_pci:+$('p_pci').value, routine_rate_per_km_year:+$('p_rate').value,
      escalation_rate:+$('p_esc').value, contingency_pct:+$('p_cont').value,
      overhead_pct:+$('p_oh').value, discount_rate:+$('p_disc').value};
    const e=await postJSON('/api/pbmc',body);
    const comp=e.compliant? '<span style="color:var(--green)">compliant</span>'
      : '<span class="err">below service level (min PCI '+e.min_pci+')</span>';
    const rows=e.years.map(y=>'<tr><td>'+y.year+'</td><td>'+y.routine.toFixed(1)+'</td><td>'+
        y.periodic.toFixed(1)+'</td><td>'+y.initial.toFixed(1)+'</td><td>'+
        y.total.toFixed(1)+'</td><td style="text-align:left">'+(y.treatments.join(', ')||'')+'</td></tr>');
    rows.push('<tr><th>Total</th><th>'+e.total_routine.toFixed(1)+'</th><th>'+
        e.total_periodic.toFixed(1)+'</th><th>'+e.initial_rectification.toFixed(1)+
        '</th><th>'+e.contract_value.toFixed(1)+'</th><th></th></tr>');
    const header='<tr><th>Yr</th><th>Routine</th><th>Periodic</th><th>Initial</th>'+
        '<th>Total</th><th style="text-align:left">Treatments</th></tr>';
    $('pbmcOut').innerHTML=
      '<div class="kpis" style="margin:14px 0">'+
        kpi(e.contract_value.toFixed(1),'contract value')+
        kpi(e.npv.toFixed(1),'NPV')+
        kpi(e.cost_per_km.toFixed(1),'per km')+
        kpi(e.initial_rectification.toFixed(1),'handover rectification')+'</div>'+
      '<p class="muted">'+e.term_years+'-yr term &middot; performance: '+comp+'</p>'+
      '<div id="pbmcTbl"></div>'+
      '<p class="muted" style="margin-top:10px">'+e.rationale+'</p>';
    renderPaged('pbmcTbl', header, rows, '');
  }catch(e){ $('pbmcOut').innerHTML=''; $('pbmcErr').textContent=e.message; }
}

function _lcaBody(){
  return {iri:+$('l_iri').value, rut:+$('l_rut').value, crack:+$('l_crack').value,
    msa:+$('l_msa').value, growth:+$('l_growth').value, zone:$('l_zone').value,
    id:'SEGMENT', length_km:+$('l_len').value, years:+$('l_years').value,
    width_m:+$('l_width').value, discount_rate:+$('l_disc').value};
}
const DECC={ROUTINE:'#1a9850', PREVENTIVE:'#f0a000', OVERLAY:'#d73027', RECONSTRUCTION:'#7b1fa2'};
async function runLCA(){
  $('lcaErr').textContent=''; $('lcaOut').innerHTML='<p class="muted">projecting life cycle&hellip;</p>';
  try{
    const d=await postJSON('/api/lca',_lcaBody());
    const rows=d.years.map(y=>{
      const dc=DECC[y.decision]||'#1f2a36';
      return '<tr><td>'+y.year+'</td><td>'+y.cumulative_msa+'</td><td>'+y.iri+'</td><td>'+y.rut+
        '</td><td>'+y.crack+'</td><td>'+y.pci+'</td><td style="text-align:left;color:'+dc+';font-weight:600">'+
        y.decision+(y.deferred?' *':'')+'</td><td>'+y.cost_lakh.toFixed(1)+'</td></tr>';
    });
    const header='<tr><th>Yr</th><th>Cum.MSA</th><th>IRI</th><th>Rut</th>'+
      '<th>Crack%</th><th>PCI</th><th style="text-align:left">Decision</th><th>Cost(&#8377;L)</th></tr>';
    const extra='<div class="kpis" style="margin:14px 0">'+
        kpi('&#8377;'+d.total_cost_lakh.toFixed(1)+'L','total life-cycle cost')+
        kpi('&#8377;'+d.npv_lakh.toFixed(1)+'L','NPV')+
        kpi('&#8377;'+d.euac_lakh.toFixed(1)+'L/yr','EUAC')+
        kpi(d.n_overlay+' / '+d.n_reconstruction,'overlays / rebuilds')+'</div>'+
      '<div style="margin-bottom:6px">'+
        '<button class="go" style="margin:0 8px 0 0" onclick="downloadLCA(\'xlsx\')">Download XLSX</button>'+
        '<button class="go" style="margin:0" onclick="downloadLCA(\'pdf\')">Download PDF</button></div>'+
      '<p class="muted" style="margin:4px 0 0">'+d.rationale+' (* deferred by the minimum interval)</p>';
    renderPaged('lcaOut', header, rows, extra);
  }catch(e){ $('lcaOut').innerHTML=''; $('lcaErr').textContent=e.message; }
}
async function downloadLCA(fmt){
  try{
    const body=Object.assign({report:'lca'}, _lcaBody());
    const r=await fetch('/api/export?format='+fmt,{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok){ const j=await r.json(); throw new Error(j.error||'export failed'); }
    const blob=await r.blob(); const url=URL.createObjectURL(blob);
    const a=document.createElement('a'); a.href=url; a.download='rams_lca_matrix.'+fmt;
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  }catch(e){ $('lcaErr').textContent=e.message; }
}

// ---- cross-tab reuse of the uploaded survey -------------------------------
function refreshReuse(){
  document.querySelectorAll('.reuse').forEach(b=>{ b.style.display = SURVEY ? 'inline-block' : 'none'; });
  const cb=$('calReuse'); if(cb) cb.style.display = UPLOAD_RAW ? 'inline-block' : 'none';
}
function useUploadedPBMC(){
  const s=worstSeg(); if(!s){ alert('Upload a survey in "Load a segment" (Segment Forecast tab) first.'); return; }
  _setVal('p_iri',s.base_iri); _setVal('p_rut',s.base_rut); _setVal('p_crack',s.base_crack);
  _setVal('p_msa',s.annual_msa); _setVal('p_growth',s.traffic_growth_rate);
  _setVal('p_zone',s.monsoon_zone); _setVal('p_len',s.length_km); runPBMC();
}
function useUploadedLCA(){
  const s=worstSeg(); if(!s){ alert('Upload a survey in "Load a segment" (Segment Forecast tab) first.'); return; }
  _setVal('l_iri',s.base_iri); _setVal('l_rut',s.base_rut); _setVal('l_crack',s.base_crack);
  _setVal('l_msa',s.annual_msa); _setVal('l_growth',s.traffic_growth_rate);
  _setVal('l_zone',s.monsoon_zone); _setVal('l_len',s.length_km); runLCA();
}
function useUploadedNetwork(){
  if(!SURVEY){ alert('Upload a survey in "Load a segment" first.'); return; }
  IMPORTED=SURVEY; runNetwork();
}
function useUploadedCalibrate(){
  if(!UPLOAD_RAW){ alert('Upload a .csv in "Load a segment" first (XLSX/PDF cannot prefill calibration data).'); return; }
  _setVal('c_csv', UPLOAD_RAW); calHint();
}

// auto-run the segment forecast on load so the page isn't empty
toggleHdm4();
calHint();
runForecast();
refreshReuse();
</script>
</body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
