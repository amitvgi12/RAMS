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

from .config import DEFAULT_BOUNDS, InputBounds, MonsoonZone
from .engine import forecast_segment
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
                seg = SegmentInput(
                    segment_id=row["segment_id"],
                    base_iri=row["base_iri"],
                    base_rut=row["base_rut"],
                    base_crack=row["base_crack"],
                    annual_msa=row["annual_msa"],
                    traffic_growth_rate=row["traffic_growth_rate"],
                    monsoon_zone=MonsoonZone.from_str(row["monsoon_zone"]),
                    length_km=row.get("length_km", 1.0) or 1.0,
                ).validate(bounds)
                segments.append(seg)
            except (ValueError, KeyError) as exc:
                errors.append((i, str(exc)))

    return IngestResult(segments=segments, errors=errors)


def forecast_network(
    segments: Iterable[SegmentInput],
    horizon_years: int = 10,
    policy: Optional[MaintenancePolicy] = None,
    bounds: InputBounds = DEFAULT_BOUNDS,
) -> Iterator[SegmentForecast]:
    """Lazily forecast each segment and build its maintenance plan.

    Generator => constant memory regardless of network size; callers can
    stream straight to disk/DB.
    """
    policy = policy or MaintenancePolicy()
    for seg in segments:
        timeline = forecast_segment(seg, horizon_years, bounds=bounds)
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
