"""
Remaining structural (fatigue) life -- IRC:81 deflection life + IRC:37 budget.

A surface-condition forecast says *when the road will look bad*; a concessionaire
on a BOT/HAM contract must also prove *how much structural life is left* -- at
handback, and to time strengthening overlays. This module produces that scalar.

Two independent estimates, and we report the governing (minimum) one:

  1. Deflection-based (IRC:81 / Benkelman). The measured rebound deflection maps
     to the cumulative standard axles the *current* structure can still carry
     before strengthening is required:

         N_allow (MSA) = a * DEF^(-b)

     i.e. higher deflection (weaker pavement) -> fewer remaining MSA. `a`, `b`
     are the deflection-life coefficients (IRC:81-style defaults below; override
     with your agency's calibrated relationship).

  2. Traffic budget (IRC:37). The pavement was designed for `design_msa`; the
     fatigue life already consumed is `cumulative_msa`, so the remaining design
     budget is `design_msa - cumulative_msa`.

The remaining life in *years* is found by growing the current annual MSA forward
(compounding at the traffic growth rate) until it consumes the governing
remaining MSA -- consistent with how the engine accumulates traffic.

All coefficients/limits are illustrative defaults seeded to common Indian
practice; none should reach production without local calibration.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class DeflectionLifeModel:
    """IRC:81-style deflection->allowable-traffic relationship.

    N_allow (MSA) = coeff_a * deflection_mm ** (-coeff_b).
    Defaults give ~40 MSA at 0.5 mm, ~5 MSA at 1.0 mm, ~1.5 MSA at 1.5 mm.
    """

    coeff_a: float = 5.0
    coeff_b: float = 3.0

    def allowable_msa(self, deflection_mm: float) -> float:
        d = max(1e-6, float(deflection_mm))
        return self.coeff_a * math.pow(d, -self.coeff_b)

    def deflection_for_msa(self, target_msa: float) -> float:
        """Inverse: the deflection a structure must reach to carry `target_msa`.

        Used to size a strengthening overlay (it must lower DEF to <= this)."""
        n = max(1e-6, float(target_msa))
        return math.pow(self.coeff_a / n, 1.0 / self.coeff_b)


DEFAULT_DEFLECTION_LIFE = DeflectionLifeModel()


class HandbackVerdict(str, Enum):
    PASS = "PASS"            # residual life comfortably above the requirement
    MARGINAL = "MARGINAL"    # within 20% of the requirement
    FAIL = "FAIL"            # below the requirement -> strengthening needed


@dataclass
class ResidualLife:
    """Remaining structural life from the deflection and traffic-budget views."""

    allowable_msa_deflection: float    # IRC:81 view (current structure)
    remaining_msa_traffic: float       # IRC:37 view (design budget left)
    governing_remaining_msa: float     # min of the two -- the binding limit
    governing_basis: str               # "deflection (IRC:81)" | "traffic budget (IRC:37)"
    residual_years: Optional[float]    # None if traffic is ~0 (never consumed)
    consumed_fraction: Optional[float]  # cumulative/design, if design_msa given
    rationale: str

    def as_dict(self) -> dict:
        return {
            "allowable_msa_deflection": round(self.allowable_msa_deflection, 2),
            "remaining_msa_traffic": (
                round(self.remaining_msa_traffic, 2)
                if self.remaining_msa_traffic == self.remaining_msa_traffic else None
            ),
            "governing_remaining_msa": round(self.governing_remaining_msa, 2),
            "governing_basis": self.governing_basis,
            "residual_years": (
                round(self.residual_years, 1) if self.residual_years is not None else None
            ),
            "consumed_fraction": (
                round(self.consumed_fraction, 3) if self.consumed_fraction is not None else None
            ),
            "rationale": self.rationale,
        }


def _years_to_consume(remaining_msa: float, annual_msa: float, growth: float) -> Optional[float]:
    """Years for traffic (annual_msa, compounding at `growth`) to sum to remaining."""
    if remaining_msa <= 0:
        return 0.0
    if annual_msa <= 0:
        return None  # no traffic -> never consumed
    if abs(growth) < 1e-9:
        return remaining_msa / annual_msa
    # cumulative over t years = annual_msa * ((1+g)^t - 1)/g  ->  solve for t
    ratio = 1.0 + growth * remaining_msa / annual_msa
    if ratio <= 0:  # declining traffic that never reaches the target
        return None
    return math.log(ratio) / math.log(1.0 + growth)


def remaining_fatigue_life(
    *,
    deflection_mm: float,
    annual_msa: float,
    traffic_growth_rate: float = 0.0,
    cumulative_msa: float = 0.0,
    design_msa: Optional[float] = None,
    model: DeflectionLifeModel = DEFAULT_DEFLECTION_LIFE,
) -> ResidualLife:
    """Compute remaining structural life (the governing of IRC:81 vs IRC:37).

    `cumulative_msa` is traffic carried since the last structural renewal;
    `design_msa` (optional) enables the IRC:37 traffic-budget view.
    """
    allow_def = model.allowable_msa(deflection_mm)

    if design_msa is not None and design_msa > 0:
        rem_traffic = max(0.0, design_msa - cumulative_msa)
        consumed = cumulative_msa / design_msa
    else:
        rem_traffic = float("nan")
        consumed = None

    candidates = [(allow_def, "deflection (IRC:81)")]
    if design_msa is not None and design_msa > 0:
        candidates.append((rem_traffic, "traffic budget (IRC:37)"))
    governing, basis = min(candidates, key=lambda c: c[0])

    years = _years_to_consume(governing, annual_msa, traffic_growth_rate)
    yrs_txt = "never (no traffic)" if years is None else f"~{years:.1f} yr"
    rationale = (
        f"Governing remaining life {governing:.1f} MSA ({basis}) -> {yrs_txt} at the "
        f"current {annual_msa:.1f} MSA/yr"
        + (f", {growth_pct}% growth." if (growth_pct := round(traffic_growth_rate * 100, 1)) else ".")
    )
    return ResidualLife(
        allowable_msa_deflection=allow_def,
        remaining_msa_traffic=rem_traffic,
        governing_remaining_msa=governing,
        governing_basis=basis,
        residual_years=years,
        consumed_fraction=consumed,
        rationale=rationale,
    )


@dataclass
class HandbackAssessment:
    verdict: HandbackVerdict
    required_residual_msa: float
    governing_remaining_msa: float
    shortfall_msa: float                 # >0 if FAIL/MARGINAL
    overlay_target_deflection_mm: Optional[float]  # deflection to reach to comply
    rationale: str

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "required_residual_msa": round(self.required_residual_msa, 2),
            "governing_remaining_msa": round(self.governing_remaining_msa, 2),
            "shortfall_msa": round(self.shortfall_msa, 2),
            "overlay_target_deflection_mm": (
                round(self.overlay_target_deflection_mm, 3)
                if self.overlay_target_deflection_mm is not None else None
            ),
            "rationale": self.rationale,
        }


def handback_assessment(
    residual: ResidualLife,
    *,
    required_residual_msa: float,
    model: DeflectionLifeModel = DEFAULT_DEFLECTION_LIFE,
) -> HandbackAssessment:
    """Judge a residual-life result against a contractual handback requirement.

    Returns PASS / MARGINAL (within 20%) / FAIL, and -- when short -- the
    deflection a strengthening overlay must achieve to meet the requirement.
    """
    have = residual.governing_remaining_msa
    shortfall = max(0.0, required_residual_msa - have)
    if have >= required_residual_msa:
        verdict = (
            HandbackVerdict.MARGINAL
            if have < required_residual_msa * 1.2
            else HandbackVerdict.PASS
        )
        overlay_def = None
    else:
        verdict = HandbackVerdict.FAIL
        overlay_def = model.deflection_for_msa(required_residual_msa)

    if verdict is HandbackVerdict.PASS:
        rationale = (
            f"PASS: {have:.1f} MSA residual >= required {required_residual_msa:.1f} MSA."
        )
    elif verdict is HandbackVerdict.MARGINAL:
        rationale = (
            f"MARGINAL: {have:.1f} MSA residual is within 20% of the required "
            f"{required_residual_msa:.1f} MSA -- monitor / plan strengthening."
        )
    else:
        rationale = (
            f"FAIL: {have:.1f} MSA residual is {shortfall:.1f} MSA short of the "
            f"required {required_residual_msa:.1f} MSA. A strengthening overlay must "
            f"bring rebound deflection to <= {overlay_def:.2f} mm."
        )
    return HandbackAssessment(
        verdict=verdict,
        required_residual_msa=required_residual_msa,
        governing_remaining_msa=have,
        shortfall_msa=shortfall,
        overlay_target_deflection_mm=overlay_def,
        rationale=rationale,
    )
