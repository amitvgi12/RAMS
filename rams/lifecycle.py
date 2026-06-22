"""
Treatment-aware lifecycle simulation (MoRTH catalog reset values in action).

Where engine.py projects *untreated* decay, this module projects the
trajectory of a *managed* asset: when the PCI enters a maintenance band, the
recommended MoRTH treatment is applied, the condition state is reset using the
catalog's reset values, and the simulation continues from the restored state.

This is what makes the catalog's `reset_*` values meaningful, and it produces
the "treated vs untreated" comparison shown in the web dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .config import (
    DEFAULT_BOUNDS,
    DEFAULT_CALIBRATION,
    DEFAULT_SCORING,
    Calibration,
    CrackModelType,
    InputBounds,
    IRC82Scoring,
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
from .engine import IndianPavementDeteriorationEngine
from .hdm4 import DEFAULT_HDM4, HDM4RutCalibration
from .maintenance import MaintenanceFlag, MaintenancePolicy, Treatment
from .models import SegmentInput, YearResult
from .morth import cost_for_treatment_code_lakh


@dataclass
class Intervention:
    """A treatment actually applied in the managed trajectory."""

    year: int
    treatment: Treatment
    cost: float
    pci_before: float
    pci_after: float


@dataclass
class ManagedLifecycle:
    """Result of a treatment-aware simulation."""

    timeline: List[YearResult]          # treated trajectory (post-reset each year)
    interventions: List[Intervention]
    total_cost: float

    def treated_pci(self) -> List[float]:
        return [yr.irc82_pci for yr in self.timeline]


def treatment_cost(treatment: Treatment, length_km: float, base_unit_cost: float) -> float:
    """Cost of a treatment = relative_cost x base_unit_cost x length (cost units)."""
    return round(treatment.relative_cost * base_unit_cost * length_km, 2)


def simulate_managed_lifecycle(
    segment: SegmentInput,
    horizon_years: int = 10,
    *,
    policy: Optional[MaintenancePolicy] = None,
    base_unit_cost: float = 30.0,
    width_m: float = 7.0,
    morth_costing: bool = True,
    min_treatment_interval: int = 3,
    calibration: Calibration = DEFAULT_CALIBRATION,
    scoring: IRC82Scoring = DEFAULT_SCORING,
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
) -> ManagedLifecycle:
    """Simulate one segment with interventions applied as bands are entered.

    Policy: each year, classify the year-end PCI. If it is not ROUTINE and at
    least `min_treatment_interval` years have passed since the last treatment,
    apply the band's recommended treatment, reset condition state to the
    catalog reset values, and record the intervention + cost. The PCI is then
    recomputed on the restored state for that year.
    """
    policy = policy or MaintenancePolicy()
    v = segment.validate(bounds)
    engine = IndianPavementDeteriorationEngine(
        base_iri=v.base_iri, base_rut=v.base_rut, base_crack=v.base_crack,
        annual_msa=v.annual_msa, traffic_growth_rate=v.traffic_growth_rate,
        monsoon_zone=v.monsoon_zone.value,
        calibration=calibration, scoring=scoring, bounds=bounds,
        rut_model=rut_model, hdm4_calibration=hdm4_calibration,
        crack_model=crack_model, mlit_crack=mlit_crack,
        roughness_model=roughness_model, hdm4_roughness=hdm4_roughness,
        skid_model=skid_model, hdm4_skid=hdm4_skid, base_skid=v.base_skid,
        pothole_model=pothole_model, hdm4_pothole=hdm4_pothole, base_potholes=v.base_potholes,
        deflection_mm=v.deflection_mm, structural_number=v.structural_number,
        compaction_pct=v.compaction_pct, surfacing_thickness_mm=v.surfacing_thickness_mm,
        cds=v.cds, heavy_speed_kmh=v.heavy_speed_kmh,
    )

    timeline: List[YearResult] = []
    interventions: List[Intervention] = []
    total_cost = 0.0
    last_treatment_year = -10_000

    for _ in range(horizon_years):
        yr = engine.simulate_year()
        flag = policy.classify(yr.irc82_pci)

        if flag is not MaintenanceFlag.ROUTINE and (
            yr.year - last_treatment_year >= min_treatment_interval
        ):
            treatment = policy.recommended_treatment(yr.irc82_pci)
            pci_before = yr.irc82_pci
            engine.apply_reset(
                iri=treatment.reset_iri,
                rut=treatment.reset_rut,
                crack=treatment.reset_crack,
                # A new wearing course restores skid resistance and removes potholes.
                skid=v.base_skid if skid_model is SkidModelType.HDM4 else None,
                potholes=0.0 if pothole_model is PotholeModelType.HDM4 else None,
            )
            # Recompute this year's KPIs/PCI on the restored condition.
            yr.iri, yr.rutting_mm, yr.cracking_pct = engine.iri, engine.rut, engine.crack
            yr.irc82_pci = engine.calculate_irc82_pci(engine.iri, engine.rut, engine.crack)
            if skid_model is SkidModelType.HDM4:
                yr.skid = round(engine.skid, 3)
            if pothole_model is PotholeModelType.HDM4:
                yr.potholes = round(engine.potholes, 2)
            yr.treatment = treatment.name

            cost = (
                round(cost_for_treatment_code_lakh(treatment.code, v.length_km, width_m), 2)
                if morth_costing
                else treatment_cost(treatment, v.length_km, base_unit_cost)
            )
            total_cost += cost
            interventions.append(
                Intervention(
                    year=yr.year, treatment=treatment, cost=cost,
                    pci_before=pci_before, pci_after=yr.irc82_pci,
                )
            )
            last_treatment_year = yr.year

        timeline.append(yr)

    return ManagedLifecycle(
        timeline=timeline, interventions=interventions, total_cost=round(total_cost, 2)
    )
