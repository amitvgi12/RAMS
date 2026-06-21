"""
IRC:37 traffic loading -- commercial vehicles (CVPD) + Vehicle Damage Factor
(VDF) -> design traffic in Million Standard Axles (MSA).

Indian network-level PMS keys structural design and the fatigue-life trigger to
MSA, not to US axle-load spectra. This module turns the natively-collected
quantities (CVPD from toll/ATCC counters, VDF reflecting overloading) into:

  * design_msa   -- cumulative standard axles over the design life (IRC:37), the
                    fatigue budget the residual-life model consumes, and
  * annual_msa   -- the first-year standard axles per lane (feeds the engine).

IRC:37 cumulative-repetitions formula:

    N = (365 * ((1 + r)^n - 1) / r) * A * D * F / 1e6

    A = initial CVPD (commercial vehicles/day, both directions)
    r = annual growth rate (fraction)
    n = design life (years)
    D = lane distribution factor
    F = VDF (standard axles per commercial vehicle)

The VDF defaults below are IRC:37 *indicative* values (terrain x initial-traffic
band); they explicitly bake in Indian overloading and must be replaced by an
axle-load survey for design. Everything here is overridable.
"""
from __future__ import annotations

from dataclasses import dataclass

# IRC:37 lane-distribution factor D by carriageway type.
LANE_DISTRIBUTION = {
    "single": 1.00,        # single-lane: all CV in one path
    "intermediate": 0.75,  # intermediate-lane carriageway
    "two_lane": 0.75,      # 2-lane single carriageway (two-way)
    "four_lane": 0.40,     # dual 2-lane (per direction, outer lane)
    "six_lane": 0.40,
}

# IRC:37 indicative VDF (standard axles / commercial vehicle), by initial CVPD
# band and terrain. Captures higher damage on heavily-trafficked/plain corridors.
_VDF_TABLE = {
    "plain": ((150, 1.5), (1500, 3.5), (float("inf"), 4.5)),
    "rolling": ((150, 1.5), (1500, 3.5), (float("inf"), 4.5)),
    "hilly": ((150, 0.5), (1500, 1.5), (float("inf"), 2.5)),
}


def default_vdf(cvpd: float, terrain: str = "plain") -> float:
    """IRC:37 indicative VDF for an initial CVPD and terrain (overridable)."""
    bands = _VDF_TABLE.get(str(terrain).strip().lower())
    if bands is None:
        raise ValueError(f"unknown terrain {terrain!r}; expected plain, rolling or hilly.")
    for upper, vdf in bands:
        if cvpd < upper:
            return vdf
    return bands[-1][1]


def lane_distribution_factor(carriageway: str) -> float:
    key = str(carriageway).strip().lower()
    if key not in LANE_DISTRIBUTION:
        raise ValueError(
            f"unknown carriageway {carriageway!r}; expected one of: "
            f"{', '.join(LANE_DISTRIBUTION)}."
        )
    return LANE_DISTRIBUTION[key]


@dataclass
class TrafficLoading:
    """Result of an IRC:37 traffic computation."""

    cvpd: float
    vdf: float
    growth_rate: float
    design_life_years: int
    lane_distribution: float
    annual_msa: float   # first-year standard axles per lane (millions)
    design_msa: float   # cumulative standard axles over the design life (millions)

    def as_dict(self) -> dict:
        return {
            "cvpd": self.cvpd,
            "vdf": round(self.vdf, 3),
            "growth_rate": self.growth_rate,
            "design_life_years": self.design_life_years,
            "lane_distribution": self.lane_distribution,
            "annual_msa": round(self.annual_msa, 3),
            "design_msa": round(self.design_msa, 2),
        }


def design_msa(
    cvpd: float,
    *,
    vdf: float,
    growth_rate: float = 0.05,
    design_life_years: int = 15,
    lane_distribution: float = 0.75,
) -> TrafficLoading:
    """Compute IRC:37 design (cumulative) and first-year MSA from CVPD + VDF."""
    if cvpd < 0:
        raise ValueError("cvpd must be non-negative.")
    if vdf <= 0:
        raise ValueError("vdf must be positive.")
    if not (1 <= design_life_years <= 100):
        raise ValueError("design_life_years out of range [1, 100].")
    if not (0.0 <= lane_distribution <= 1.0):
        raise ValueError("lane_distribution must be in [0, 1].")

    r, n, D, F = growth_rate, design_life_years, lane_distribution, vdf
    if abs(r) < 1e-9:
        cumulative_cv = cvpd * 365.0 * n
    else:
        cumulative_cv = cvpd * 365.0 * ((pow(1.0 + r, n) - 1.0) / r)
    cum_msa = cumulative_cv * D * F / 1.0e6
    annual_msa = cvpd * 365.0 * D * F / 1.0e6
    return TrafficLoading(
        cvpd=cvpd, vdf=vdf, growth_rate=growth_rate, design_life_years=n,
        lane_distribution=D, annual_msa=annual_msa, design_msa=cum_msa,
    )
