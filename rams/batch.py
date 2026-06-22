"""
Network-level batch forecasting + CSV ingestion.

Performance Engineer note:
    The single-segment engine is O(horizon). A national network is O(N) of
    those, embarrassingly parallel and CPU-light. We expose:
      * forecast_network()  -- streaming, constant-memory per segment, the
        default and safest path (no third-party deps).
      * ingest_segments_csv() -- defensive CSV loader with row caps, strict
        typing and per-row error isolation so one bad row cannot abort an
        entire 500k-segment NSV import.

Security Lead note:
    CSV is read with the stdlib `csv` module (no eval/formula execution).
    A hard MAX_ROWS cap bounds memory/CPU. Every value is validated through
    SegmentInput before use; malformed rows are collected as errors, never
    silently coerced.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .config import (
    DEFAULT_BOUNDS,
    CrackModelType,
    InputBounds,
    MonsoonZone,
    PotholeModelType,
    RoughnessModelType,
    RutModelType,
    SkidModelType,
)
from .distress import (
    DEFAULT_HDM4_POTHOLE,
    DEFAULT_HDM4_ROUGHNESS,
    DEFAULT_HDM4_SKID,
    DEFAULT_MLIT_CRACK,
    HDM4PotholeModel,
    HDM4RoughnessModel,
    HDM4SkidModel,
    MLITCrackModel,
)
from .engine import forecast_segment
from .fwd import snp_from_deflection
from .hdm4 import DEFAULT_HDM4, HDM4RutCalibration
from .maintenance import MaintenancePlan, MaintenancePolicy, build_maintenance_plan
from .models import SegmentInput, YearResult

MAX_ROWS = 1_000_000  # DoS guard on ingestion

REQUIRED_COLUMNS = (
    "segment_id",
    "base_iri",
    "base_rut",
    "base_crack",
    "annual_msa",
    "traffic_growth_rate",
    "monsoon_zone",
)

# Optional HDM-4 structural / FWD columns, consumed only by the HDM-4 models.
_STRUCTURAL_FIELDS = (
    "deflection_mm",
    "structural_number",
    "compaction_pct",
    "surfacing_thickness_mm",
    "cds",
    "heavy_speed_kmh",
    "base_skid",
    "base_potholes",
)


def segment_from_mapping(row: Dict[str, str], bounds: InputBounds = DEFAULT_BOUNDS) -> SegmentInput:
    """Build + validate one SegmentInput from a string mapping (CSV/XLSX/PDF).

    The single row->segment contract shared by every importer, so all formats
    pick up the optional structural/FWD columns identically. A raw FWD survey
    (deflection but no structural number) gets SNP derived from deflection.
    """
    missing = [c for c in REQUIRED_COLUMNS if not str(row.get(c, "")).strip()]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")
    structural = {
        f: str(row[f]).strip() for f in _STRUCTURAL_FIELDS if str(row.get(f, "")).strip()
    }
    if "deflection_mm" in structural and "structural_number" not in structural:
        structural["structural_number"] = snp_from_deflection(float(structural["deflection_mm"]))
    return SegmentInput(
        segment_id=row.get("segment_id", "SEGMENT"),
        base_iri=row["base_iri"],
        base_rut=row["base_rut"],
        base_crack=row["base_crack"],
        annual_msa=row["annual_msa"],
        traffic_growth_rate=row["traffic_growth_rate"],
        monsoon_zone=MonsoonZone.from_str(row["monsoon_zone"]),
        length_km=(str(row.get("length_km", "")).strip() or 1.0),
        **structural,
    ).validate(bounds)


@dataclass
class SegmentForecast:
    segment_id: str
    timeline: List[YearResult]
    plan: MaintenancePlan


@dataclass
class IngestResult:
    segments: List[SegmentInput]
    errors: List[Tuple[int, str]]  # (1-based row number, message)


def ingest_segments_csv(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Load and validate segments from a CSV with the REQUIRED_COLUMNS header.

    Bad rows are isolated into `errors`; good rows land in `segments`.
    """
    segments: List[SegmentInput] = []
    errors: List[Tuple[int, str]] = []

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        for i, row in enumerate(reader, start=1):
            if i > max_rows:
                errors.append((i, f"row limit {max_rows} exceeded; ingestion truncated"))
                break
            try:
                segments.append(segment_from_mapping(row, bounds))
            except (ValueError, KeyError) as exc:
                errors.append((i, str(exc)))

    return IngestResult(segments=segments, errors=errors)


def forecast_network(
    segments: Iterable[SegmentInput],
    horizon_years: int = 10,
    policy: Optional[MaintenancePolicy] = None,
    bounds: InputBounds = DEFAULT_BOUNDS,
    rut_model: RutModelType = RutModelType.DEFAULT,
    hdm4_calibration: HDM4RutCalibration = DEFAULT_HDM4,
    crack_model: CrackModelType = CrackModelType.DEFAULT,
    mlit_crack: MLITCrackModel = DEFAULT_MLIT_CRACK,
    roughness_model: RoughnessModelType = RoughnessModelType.DEFAULT,
    hdm4_roughness: HDM4RoughnessModel = DEFAULT_HDM4_ROUGHNESS,
    skid_model: SkidModelType = SkidModelType.NONE,
    hdm4_skid: HDM4SkidModel = DEFAULT_HDM4_SKID,
    pothole_model: PotholeModelType = PotholeModelType.NONE,
    hdm4_pothole: HDM4PotholeModel = DEFAULT_HDM4_POTHOLE,
) -> Iterator[SegmentForecast]:
    """Lazily forecast each segment and build its maintenance plan.

    Generator => constant memory regardless of network size; callers can
    stream straight to disk/DB. Pass HDM-4/MLIT model selectors to forecast the
    whole network mechanistically -- each segment then uses its own FWD
    deflection / structural number.
    """
    policy = policy or MaintenancePolicy()
    for seg in segments:
        timeline = forecast_segment(
            seg, horizon_years, bounds=bounds,
            rut_model=rut_model, hdm4_calibration=hdm4_calibration,
            crack_model=crack_model, mlit_crack=mlit_crack,
            roughness_model=roughness_model, hdm4_roughness=hdm4_roughness,
            skid_model=skid_model, hdm4_skid=hdm4_skid,
            pothole_model=pothole_model, hdm4_pothole=hdm4_pothole,
        )
        plan = build_maintenance_plan(timeline, policy)
        yield SegmentForecast(segment_id=seg.segment_id, timeline=timeline, plan=plan)


def network_summary(forecasts: Iterable[SegmentForecast]) -> Dict[str, int]:
    """Aggregate counts useful for a network dashboard / triage."""
    summary = {"total": 0, "needs_preventive": 0, "window_expired": 0, "routine_only": 0}
    for fc in forecasts:
        summary["total"] += 1
        if fc.plan.window_expired_year is not None:
            summary["window_expired"] += 1
        elif fc.plan.preventive_window_year is not None:
            summary["needs_preventive"] += 1
        else:
            summary["routine_only"] += 1
    return summary
