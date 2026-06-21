"""
Indian Pavement Deterioration Engine.

Deterministic, year-by-year forward simulation of pavement condition under
Indian conditions, combining IRI roughness, cracking and rutting into an
IRC:82 composite Pavement Condition Score (0-4).

Faithful to the documented model. Two engineering changes vs the prototype:
  1. Pure standard library (math.pow), so it runs with zero third-party
     dependencies. numpy/pandas are optional accelerators (see batch.py),
     not requirements -- important for locked-down government deployments.
  2. All coefficients are injected via `Calibration`; inputs are validated.
"""
from __future__ import annotations

import math
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
from .hdm4 import DEFAULT_HDM4, HDM4RutCalibration, annual_rut_increment
from .models import SegmentInput, YearResult


class IndianPavementDeteriorationEngine:
    """Stateful single-segment simulator. One instance == one segment."""

    def __init__(
        self,
        base_iri: float,
        base_rut: float,
        base_crack: float,
        annual_msa: float,
        traffic_growth_rate: float,
        monsoon_zone: str,
        *,
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
        base_skid: float = 0.55,
        pothole_model: PotholeModelType = PotholeModelType.NONE,
        hdm4_pothole: HDM4PotholeModel = DEFAULT_HDM4_POTHOLE,
        base_potholes: float = 0.0,
        deflection_mm: float = 0.5,
        structural_number: float = 4.0,
        compaction_pct: float = 98.0,
        surfacing_thickness_mm: float = 100.0,
        cds: float = 1.0,
        heavy_speed_kmh: float = 50.0,
    ) -> None:
        """Initialise and validate a single homogeneous segment.

        Parameters mirror the field NSV ingestion record:
        - base_iri:  Initial IRI (mm/m)
        - base_rut:  Initial rut depth (mm)
        - base_crack: Initial cracking area (%)
        - annual_msa: Current traffic loading (Million Standard Axles / year)
        - traffic_growth_rate: Annual compound growth (0.05 == 5%)
        - monsoon_zone: 'HIGH' | 'MEDIUM' | 'LOW'

        Rut-model selection:
        - rut_model=DEFAULT uses the IRC:82-style power law (unchanged).
        - rut_model=HDM4 uses the mechanistic delta-RDM model, driven by the
          structural inputs (FWD deflection, structural number, compaction,
          surfacing thickness, CDS, heavy-vehicle speed).
        """
        validated = SegmentInput(
            base_iri=base_iri,
            base_rut=base_rut,
            base_crack=base_crack,
            annual_msa=annual_msa,
            traffic_growth_rate=traffic_growth_rate,
            monsoon_zone=monsoon_zone,
            deflection_mm=deflection_mm,
            structural_number=structural_number,
            compaction_pct=compaction_pct,
            surfacing_thickness_mm=surfacing_thickness_mm,
            cds=cds,
            heavy_speed_kmh=heavy_speed_kmh,
            base_skid=base_skid,
            base_potholes=base_potholes,
        ).validate(bounds)

        self.calibration = calibration
        self.scoring = scoring
        self.bounds = bounds
        self.rut_model = rut_model
        self.hdm4_calibration = hdm4_calibration
        self.crack_model = crack_model
        self.mlit_crack = mlit_crack
        self.roughness_model = roughness_model
        self.hdm4_roughness = hdm4_roughness
        self.skid_model = skid_model
        self.hdm4_skid = hdm4_skid
        self.pothole_model = pothole_model
        self.hdm4_pothole = hdm4_pothole

        # Mutable simulation state.
        self.iri = validated.base_iri
        self.rut = validated.base_rut
        self.crack = validated.base_crack
        self.annual_msa = validated.annual_msa
        self.growth_rate = validated.traffic_growth_rate
        self.zone: MonsoonZone = validated.monsoon_zone
        self.age = 0
        self.cumulative_msa = 0.0
        # Skid resistance state (SFC); only advanced when a skid model is active.
        self.skid = validated.base_skid
        # Potholing state (area %); only advanced when a pothole model is active.
        self.potholes = validated.base_potholes

        # Structural inputs (HDM-4 only).
        self.deflection_mm = validated.deflection_mm
        self.structural_number = validated.structural_number
        self.compaction_pct = validated.compaction_pct
        self.surfacing_thickness_mm = validated.surfacing_thickness_mm
        self.cds = validated.cds
        self.heavy_speed_kmh = validated.heavy_speed_kmh
        # Per-year HDM-4 component breakdown (empty unless HDM-4 is selected).
        self.rut_breakdown: List[Dict[str, float]] = []

        self.monsoon_multiplier = calibration.monsoon_multiplier(self.zone)

    # --- IRC:82 composite scoring ------------------------------------------

    def calculate_irc82_pci(self, iri: float, rut: float, crack: float) -> float:
        """Composite IRC:82 PCI in [1.0, 4.0]. 4.0 perfect, <1.0 critical.

        Each distress is converted to a deduct-value score, then combined
        with the IRC:82 weight split. Scores are clamped to [min, max].
        """
        s = self.scoring

        score_iri = (
            s.score_max
            if iri < s.iri_free_threshold
            else max(s.score_min, s.score_max - s.iri_deduct_rate * (iri - s.iri_free_threshold))
        )
        score_rut = (
            s.score_max
            if rut < s.rut_free_threshold
            else max(s.score_min, s.score_max - s.rut_deduct_rate * (rut - s.rut_free_threshold))
        )
        score_crack = (
            s.score_max
            if crack < s.crack_free_threshold
            else max(s.score_min, s.score_max - s.crack_deduct_rate * (crack - s.crack_free_threshold))
        )

        composite = (
            score_iri * s.weight_iri
            + score_rut * s.weight_rut
            + score_crack * s.weight_crack
        )
        return round(composite, 2)

    # --- single time step --------------------------------------------------

    def simulate_year(self) -> YearResult:
        """Advance exactly one calendar year, applying the deterioration laws."""
        c = self.calibration
        self.age += 1
        self.cumulative_msa += self.annual_msa
        rut_before, crack_before = self.rut, self.crack

        # Distresses advance rut -> crack -> roughness so the HDM-4 roughness
        # model can read this year's rut/crack increments. (The default laws are
        # mutually independent, so this ordering leaves their values unchanged.)

        # 1. Rutting progression -- pluggable law.
        if self.rut_model is RutModelType.HDM4:
            # Mechanistic HDM-4 delta-RDM. YE4 == this year's MSA; monsoon is
            # NOT applied (moisture enters structurally via DEF/SNP).
            inc = annual_rut_increment(
                self.hdm4_calibration,
                ye4=self.annual_msa,
                age=self.age,
                deflection_mm=self.deflection_mm,
                structural_number=self.structural_number,
                compaction_pct=self.compaction_pct,
                cds=self.cds,
                heavy_speed_kmh=self.heavy_speed_kmh,
                surfacing_thickness_mm=self.surfacing_thickness_mm,
            )
            rut_delta = inc.total
            self.rut_breakdown.append({"year": self.age, **inc.as_dict()})
        else:
            # IRC:82-style power law -- plastic deformation, monsoon-weighted.
            rut_delta = (
                c.rut_factor * math.pow(self.annual_msa, c.rut_exponent)
            ) * self.monsoon_multiplier
        self.rut = min(c.rut_cap, self.rut + rut_delta)

        # 2. Cracking progression -- pluggable law.
        if self.crack_model is CrackModelType.MLIT:
            # Paper's empirical recursion C_{i+1} = a + b * C_i (no traffic term).
            self.crack = min(c.crack_cap, self.mlit_crack.next_crack(self.crack))
        else:
            # IRC:82-style S-curve after a binder-oxidation lag.
            if self.age > c.crack_lag_years:
                crack_delta = c.crack_factor * math.pow(self.cumulative_msa, c.crack_exponent)
            else:
                crack_delta = c.crack_lag_factor * self.annual_msa
            self.crack = min(c.crack_cap, self.crack + crack_delta)

        # 3. Roughness (IRI) progression -- pluggable law.
        if self.roughness_model is RoughnessModelType.HDM4:
            # HDM-4 incremental roughness, coupled to this year's rut/crack rise.
            iri_delta = self.hdm4_roughness.increment(
                iri=self.iri, snp=self.structural_number, age=self.age,
                d_msa=self.annual_msa,
                d_crack_pct=self.crack - crack_before,
                d_rut_mm=self.rut - rut_before,
            )
        else:
            # IRC:82-style structural decay + traffic strain (monsoon-weighted).
            iri_delta = (c.iri_structural_factor * self.iri) + (
                c.iri_traffic_factor * self.annual_msa
            ) * self.monsoon_multiplier
        self.iri = min(c.iri_cap, self.iri + iri_delta)

        # 4. Skid resistance -- only advanced when a skid model is selected.
        if self.skid_model is SkidModelType.HDM4:
            self.skid = max(
                self.hdm4_skid.sfc_min,
                self.skid + self.hdm4_skid.increment(self.skid, self.annual_msa),
            )

        # 5. Potholing -- crack-initiated, only when a pothole model is selected.
        if self.pothole_model is PotholeModelType.HDM4:
            self.potholes = min(
                self.hdm4_pothole.cap_pct,
                self.potholes + self.hdm4_pothole.increment(self.crack, self.annual_msa),
            )

        # 6. Year-end composite KPI (computed on this year's condition).
        pci = self.calculate_irc82_pci(self.iri, self.rut, self.crack)

        # Compound traffic load for the next iteration.
        self.annual_msa *= (1.0 + self.growth_rate)

        return YearResult(
            year=self.age,
            cumulative_msa=self.cumulative_msa,
            iri=self.iri,
            rutting_mm=self.rut,
            cracking_pct=self.crack,
            irc82_pci=pci,
            skid=(round(self.skid, 3) if self.skid_model is SkidModelType.HDM4 else None),
            potholes=(round(self.potholes, 2) if self.pothole_model is PotholeModelType.HDM4 else None),
        )

    def run_lifecycle_forecast(self, horizon_years: int = 10) -> List[YearResult]:
        """Run a multi-year forward forecast and return the yearly timeline."""
        if not isinstance(horizon_years, int) or isinstance(horizon_years, bool):
            raise ValueError("horizon_years must be an integer.")
        if not (self.bounds.horizon_min <= horizon_years <= self.bounds.horizon_max):
            raise ValueError(
                f"horizon_years={horizon_years} out of range "
                f"[{self.bounds.horizon_min}, {self.bounds.horizon_max}]."
            )
        return [self.simulate_year() for _ in range(horizon_years)]

    # --- treatment reset hook ---------------------------------------------

    def apply_reset(
        self,
        *,
        iri: Optional[float] = None,
        rut: Optional[float] = None,
        crack: Optional[float] = None,
        skid: Optional[float] = None,
        potholes: Optional[float] = None,
    ) -> None:
        """Reset condition state after a maintenance treatment is applied.

        Used by the maintenance layer to model the post-treatment 'reset' of
        the simulation (e.g. microsurfacing restoring surface condition).
        Values are clamped to the engine's caps; None leaves a KPI untouched.
        """
        c = self.calibration
        if iri is not None:
            self.iri = max(0.0, min(c.iri_cap, float(iri)))
        if rut is not None:
            self.rut = max(0.0, min(c.rut_cap, float(rut)))
        if crack is not None:
            self.crack = max(0.0, min(c.crack_cap, float(crack)))
        if skid is not None:
            self.skid = max(0.0, min(1.0, float(skid)))
        if potholes is not None:
            self.potholes = max(0.0, min(100.0, float(potholes)))


def forecast_segment(
    segment: SegmentInput,
    horizon_years: int = 10,
    *,
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
) -> List[YearResult]:
    """Convenience wrapper: validated SegmentInput -> yearly timeline.

    Pass rut_model / crack_model / roughness_model to forecast with the
    mechanistic HDM-4 and MLIT models (using the segment's structural/FWD
    inputs) instead of the default IRC:82 laws.
    """
    v = segment.validate(bounds)
    engine = IndianPavementDeteriorationEngine(
        base_iri=v.base_iri,
        base_rut=v.base_rut,
        base_crack=v.base_crack,
        annual_msa=v.annual_msa,
        traffic_growth_rate=v.traffic_growth_rate,
        monsoon_zone=v.monsoon_zone.value,
        calibration=calibration,
        scoring=scoring,
        bounds=bounds,
        rut_model=rut_model,
        hdm4_calibration=hdm4_calibration,
        crack_model=crack_model,
        mlit_crack=mlit_crack,
        roughness_model=roughness_model,
        hdm4_roughness=hdm4_roughness,
        skid_model=skid_model,
        hdm4_skid=hdm4_skid,
        base_skid=v.base_skid,
        pothole_model=pothole_model,
        hdm4_pothole=hdm4_pothole,
        base_potholes=v.base_potholes,
        deflection_mm=v.deflection_mm,
        structural_number=v.structural_number,
        compaction_pct=v.compaction_pct,
        surfacing_thickness_mm=v.surfacing_thickness_mm,
        cds=v.cds,
        heavy_speed_kmh=v.heavy_speed_kmh,
    )
    return engine.run_lifecycle_forecast(horizon_years)
