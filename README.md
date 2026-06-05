# RAMS — Indian Pavement Deterioration Engine

A deterministic, year-by-year pavement deterioration forecasting engine for an
Indian Road Asset Management System (RAMS). It reads a segment's baseline NSV
condition and projects IRI (roughness), cracking and rutting forward under
cumulative traffic loading (MSA), pavement age and a monsoon environmental
penalty, then combines them into an **IRC:82 composite Pavement Condition Score
(PCI, 0–4)** and recommends a MoRTH maintenance treatment.

This repository is the productionised result of a cross-functional review of the
original prototype (Solution Architecture, UI/UX, QA, Security, Performance).
See [Cross-functional analysis](#cross-functional-analysis) for what each
discipline changed and why.

---

## Quick start

No third-party dependencies are required for the core engine, CLI, reports or
tests — it runs on a stock Python 3.8+ interpreter (validated on 3.9).

```bash
# Interactive web dashboard (forecast + treatment lifecycle + budget optimiser)
python -m rams.server            # then open http://127.0.0.1:8000

# Single segment (defaults reproduce the spec example: HIGH zone, 4.5 MSA, 6% growth)
python -m rams.cli --id NH66-KL-012 --years 10

# Emit a self-contained HTML report + JSON export
python -m rams.cli --id NH66-KL-012 --html forecast.html --json forecast.json

# Forecast and triage an entire network from a CSV of NSV records
python -m rams.cli --csv examples/sample_network.csv --summary

# Run the test suite (55 tests)
python -m unittest discover -s tests
```

Python API:

```python
from rams import IndianPavementDeteriorationEngine, build_maintenance_plan

engine = IndianPavementDeteriorationEngine(
    base_iri=1.5, base_rut=2.0, base_crack=0.0,
    annual_msa=4.5, traffic_growth_rate=0.06, monsoon_zone="HIGH",
)
timeline = engine.run_lifecycle_forecast(horizon_years=10)
plan = build_maintenance_plan(timeline)
print(plan.rationale)
```

---

## Verified 10-year output

> **QA finding (important).** The "Output Vector Analysis" table in the original
> brief does **not** match the formulas in the prototype code. Tracing year 1 by
> hand, the rutting formula yields ≈3.5 mm (not 5.8) and every sub-score is 4.0,
> so the PCI is 4.00 (not 3.73). The brief's table was illustrative and
> internally inconsistent. Below is the **actual, reproducible** output of the
> documented formulas, locked as golden values in the test suite.

Input: `base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5, growth=6%, zone=HIGH`

| Year | Cum. MSA | IRI  | Rut (mm) | Crack (%) | PCI  | Flag       |
|-----:|---------:|-----:|---------:|----------:|-----:|------------|
| 1    | 4.50     | 1.66 | 3.5      | 0.5       | 4.00 | ROUTINE    |
| 2    | 9.27     | 1.83 | 5.0      | 0.9       | 4.00 | ROUTINE    |
| 3    | 14.33    | 2.01 | 6.5      | 1.4       | 3.86 | ROUTINE    |
| 4    | 19.69    | 2.21 | 8.2      | 8.6       | 3.54 | ROUTINE    |
| 5    | 25.37    | 2.42 | 9.9      | 17.0      | 3.02 | **PREVENTIVE** |
| 6    | 31.39    | 2.65 | 11.7     | 26.4      | 2.51 | **PREVENTIVE** |
| 7    | 37.77    | 2.89 | 13.5     | 37.1      | 2.29 | STRUCTURAL |
| 8    | 44.54    | 3.16 | 15.5     | 48.8      | 2.06 | STRUCTURAL |
| 9    | 51.71    | 3.44 | 17.5     | 61.6      | 1.85 | STRUCTURAL |
| 10   | 59.31    | 3.74 | 19.6     | 75.5      | 1.78 | STRUCTURAL |

**Decision:** the *window of maximum return* opens in **year 5** (PCI 3.02) and
closes after **year 6** (PCI 2.51). Apply MoRTH Section 514 microsurfacing in
years 5–6; delay into year 7 and only structural mill & overlay (~5× cost)
remains.

---

## Maintenance decision bands (Section 4)

| PCI band            | Flag         | Treatment                         | MoRTH ref            |
|---------------------|--------------|-----------------------------------|----------------------|
| `PCI ≥ 3.20`        | ROUTINE      | Routine crack sealing             | Section 3000         |
| `2.50 ≤ PCI < 3.20` | PREVENTIVE   | Microsurfacing (window of return) | Section 514          |
| `PCI < 2.50`        | STRUCTURAL   | Mill & overlay (cheap fixes locked out) | Section 500    |

`apply_reset()` on the engine models the post-treatment "reset" of condition
state, so a treated segment can be re-simulated forward from its restored
condition. Treatment reset targets and relative costs live in
`TREATMENT_CATALOG` (`rams/maintenance.py`).

---

## Project layout

```
rams/
  config.py       Calibration constants, MonsoonZone, IRC:82 scoring, input bounds
  models.py       SegmentInput / YearResult with hard validation (trust boundary)
  engine.py       IndianPavementDeteriorationEngine (deterioration laws + scoring)
  maintenance.py  Decision bands, MoRTH treatment catalog, plan builder
  lifecycle.py    Treatment-aware simulation (applies catalog reset values)
  optimize.py     Multi-year budget optimisation across the network
  batch.py        Defensive CSV ingestion + streaming network forecaster
  report.py       CSV / JSON / self-contained HTML (inline-SVG) reporting
  api.py          Pure request/response functions (testable, no HTTP)
  server.py       Stdlib web server + embedded interactive dashboard (SPA)
  cli.py          Command-line interface
tests/            unittest suite (55 tests, golden values, edge & security cases)
examples/         sample_network.csv
docs/             ARCHITECTURE.md (deep-dive design rationale)
```

---

## Cross-functional analysis

### Senior Solution Architect
- **Separation of concerns.** The single 130-line prototype is decomposed into
  config / models / engine / maintenance / batch / report / cli. The
  deterioration *math* knows nothing about I/O, scoring weights or treatments.
- **Calibration is data, not code.** Every coefficient lives in an immutable
  `Calibration` dataclass. Recalibrating for State Highways vs National Highways
  is a config change, never an edit to the simulation loop.
- **Typed contracts.** `SegmentInput` (in) and `YearResult` (out) are explicit
  dataclasses, so ingestion, engine, reporting and the future budget optimiser
  share one stable schema.

### Senior UI/UX Designer
- **One screen, one story.** The HTML report leads with a colour-coded verdict
  banner, then a PCI lifecycle chart with the three decision bands shaded
  (green/amber/red), then the detail table — decision first, evidence after.
- **Consistent semantic colour** across the terminal table, the chart bands and
  the HTML, so "amber = act now" means the same thing everywhere. `NO_COLOR` and
  non-TTY fallbacks are respected.
- **Offline by design.** The report is a single self-contained HTML file with an
  inline SVG chart — no CDN, no JS framework — so it opens on an air-gapped
  PWD/NHAI workstation and can be emailed as one artifact.

### Lead QA
- **Found and documented a spec defect:** the brief's sample output table does
  not match its own formulas (see above). We implement the formulas faithfully
  and publish the real numbers.
- **41 tests, zero external deps** (`unittest`): golden 10-year lifecycle,
  determinism, deterioration caps, IRC:82 score floors/boundaries, the cracking
  lag-phase boundary, monsoon sensitivity, growth compounding, treatment reset,
  decision-band classification, plan edge cases, CSV row isolation and HTML
  escaping.

### Security Lead
- **Hard trust boundary.** All external data passes through `SegmentInput.validate()`:
  NaN/inf rejected, ranges enforced, non-numeric coercion caught.
- **Fail loud, not silent.** The prototype's `dict.get(zone, 'MEDIUM')` silently
  mapped typos to MEDIUM; `MonsoonZone.from_str` now raises on unknown zones.
- **Ingestion hardening.** CSV is read with the stdlib `csv` module (no formula
  execution), a `MAX_ROWS` cap bounds resource use, and bad rows are isolated
  rather than aborting the import.
- **Output hardening.** Untrusted identifiers/strings are `html.escape()`d before
  templating — a `segment_id` of `<script>…</script>` cannot become stored XSS in
  a report a planner opens in a browser.

### Performance Engineer
- **Zero-dependency core.** Replaced `numpy.power` with `math.pow`; the engine no
  longer needs numpy/pandas (which were not even installed in the target env).
  Cost is O(horizon) per segment.
- **Constant-memory network runs.** `forecast_network()` is a generator, so a
  500k-segment NSV import streams to disk/DB without materialising every
  timeline. The workload is embarrassingly parallel (per-segment independent),
  ready for `multiprocessing`/`concurrent.futures` if a network ever needs it.
- **Bounded work.** `horizon_years` and `MAX_ROWS` caps prevent a pathological
  input from turning into an unbounded loop.

---

## Network triage semantics

`network_summary()` buckets each segment by its **worst projected state within
the horizon** (terminal-state triage): `window_expired` (will reach structural
failure if untreated) takes priority over `needs_preventive`, which takes
priority over `routine_only`. A segment counted as `window_expired` still has an
earlier preventive window — that earlier year is what `--csv` prints per row.

---

## Web dashboard

`python -m rams.server` starts a dependency-free local web app (stdlib
`http.server`) at **http://127.0.0.1:8000** with two tabs:

- **Segment Forecast** — enter a segment's condition and see the IRC:82 PCI
  curve with decision-band shading, plus an **untreated vs managed** comparison
  (the managed line applies MoRTH treatments and resets condition), the timeline
  table, and the treatments applied with their cost.
- **Network & Budget** — runs the multi-year budget optimiser over the demo
  network and shows the per-year treatment schedule, spend-vs-budget bars,
  avoided structural cost, and which segments go unfunded.

Security: binds to loopback only, caps request bodies, sets `nosniff` +
`X-Frame-Options` + a restrictive CSP, and maps bad input to HTTP 400 without
leaking internals. The page is fully self-contained (inline CSS/JS, no CDN).

## MoRTH treatment lifecycle (reset values)

`rams/lifecycle.py` simulates a *managed* asset: when PCI enters a maintenance
band, the recommended treatment from `TREATMENT_CATALOG` is applied, condition
is reset to the catalog's `reset_*` values, and the simulation continues.

```python
from rams import SegmentInput, MonsoonZone, simulate_managed_lifecycle
seg = SegmentInput(1.5, 2.0, 0.0, 4.5, 0.06, MonsoonZone.HIGH, length_km=12.0)
managed = simulate_managed_lifecycle(seg, horizon_years=10)
print(managed.total_cost, [i.year for i in managed.interventions])
```

For the spec segment the managed trajectory ends at **PCI 3.09** vs **1.78**
untreated, after **2** microsurfacing treatments.

## Multi-Year Budget Optimization

`rams/optimize.py` allocates a constrained **annual** budget across competing
segments. Each at-risk segment has a preventive window `[start, deadline]`;
funding microsurfacing in-window avoids the ~5× structural mill & overlay later
(the *avoided premium* = realised benefit). Under scarcity the optimiser ranks
by **traffic exposure** (`annual_msa × length_km`), respecting each segment's
deadline and the per-year budget cap. It is a transparent greedy heuristic
(auditable for public spend), with hooks to swap in an ILP solver later.

```python
from rams import optimize_budget, BudgetParams
from rams.batch import forecast_network
forecasts = list(forecast_network(segments, 10))
plan = optimize_budget(segments, forecasts, BudgetParams(annual_budget=600))
print(plan.rationale)   # funded vs unfunded, avoided premium
```

> **Honest accounting:** "avoided cost" counts only *funded* segments —
> unfunded ones still incur structural cost, so they save nothing. (An earlier
> draft naively reported `do_nothing − spend`, which overstated savings; fixed.)
