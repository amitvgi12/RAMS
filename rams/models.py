"""
Typed input/output contracts with hard validation.

Security Lead note:
    All external data (CSV rows, API payloads, CLI args) is funnelled through
    `SegmentInput.validate()` before it can touch the deterioration math.
    We reject NaN/inf and out-of-range values up front -- a single inf MSA
    would otherwise poison every downstream KPI and corrupt budget decisions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config import DEFAULT_BOUNDS, InputBounds, MonsoonZone


def _check_finite(name: str, value: float) -> float:
    """Reject NaN / +-inf and non-numeric coercion failures."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric, got {value!r}.") from None
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {v}.")
    return v


def _check_range(name: str, value: float, lo: float, hi: float) -> float:
    v = _check_finite(name, value)
    if not (lo <= v <= hi):
        raise ValueError(f"{name}={v} out of allowed range [{lo}, {hi}].")
    return v


@dataclass
class SegmentInput:
    """Validated initial condition for one homogeneous road segment.

    The structural fields (deflection / structural number / compaction /
    surfacing thickness / CDS / heavy-vehicle speed) are only consumed by the
    HDM-4 rut model. They default to sane Indian-flexible-NH values so the
    default IRC:82 law is unaffected and HDM-4 runs without them -- but supply
    real FWD/Benkelman deflection and structural number for a calibrated HDM-4
    forecast.
    """

    base_iri: float          # mm/m
    base_rut: float          # mm
    base_crack: float        # % area
    annual_msa: float        # Million Standard Axles / year
    traffic_growth_rate: float
    monsoon_zone: MonsoonZone
    segment_id: str = "SEGMENT"
    length_km: float = 1.0   # used for cost-weighting in budget optimisation
    # --- HDM-4 structural inputs (FWD / pavement composition) --------------
    deflection_mm: float = 0.5          # DEF -- Benkelman/FWD rebound deflection
    structural_number: float = 4.0      # SNP -- adjusted structural number
    compaction_pct: float = 98.0        # COMP -- relative compaction (%)
    surfacing_thickness_mm: float = 100.0  # HS -- total bituminous thickness
    cds: float = 1.0                    # construction-defects indicator (0.5..1.5)
    heavy_speed_kmh: float = 50.0       # Sh -- heavy-vehicle speed (km/h)
    base_skid: float = 0.55             # initial skid resistance (SFC, fraction)
    base_potholes: float = 0.0          # initial potholing area (%)

    def validate(self, bounds: InputBounds = DEFAULT_BOUNDS) -> "SegmentInput":
        """Return a normalised, range-checked copy. Raises ValueError."""
        zone = (
            self.monsoon_zone
            if isinstance(self.monsoon_zone, MonsoonZone)
            else MonsoonZone.from_str(self.monsoon_zone)
        )
        sid = str(self.segment_id).strip() or "SEGMENT"
        if len(sid) > 128:
            raise ValueError("segment_id too long (max 128 chars).")
        return SegmentInput(
            base_iri=_check_range("base_iri", self.base_iri, bounds.iri_min, bounds.iri_max),
            base_rut=_check_range("base_rut", self.base_rut, bounds.rut_min, bounds.rut_max),
            base_crack=_check_range("base_crack", self.base_crack, bounds.crack_min, bounds.crack_max),
            annual_msa=_check_range("annual_msa", self.annual_msa, bounds.msa_min, bounds.msa_max),
            traffic_growth_rate=_check_range(
                "traffic_growth_rate", self.traffic_growth_rate,
                bounds.growth_min, bounds.growth_max,
            ),
            monsoon_zone=zone,
            segment_id=sid,
            length_km=_check_range(
                "length_km", self.length_km, bounds.length_min, bounds.length_max
            ),
            deflection_mm=_check_range(
                "deflection_mm", self.deflection_mm, bounds.deflection_min, bounds.deflection_max
            ),
            structural_number=_check_range(
                "structural_number", self.structural_number, bounds.snp_min, bounds.snp_max
            ),
            compaction_pct=_check_range(
                "compaction_pct", self.compaction_pct, bounds.compaction_min, bounds.compaction_max
            ),
            surfacing_thickness_mm=_check_range(
                "surfacing_thickness_mm", self.surfacing_thickness_mm,
                bounds.surfacing_min, bounds.surfacing_max,
            ),
            cds=_check_range("cds", self.cds, bounds.cds_min, bounds.cds_max),
            heavy_speed_kmh=_check_range(
                "heavy_speed_kmh", self.heavy_speed_kmh, bounds.speed_min, bounds.speed_max
            ),
            base_skid=_check_range("base_skid", self.base_skid, bounds.skid_min, bounds.skid_max),
            base_potholes=_check_range(
                "base_potholes", self.base_potholes, bounds.potholes_min, bounds.potholes_max
            ),
        )


@dataclass
class YearResult:
    """One year-end snapshot of a segment's condition."""

    year: int
    cumulative_msa: float
    iri: float
    rutting_mm: float
    cracking_pct: float
    irc82_pci: float
    treatment: Optional[str] = None  # populated by the maintenance layer
    skid: Optional[float] = None     # SFC; populated only when a skid model runs
    potholes: Optional[float] = None  # area %; populated only when a pothole model runs

    # Public column order, used by exporters and the CLI table. (Skid is kept out
    # of the golden column set; it is surfaced separately when modelled.)
    COLUMNS = (
        "Year", "Cumulative_MSA", "IRI", "Rutting_mm",
        "Cracking_Pct", "IRC82_PCI", "Treatment",
    )

    def as_row(self) -> dict:
        return {
            "Year": self.year,
            "Cumulative_MSA": round(self.cumulative_msa, 2),
            "IRI": round(self.iri, 2),
            "Rutting_mm": round(self.rutting_mm, 1),
            "Cracking_Pct": round(self.cracking_pct, 1),
            "IRC82_PCI": self.irc82_pci,
            "Treatment": self.treatment or "",
        }
