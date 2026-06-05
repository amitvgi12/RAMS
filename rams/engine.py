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
from typing import List, Optional

from .config import (
    DEFAULT_BOUNDS,
    DEFAULT_CALIBRATION,
    DEFAULT_SCORING,
    Calibration,
    InputBounds,
    IRC82Scoring,
    MonsoonZone,
)
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
    ) -> None:
        """Initialise and validate a single homogeneous segment.

        Parameters mirror the field NSV ingestion record:
        - base_iri:  Initial IRI (mm/m)
        - base_rut:  Initial rut depth (mm)
        - base_crack: Initial cracking area (%)
        - annual_msa: Current traffic loading (Million Standard Axles / year)
        - traffic_growth_rate: Annual compound growth (0.05 == 5%)
        - monsoon_zone: 'HIGH' | 'MEDIUM' | 'LOW'
        """
        validated = SegmentInput(
            base_iri=base_iri,
            base_rut=base_rut,
            base_crack=base_crack,
            annual_msa=annual_msa,
            traffic_growth_rate=traffic_growth_rate,
            monsoon_zone=monsoon_zone,
        ).validate(bounds)

        self.calibration = calibration
        self.scoring = scoring
        self.bounds = bounds

        # Mutable simulation state.
        self.iri = validated.base_iri
        self.rut = validated.base_rut
        self.crack = validated.base_crack
        self.annual_msa = validated.annual_msa
        self.growth_rate = validated.traffic_growth_rate
        self.zone: MonsoonZone = validated.monsoon_zone
        self.age = 0
        self.cumulative_msa = 0.0

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

        # 1. IRI progression -- structural decay + traffic strain (monsoon-weighted).
        iri_delta = (c.iri_structural_factor * self.iri) + (
            c.iri_traffic_factor * self.annual_msa
        ) * self.monsoon_multiplier
        self.iri = min(c.iri_cap, self.iri + iri_delta)

        # 2. Rutting progression -- plastic deformation, monsoon-weighted.
        rut_delta = (
            c.rut_factor * math.pow(self.annual_msa, c.rut_exponent)
        ) * self.monsoon_multiplier
        self.rut = min(c.rut_cap, self.rut + rut_delta)

        # 3. Cracking progression -- S-curve after a binder-oxidation lag.
        if self.age > c.crack_lag_years:
            crack_delta = c.crack_factor * math.pow(self.cumulative_msa, c.crack_exponent)
        else:
            crack_delta = c.crack_lag_factor * self.annual_msa
        self.crack = min(c.crack_cap, self.crack + crack_delta)

        # 4. Year-end composite KPI (computed on this year's condition).
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


def forecast_segment(
    segment: SegmentInput,
    horizon_years: int = 10,
    *,
    calibration: Calibration = DEFAULT_CALIBRATION,
    scoring: IRC82Scoring = DEFAULT_SCORING,
    bounds: InputBounds = DEFAULT_BOUNDS,
) -> List[YearResult]:
    """Convenience wrapper: validated SegmentInput -> yearly timeline."""
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
    )
    return engine.run_lifecycle_forecast(horizon_years)
