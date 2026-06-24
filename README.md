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

**Documentation:**
- [User Guide & Handbook](docs/USER_GUIDE.md) — how to use every feature, the data
  each one needs, accepted formats, and how the calculations are done.
- **Client presentation** — regenerate the deck with
  `python scripts/make_ppt.py` → `docs/RAMS_Client_Presentation.pptx`.

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

# Same network from an XLSX survey/condition workbook, or a PDF condition report
python -m rams.cli --xlsx survey.xlsx --summary
python -m rams.cli --pdf condition_survey.pdf --summary

# Run the test suite
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

## Input data formats (CSV / XLSX / PDF)

The portal ingests a road network from three formats, all funnelled through the
same `SegmentInput.validate()` trust boundary with per-record error isolation
(one bad segment never aborts the import):

| Format | Loader | Web portal | Notes |
|--------|--------|-----------|-------|
| CSV    | `ingest_segments_csv(path)`   | upload `.csv`  | header = `REQUIRED_COLUMNS`, or an NSV chainage survey |
| XLSX   | `ingest_segments_xlsx(path)`  | upload `.xlsx` | all worksheets scanned; NSV surveys merged by chainage |
| PDF    | `ingest_segments_pdf(path)`   | upload `.pdf`  | text layer of a digitally-generated condition report |

```python
from rams import ingest_segments            # dispatches on .csv / .xlsx / .pdf
result = ingest_segments("examples/sample_network.csv")
print(len(result.segments), result.errors)
```

In the **web dashboard** (`python -m rams.server`), the *Network & Budget* tab
has an **Import pavement-databank network** card: pick a `.csv`, `.xlsx` or
`.pdf` file and the parsed segments replace the demo network for the optimiser.
The *Segment Forecast* tab accepts **multiple files at once** and merges them (see
[NSV chainage-survey ingestion](#nsv-chainage-survey-ingestion-vendor-schemas)).

**Large files.** Browser uploads stream as a raw body to `/api/upload?format=…`
(no base64, no in-memory JSON), so a multi-tens-of-MB NSV/FWD export goes
straight through — up to a 128 MB transport cap. The **dashboard** renders up to
5000 segments; a raw per-chainage survey with hundreds of thousands of rows
should go through the **CLI batch** path (`python -m rams.cli --csv <file>`),
which row-streams the whole network at constant memory. (CSV is always
row-streamed; XLSX/PDF are buffered up to a 64 MB blob cap.)

**Security.** XLSX is a ZIP of XML parts, read with the stdlib `zipfile` +
`ElementTree`; any internal part carrying a `DOCTYPE`/DTD is rejected up front —
blocking external-entity (XXE) disclosure and "billion-laughs" expansion with no
third-party hardened parser. PDF reads the *text layer only* (stdlib
`FlateDecode` + text operators, or `pypdf` if installed) with a size cap;
scanned/image PDFs have no text layer and raise a clear "OCR required" error.
Blob sizes and ZIP member counts are bounded (`MAX_BLOB_BYTES`, `_XLSX_MAX_PARTS`).

## MLIT-PMS Maintenance Control Index (paper cross-reference)

`rams/mci.py` implements the Japanese **MCI**, the integrated condition index the
paper uses to time overlays — reported *alongside* the IRC:82 PCI, never feeding
the MoRTH bands:

```
MCI = 10 − 1.48·C^0.3 − 0.29·D^0.7 − 0.47·σ^0.2
   C = cracking %, D = rut depth (mm), σ = longitudinal roughness (mm)
Bands:  MCI > 5 desirable · 3–5 needs repair · < 3 immediate repair
Overlay trigger: cut-and-overlay once rut depth exceeds 30 mm (RUT_OVERLAY_THRESHOLD_MM)
```

```python
from rams import compute_mci, mci_band
mci = compute_mci(rut_mm=6.0, cracking_pct=3.0, roughness_mm=4.0)
print(mci, mci_band(mci).value)
```

The segment-forecast tab of the dashboard shows the per-year MCI and management
band next to the PCI table. **Fidelity caveat:** the engine carries roughness as
IRI (mm/m), so when no explicit σ is supplied IRI is used as a proxy — the MCI is
then an approximation, labelled as such. Supply `roughness_mm` for a faithful MCI.

## HDM-4 mechanistic rut model (selectable)

The rutting law is pluggable. The **default** is the IRC:82-style power law
(unchanged, golden-locked). Select **HDM-4** to forecast rutting with the
mechanistic Δ*RDM* model from the calibration paper, which splits the annual rut
increment into three physically-distinct components:

```
ΔRDM = K_rid·RDO(densification) + K_rst·RDST(structural) + K_rpd·RDPD(plastic)
  RDO  = a0·YE4^(a1 + a2·DEF)·SNP^a3·COMP^a4      (one-off, year 1)
  RDST = a0·SNP^a1·YE4^a2·COMP^a3                  (structural, every year)
  RDPD = a0·CDS^3·YE4·Sh^a1·HS^a2                  (asphalt plastic flow)
```

`YE4` is the year's MSA; **`DEF` is FWD/Benkelman deflection and `SNP` the
structural number** — so structural (FWD) data drives the prediction. The two
paper-calibrated presets ship as `HDM4_DENSE_GRADED` (Krid=3.26, Krst=3.11,
Krpd=0.59) and `HDM4_POROUS` (Krid=1.48, Krst=0.83, Krpd=0). Every coefficient is
overridable — **these are Japanese NH calibrations and must be re-calibrated
against Indian NSV+FWD data before production use.**

```bash
# CLI: HDM-4 forecast driven by a measured FWD deflection + structural number
python -m rams.cli --model hdm4 --pavement dense --deflection 0.9 --snp 4.0 \
    --design-msa 30 --years 10
```

```python
from rams import forecast_segment, SegmentInput, MonsoonZone, RutModelType, HDM4_POROUS
seg = SegmentInput(1.5, 2.0, 0.0, 4.5, 0.06, MonsoonZone.HIGH,
                   deflection_mm=0.9, structural_number=4.0)
timeline = forecast_segment(seg, 10, rut_model=RutModelType.HDM4)   # or hdm4_calibration=HDM4_POROUS
```

The web dashboard's *Segment Forecast* tab has a **Rut model** selector; choosing
HDM-4 reveals the pavement preset and the FWD-deflection / structural-number
inputs, and the result adds a **per-year component-breakdown table**. The
*Network & Budget* tab has the same selector — under HDM-4 every segment is
forecast from **its own** imported FWD deflection / structural number, so the
budget windows (and the funded/unfunded split) reflect each segment's real
structural condition, not just its surface state.

### FWD → structural number (auto back-calculation)

A raw FWD/Benkelman survey carries a rebound *deflection* but no structural
number. `rams/fwd.py` back-calculates SNP from deflection
(`SNP = a·DEF^(−b)`, monotonic — weaker pavement deflects more):

```python
from rams import snp_from_deflection
snp_from_deflection(1.0)   # -> 3.2   (≈5.0 at 0.5 mm, ≈2.5 at 1.5 mm)
```

This happens automatically on import: a CSV/XLSX/PDF row with `deflection_mm` but
no `structural_number` gets SNP derived for it, so a bare deflection survey drives
the HDM-4 model directly. In the CLI use `--derive-snp` (with `--deflection`); in
the Segment Forecast tab tick **derive SNP from FWD**. An explicitly-supplied SNP
is never overridden.

### FWD structural data in the algorithm

FWD/Benkelman rebound deflection (`deflection_mm`, the HDM-4 `DEF`) and the
structural number (`structural_number`, `SNP`) feed the HDM-4 densification and
structural terms directly — a weaker/wetter pavement (higher deflection, lower
SNP) ruts faster. These columns are importable from CSV/XLSX/PDF (aliases:
`deflection`, `fwd_deflection`, `benkelman`, `snp`, `sn`), so a deflection survey
merges straight into the network. Deflection also drives an explicit IRC:81
structural-strengthening trigger (below).

## IRC:37 traffic loading (CVPD / VDF → MSA)

Indian PMS keys structural design and the fatigue trigger to **MSA**, derived from
**commercial vehicles/day (CVPD)** and a **Vehicle Damage Factor (VDF)** that bakes
in overloading — not US axle-load spectra. `rams/traffic.py` implements the IRC:37
cumulative-repetitions formula:

```
N = (365 · ((1+r)^n − 1)/r) · CVPD · D · VDF / 1e6
   D = lane-distribution factor (IRC:37: 0.75 two-lane, 0.40 dual carriageway, …)
   VDF = standard axles per commercial vehicle (IRC:37 indicative defaults by
         terrain × CVPD band; replace with an axle-load survey)
```

```bash
# Drive the forecast from CVPD/VDF: derives annual MSA + the IRC:37 design MSA
# (which feeds the residual-life / fatigue-life trigger), e.g. a 4-lane NH:
python -m rams.cli --cvpd 4500 --vdf 4.5 --carriageway two_lane --design-life 15 \
    --required-residual-msa 20
# -> annual 5.54 MSA, 15y design 129 MSA
```

```python
from rams import design_msa, default_vdf
t = design_msa(4500, vdf=default_vdf(4500, "plain"), growth_rate=0.05,
               design_life_years=15, lane_distribution=0.75)
print(t.annual_msa, t.design_msa)
```

The dashboard exposes this at `POST /api/traffic` (`{cvpd, vdf|terrain, carriageway,
design_life_years, growth}` → annual + design MSA).

## Intervention triggers (Indian IRC thresholds, incl. MSA)

`rams/triggers.py` evaluates each forecast year against explicit Indian
thresholds and reports the first crossing of each — separate from, and
complementary to, the IRC:82 PCI bands:

| Trigger    | Default | Severity split | Reference |
|------------|---------|----------------|-----------|
| Rutting    | 10 mm / 20 mm | functional → structural | IRC:82 / IRC:81 |
| Cracking   | 10% / 20% area | functional → structural | IRC:82 / IRC:37 |
| Roughness  | IRI 2.5 / 4.0 mm/m | functional → structural | IRC:SP:16 / NH O&M |
| Deflection | 1.0 mm rebound | structural | IRC:81 |
| **Traffic (MSA)** | **80% of design MSA** | **structural** | **IRC:37** |

The **MSA fatigue-life trigger** fires when cumulative traffic since the last
renewal reaches `design_life_fraction` of the section's design MSA (`--design-msa`),
i.e. the pavement has consumed its IRC:37 fatigue life and is due for structural
renewal *regardless of surface condition*. Triggers appear in the CLI output, the
`/api/forecast` response, and the dashboard.

## Surface-distress models (cracking, roughness, skid, potholes)

The paper flags cracking, roughness and skid as the next models to calibrate
after rutting. RAMS now carries **four** further distresses on the **same
selectable, calibratable footing as rutting** (`rams/distress.py`), each off by
default so the golden IRC:82 behaviour is unchanged:

- **Cracking** — `CrackModelType.MLIT`: the paper's empirical recursion
  `C_{i+1} = a + b·C_i` (dense `0.40 + 1.16·C`, porous `0.40 + 1.10·C`).
- **Roughness** — `RoughnessModelType.HDM4`: the HDM-4 incremental IRI model,
  **coupled to the year's rut and crack increments** + structural (SNP) +
  environmental terms.
- **Skid** — `SkidModelType.HDM4`: aggregate-polishing decay of the side-force
  coefficient toward a terminal SFC (skid *decreases* with traffic).
- **Potholes** — `PotholeModelType.HDM4`: crack-initiated potholing — starts once
  cracking passes a threshold (default 20%), then grows with traffic.

```bash
python -m rams.cli --crack-model mlit --roughness-model hdm4 \
    --skid-model hdm4 --pothole-model hdm4 --pavement dense
```

All are selectable in the dashboard's Segment Forecast tab (skid and potholing
add their own trajectory tables and fire their own IRC:SP:16 / IRC:82 triggers).

### Load a segment from a survey / FWD file

The Segment Forecast tab has a **Load a segment from a survey / FWD file** card:
upload an NSV/FWD `.csv`, `.xlsx` or `.pdf` with the standard columns and the first
segment fills the form (including FWD deflection → derived SNP) and forecasts
automatically. It reuses the same hardened `/api/ingest` parser as the network
import.

## Calibrating the models to your own field data

`rams/calibrate.py` fits **all three** deterioration models by OLS regression —
the paper's method — pure stdlib (a small Gaussian-elimination solver, no numpy):

| `--calibrate-kind` | Fits | Method |
|--------------------|------|--------|
| `rut` (default)    | `K_rid / K_rst / K_rpd` | 3-predictor OLS; the paper's `K_rpd<0`→0 refit is reproduced |
| `cracking`         | MLIT `a, b`             | linear regression of `C_{i+1}` on `C_i` |
| `roughness`        | HDM-4 `env, a0, K_c, K_r` | 4-predictor OLS on the roughness components |
| `skid`             | HDM-4 `decay_k`          | single-predictor OLS (polishing decay) |
| `potholes`         | HDM-4 `rate`            | single-predictor OLS (crack-initiated progression) |

```bash
# Rutting (default kind)
python -m rams.cli --calibrate-csv examples/sample_observations.csv --calibrate-out cal.json
# Cracking recursion
python -m rams.cli --calibrate-csv crack_pairs.csv --calibrate-kind cracking
```

```python
from rams import calibrate_hdm4_rut, calibrate_mlit_cracking, load_observations_csv
res = calibrate_hdm4_rut(load_observations_csv("examples/sample_observations.csv"))
print(res.summary())          # Krid/Krst/Krpd, R², RMSE before→after
forecast = res.calibration    # a ready-to-use HDM4RutCalibration

crk = calibrate_mlit_cracking([(0.0, 0.40), (0.40, 0.864), (0.864, 1.40)])
print(crk.a, crk.b)           # -> ~0.40, ~1.16
```

CSV columns by kind — rut: `ye4, age, deflection_mm, structural_number,
measured_rut_increment_mm`; cracking: `crack_prev, crack_next`; roughness:
`measured_iri_increment, iri, structural_number, age, d_msa, d_crack_pct,
d_rut_mm`. The dashboard's **Calibrate & Residual Life** tab does all three from a
pasted/uploaded CSV (pick the model). `examples/sample_observations.csv` is a
synthetic rut set generated from K=(2.50, 1.80, 0.40) — calibration recovers it.

## Remaining structural (fatigue) life — IRC:81 / IRC:37

`rams/residual.py` produces the scalar a BOT/HAM concessionaire needs: how much
structural life is left, and whether a section meets its **handback**
requirement. It reports the **governing (minimum)** of two views:

- **IRC:81 deflection capacity** — `N_allow = a·DEF^(−b)`: measured FWD/Benkelman
  deflection → cumulative MSA the current structure can still carry.
- **IRC:37 traffic budget** — `design_MSA − cumulative_MSA` already carried.

Remaining life in **years** is found by growing the current annual MSA forward at
the traffic growth rate until it consumes the governing remaining MSA.

```bash
python -m rams.cli --deflection 1.1 --design-msa 30 --cumulative-msa 12 \
    --required-residual-msa 20 --residual
# -> governing remaining MSA, residual years, and a PASS/MARGINAL/FAIL handback
#    verdict (FAIL also prints the deflection an overlay must achieve to comply)
```

```python
from rams import remaining_fatigue_life, handback_assessment
r = remaining_fatigue_life(deflection_mm=1.1, annual_msa=4.5, traffic_growth_rate=0.06,
                           cumulative_msa=12.0, design_msa=30.0)
print(handback_assessment(r, required_residual_msa=20.0).rationale)
```

The **Calibrate & Residual Life** dashboard tab exposes both interactively, and
the **Network & Budget** tab runs this per segment: give it a design MSA and a
handback requirement, and it flags every asset that **fails handback** (and lists
which ones need strengthening before the concession ends).

## AASHTOWare-style three-layer workflow (design → field data → PBMC)

RAMS isolates the three concerns a Performance-Based Maintenance Contract (PBMC)
estimate needs, so each can be calibrated and audited independently:

1. **Engineering / design layer** — IRC:37 structural design from design inputs
   (CBR, design traffic MSA, design life) → pavement section. *No field data
   required; this is the "as-designed" road.* (`design.py`)
2. **Field-data layer** — NSV condition, FWD deflection, traffic census, monsoon
   zone → deterioration forecast, residual life, predictive maintenance.
   (`ingest.py`, `engine.py`, `residual.py`, `lifecycle.py`)
3. **Financial-forecast layer** — turns the managed-lifecycle forecast into a
   priced 5-to-7-year contract. (`pbmc.py`)

```bash
# 1. Design a pavement for CBR 6% on a 4500-CVPD corridor (derives design MSA)
python -m rams.cli --design --cbr 6 --cvpd 4500 --vdf 4.5 --design-life 15

# 3. Price a 5-year PBMC for a segment (after the field-data forecast)
python -m rams.cli --id MDR-UP-451 --iri 2.6 --rut 5 --crack 5 --msa 3.5 \
    --zone MEDIUM --pbmc --pbmc-years 5 --pbmc-pci 3.0

# Price a PBMC across a whole imported network
python -m rams.cli --csv examples/sample_network.csv --pbmc --pbmc-years 7
```

## IRC:37 flexible-pavement design (CBR → layer thicknesses)

`rams/design.py` sizes a new flexible pavement from the **design inputs** (before
any condition survey exists): subgrade **CBR**, **design traffic (MSA)** from
`traffic.py`, and the **design life**. It returns the layer section (BC wearing /
DBM binder / WMM granular base / GSB sub-base), the subgrade resilient modulus,
and the governing IRC:37-2018 reliability (80% below 20 MSA, 90% at/above).

```
M_RS   = 10·CBR (CBR≤5) | 17.6·CBR^0.64 (CBR>5)        subgrade modulus (MPa)
section = catalogue(CBR, design_MSA)                    bituminous + granular (mm)
```

```python
from rams import design_pavement, design_msa
t = design_msa(4500, vdf=4.5, growth_rate=0.06, design_life_years=15, lane_distribution=0.75)
d = design_pavement(cbr=6.0, design_msa=t.design_msa, design_life_years=15)
print(d.total_mm, d.bituminous_mm, d.granular_mm)   # e.g. 708 / 166 / 542 mm
```

The thickness design is a smooth, **editable** fit to the IRC:37-2018 design
catalogue (`DesignCatalogue` — coefficients-as-data, like every other model
here); the **exact** IRC:37-2018 mechanistic-empirical fatigue and rutting
performance equations are also exposed (`fatigue_life_msa`, `rutting_life_msa`)
to verify a section against IITPAVE-computed strains. As with all defaults: these
are indicative and a project design must be confirmed by a mechanistic run.

## Performance-Based Maintenance Contract (PBMC) estimate (5–7 yr)

`rams/pbmc.py` is the financial-forecast deliverable: a priced PBMC/OPRC over a
fixed term that keeps the road **above a contractual service level** (an IRC:82
PCI threshold). It consumes the managed-lifecycle forecast and adds the four
PBMC cost streams:

| Stream | Driver |
|--------|--------|
| **Initial rectification** | current condition below the service level at handover |
| **Routine maintenance** | per-km annual rate × monsoon-zone burden factor |
| **Periodic renewals** | preventive/structural treatments the forecast schedules to hold the PCI |
| **Loadings** | annual escalation, contingency, contractor overhead+profit; NPV-discounted |

```python
from rams import estimate_pbmc, PBMCParams, SegmentInput, MonsoonZone
seg = SegmentInput(2.6, 5.0, 5.0, 3.5, 0.05, MonsoonZone.MEDIUM, segment_id="MDR-UP-451", length_km=15.0)
est = estimate_pbmc(seg, PBMCParams(term_years=5, performance_pci=3.0))
print(est.contract_value, est.npv, est.cost_per_km, est.compliant)
for y in est.years:
    print(y.year, y.routine, y.periodic, y.total, y.treatments)
```

`estimate_pbmc_network(segments, params)` aggregates across an imported network
(per-segment estimates + a network contract value and a list of any segments the
budget/interval cannot keep performance-compliant). Both are exposed at
`POST /api/pbmc` (single segment, or a `segments` list) and `POST /api/design`,
and as the dashboard's **Design & PBMC** tab. Every rate is a planning default —
replace with the agency's schedule of rates for a tender-grade estimate.

## IITPAVE-style mechanistic design & section evaluation

`rams/iitpave.py` adds the mechanistic layer IRC:37-2018 actually designs with: a
layered-elastic analysis that computes the two critical strains under the
standard axle and converts them to fatigue/rutting life. RAMS cannot ship the
IRC IITPAVE binary, so it uses the **Odemark--Boussinesq method of equivalent
thickness** -- a documented layered-elastic approximation in the IITPAVE
tradition (standard 80 kN dual-wheel axle, 0.56 MPa, second wheel superposed).
It is monotonic and lands in the IRC:37 strain range, but is an approximation:
**confirm a final design against IITPAVE proper.**

Two uses, both on the **Design & PBMC** tab and at `POST /api/design`
(`method:"iitpave"`) / `POST /api/iitpave`:

```bash
# Mechanistic design: lowest-cost section meeting fatigue AND rutting for the MSA
python -m rams.cli --design --design-method iitpave --cbr 8 --design-msa 50
#   -> 145 mm bituminous over 360 mm granular; eps_t 185 / eps_v 368 microstrain

# Section check from FWD back-calculated 15th-percentile moduli (residual life)
python -m rams.cli --iitpave --e-bt 977 --e-gran 200 --e-sub 70 \
    --h-bt 300 --h-gran 350 --msa 10 --growth 0.05 --cumulative-msa 20 --design-msa 150
#   -> capacity 258 MSA (subgrade rutting), ~16 yr residual, carries 150 MSA
```

```python
from rams import design_pavement_mechanistic, evaluate_section, LayerModel
d = design_pavement_mechanistic(cbr=8, design_msa=50)        # CBR -> thicknesses
a = evaluate_section(LayerModel(977, 200, 70, 300, 350),     # FWD moduli -> life
                     annual_msa=10, cumulative_msa=20, design_msa=150)
```

The design tab's **Catalogue / IITPAVE** selector switches the design method, and
the **IITPAVE section check** card evaluates an in-service pavement from its FWD
back-calculated layer moduli + thicknesses (the natural input from a deflection
survey's homogeneous-section report).

### Calibration & accuracy (validated against real IITPAVE output)

The Odemark–Boussinesq strains are calibrated to **real IITPAVE results** — the
IRC:37-2018 Annex II worked examples and a published FWD evaluation report —
with multipliers (`TENSILE_CORRECTION_IRC37=1.30`, `TENSILE_CORRECTION_IRC115=1.38`,
`VERTICAL_CORRECTION=0.97`) that bring εt/εv to within **~5–10%** of IITPAVE over
the validated range (50–1600 MPa bituminous modulus, 190–310 mm thickness). The
golden cases are locked in the test suite. Two honest caveats: very thick
(>250 mm) perpetual-pavement sections under-predict tensile strain, and overlay
decisions within **±15% of the design life are flagged "confirm with IITPAVE"** —
the screening defers the close calls to a rigorous run rather than guessing.

### FWD remaining-life & overlay (IRC:115-2014)

`evaluate_fwd_sections` reproduces the real FWD-report workflow (KGPBACK
back-calculation → 15th-percentile moduli → IITPAVE remaining life → overlay
decision), using the **IRC:115-2014** fatigue model (`0.711e-4`, no mix C factor,
Poisson 0.5/0.4/0.4) that those reports use.

```bash
# Overlay screening from a homogeneous-section moduli table (sample FWD data)
python -m rams.cli --fwd examples/fwd_sections_sample.csv --design-msa 300
#   -> per-section remaining life vs the 300 MSA design life, overlay yes/no,
#      and borderline sections flagged (*) for IITPAVE confirmation
```

```python
from rams import FWDSection, evaluate_fwd_sections
secs = [FWDSection("LHS-1", 870, 348, 77, 300, 350),   # E_bt, E_gran, E_sub, h_bt, h_gran
        FWDSection("RHS-2", 801, 352, 77, 300, 350)]
res = evaluate_fwd_sections(secs, design_msa=300)
print(res.overlay_sections, res.borderline_sections)
```

Exposed at `POST /api/fwd` and the dashboard's **FWD remaining-life & overlay**
card (paste the report's 15th-percentile moduli as CSV, or upload .csv/.xlsx/.pdf).
`examples/fwd_sections_sample.csv` is a sample corrected 15th-percentile moduli table.

## NSV chainage-survey ingestion (vendor schemas)

Real network surveys ship one **distress per file**, keyed to chainage + GPS,
with vendor column names and text condition bands -- not RAMS' native schema.
`rams/survey.py` recognises these and maps them automatically: the **Load a
segment** and network-import paths now accept a ROMDAS/Hawkeye-style survey
(rutting, roughness `Lane IRI`, cracking `Condition` band, potholes) directly,
with no reformatting.

```python
from rams import merge_surveys, SurveyDefaults
# join rutting + roughness + cracking + potholes surveys by (chainage, lane)
merged = merge_surveys([rut_rows, rough_rows, crack_rows, pothole_rows],
                       SurveyDefaults(annual_msa=8.0, monsoon_zone="HIGH"))
```

Text bands are converted to representative values (`<5%` -> 2.5, `5-10%` -> 7.5,
`>20%` -> 25); a single distress file loads with the surveyed field populated and
the rest defaulted, while `merge_surveys` joins several files by `(chainage,
lane)` into the fully-populated condition a network forecast needs. Uploading a
`.csv` / `.xlsx` survey to the dashboard auto-detects the schema.

**Multi-file & multi-sheet.** `ingest_multi_files([(name, bytes), ...])` ingests
several files at once and merges *every* survey across them by chainage — so the
four separate rutting / roughness / cracking / pothole exports become one
fully-populated condition. **All worksheets** of a workbook are scanned (a sheet
that is neither standard nor a recognised survey is skipped, not an error, with a
per-sheet status); wide per-lane sheets (`L1 Lane Roughness BI`, `L1 % Crack
Area`, ...) are expanded to one row per lane. On the dashboard, the upload card
takes **multiple files at once** (`POST /api/ingest_multi`) and reports what each
file/sheet contributed.

**Messy real templates (no hard-coding).** Vendor condition workbooks are parsed
generically: two-row / merged **grouped headers** are detected from the sheet's
`<mergeCells>` and combined (`Roughness BI` + `L1` → `roughness_bi_l1`, merges
expanded), and when a sheet repeats columns in a working block the **first
non-empty value wins**, so a real measurement is never overwritten by a later
flag column. This reads heterogeneous per-lane condition workbooks (roughness BI,
% crack area, rut depth per L1/L2/R1/R2) without any template-specific code.

## Homogeneous sectioning & downloadable reports

A raw survey is thousands of 100 m sub-segments; maintenance is planned on
**homogeneous sections**. `rams/sections.py` delineates them with the IRC:115-2014
**cumulative-difference** method (a new section starts where the local condition
crosses the network mean; short runs merge forward to a minimum length), then
aggregates each section (length-weighted mean distress), scores the IRC:82 PCI,
classifies the MoRTH band and forecasts the preventive window — a per-section table.

```python
from rams import section_survey, sections_to_xlsx, sections_to_pdf
from rams.ingest import ingest_segments_xlsx
res = section_survey(ingest_segments_xlsx("Test5.xlsx").segments, key="rut")
print(res.as_dict()["n_sections"])          # e.g. 1734 points -> ~120 sections
open("sections.xlsx", "wb").write(sections_to_xlsx(res))
open("sections.pdf",  "wb").write(sections_to_pdf(res))
```

On the **Segment Forecast** tab, uploading a multi-row chainage survey now renders
the homogeneous-section table directly under the import card, with **Download XLSX**
and **Download PDF** buttons (`POST /api/sections`, `POST /api/export?format=…`).
Both writers are pure standard library — the `.xlsx` is a minimal Office Open XML
package and the `.pdf` a paginated Courier table — so reports download with no
third-party dependency.

## Life-Cycle Analysis (LCA) decision matrix & MoRTH cost

`rams/lca.py` projects a segment's deterioration over a horizon **you choose** and
fixes the maintenance **decision** each year as the condition crosses IRC
thresholds — routine → preventive → structural **overlay** → **reconstruction** —
resetting the condition after each major treatment and continuing, so the matrix
shows *when* overlays/rebuilds fall due. Every action is priced from the **MoRTH
Standard Data Book** (`rams/morth.py`, indicative Rs/m² rates, editable), and the
life cycle is summarised by total cost, NPV and the equivalent uniform annual cost
(EUAC).

```python
# 20-year LCA decision matrix for a 10 km x 7 m carriageway
from rams import lca_matrix, SegmentInput, MonsoonZone
seg = SegmentInput(2.6, 5.0, 5.0, 4.5, 0.06, MonsoonZone.HIGH, segment_id="NH66", length_km=10.0)
r = lca_matrix(seg, horizon_years=20, width_m=7.0, discount_rate=0.08)
for y in r.years:
    print(y.year, y.decision, y.treatment, round(y.cost_inr/1e5, 1), "lakh")
print(r.total_cost_inr, r.npv_inr, r.euac_inr)
```

Exposed at `POST /api/lca` and the dashboard's **Life-Cycle Analysis** card, with
**Download XLSX / PDF** of the decision matrix. MoRTH SDB rates are indicative —
replace `MORTH_RATES` with the project's current SDB / state Schedule of Rates
before tendering.

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
  ingest.py       Multi-format network import (CSV / XLSX / PDF), one trust boundary
  hdm4.py         HDM-4 mechanistic delta-RDM rut model + paper-calibrated presets
  distress.py     Selectable cracking (MLIT) / roughness / skid / pothole models
  traffic.py      IRC:37 CVPD + VDF -> design / annual MSA (Indian overloading)
  design.py       IRC:37 new-pavement design: CBR + MSA -> BC/DBM/WMM/GSB section
  iitpave.py      Mechanistic layered-elastic analysis (Odemark-Boussinesq):
                  critical strains -> fatigue/rutting life, section check + design
  pbmc.py         Performance-Based Maintenance Contract cost estimator (5-7 yr)
  morth.py        MoRTH Standard Data Book treatment unit rates (Rs/m^2) + costing
  lca.py          Life-cycle decision matrix (routine/preventive/overlay/recon) + NPV/EUAC
  survey.py       NSV chainage-survey ingestion (ROMDAS/Hawkeye vendor schemas)
  sections.py     Dynamic segmentation: chainage survey -> homogeneous sections
  export.py       Stdlib XLSX + PDF writers for downloadable section/LCA reports
  fwd.py          FWD/Benkelman deflection -> structural number (SNP) back-calc
  calibrate.py    OLS harnesses: fit rut / cracking / roughness / skid / potholes
  residual.py     IRC:81/IRC:37 remaining fatigue life + HAM handback verdict
  triggers.py     Indian intervention triggers (rut/crack/IRI/FWD/MSA, IRC refs)
  mci.py          MLIT-PMS Maintenance Control Index (paper cross-reference)
  report.py       CSV / JSON / self-contained HTML (inline-SVG) reporting
  api.py          Pure request/response functions (testable, no HTTP)
  server.py       Stdlib web server + embedded interactive dashboard (SPA)
  cli.py          Command-line interface
tests/            unittest suite (golden values, edge & security cases)
examples/         sample_network.csv              (Segment & ingestion demos)
                  budget_network.csv             (16-segment Network & Budget demo)
                  sample_observations.csv        (HDM-4 calibration demo)
docs/             ARCHITECTURE.md (design rationale)
                  INDIAN_RAMS_STRATEGY.md (AASHTOWare-scope, customer, FWD, MSA)
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
  agency workstation and can be emailed as one artifact.

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
`http.server`) at **http://127.0.0.1:8000** with four tabs:

- **Segment Forecast** — enter a segment's condition and see the IRC:82 PCI
  curve with decision-band shading, plus an **untreated vs managed** comparison
  (the managed line applies MoRTH treatments and resets condition), the timeline
  table, and the treatments applied with their cost.
- **Network & Budget** — runs the multi-year budget optimiser over the demo
  network and shows the per-year treatment schedule, spend-vs-budget bars,
  avoided structural cost, and which segments go unfunded.
- **Calibrate & Residual Life** — fit any deterioration model to field data, and
  assess IRC:81/IRC:37 remaining structural life + HAM handback verdict.
- **Design & PBMC** — IRC:37 pavement design (CBR → layer thicknesses) via the
  catalogue **or** an IITPAVE-style mechanistic analysis, an **IITPAVE section
  check** (FWD moduli → strains, fatigue/rutting life, residual life), a priced
  5–7-year Performance-Based Maintenance Contract, and the **Life-Cycle Analysis**
  decision matrix with MoRTH costs (NPV/EUAC).

**Shared upload & paged tables.** A survey loaded once in *Load a segment* is
shared across tabs — **Network & Budget**, **PBMC** and **LCA** offer a green
*"Use uploaded survey"* button (no re-upload), and *Calibrate* a *"Use uploaded
CSV"* button. Missing fields fall back to defaults and differently-named columns
are matched on import. Long result tables (homogeneous sections, LCA matrix,
network risk, FWD) are **paginated** (25/50/100/all rows per page). All
maintenance/treatment costs shown — managed lifecycle and LCA — are priced from
the **MoRTH Standard Data Book** (rate × carriageway area).

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
