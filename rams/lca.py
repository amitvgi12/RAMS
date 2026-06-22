"""
Life-Cycle Analysis (LCA) decision matrix over a user-defined horizon.

Given a segment's current condition and a number of years, this projects the
deterioration forward and, each year, fixes the maintenance **decision** the
condition triggers -- routine -> preventive -> structural overlay ->
reconstruction -- using IRC thresholds. A major treatment resets the condition
(IRC/MoRTH reset values) and the projection continues, so the matrix shows *when*
overlays/rebuilds fall due across the life cycle. Every action is costed from the
MoRTH Standard Data Book (`morth.py`), and the life cycle is summarised by total
cost, present value (NPV) and the equivalent uniform annual cost (EUAC).

This is the decision/LCA layer of the PMS: it consumes the engineering forecast
and the MoRTH cost basis and produces the year-by-year capital plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import (
    DEFAULT_BOUNDS,
    DEFAULT_CALIBRATION,
    DEFAULT_SCORING,
    Calibration,
    CrackModelType,
    InputBounds,
    IRC82Scoring,
    RoughnessModelType,
    RutModelType,
)
from .engine import IndianPavementDeteriorationEngine
from .hdm4 import DEFAULT_HDM4, HDM4RutCalibration
from .models import SegmentInput
from .morth import (
    DEFAULT_CARRIAGEWAY_WIDTH_M,
    MORTH_RATES,
    Decision,
    MorthRate,
    treated_area_sqm,
    treatment_cost_inr,
)


@dataclass(frozen=True)
class LCAThresholds:
    """Condition thresholds that trigger each life-cycle decision (IRC-based)."""

    preventive_pci: float = 3.20    # below -> preventive due
    overlay_pci: float = 2.50       # below -> structural overlay due
    reconstruction_pci: float = 1.50  # below -> reconstruction due
    rut_overlay_mm: float = 20.0    # IRC:81 structural rutting
    crack_overlay_pct: float = 20.0  # IRC:37 structural cracking
    iri_overlay: float = 4.0        # NHAI O&M structural roughness (mm/m)
    min_major_interval: int = 3     # min years between major treatments


DEFAULT_LCA_THRESHOLDS = LCAThresholds()


def _decide(pci: float, rut: float, crack: float, iri: float,
            th: LCAThresholds) -> Decision:
    if pci < th.reconstruction_pci:
        return Decision.RECONSTRUCTION
    if (pci < th.overlay_pci or rut >= th.rut_overlay_mm
            or crack >= th.crack_overlay_pct or iri >= th.iri_overlay):
        return Decision.OVERLAY
    if pci < th.preventive_pci:
        return Decision.PREVENTIVE
    return Decision.ROUTINE


@dataclass
class LCAYear:
    """One year of the life-cycle decision matrix."""

    year: int
    cumulative_msa: float
    iri: float
    rut: float
    crack: float
    pci: float
    decision: str
    treatment: str
    morth_reference: str
    cost_inr: float
    deferred: bool          # a major treatment was due but the min-interval blocked it

    def as_dict(self) -> dict:
        return {
            "year": self.year,
            "cumulative_msa": round(self.cumulative_msa, 2),
            "iri": round(self.iri, 2),
            "rut": round(self.rut, 1),
            "crack": round(self.crack, 1),
            "pci": round(self.pci, 2),
            "decision": self.decision,
            "treatment": self.treatment,
            "morth_reference": self.morth_reference,
            "cost_inr": round(self.cost_inr, 0),
            "cost_lakh": round(self.cost_inr / 1e5, 2),
            "deferred": self.deferred,
        }


@dataclass
class LCAResult:
    """The full life-cycle decision matrix + economics for one segment."""

    segment_id: str
    horizon_years: int
    length_km: float
    width_m: float
    discount_rate: float
    years: List[LCAYear]
    total_cost_inr: float
    npv_inr: float
    euac_inr: float
    n_preventive: int
    n_overlay: int
    n_reconstruction: int
    final_pci: float
    rationale: str

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "horizon_years": self.horizon_years,
            "length_km": round(self.length_km, 3),
            "width_m": self.width_m,
            "discount_rate": self.discount_rate,
            "years": [y.as_dict() for y in self.years],
            "total_cost_inr": round(self.total_cost_inr, 0),
            "total_cost_lakh": round(self.total_cost_inr / 1e5, 2),
            "total_cost_crore": round(self.total_cost_inr / 1e7, 3),
            "npv_inr": round(self.npv_inr, 0),
            "npv_lakh": round(self.npv_inr / 1e5, 2),
            "euac_inr": round(self.euac_inr, 0),
            "euac_lakh": round(self.euac_inr / 1e5, 2),
            "n_preventive": self.n_preventive,
            "n_overlay": self.n_overlay,
            "n_reconstruction": self.n_reconstruction,
            "final_pci": round(self.final_pci, 2),
            "rationale": self.rationale,
        }


def lca_matrix(
    segment: SegmentInput,
    horizon_years: int,
    *,
    width_m: float = DEFAULT_CARRIAGEWAY_WIDTH_M,
    discount_rate: float = 0.08,
    thresholds: LCAThresholds = DEFAULT_LCA_THRESHOLDS,
    rates: Dict[Decision, MorthRate] = MORTH_RATES,
    calibration: Calibration = DEFAULT_CALIBRATION,
    scoring: IRC82Scoring = DEFAULT_SCORING,
    bounds: InputBounds = DEFAULT_BOUNDS,
    rut_model: RutModelType = RutModelType.DEFAULT,
    hdm4_calibration: HDM4RutCalibration = DEFAULT_HDM4,
    crack_model: CrackModelType = CrackModelType.DEFAULT,
    roughness_model: RoughnessModelType = RoughnessModelType.DEFAULT,
) -> LCAResult:
    """Project `horizon_years` of deterioration, decide + cost each year (MoRTH)."""
    if not (1 <= horizon_years <= 100):
        raise ValueError("horizon_years out of range [1, 100].")
    if width_m <= 0:
        raise ValueError("width_m must be positive.")
    v = segment.validate(bounds)
    engine = IndianPavementDeteriorationEngine(
        base_iri=v.base_iri, base_rut=v.base_rut, base_crack=v.base_crack,
        annual_msa=v.annual_msa, traffic_growth_rate=v.traffic_growth_rate,
        monsoon_zone=v.monsoon_zone.value,
        calibration=calibration, scoring=scoring, bounds=bounds,
        rut_model=rut_model, hdm4_calibration=hdm4_calibration,
        crack_model=crack_model, roughness_model=roughness_model,
        deflection_mm=v.deflection_mm, structural_number=v.structural_number,
    )
    area = treated_area_sqm(v.length_km, width_m)

    rows: List[LCAYear] = []
    total = 0.0
    npv = 0.0
    last_major = -10_000
    counts = {Decision.PREVENTIVE: 0, Decision.OVERLAY: 0, Decision.RECONSTRUCTION: 0}

    for _ in range(horizon_years):
        yr = engine.simulate_year()
        decision = _decide(yr.irc82_pci, yr.rutting_mm, yr.cracking_pct, yr.iri, thresholds)
        deferred = False

        if decision is Decision.ROUTINE:
            applied = Decision.ROUTINE
        elif yr.year - last_major >= thresholds.min_major_interval:
            applied = decision
            rate = rates[decision]
            engine.apply_reset(iri=rate.reset_iri, rut=rate.reset_rut, crack=rate.reset_crack)
            yr.iri, yr.rutting_mm, yr.cracking_pct = engine.iri, engine.rut, engine.crack
            yr.irc82_pci = engine.calculate_irc82_pci(engine.iri, engine.rut, engine.crack)
            last_major = yr.year
            counts[decision] += 1
        else:
            # Major treatment due but blocked by the minimum interval -> hold on
            # routine this year and flag the deferral.
            applied = Decision.ROUTINE
            deferred = True

        cost = treatment_cost_inr(applied, v.length_km, width_m, rates)
        discounted = cost / ((1.0 + discount_rate) ** yr.year)
        total += cost
        npv += discounted
        rows.append(LCAYear(
            year=yr.year, cumulative_msa=yr.cumulative_msa, iri=yr.iri,
            rut=yr.rutting_mm, crack=yr.cracking_pct, pci=yr.irc82_pci,
            decision=applied.value, treatment=rates[applied].name,
            morth_reference=rates[applied].morth_reference,
            cost_inr=cost, deferred=deferred,
        ))

    euac = _euac(npv, discount_rate, horizon_years)
    rationale = (
        f"{horizon_years}-yr life cycle for {v.segment_id} ({v.length_km:.1f} km x "
        f"{width_m:.1f} m = {area/1e4:.2f} ha): {counts[Decision.PREVENTIVE]} preventive, "
        f"{counts[Decision.OVERLAY]} overlay, {counts[Decision.RECONSTRUCTION]} reconstruction. "
        f"Total Rs {total/1e5:.1f} lakh (NPV Rs {npv/1e5:.1f} lakh, EUAC Rs {euac/1e5:.1f} "
        f"lakh/yr at {discount_rate*100:.0f}% discount), MoRTH SDB rates."
    )
    return LCAResult(
        segment_id=v.segment_id, horizon_years=horizon_years, length_km=v.length_km,
        width_m=width_m, discount_rate=discount_rate, years=rows,
        total_cost_inr=total, npv_inr=npv, euac_inr=euac,
        n_preventive=counts[Decision.PREVENTIVE], n_overlay=counts[Decision.OVERLAY],
        n_reconstruction=counts[Decision.RECONSTRUCTION],
        final_pci=rows[-1].pci if rows else 0.0, rationale=rationale,
    )


def _euac(npv: float, rate: float, n: int) -> float:
    """Equivalent uniform annual cost = NPV x capital-recovery factor."""
    if n <= 0:
        return 0.0
    if abs(rate) < 1e-9:
        return npv / n
    crf = rate * (1.0 + rate) ** n / ((1.0 + rate) ** n - 1.0)
    return npv * crf
