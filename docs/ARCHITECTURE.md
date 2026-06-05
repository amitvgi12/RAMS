# Architecture & Design Rationale

This document records the deeper design decisions behind the RAMS deterioration
engine, for maintainers and reviewers.

## 1. Data flow

```
NSV CSV / API ─▶ SegmentInput.validate()  ◀── trust boundary (Security)
                      │
                      ▼
      IndianPavementDeteriorationEngine.run_lifecycle_forecast()
                      │  (deterministic, O(horizon) per segment)
                      ▼
                List[YearResult]
                 │            │
                 ▼            ▼
   build_maintenance_plan()   report.to_csv / to_json / to_html
                 │
                 ▼
           MaintenancePlan ─▶ priority / budget algorithms (future)
```

The engine is **pure**: identical inputs always yield identical outputs (no
clock, RNG, network or global state). This is the single most important property
for a system that allocates public maintenance spending — every forecast is
auditable and reproducible.

## 2. The deterioration laws (faithful to spec)

Per simulated year, with `m` = monsoon multiplier:

```
IRI:    iri  += (0.04 · iri) + (0.015 · annual_msa) · m            cap 12.0
RUT:    rut  += (0.35 · annual_msa^0.7) · m                        cap 35.0
CRACK:  if age > 3:  crack += 1.2 · cumulative_msa^0.6             cap 100.0
        else:        crack += 0.1 · annual_msa
PCI:    weighted IRC:82 deduct-value composite of (iri, rut, crack), clamped 1–4
annual_msa *= (1 + growth)        # compounded for the next year
```

All coefficients, caps and the `age > 3` lag are externalised to
`config.Calibration`, so the laws above are the *default calibration*, not
hard-coded behaviour.

### Why `math.pow`, not `numpy.power`
The target interpreter had no numpy/pandas installed. For a scalar,
year-stepped recurrence there is nothing to vectorise *within* a segment, so the
numpy dependency bought nothing but a deployment risk. numpy remains a valid
*optional* accelerator for cross-segment batch math if a future profile shows it
is worthwhile (see §5).

## 3. IRC:82 composite scoring

Each distress maps to a deduct-value sub-score in `[1.0, 4.0]`:

| Distress | Free threshold | Deduct rate | Weight |
|----------|---------------:|------------:|-------:|
| IRI      | < 2.0 mm/m     | 0.60        | 0.40   |
| Rutting  | < 5.0 mm       | 0.25        | 0.35   |
| Cracking | < 5.0 %        | 0.15        | 0.25   |

`IRC82Scoring.__post_init__` asserts the three weights sum to 1.0, so a
mis-calibration is caught at construction, not silently skewing every forecast.

## 4. Maintenance decision logic

`MaintenancePolicy` defines two thresholds (`preventive_upper=3.20`,
`structural_lower=2.50`). `build_maintenance_plan()` scans the timeline once and
records:

- `preventive_window_year` — first year entering the amber band (act here).
- `window_expired_year` — first year dropping into the red band (too late for
  cheap fixes).
- `recommended_year` / `recommended_treatment` / human-readable `rationale`.

The `apply_reset()` engine hook plus `Treatment.reset_*` fields are the
foundation for modelling *treated* trajectories (re-simulate forward from the
restored condition) — used by the future budget optimiser.

## 5. Performance & scaling

- **Per segment:** O(horizon) time, O(1) extra memory beyond the timeline.
- **Per network:** `forecast_network()` is a generator → O(1) resident memory
  regardless of segment count; results stream out as produced.
- **Parallelism:** segments are independent. Wrapping `forecast_segment` in a
  `ProcessPoolExecutor` is a drop-in scale-out path; the functional
  `forecast_segment` API (no shared mutable state) was chosen specifically to
  make that safe.
- **Bounds:** `horizon_years ≤ 100` and `MAX_ROWS = 1_000_000` cap worst-case
  work from hostile or fat-fingered input.

## 6. Security posture

| Threat | Mitigation |
|--------|------------|
| NaN/inf poisoning KPIs | `_check_finite` rejects non-finite values |
| Out-of-range / nonsense inputs | `InputBounds` range checks at the boundary |
| Silent typo → wrong zone | `MonsoonZone.from_str` raises on unknown values |
| CSV formula/macro execution | stdlib `csv` only; no `eval`/`exec`/`pickle`/`yaml` |
| Oversized import (DoS) | `MAX_ROWS` cap; bad rows isolated, not fatal |
| Stored XSS in HTML report | `html.escape()` on all dynamic strings |
| Supply-chain surface | zero runtime third-party dependencies in the core |

## 7. Testing strategy

`unittest` (not pytest) so the suite runs on a bare interpreter. Golden values
for the spec scenario are asserted to the exported precision and double as
regression protection for any future calibration change — if a coefficient moves,
the golden test fails loudly and forces a deliberate re-baseline.
