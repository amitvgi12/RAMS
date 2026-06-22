"""
MoRTH cost basis -- treatment unit rates from the MoRTH Standard Data Book (SDB).

Maintenance/rehabilitation costs in an Indian PMS are quantity x rate, where the
rate comes from the MoRTH *Standard Data Book for Analysis of Rates* (and the
state Schedule of Rates). This module holds an editable schedule of **indicative
SDB unit rates** (Rs per m^2 of carriageway) for the standard treatments, mapped
to the relevant MoRTH specification clause, plus an area-based cost function.

The rates below are indicative (circa current SDB, generic terrain) and are
*data, not code* -- replace `MORTH_RATES` (or pass an override) with the project's
own SDB/SoR analysis before tendering. Costs are returned in rupees; helpers give
lakh/crore for reporting.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


class Decision(str, Enum):
    """Life-cycle maintenance decision, in escalating order of intervention."""

    ROUTINE = "ROUTINE"                  # crack sealing, pothole patching, drainage
    PREVENTIVE = "PREVENTIVE"            # microsurfacing / surface seal
    OVERLAY = "OVERLAY"                  # structural mill & overlay (BC + DBM)
    RECONSTRUCTION = "RECONSTRUCTION"    # full-depth rebuild


@dataclass(frozen=True)
class MorthRate:
    """A MoRTH-referenced treatment and its SDB unit rate (Rs per m^2)."""

    decision: Decision
    name: str
    morth_reference: str
    rate_per_sqm: float            # Rs / m^2 of treated carriageway
    # Post-treatment reset condition (mm/m, mm, % area). None = not restored.
    reset_iri: float
    reset_rut: float
    reset_crack: float


# Indicative MoRTH SDB rates (Rs/m^2). Replace with the current SDB/SoR analysis.
MORTH_RATES: Dict[Decision, MorthRate] = {
    Decision.ROUTINE: MorthRate(
        Decision.ROUTINE, "Routine maintenance (crack sealing + pothole patching)",
        "MoRTH Section 3000 (maintenance of bituminous surfaces)",
        rate_per_sqm=60.0, reset_iri=None, reset_rut=None, reset_crack=0.0),
    Decision.PREVENTIVE: MorthRate(
        Decision.PREVENTIVE, "Microsurfacing (preventive surface course)",
        "MoRTH Section 515 (microsurfacing)",
        rate_per_sqm=160.0, reset_iri=1.8, reset_rut=2.0, reset_crack=0.0),
    Decision.OVERLAY: MorthRate(
        Decision.OVERLAY, "Mill & overlay (40 mm BC + 50 mm DBM)",
        "MoRTH Sections 507/505 (BC/DBM) + 3009 (profiling)",
        rate_per_sqm=820.0, reset_iri=1.5, reset_rut=0.0, reset_crack=0.0),
    Decision.RECONSTRUCTION: MorthRate(
        Decision.RECONSTRUCTION, "Full-depth reconstruction (bituminous + granular)",
        "MoRTH Sections 500 + 300 (bituminous + granular courses)",
        rate_per_sqm=3200.0, reset_iri=1.2, reset_rut=0.0, reset_crack=0.0),
}

DEFAULT_CARRIAGEWAY_WIDTH_M = 7.0   # two-lane carriageway; 3.5 m per lane

# Map the MoRTH treatment catalogue (maintenance.py) onto life-cycle decisions,
# so any treatment can be priced from the SDB rate schedule.
TREATMENT_DECISION: Dict[str, Decision] = {
    "ROUTINE_CRACK_SEAL": Decision.ROUTINE,
    "MICROSURFACING": Decision.PREVENTIVE,
    "MILL_AND_OVERLAY": Decision.OVERLAY,
}


def treated_area_sqm(length_km: float, width_m: float = DEFAULT_CARRIAGEWAY_WIDTH_M) -> float:
    """Carriageway area treated = length x width (m^2)."""
    return max(0.0, length_km) * 1000.0 * max(0.0, width_m)


def treatment_cost_inr(
    decision: Decision,
    length_km: float,
    width_m: float = DEFAULT_CARRIAGEWAY_WIDTH_M,
    rates: Dict[Decision, MorthRate] = MORTH_RATES,
) -> float:
    """Cost (Rs) of a treatment over a length: SDB rate x carriageway area."""
    rate = rates.get(decision)
    if rate is None:
        return 0.0
    return rate.rate_per_sqm * treated_area_sqm(length_km, width_m)


def treatment_cost_lakh(
    decision: Decision,
    length_km: float,
    width_m: float = DEFAULT_CARRIAGEWAY_WIDTH_M,
    rates: Dict[Decision, MorthRate] = MORTH_RATES,
) -> float:
    """MoRTH treatment cost in Rs lakh (SDB rate x area)."""
    return treatment_cost_inr(decision, length_km, width_m, rates) / 1.0e5


def cost_for_treatment_code_lakh(
    code: str,
    length_km: float,
    width_m: float = DEFAULT_CARRIAGEWAY_WIDTH_M,
    rates: Dict[Decision, MorthRate] = MORTH_RATES,
) -> float:
    """MoRTH cost (Rs lakh) for a TREATMENT_CATALOG code, via its decision mapping."""
    decision = TREATMENT_DECISION.get(code, Decision.ROUTINE)
    return treatment_cost_lakh(decision, length_km, width_m, rates)


def to_lakh(inr: float) -> float:
    return inr / 1.0e5


def to_crore(inr: float) -> float:
    return inr / 1.0e7
