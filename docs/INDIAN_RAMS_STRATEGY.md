# Building an India-fit RAMS (AASHTOWare-class): scope, customer, FWD math, MSA triggers

This note answers four product/technical questions raised against the engine and
records the decisions already implemented in code.

---

## 1. Can we build an AASHTOWare-class asset-management system for Indian conditions?

Yes — and the gap to one is mostly **modules around the engine, not the engine**.
AASHTOWare Pavement ME / the older DARWin-ME stack is essentially:

1. a **data layer** (inventory + condition surveys + traffic + structural data),
2. a **prediction layer** (deterioration models, calibrated to local data),
3. a **decision layer** (triggers, treatments, multi-year optimisation), and
4. a **reporting/GIS layer**.

What this repo already has, mapped to those layers:

| AASHTOWare-class capability | Status in RAMS |
|---|---|
| Condition prediction (IRI, rut, crack → PCI) | ✅ `engine.py` (IRC:82) |
| **Mechanistic rut model (HDM-4)** | ✅ `hdm4.py`, selectable |
| Multi-format condition/structural import (NSV/PCI) | ✅ `ingest.py` (CSV/XML/PDF) |
| Treatment catalog + reset modelling | ✅ `maintenance.py`, `lifecycle.py` |
| Intervention triggers (rut/crack/IRI/FWD/**MSA**) | ✅ `triggers.py` |
| Multi-year budget optimisation | ✅ `optimize.py` (greedy; ILP-ready) |
| Network triage + dashboard | ✅ `server.py`, `api.py` |
| Composite-index cross-reference (MCI) | ✅ `mci.py` |

**The real differentiator vs. importing HDM-4/AASHTOWare wholesale is local
calibration.** HDM-4's defaults and the paper's K-factors are *not* Indian. The
honest, defensible position is: ship the **forms** (mechanistic + empirical),
keep every coefficient as data, and run a calibration programme against NSV +
FWD + traffic-census data. That calibration *is* the moat — it is what makes the
tool credible to NHAI/PWDs and is hard for a generic foreign import to match.

**Recommended build-out order (each is an additive module, not a rewrite):**
1. Calibration harness: regress engine/HDM-4 coefficients against measured
   NSV/FWD time-series (the paper's own method — multiple regression on K).
2. Cracking + roughness models on the same calibratable footing as rutting
   (rutting is done; the paper flags cracking/roughness/skid as the next models).
3. Concrete & composite pavements (paper's stated future work) — India has both.
4. GIS/LRS layer (chainage → map) for network visualisation.
5. ILP/MILP swap in `optimize.py` for auditable optimal budget allocation.

---

## 2. Customer: private BOT/HAM concessionaires vs. state PWDs / NHAI?

These are **different products sharing one engine**. A recommendation, not a
mandate — but the split matters because it changes the objective function.

| | BOT / HAM concessionaire | State PWD / NHAI |
|---|---|---|
| Objective | NPV over the **concession period**; meet **handback** condition with least spend | **Network-level** condition at a fixed annual budget |
| Time horizon | Fixed (15–20 yr concession / O&M period) | Rolling, perpetual |
| Key constraint | Contractual O&M thresholds + handback (residual life) | Annual budget ceilings, public-spend auditability |
| What they pay for | "Cheapest plan that keeps me compliant and hands back clean" | "Most network condition per ₹ crore" |
| Decisive feature | Per-asset lifecycle NPV + handback-residual-life check | Multi-year budget optimiser + transparent ranking |

**Recommendation: start with BOT/HAM concessionaires.** Faster sales cycle,
clearer ROI (penalty/handback avoidance is a hard number), they already collect
NSV/FWD for compliance, and they are willing to pay for software that reduces
O&M spend. Then move up-market to PWD/NHAI network deployments, where the
budget-optimiser and auditability become the lead features. The codebase already
leans this way: the optimiser models the *avoided structural premium* (a
concessionaire's exact cost-avoidance argument) and uses a transparent,
auditable greedy heuristic suitable for public spend.

*Engineering implication already honoured:* the engine is calibration-as-data
and I/O-agnostic, so the same core serves both — only the decision/objective
layer differs (concession-NPV vs. network-budget).

---

## 3. Integrating FWD structural deflection alongside NSV surface PCI

**Status: implemented for rutting; here is the maths and where it plugs in.**

Surface PCI (IRI/rut/crack) describes the *symptom*; FWD/Benkelman deflection
describes the *structural cause*. They must enter the prediction at different
points, not be averaged together:

1. **Deflection → structural capacity.** Convert FWD/Benkelman rebound deflection
   to an **adjusted structural number** `SNP` (e.g. via the AASHTO/HDM-4
   back-calculation `SNP = f(D0, layer moduli)` or the IRC:81 deflection-life
   relationship). Both `DEF` and `SNP` are first-class inputs.

2. **Drive the mechanistic rut model with them.** In `hdm4.py`:
   - densification `RDO ∝ YE4^(a1 + a2·DEF)·SNP^a3` — higher deflection raises the
     traffic exponent; lower SNP raises rut directly,
   - structural `RDST ∝ SNP^a1·YE4^a2` — the standing structural-deformation term.

   This is exactly the HDM-4 structure the paper calibrates. A weak, high-deflection
   pavement and a sound one with identical *surface* PCI now diverge in forecast —
   which surface-only models cannot capture.

3. **An independent structural trigger.** `triggers.py` fires an **IRC:81**
   structural-strengthening trigger on deflection alone (default ≥ 1.0 mm),
   independent of surface PCI — so a structurally-failing-but-smooth section is
   still flagged for overlay design.

**Recommended next step (residual life):** add IRC:81 / mechanistic-empirical
**remaining-fatigue-life** from `(SNP, cumulative MSA, design MSA)` →
`remaining MSA`. That is the missing scalar that lets a concessionaire prove
handback residual life, and it reuses the MSA accounting already in the engine.

---

## 4. Exact intervention triggers on Indian traffic metrics (MSA)

**Status: implemented in `triggers.py`.** Indian practice keys structural renewal
to **MSA (Million Standard Axles)** consumption, not just surface defects.

- **IRC:37 design traffic (MSA)** is the fatigue-life budget. The engine tracks
  cumulative MSA each year. The trigger fires structural renewal when
  `cumulative_MSA ≥ design_life_fraction × design_MSA` (default 0.8) — i.e. 80%
  of the design fatigue life consumed → plan the overlay now.
- **Traffic categories** (`msa_category`) follow IRC:37 bands: <5, 5–10, 10–20,
  20–30, 30–50, 50–100, 100–150, >150 MSA, to classify a corridor's structural
  class.
- **Surface defect triggers** layer on top (rut 10/20 mm, cracking 10/20%, IRI
  2.5/4.0), each carrying its IRC reference and a functional→structural severity.

So a section can be flagged for structural renewal because (a) rutting/cracking
crossed a defect limit, (b) FWD deflection says it is structurally weak, **or**
(c) it has simply carried its design MSA — whichever comes first. All three are
surfaced together with the firing year and the governing IRC clause.

*Calibration caveat (applies to all four answers):* every threshold and
coefficient above is an overridable default seeded to common Indian practice /
the paper's Japanese calibration. None should reach production without being
re-fitted to the deploying agency's own NSV + FWD + traffic data.

---

## 5. The 4-layer PMS vision mapped onto RAMS

A reference PMS architecture (GIS/inventory → condition assessment → "ME"
deterioration engine → decision/LCA) maps onto RAMS as follows. The point of this
table is to be honest about **what already exists, what is a threshold/config
alignment, and what is genuinely external integration** that should not be stubbed.

| Vision layer | Status in RAMS | Gap to close |
|---|---|---|
| **1. GIS & inventory** (asset id, chainage, width) | Partial — `SegmentInput.segment_id` + `length_km`; importers carry ids | **Dynamic segmentation / LRS** is a real gap: add `start/end_chainage`, `highway_no`, lane count, and a PostGIS-backed spatial join (Shapely/GeoPandas). This is a DB/GIS service, not engine work. |
| **2. Condition assessment** (NSV/ROMDAS/Hawkeye → IRI, rut, crack, FWD) | ✅ CSV/XML/PDF ingestion (now streaming for big files), FWD→SNP, IRC:82 distress fields | **Column-mapping** for vendor schemas (ROMDAS/Hawkeye headers ≠ RAMS headers) — the next concrete step so raw survey files load without reformatting. |
| **3. "Indian ME" deterioration engine** | ✅ Selectable rut/crack/roughness/skid/pothole models, HDM-4 + MLIT, all calibratable; **IRC:37 CVPD/VDF→MSA** (`traffic.py`); **IRC:37 structural design** CBR+MSA→layer section + the exact IRC:37-2018 fatigue/rutting performance equations (`design.py`) | **IITPAVE layered-elastic strain solver** is the remaining mechanistic gap — `design.py` ships the IRC:37-2018 performance equations and an editable catalogue, but the εt/εv that feed those equations still need an IITPAVE run (a deliberate external integration). **Climate**: a monsoon multiplier exists; a true **drainage/waterlogging** score + IMD gridded-rainfall connector would sharpen it. |
| **4. Decision & LCA** (trigger BC/SDBC/micro-surfacing under budget) | ✅ IRC:82 PCI bands + MoRTH catalog, IRC-referenced triggers (rut/crack/IRI/FWD/**MSA**/skid/potholes), greedy multi-year **budget optimiser**, IRC:81/IRC:37 residual life + handback, **5–7-yr PBMC cost estimator** (`pbmc.py`: initial rectification + routine + periodic renewals + escalation/contingency/overhead + NPV) | **ILP / genetic-algorithm** optimiser to replace the greedy heuristic (the API already isolates the optimiser behind a stable interface); align treatment names to the exact IRC:SP:81 / IRC:109 menu if an agency wants that vocabulary. |

**What I deliberately did *not* build** (external integrations / heavy ports that
should be real services, not fabricated stubs): the PostGIS/GeoPandas spatial
layer, the live **IMD** weather API connector, the **IITPAVE** mechanistic wrapper,
and a production ILP/GA solver. Each is a well-scoped follow-up; the engine and
data contracts are already shaped to accept them (calibration-as-data, a stable
optimiser interface, per-segment structural inputs).

**Recommended next two, in order:** (1) **vendor column-mapping** for NSV/ROMDAS
files (immediate, unblocks real data); (2) **IITPAVE remaining-life wrapper**
feeding the existing residual-life/handback scalars (the highest-value engine gap).
