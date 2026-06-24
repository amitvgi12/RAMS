# RAMS — User Guide & Handbook

**RAMS** (Road Asset Management System) is an Indian pavement deterioration,
remaining-life and investment-planning engine. It takes you from **pavement
design** → **field-condition data** → **deterioration forecasting** →
**investment decisions** (life-cycle analysis, network budgeting, PBMC pricing),
with every rule citing its IRC clause and every cost grounded in the MoRTH
Standard Data Book.

This guide explains **how to use each feature, what data it needs, the accepted
file formats, and how the calculations are done**.

---

## 1. Getting started

RAMS is pure-Python (standard library only — no installation of third-party
packages).

### Run the dashboard

```bash
python -m rams.server --port 8000
# then open http://127.0.0.1:8000
```

### Command line (batch / automation)

```bash
python -m rams.cli --design --cbr 8 --design-msa 120 --design-life 15
python -m rams.cli --fwd examples/fwd_sections_sample.csv --design-msa 300
python -m rams.cli --pbmc --term-years 5            # see --help for all flags
```

The dashboard has **four tabs**: *Segment Forecast*, *Network & Budget*,
*Calibrate & Residual Life*, and *Design & PBMC*.

---

## 2. Data formats (general rules)

These rules apply everywhere a file is accepted:

| Format | Notes |
|--------|-------|
| **CSV** | A header row plus data rows. Most universal. |
| **XLSX** | Multiple sheets are read; merged/grouped two-row headers are combined; the *first non-empty* value wins for duplicate columns. |
| **PDF** | Text-based (machine-readable) PDFs only — the table is recovered from the text layer. Scanned/image PDFs are not OCR'd. |

**Column naming is forgiving.** Headers are normalised (lower-cased, spaces and
newlines become `_`), and each feature keeps a list of **aliases** so common
template variations are recognised automatically.

**Missing optional columns fall back to documented defaults** rather than
erroring — only the *primary* field(s) of each feature are required.

**One upload, many tabs.** A survey loaded in *Segment Forecast → Load a segment*
is shared with the Network, PBMC and LCA tabs (look for the green **“Use uploaded
survey”** button), so you don't re-upload.

---

## 3. Tab: Segment Forecast

**Purpose:** project a single segment (or a loaded survey) forward in time and
see when it crosses maintenance thresholds.

### Inputs
- Condition: IRI (mm/m), rut (mm), cracking (% area), and traffic (annual MSA,
  growth). Monsoon zone (HIGH/MEDIUM/LOW) scales environmental deterioration.
- Optional structural inputs (FWD deflection, structural number) when using the
  HDM-4 rut model.

### Load a segment from a survey / FWD file
Upload one or more **.csv / .xlsx / .pdf** files. RAMS:
- detects rut / roughness / cracking / pothole surveys and **merges them by
  chainage** into one dataset;
- builds **homogeneous sections** and shows a paginated table;
- fills traffic/zone with defaults if the survey doesn't carry them (it usually
  doesn't — an NSV survey is condition-only). A provenance note states exactly
  which columns came from the file vs. defaults.

### What you get
- **PCI curve** (IRC:82) with decision-band shading.
- **Untreated vs managed** trajectories (the managed line applies treatments and
  resets condition).
- **Intervention triggers**, HDM-4 breakdowns, skid, potholes, and an MCI
  cross-reference — each in a paginated table.
- **Treatments applied** with **MoRTH-based costs**.
- Download the homogeneous-section report as **XLSX / PDF**.

### How it's calculated
- **PCI** from IRI, rut and cracking per IRC:82.
- **Rut / cracking / roughness** progress year-by-year using IRC:82 power-law
  defaults or **HDM-4** models (driven by traffic and, under HDM-4, FWD
  deflection / structural number).
- **Triggers** fire at IRC thresholds (rut, cracking, IRI, deflection, MSA).

---

## 4. Tab: Network & Budget

**Purpose:** allocate a fixed annual budget across many segments to maximise
avoided structural cost.

### Inputs
- A network: the demo network, an imported file, or the **uploaded survey**.
- Annual budget (₹ lakh), unit cost, horizon, rut model, design MSA, and an
  optional handback requirement (residual MSA).

### Import / preview
Upload **.csv / .xlsx / .pdf**. The preview table is paginated; a note shows
which columns are file-derived vs defaulted.

### What you get
- KPIs: total spend, avoided structural cost, segments funded / **unfunded**.
- **Spend-by-year** bars vs the annual budget.
- A paginated **treatment schedule** and a **network risk / residual-life** table.
- **“Unfunded” explainer:** the recommended annual budget that would clear the
  backlog (so the warning becomes an actionable number).

### How it's calculated
- Each segment is forecast; those entering their preventive window become
  candidates, ranked by exposure (MSA × length), deadline, then cost.
- A greedy multi-year allocation funds each candidate in the first in-window year
  with budget left; the rest are **unfunded** (they fall to costlier structural
  repair later).
- The recommended budget is found by binary-searching for the smallest annual
  budget that leaves zero unfunded.
- **Residual life** = the governing minimum of IRC:81 deflection capacity and the
  IRC:37 traffic budget.

---

## 5. Tab: Calibrate & Residual Life

### 5a. Calibrate a deterioration model

**Purpose:** fit a deterioration model's coefficients to *your* field data by OLS
regression, so forecasts match local behaviour.

**Pick the model, then paste or upload observations (.csv / .xlsx / .pdf).**

| Model | Required columns | Optional (defaulted) |
|-------|------------------|----------------------|
| Rutting (Krid/Krst/Krpd) | `measured_rut_increment_mm`, `ye4`, `age` | `deflection_mm`, `structural_number`, `compaction_pct`, `cds`, `heavy_speed_kmh`, `surfacing_thickness_mm` |
| Cracking (MLIT a,b) | `crack_prev`, `crack_next` | — |
| Roughness (HDM-4) | `measured_iri_increment`, `iri`, `age` | `structural_number`, `d_msa`, `d_crack_pct`, `d_rut_mm` |
| Skid (decay_k) | `measured_sfc_decrement`, `sfc` | `d_msa` |
| Potholes (rate) | `measured_pothole_increment`, `cracking_pct` | `d_msa` |

- **Column aliases** are recognised (e.g. `Rut Increment mm`, `ESA MSA`→`ye4`,
  `SNP`→`structural_number`, `crack_t0/crack_t1`).
- Each model only consumes rows (from any sheet) that carry its target column;
  bad rows are skipped, not fatal.

> **Important — calibration needs *change over time.*** Calibration fits a
> deterioration **rate**, so it requires either repeat surveys of the same
> segment (year-on-year increments) or per-segment age + traffic. A single NSV
> condition snapshot only has current condition — RAMS detects this and tells you
> to use that file in the Forecast / Sections / Network / LCA tabs instead.

**How it's calculated:** ordinary least squares on the model's linear form, with a
non-negativity refit (coefficients that come out negative are pinned to zero and
the rest re-fit). Reports the coefficients, R², RMSE before/after, and rows used.

### 5b. Residual life
Enter FWD deflection, traffic and (optionally) design MSA and a required residual
MSA. Returns the **IRC:81 deflection capacity vs IRC:37 traffic budget**, the
governing minimum, and a **HAM handback verdict** (PASS / MARGINAL / FAIL) with
the overlay deflection target needed to pass.

---

## 6. Tab: Design & PBMC

### 6.1 IRC:37 pavement design (CBR → layer thicknesses)
**Inputs:** subgrade CBR, design traffic (design MSA directly, or CVPD × VDF →
MSA via IRC:37), design life, carriageway, and method (**Catalogue** or
**IITPAVE mechanistic**).

**Outputs:** BC / DBM / WMM / GSB thicknesses, subgrade modulus, a **to-scale
cross-section diagram**, and (mechanistic method) a fatigue/rutting strain check.

**How it's calculated:**
- *Catalogue* — the IRC:37-2018 design catalogue section for the CBR and traffic.
- *Mechanistic* — sizes the **least-cost** section whose Odemark–Boussinesq
  fatigue (ε_t at the bottom of bituminous) and rutting (ε_v at the subgrade top)
  strains both satisfy the design traffic, calibrated to IITPAVE.

### 6.2 IITPAVE section check
Enter layer moduli and thicknesses → tensile/vertical strains and fatigue/rutting
life (IRC:37 or IRC:115 standard). Used to confirm a design or a back-calculated
section.

### 6.3 FWD remaining-life & overlay
**Purpose:** from an FWD report's homogeneous sub-sections, compute remaining life
and flag overlays.

- Paste the sections, or upload the report (**.csv / .xlsx / .pdf**).
- **Columns:** `section_id`, `e_bituminous` (**required**), `e_granular`,
  `e_subgrade`, `h_bituminous`, `h_granular` (optional — **default if absent**),
  plus optional `chainage_from/to`. Aliases like `E_BC`, `E_base`, `E_sg`,
  `H_BC`, `H_base` are recognised.
- **How it's calculated:** Odemark–Boussinesq strains → IRC:115-2014 remaining
  fatigue/rutting life; sections below the design traffic need an overlay, and
  those within 15% of the threshold are marked *confirm with IITPAVE*
  (screening-grade, ~±10% of full IITPAVE).

### 6.4 PBMC estimate (5–7 yr)
Prices a Performance-Based Maintenance Contract to hold a service level.
**Inputs:** term, performance PCI, routine rate, escalation, contingency,
overhead, discount rate (and a segment or the uploaded survey).
**Outputs:** per-year cash flow (routine + periodic + initial rectification),
contract value, cost per km, NPV, and a service-level compliance check.

### 6.5 Life-Cycle Analysis (LCA) & decision matrix
**Purpose:** project one segment over a horizon and decide, each year, the
treatment that keeps it serviceable — at MoRTH cost.

- **Decisions:** ROUTINE → PREVENTIVE → OVERLAY → RECONSTRUCTION, triggered by
  PCI / rut / cracking / roughness thresholds, with condition reset after a major
  treatment and a minimum-interval deferral.
- **Costs:** MoRTH SDB rate × carriageway area; reports total, **NPV** and
  **EUAC** (equivalent uniform annual cost).
- Download the matrix as **XLSX / PDF**.

---

## 7. Costing basis (MoRTH Standard Data Book)

All maintenance/treatment costs use **indicative MoRTH SDB unit rates** (₹/m²):
routine ₹60, microsurfacing ₹160, mill & overlay ₹820, reconstruction ₹3200,
each × carriageway area (length × width, default 7 m). These rates and all
thresholds are **editable data, not hard-coded logic** — replace them with the
current SDB / State Schedule of Rates before tendering.

---

## 8. Outputs & reporting

- Interactive tables are **paginated** (25 / 50 / 100 / all rows) so large
  surveys load fast.
- Homogeneous sections, LCA matrices and forecasts export to **XLSX and PDF**.
- Reports are **self-contained and offline** — they open on an air-gapped
  workstation and can be emailed as a single file.

---

## 9. Limitations & honest caveats

- **Mechanistic strains are screening-grade** (Odemark–Boussinesq). Confirm
  borderline sections with full IITPAVE before construction/tender.
- **MoRTH rates are indicative.** Load the current SDB / State SoR for tendering.
- **Calibration needs repeat surveys** (or age + traffic). A single condition
  snapshot feeds forecasting, not rate-fitting.
- **PDF ingestion** requires a text layer; scanned images are not OCR'd.
- Defaults (traffic MSA, monsoon zone, lane width, layer thicknesses) are clearly
  flagged where used; override them with real values for project-grade results.

---

## 10. Quick reference — required columns

| Feature | Required | Common optional (defaulted) |
|---------|----------|------------------------------|
| Survey / Forecast / Sections | chainage + at least one of rut / IRI / cracking | traffic MSA, monsoon zone, growth, length |
| FWD overlay | `e_bituminous` | `e_granular`, `e_subgrade`, `h_bituminous`, `h_granular`, chainage |
| Calibrate (rut) | `measured_rut_increment_mm`, `ye4`, `age` | deflection, SNP, compaction, cds, speed, surfacing |
| Calibrate (cracking) | `crack_prev`, `crack_next` | — |
| Calibrate (roughness) | `measured_iri_increment`, `iri`, `age` | SNP, d_msa, d_crack_pct, d_rut_mm |
| Network & Budget | a network (file or uploaded survey) | budget, horizon, design MSA |
| Design | CBR + design traffic | VDF, design life, carriageway |
