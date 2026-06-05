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
    """Validated initial condition for one homogeneous road segment."""

    base_iri: float          # mm/m
    base_rut: float          # mm
    base_crack: float        # % area
    annual_msa: float        # Million Standard Axles / year
    traffic_growth_rate: float
    monsoon_zone: MonsoonZone
    segment_id: str = "SEGMENT"
    length_km: float = 1.0   # used for cost-weighting in budget optimisation

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

    # Public column order, used by exporters and the CLI table.
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
