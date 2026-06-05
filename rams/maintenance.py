"""
Preventive-maintenance decision logic + MoRTH treatment catalog.

Implements Section 4 of the RAMS spec: scan a forecast timeline, classify
each year by IRC:82 PCI, locate the 'window of maximum return' (the cheap
preventive window), and detect when that window has expired and only a
structural fix remains.

Decision bands (PCI, 0-4 scale):
    PCI >= 3.20            -> ROUTINE        (do nothing / routine crack seal)
    2.50 <= PCI < 3.20     -> PREVENTIVE     (microsurfacing -- max return)
    PCI <  2.50            -> STRUCTURAL     (mill & overlay; cheap fixes locked out)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from .models import YearResult


class MaintenanceFlag(str, Enum):
    ROUTINE = "ROUTINE"
    PREVENTIVE = "PREVENTIVE"
    STRUCTURAL = "STRUCTURAL"


@dataclass(frozen=True)
class Treatment:
    """A MoRTH-referenced treatment and the condition state it resets to.

    `reset_*` are absolute post-treatment target values (mm/m, mm, %). None
    means the treatment does not restore that distress. `relative_cost` is a
    unit multiplier used by downstream budget optimisation.
    """

    code: str
    name: str
    morth_reference: str
    flag: MaintenanceFlag
    relative_cost: float
    reset_iri: Optional[float] = None
    reset_rut: Optional[float] = None
    reset_crack: Optional[float] = None


# MoRTH treatment catalog. Reset values are calibration inputs -- tune per
# agency. A microsurface restores the surface (IRI/cracking) but does little
# for structural rutting; a mill & overlay rebuilds the bound layer.
TREATMENT_CATALOG: Dict[str, Treatment] = {
    "ROUTINE_CRACK_SEAL": Treatment(
        code="ROUTINE_CRACK_SEAL",
        name="Routine Crack Sealing",
        morth_reference="MoRTH Section 3000 (routine maintenance)",
        flag=MaintenanceFlag.ROUTINE,
        relative_cost=0.2,
        reset_crack=0.0,
    ),
    "MICROSURFACING": Treatment(
        code="MICROSURFACING",
        name="Microsurfacing (preventive)",
        morth_reference="MoRTH Section 514",
        flag=MaintenanceFlag.PREVENTIVE,
        relative_cost=1.0,
        reset_iri=1.8,
        reset_rut=2.0,
        reset_crack=0.0,
    ),
    "MILL_AND_OVERLAY": Treatment(
        code="MILL_AND_OVERLAY",
        name="Structural Mill & Overlay",
        morth_reference="MoRTH Section 500 (bituminous courses)",
        flag=MaintenanceFlag.STRUCTURAL,
        relative_cost=5.0,   # ~5x preventive, per the spec
        reset_iri=1.5,
        reset_rut=0.0,
        reset_crack=0.0,
    ),
}


@dataclass(frozen=True)
class MaintenancePolicy:
    """PCI thresholds defining the maintenance decision bands."""

    preventive_upper: float = 3.20  # at/above -> routine
    structural_lower: float = 2.50  # below    -> structural (window expired)

    def __post_init__(self) -> None:
        if not (0.0 <= self.structural_lower < self.preventive_upper <= 4.0):
            raise ValueError(
                "Require 0 <= structural_lower < preventive_upper <= 4."
            )

    def classify(self, pci: float) -> MaintenanceFlag:
        if pci >= self.preventive_upper:
            return MaintenanceFlag.ROUTINE
        if pci >= self.structural_lower:
            return MaintenanceFlag.PREVENTIVE
        return MaintenanceFlag.STRUCTURAL

    def recommended_treatment(self, pci: float) -> Treatment:
        return {
            MaintenanceFlag.ROUTINE: TREATMENT_CATALOG["ROUTINE_CRACK_SEAL"],
            MaintenanceFlag.PREVENTIVE: TREATMENT_CATALOG["MICROSURFACING"],
            MaintenanceFlag.STRUCTURAL: TREATMENT_CATALOG["MILL_AND_OVERLAY"],
        }[self.classify(pci)]


@dataclass
class MaintenancePlan:
    """Result of scanning a forecast timeline."""

    flags_by_year: List[MaintenanceFlag]
    preventive_window_year: Optional[int]   # first year entering preventive band
    window_expired_year: Optional[int]      # first year dropping to structural
    recommended_year: Optional[int]
    recommended_treatment: Optional[Treatment]
    rationale: str


def annotate_timeline(
    timeline: List[YearResult], policy: MaintenancePolicy
) -> List[YearResult]:
    """Stamp each YearResult.treatment with its recommended treatment name.

    Returns the same list (mutated in place) for convenience.
    """
    for yr in timeline:
        yr.treatment = policy.recommended_treatment(yr.irc82_pci).name
    return timeline


def build_maintenance_plan(
    timeline: List[YearResult], policy: Optional[MaintenancePolicy] = None
) -> MaintenancePlan:
    """Scan a forecast for the preventive window and expiry, per Section 4."""
    policy = policy or MaintenancePolicy()
    if not timeline:
        raise ValueError("Cannot plan maintenance on an empty timeline.")

    flags = [policy.classify(yr.irc82_pci) for yr in timeline]

    preventive_year: Optional[int] = None
    expired_year: Optional[int] = None
    for yr, flag in zip(timeline, flags):
        if flag is MaintenanceFlag.PREVENTIVE and preventive_year is None:
            preventive_year = yr.year
        if flag is MaintenanceFlag.STRUCTURAL and expired_year is None:
            expired_year = yr.year

    if preventive_year is not None:
        rec_year = preventive_year
        rec_treatment = TREATMENT_CATALOG["MICROSURFACING"]
        if expired_year is not None and expired_year <= preventive_year:
            # Never actually entered a usable preventive window.
            rec_treatment = TREATMENT_CATALOG["MILL_AND_OVERLAY"]
            rec_year = expired_year
            rationale = (
                f"Asset reaches structural failure by year {expired_year} "
                f"without a usable preventive window: schedule "
                f"{rec_treatment.name} ({rec_treatment.morth_reference})."
            )
        else:
            rationale = (
                f"Window of maximum return opens in year {preventive_year} "
                f"(PCI enters {policy.structural_lower:.2f}-"
                f"{policy.preventive_upper:.2f}). Schedule "
                f"{rec_treatment.name} ({rec_treatment.morth_reference}) now; "
                + (
                    f"if delayed past year {expired_year - 1}, only structural "
                    f"mill & overlay (~5x cost) remains."
                    if expired_year is not None
                    else "monitor for window expiry beyond the horizon."
                )
            )
    elif expired_year is not None:
        rec_year = expired_year
        rec_treatment = TREATMENT_CATALOG["MILL_AND_OVERLAY"]
        rationale = (
            f"No preventive window observed; asset is structural by year "
            f"{expired_year}. Schedule {rec_treatment.name}."
        )
    else:
        rec_year = None
        rec_treatment = None
        rationale = (
            "Asset stays in the routine band across the horizon; "
            "routine crack sealing only."
        )

    return MaintenancePlan(
        flags_by_year=flags,
        preventive_window_year=preventive_year,
        window_expired_year=expired_year,
        recommended_year=rec_year,
        recommended_treatment=rec_treatment,
        rationale=rationale,
    )
