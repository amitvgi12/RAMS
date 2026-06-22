"""
Performance-Based Maintenance Contract (PBMC) cost estimator.

This is the *financial-forecasting* layer of the RAMS workflow. It consumes the
deterioration / managed-lifecycle forecast (the engineering layer) and turns it
into a priced 5-to-7-year maintenance contract -- the deliverable a concession
or PWD needs to float or evaluate a PBMC/OPRC tender.

Indian PBMCs (MoRTH Performance-Based Maintenance, World-Bank OPRC) pay a
contractor to keep a road **above a contractual service level** (a minimum
condition, here an IRC:82 PCI threshold) for a fixed term, rather than paying for
quantities of work. The estimate therefore has four cost streams:

  1. **Initial rectification** -- bring a road that is already below the service
     level up to standard at handover (year 1). Driven by the *current* condition.
  2. **Routine maintenance** -- the recurring per-km annual obligation (potholes,
     crack sealing, drainage, shoulders, markings, vegetation). Wet (HIGH-monsoon)
     corridors cost more to keep clean, so routine carries a zone factor.
  3. **Periodic renewals** -- preventive/structural treatments scheduled *within
     the term* when the forecast says the PCI would otherwise fall below the
     service level. These come straight from `simulate_managed_lifecycle`, run
     against a policy whose trigger is the contractual PCI.
  4. **Loadings** -- price escalation per year, a contingency, and contractor
     overhead+profit; the contract value is reported both nominal and as NPV.

Every rate here is an editable planning default; replace with the agency's
schedule of rates (MoRTH/State SoR) for a tender-grade estimate.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .config import (
    DEFAULT_BOUNDS,
    DEFAULT_CALIBRATION,
    DEFAULT_SCORING,
    Calibration,
    CrackModelType,
    InputBounds,
    IRC82Scoring,
    MonsoonZone,
    RoughnessModelType,
    RutModelType,
)
from .engine import IndianPavementDeteriorationEngine
from .hdm4 import DEFAULT_HDM4, HDM4RutCalibration
from .lifecycle import simulate_managed_lifecycle, treatment_cost
from .maintenance import MaintenancePolicy
from .models import SegmentInput


# Extra routine-maintenance burden by monsoon zone (drainage, edge repair,
# pothole recurrence). LOW = baseline; wetter zones cost more to hold to standard.
DEFAULT_MONSOON_ROUTINE_FACTOR: Dict[MonsoonZone, float] = {
    MonsoonZone.HIGH: 1.25,
    MonsoonZone.MEDIUM: 1.10,
    MonsoonZone.LOW: 1.00,
}


@dataclass
class PBMCParams:
    """Commercial parameters for pricing a PBMC. All rates in consistent cost
    units (e.g. Rs lakh); thicknesses/condition come from the engine."""

    term_years: int = 5                     # PBMC term (typically 5-7)
    performance_pci: float = 3.0            # contractual minimum IRC:82 PCI
    routine_rate_per_km_year: float = 1.5   # routine maintenance, year-1 prices
    base_unit_cost: float = 30.0            # per relative-cost-unit per km (treatments)
    min_treatment_interval: int = 2         # min years between periodic renewals
    escalation_rate: float = 0.05           # annual price escalation
    contingency_pct: float = 0.10           # risk/quantity contingency
    overhead_pct: float = 0.10              # contractor overhead + profit
    discount_rate: float = 0.08             # for NPV of the contract cash flow
    monsoon_routine_factor: Dict[MonsoonZone, float] = field(
        default_factory=lambda: dict(DEFAULT_MONSOON_ROUTINE_FACTOR)
    )

    def __post_init__(self) -> None:
        if not (1 <= self.term_years <= 10):
            raise ValueError("term_years out of range [1, 10] (PBMCs are typically 5-7).")
        if not (1.5 <= self.performance_pci <= 4.0):
            raise ValueError("performance_pci must be in [1.5, 4.0] (IRC:82 scale).")
        if self.routine_rate_per_km_year < 0:
            raise ValueError("routine_rate_per_km_year must be non-negative.")
        if self.base_unit_cost <= 0:
            raise ValueError("base_unit_cost must be positive.")
        for name in ("escalation_rate", "contingency_pct", "overhead_pct", "discount_rate"):
            v = getattr(self, name)
            if not (-0.5 <= v <= 1.0):
                raise ValueError(f"{name}={v} out of range [-0.5, 1.0].")

    def policy(self) -> MaintenancePolicy:
        """A maintenance policy whose preventive trigger is the service level,
        so the lifecycle schedules renewals to keep PCI >= performance_pci."""
        structural_lower = min(2.5, max(0.5, self.performance_pci - 0.3))
        return MaintenancePolicy(
            preventive_upper=self.performance_pci, structural_lower=structural_lower
        )

    def routine_factor(self, zone: MonsoonZone) -> float:
        return self.monsoon_routine_factor.get(zone, 1.0)


@dataclass
class PBMCYear:
    """One contract year's priced cash flow."""

    year: int
    routine: float
    periodic: float
    initial: float
    nominal_subtotal: float       # routine + periodic + initial, year-1 prices
    escalation_factor: float
    escalated: float              # nominal_subtotal * escalation_factor
    total: float                  # escalated, loaded with contingency + overhead
    discounted: float             # total discounted to present value
    treatments: List[str]

    def as_dict(self) -> dict:
        return {
            "year": self.year,
            "routine": round(self.routine, 2),
            "periodic": round(self.periodic, 2),
            "initial": round(self.initial, 2),
            "nominal_subtotal": round(self.nominal_subtotal, 2),
            "escalation_factor": round(self.escalation_factor, 4),
            "escalated": round(self.escalated, 2),
            "total": round(self.total, 2),
            "discounted": round(self.discounted, 2),
            "treatments": list(self.treatments),
        }


@dataclass
class PBMCEstimate:
    """A complete priced PBMC for one segment (or a network aggregate)."""

    segment_id: str
    term_years: int
    performance_pci: float
    length_km: float
    years: List[PBMCYear]
    initial_rectification: float
    initial_treatment: Optional[str]
    total_routine: float
    total_periodic: float
    total_nominal: float          # before escalation/loadings
    contract_value: float         # sum of loaded annual totals (nominal of the day)
    npv: float                    # discounted contract value
    cost_per_km: float            # contract_value / length
    interventions: List[dict]     # [{year, treatment, cost}]
    compliant: bool               # managed PCI kept >= service level across term
    min_pci: float
    rationale: str

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "term_years": self.term_years,
            "performance_pci": self.performance_pci,
            "length_km": round(self.length_km, 3),
            "years": [y.as_dict() for y in self.years],
            "initial_rectification": round(self.initial_rectification, 2),
            "initial_treatment": self.initial_treatment,
            "total_routine": round(self.total_routine, 2),
            "total_periodic": round(self.total_periodic, 2),
            "total_nominal": round(self.total_nominal, 2),
            "contract_value": round(self.contract_value, 2),
            "npv": round(self.npv, 2),
            "cost_per_km": round(self.cost_per_km, 2),
            "interventions": list(self.interventions),
            "compliant": self.compliant,
            "min_pci": round(self.min_pci, 2),
            "rationale": self.rationale,
        }


def estimate_pbmc(
    segment: SegmentInput,
    params: Optional[PBMCParams] = None,
    *,
    calibration: Calibration = DEFAULT_CALIBRATION,
    scoring: IRC82Scoring = DEFAULT_SCORING,
    bounds: InputBounds = DEFAULT_BOUNDS,
    rut_model: RutModelType = RutModelType.DEFAULT,
    hdm4_calibration: HDM4RutCalibration = DEFAULT_HDM4,
    crack_model: CrackModelType = CrackModelType.DEFAULT,
    roughness_model: RoughnessModelType = RoughnessModelType.DEFAULT,
) -> PBMCEstimate:
    """Price a PBMC for one segment over `params.term_years` (typically 5-7).

    Pipeline: classify the *current* condition (initial rectification if below
    the service level) -> simulate the managed lifecycle against the service-level
    policy (periodic renewals) -> add routine maintenance -> escalate, load with
    contingency + overhead, and discount to NPV.
    """
    params = params or PBMCParams()
    v = segment.validate(bounds)
    policy = params.policy()

    # 1. Current condition -> initial (handover) rectification.
    probe = IndianPavementDeteriorationEngine(
        base_iri=v.base_iri, base_rut=v.base_rut, base_crack=v.base_crack,
        annual_msa=v.annual_msa, traffic_growth_rate=v.traffic_growth_rate,
        monsoon_zone=v.monsoon_zone.value,
        calibration=calibration, scoring=scoring, bounds=bounds,
    )
    base_pci = probe.calculate_irc82_pci(v.base_iri, v.base_rut, v.base_crack)
    initial_rect = 0.0
    initial_treatment: Optional[str] = None
    sim_segment = v
    if base_pci < params.performance_pci:
        treatment = policy.recommended_treatment(base_pci)
        initial_rect = treatment_cost(treatment, v.length_km, params.base_unit_cost)
        initial_treatment = treatment.name
        # Hand the road over rectified, so the in-term forecast starts at standard.
        sim_segment = replace(
            v,
            base_iri=treatment.reset_iri if treatment.reset_iri is not None else v.base_iri,
            base_rut=treatment.reset_rut if treatment.reset_rut is not None else v.base_rut,
            base_crack=treatment.reset_crack if treatment.reset_crack is not None else v.base_crack,
        )

    # 2. Managed lifecycle over the term -> periodic renewal schedule + PCI path.
    managed = simulate_managed_lifecycle(
        sim_segment, params.term_years,
        policy=policy, base_unit_cost=params.base_unit_cost, morth_costing=False,
        min_treatment_interval=params.min_treatment_interval,
        calibration=calibration, scoring=scoring, bounds=bounds,
        rut_model=rut_model, hdm4_calibration=hdm4_calibration,
        crack_model=crack_model, roughness_model=roughness_model,
    )
    periodic_by_year: Dict[int, float] = {}
    treatments_by_year: Dict[int, List[str]] = {}
    for iv in managed.interventions:
        periodic_by_year[iv.year] = periodic_by_year.get(iv.year, 0.0) + iv.cost
        treatments_by_year.setdefault(iv.year, []).append(iv.treatment.name)

    min_pci = min((yr.irc82_pci for yr in managed.timeline), default=base_pci)
    compliant = min_pci >= params.performance_pci - 1e-9

    # 3. Routine maintenance (year-1 prices), with the monsoon-zone burden.
    routine_year1 = (
        params.routine_rate_per_km_year * v.length_km
        * params.routine_factor(v.monsoon_zone)
    )

    # 4. Assemble the priced cash flow.
    years: List[PBMCYear] = []
    loading = (1.0 + params.contingency_pct) * (1.0 + params.overhead_pct)
    total_routine = total_periodic = contract_value = npv = 0.0
    for y in range(1, params.term_years + 1):
        routine = routine_year1
        periodic = periodic_by_year.get(y, 0.0)
        initial = initial_rect if y == 1 else 0.0
        nominal = routine + periodic + initial
        factor = (1.0 + params.escalation_rate) ** (y - 1)
        escalated = nominal * factor
        total = escalated * loading
        discounted = total / ((1.0 + params.discount_rate) ** y)
        years.append(PBMCYear(
            year=y, routine=routine, periodic=periodic, initial=initial,
            nominal_subtotal=nominal, escalation_factor=factor, escalated=escalated,
            total=total, discounted=discounted,
            treatments=treatments_by_year.get(y, []),
        ))
        total_routine += routine
        total_periodic += periodic
        contract_value += total
        npv += discounted

    total_nominal = total_routine + total_periodic + initial_rect
    interventions = [
        {"year": iv.year, "treatment": iv.treatment.name, "cost": round(iv.cost, 2)}
        for iv in managed.interventions
    ]

    n_renewals = len(managed.interventions)
    rect_txt = (
        f"Handover rectification ({initial_treatment}, {initial_rect:.1f}) brings the "
        f"road from PCI {base_pci:.2f} up to the {params.performance_pci:.2f} service level. "
        if initial_treatment else
        f"Road meets the {params.performance_pci:.2f} service level at handover (PCI "
        f"{base_pci:.2f}); no initial rectification. "
    )
    rationale = (
        f"{params.term_years}-year PBMC for {v.segment_id}: {rect_txt}"
        f"{n_renewals} periodic renewal(s) scheduled to hold PCI >= "
        f"{params.performance_pci:.2f} (min reached {min_pci:.2f}). Contract value "
        f"{contract_value:.1f} (NPV {npv:.1f}) over {v.length_km:.1f} km = "
        f"{contract_value / v.length_km:.1f}/km, including {params.escalation_rate*100:.0f}% "
        f"escalation, {params.contingency_pct*100:.0f}% contingency and "
        f"{params.overhead_pct*100:.0f}% overhead."
    )
    return PBMCEstimate(
        segment_id=v.segment_id,
        term_years=params.term_years,
        performance_pci=params.performance_pci,
        length_km=v.length_km,
        years=years,
        initial_rectification=initial_rect,
        initial_treatment=initial_treatment,
        total_routine=total_routine,
        total_periodic=total_periodic,
        total_nominal=total_nominal,
        contract_value=contract_value,
        npv=npv,
        cost_per_km=contract_value / v.length_km,
        interventions=interventions,
        compliant=compliant,
        min_pci=min_pci,
        rationale=rationale,
    )


@dataclass
class PBMCNetworkEstimate:
    """Aggregate PBMC across a network plus the per-segment estimates."""

    term_years: int
    performance_pci: float
    n_segments: int
    total_length_km: float
    contract_value: float
    npv: float
    total_routine: float
    total_periodic: float
    total_initial: float
    non_compliant: List[str]
    segments: List[PBMCEstimate]

    def as_dict(self) -> dict:
        return {
            "term_years": self.term_years,
            "performance_pci": self.performance_pci,
            "n_segments": self.n_segments,
            "total_length_km": round(self.total_length_km, 2),
            "contract_value": round(self.contract_value, 2),
            "npv": round(self.npv, 2),
            "total_routine": round(self.total_routine, 2),
            "total_periodic": round(self.total_periodic, 2),
            "total_initial": round(self.total_initial, 2),
            "cost_per_km": round(self.contract_value / self.total_length_km, 2)
            if self.total_length_km else 0.0,
            "non_compliant": list(self.non_compliant),
            "segments": [e.as_dict() for e in self.segments],
        }


def estimate_pbmc_network(
    segments: List[SegmentInput],
    params: Optional[PBMCParams] = None,
    **engine_kwargs,
) -> PBMCNetworkEstimate:
    """Price a PBMC across a whole network (sum of per-segment estimates)."""
    params = params or PBMCParams()
    estimates = [estimate_pbmc(s, params, **engine_kwargs) for s in segments]
    return PBMCNetworkEstimate(
        term_years=params.term_years,
        performance_pci=params.performance_pci,
        n_segments=len(estimates),
        total_length_km=sum(e.length_km for e in estimates),
        contract_value=sum(e.contract_value for e in estimates),
        npv=sum(e.npv for e in estimates),
        total_routine=sum(e.total_routine for e in estimates),
        total_periodic=sum(e.total_periodic for e in estimates),
        total_initial=sum(e.initial_rectification for e in estimates),
        non_compliant=[e.segment_id for e in estimates if not e.compliant],
        segments=estimates,
    )
