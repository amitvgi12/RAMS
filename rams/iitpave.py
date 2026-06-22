"""
Mechanistic layered-elastic pavement analysis (IITPAVE-style) for IRC:37-2018.

IRC:37-2018 sizes a flexible pavement *mechanistically*: a layered-elastic
analysis (the IRC tool is IITPAVE) computes the two critical strains under the
standard axle, and the pavement is adequate when both exceed the design traffic:

  * horizontal tensile strain at the bottom of the bituminous layer (eps_t)
    -> bottom-up fatigue cracking life (Nf), and
  * vertical compressive strain on top of the subgrade (eps_v)
    -> subgrade rutting life (Nr).

RAMS cannot ship the IITPAVE binary (Windows Fortran, IRC-distributed), so this
module computes the same two strains with the **Odemark--Boussinesq method of
equivalent thickness (MET)** -- a documented, citable layered-elastic
approximation in the IITPAVE/IRC:37 tradition:

  1. Upper layers are transformed to an equivalent thickness of the layer below
     by `h_e = f * h * (E_upper/E_lower)^(1/3)` (Odemark).
  2. The strain is read from the Boussinesq closed-form solution for a uniformly
     loaded circular area on the resulting elastic half-space, at the equivalent
     depth.
  3. The standard axle is modelled as a dual wheel (20 kN/wheel, 0.56 MPa,
     310 mm spacing); the second wheel is superposed (point-load approximation)
     for the subgrade strain -- without it eps_v is badly under-predicted.

This is an engineering approximation: it lands within the IRC:37 strain range
and is monotonic and self-consistent for design search, but a final design must
be confirmed against IITPAVE proper. Every constant (the MET factor `f`, the
Poisson ratios, the load) is overridable -- coefficients-as-data, like the rest
of RAMS. The fatigue/rutting performance laws are reused from `design.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from .design import (
    DEFAULT_PERFORMANCE,
    PerformanceModel,
    _reliability_for_traffic,
    fatigue_life_msa,
    fatigue_life_msa_irc115,
    rutting_life_msa,
    subgrade_modulus_mpa,
)

# --- IRC:37 standard-axle load configuration --------------------------------
STD_WHEEL_LOAD_N = 20_000.0     # one wheel of the 80 kN standard axle (dual wheel)
TYRE_PRESSURE_MPA = 0.56        # IRC:37 contact pressure
DUAL_SPACING_MM = 310.0         # centre-to-centre spacing of a dual wheel set

# Odemark-Boussinesq -> IITPAVE calibration factors. The MET tensile strain at
# the bottom of the bituminous layer systematically under-predicts the rigorous
# layered-elastic (IITPAVE) value; the subgrade vertical strain is close. These
# multipliers are fitted to real IITPAVE outputs -- the IRC:37-2018 worked
# examples (Annex II, Poisson 0.35) and the NH-152D FWD evaluation report
# (IRC:115-2014, Poisson 0.5/0.4/0.4) -- and bring eps_t / eps_v to within
# ~5-8% of IITPAVE over 50-1600 MPa bituminous moduli and 190-310 mm thickness.
# The tensile factor differs by standard because the granular Poisson ratio
# (0.35 vs 0.40) changes the raw Boussinesq strain. For very thick (>250 mm)
# perpetual-pavement sections the tensile estimate is less reliable -- confirm
# with IITPAVE.
TENSILE_CORRECTION_IRC37 = 1.30    # design checks, Poisson 0.35
TENSILE_CORRECTION_IRC115 = 1.38   # FWD remaining-life, Poisson 0.40
TENSILE_CORRECTION = TENSILE_CORRECTION_IRC37  # default (back-compat)
VERTICAL_CORRECTION = 0.97


def contact_radius_mm(load_n: float = STD_WHEEL_LOAD_N,
                      pressure_mpa: float = TYRE_PRESSURE_MPA) -> float:
    """Radius of the equivalent circular contact area, a = sqrt(P / (pi*p))."""
    return math.sqrt(load_n / (math.pi * pressure_mpa))


@dataclass(frozen=True)
class LayerModel:
    """Elastic + geometric description of a flexible pavement for MET analysis."""

    e_bituminous_mpa: float     # bound bituminous (BC+DBM) resilient modulus
    e_granular_mpa: float       # granular base+sub-base (WMM+GSB) modulus
    e_subgrade_mpa: float       # subgrade resilient modulus
    h_bituminous_mm: float      # total bituminous thickness
    h_granular_mm: float        # total granular thickness
    nu_bituminous: float = 0.35
    nu_granular: float = 0.35
    nu_subgrade: float = 0.35
    met_factor: float = 0.8     # Odemark equivalent-thickness correction (f)


@dataclass
class StrainResult:
    """Critical strains and the resulting IRC:37 lives for a section."""

    tensile_microstrain: float       # eps_t at bottom of bituminous (x1e-6)
    vertical_microstrain: float      # eps_v on top of subgrade (x1e-6)
    fatigue_life_msa: float          # Nf from eps_t
    rutting_life_msa: float          # Nr from eps_v
    governing_life_msa: float        # min(Nf, Nr)
    governing_mode: str              # "fatigue cracking" | "subgrade rutting"
    reliability: int

    def as_dict(self) -> dict:
        return {
            "tensile_microstrain": round(self.tensile_microstrain, 1),
            "vertical_microstrain": round(self.vertical_microstrain, 1),
            "fatigue_life_msa": round(self.fatigue_life_msa, 2),
            "rutting_life_msa": round(self.rutting_life_msa, 2),
            "governing_life_msa": round(self.governing_life_msa, 2),
            "governing_mode": self.governing_mode,
            "reliability": self.reliability,
        }


# --- Boussinesq closed forms (uniform circular load on an elastic half-space) -

def _zeta(z: float, a: float) -> float:
    return z / math.sqrt(a * a + z * z)


def _boussinesq_center(p: float, z: float, a: float, nu: float):
    """(sigma_z, sigma_r) under the centre of a circular load at depth z.

    sigma_r == sigma_theta on the axis. Compression positive.
    """
    zeta = _zeta(z, a)
    sigma_z = p * (1.0 - zeta ** 3)
    sigma_r = (p / 2.0) * ((1.0 + 2.0 * nu) - 2.0 * (1.0 + nu) * zeta + zeta ** 3)
    return sigma_z, sigma_r


def _point_sigma_z(load_n: float, z: float, offset: float) -> float:
    """Boussinesq vertical stress from a point load, at depth z and horizontal
    `offset` from the load axis (used to superpose the second dual wheel)."""
    r = math.sqrt(offset * offset + z * z)
    return 3.0 * load_n * z ** 3 / (2.0 * math.pi * r ** 5)


def _equiv_thickness(h: float, e_upper: float, e_lower: float, f: float) -> float:
    """Odemark equivalent thickness of an upper layer in terms of the layer below."""
    return f * h * (e_upper / e_lower) ** (1.0 / 3.0)


def compute_strains(layer: LayerModel,
                    load_n: float = STD_WHEEL_LOAD_N,
                    pressure_mpa: float = TYRE_PRESSURE_MPA,
                    *,
                    standard: str = "irc37",
                    tensile_correction: Optional[float] = None,
                    vertical_correction: float = VERTICAL_CORRECTION) -> "StrainResult":
    """Critical strains via Odemark--Boussinesq (calibrated to IITPAVE), then the
    fatigue/rutting life.

    `standard`: "irc37" (IRC:37-2018 fatigue with the mix C factor, for design) or
    "irc115" (IRC:115-2014 fatigue, the FWD remaining-life model). The strains are
    multiplied by the IITPAVE calibration factors so they track real IITPAVE
    output; reliability follows IRC (90% at/above 20 MSA, else 80%)."""
    a = contact_radius_mm(load_n, pressure_mpa)
    p = pressure_mpa
    if tensile_correction is None:
        tensile_correction = (TENSILE_CORRECTION_IRC115 if standard == "irc115"
                              else TENSILE_CORRECTION_IRC37)

    # --- tensile strain at the bottom of the bituminous layer (single wheel) ---
    he_ac = _equiv_thickness(layer.h_bituminous_mm, layer.e_bituminous_mpa,
                             layer.e_granular_mpa, layer.met_factor)
    sz, sr = _boussinesq_center(p, he_ac, a, layer.nu_granular)
    # Horizontal (radial) strain at the interface, evaluated in the lower medium;
    # strain is continuous across a bonded interface, so this is eps at the AC base.
    eps_t = abs(((1.0 - layer.nu_granular) * sr - layer.nu_granular * sz)
                / layer.e_granular_mpa) * tensile_correction

    # --- vertical compressive strain on the subgrade (dual wheel) --------------
    he_sub = (
        _equiv_thickness(layer.h_bituminous_mm, layer.e_bituminous_mpa,
                         layer.e_subgrade_mpa, layer.met_factor)
        + _equiv_thickness(layer.h_granular_mm, layer.e_granular_mpa,
                           layer.e_subgrade_mpa, layer.met_factor)
    )
    sz1, sr1 = _boussinesq_center(p, he_sub, a, layer.nu_subgrade)
    sz2 = _point_sigma_z(load_n, he_sub, DUAL_SPACING_MM)  # 2nd wheel superposed
    sigma_z = sz1 + sz2
    eps_v = abs((sigma_z - 2.0 * layer.nu_subgrade * sr1)
                / layer.e_subgrade_mpa) * vertical_correction

    return _lives_from_strains(eps_t, eps_v, layer.e_bituminous_mpa, standard=standard)


def _lives_from_strains(eps_t: float, eps_v: float, e_bituminous: float,
                        model: PerformanceModel = DEFAULT_PERFORMANCE,
                        standard: str = "irc37") -> StrainResult:
    """Apply the IRC fatigue/rutting laws to a pair of strains.

    `standard`: "irc37" (with mix C factor) or "irc115" (FWD remaining-life,
    0.711e-4, no C). Reliability is chosen from the governing life itself
    (IRC: 90% once design traffic reaches 20 MSA), solved consistently in one pass.
    """
    def lives(reliability: int):
        if standard == "irc115":
            nf = fatigue_life_msa_irc115(eps_t, e_bituminous, reliability=reliability)
        else:
            nf = fatigue_life_msa(eps_t, e_bituminous, reliability=reliability, model=model)
        nr = rutting_life_msa(eps_v, reliability=reliability, model=model)
        return nf, nr

    # First pass at 80%, then upgrade to 90% if the governing life is >= 20 MSA.
    nf, nr = lives(80)
    governing = min(nf, nr)
    reliability = _reliability_for_traffic(governing)
    if reliability == 90:
        nf, nr = lives(90)
        governing = min(nf, nr)

    mode = "fatigue cracking" if nf <= nr else "subgrade rutting"
    return StrainResult(
        tensile_microstrain=eps_t * 1.0e6,
        vertical_microstrain=eps_v * 1.0e6,
        fatigue_life_msa=nf,
        rutting_life_msa=nr,
        governing_life_msa=governing,
        governing_mode=mode,
        reliability=reliability,
    )


# --- Evaluate an existing section (e.g. from FWD back-calculated moduli) -----

@dataclass
class SectionAssessment:
    """Mechanistic verdict for an in-service section + remaining structural life."""

    strains: StrainResult
    design_msa: Optional[float]
    cumulative_msa: float
    remaining_msa: Optional[float]
    residual_years: Optional[float]
    adequate: Optional[bool]
    rationale: str

    def as_dict(self) -> dict:
        return {
            "strains": self.strains.as_dict(),
            "design_msa": self.design_msa,
            "cumulative_msa": round(self.cumulative_msa, 2),
            "remaining_msa": (round(self.remaining_msa, 2)
                              if self.remaining_msa is not None else None),
            "residual_years": (round(self.residual_years, 1)
                               if self.residual_years is not None else None),
            "adequate": self.adequate,
            "rationale": self.rationale,
        }


def evaluate_section(
    layer: LayerModel,
    *,
    annual_msa: float = 0.0,
    traffic_growth_rate: float = 0.0,
    cumulative_msa: float = 0.0,
    design_msa: Optional[float] = None,
    standard: str = "irc37",
) -> SectionAssessment:
    """Assess an existing pavement from its (FWD back-calculated) layer moduli.

    The governing mechanistic life is the structural capacity of the *current*
    section. Remaining capacity = governing life - traffic already carried; the
    residual years grow the current annual MSA at the traffic growth rate until
    that remaining capacity is consumed. `standard` selects the fatigue model
    ("irc115" for an FWD remaining-life assessment).
    """
    strains = compute_strains(layer, standard=standard)
    capacity = strains.governing_life_msa
    remaining = max(0.0, capacity - cumulative_msa)
    residual_years = _years_to_consume(remaining, annual_msa, traffic_growth_rate)

    adequate = None
    if design_msa is not None and design_msa > 0:
        adequate = capacity >= design_msa

    yrs = "n/a" if residual_years is None else f"~{residual_years:.1f} yr"
    verdict = ("" if adequate is None else
               (f" Section CARRIES the {design_msa:.0f} MSA design traffic."
                if adequate else
                f" Section is DEFICIENT for {design_msa:.0f} MSA design traffic; "
                f"strengthening overlay required."))
    rationale = (
        f"Mechanistic capacity {capacity:.1f} MSA (governed by {strains.governing_mode}; "
        f"eps_t {strains.tensile_microstrain:.0f}, eps_v {strains.vertical_microstrain:.0f} "
        f"microstrain). Remaining {remaining:.1f} MSA -> {yrs} at "
        f"{annual_msa:.1f} MSA/yr.{verdict}"
    )
    return SectionAssessment(
        strains=strains, design_msa=design_msa, cumulative_msa=cumulative_msa,
        remaining_msa=remaining, residual_years=residual_years,
        adequate=adequate, rationale=rationale,
    )


def _years_to_consume(remaining_msa: float, annual_msa: float, growth: float) -> Optional[float]:
    if remaining_msa <= 0:
        return 0.0
    if annual_msa <= 0:
        return None
    if abs(growth) < 1e-9:
        return remaining_msa / annual_msa
    ratio = 1.0 + growth * remaining_msa / annual_msa
    if ratio <= 0:
        return None
    return math.log(ratio) / math.log(1.0 + growth)


# --- Mechanistic design: CBR + design MSA -> layer thicknesses (IITPAVE-style) -

@dataclass
class MechanisticDesign:
    """A mechanistically-sized IRC:37 flexible pavement."""

    cbr: float
    design_msa: float
    design_life_years: int
    subgrade_modulus_mpa: float
    e_bituminous_mpa: float
    e_granular_mpa: float
    bituminous_mm: float
    granular_mm: float
    total_mm: float
    strains: StrainResult
    rationale: str

    def as_dict(self) -> dict:
        return {
            "cbr": round(self.cbr, 2),
            "design_msa": round(self.design_msa, 2),
            "design_life_years": self.design_life_years,
            "subgrade_modulus_mpa": round(self.subgrade_modulus_mpa, 1),
            "e_bituminous_mpa": round(self.e_bituminous_mpa, 0),
            "e_granular_mpa": round(self.e_granular_mpa, 0),
            "layers": {
                "bituminous_mm": self.bituminous_mm,
                "granular_mm": self.granular_mm,
            },
            "total_mm": self.total_mm,
            "strains": self.strains.as_dict(),
            "rationale": self.rationale,
        }


# --- FWD remaining-life & overlay (IRC:115-2014, from back-calculated moduli) -

@dataclass
class FWDSection:
    """A homogeneous sub-section's 15th-percentile back-calculated moduli +
    crust thickness, e.g. one row of an FWD evaluation report (Table 6.4 / 7.2)."""

    section_id: str
    e_bituminous_mpa: float     # temperature-corrected BT modulus
    e_granular_mpa: float       # seasonal-corrected granular modulus
    e_subgrade_mpa: float       # seasonal-corrected subgrade modulus
    h_bituminous_mm: float
    h_granular_mm: float
    chainage_from: Optional[float] = None
    chainage_to: Optional[float] = None


@dataclass
class FWDOverlayRow:
    """Remaining-life + overlay verdict for one homogeneous sub-section."""

    section_id: str
    chainage_from: Optional[float]
    chainage_to: Optional[float]
    tensile_microstrain: float
    vertical_microstrain: float
    remaining_fatigue_msa: float
    remaining_rutting_msa: float
    remaining_life_msa: float          # min of the two
    design_msa: float
    overlay_required: bool
    confirm_with_iitpave: bool         # remaining life within +-15% of design -> borderline

    def as_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "chainage_from": self.chainage_from,
            "chainage_to": self.chainage_to,
            "tensile_microstrain": round(self.tensile_microstrain, 1),
            "vertical_microstrain": round(self.vertical_microstrain, 1),
            "remaining_fatigue_msa": round(self.remaining_fatigue_msa, 1),
            "remaining_rutting_msa": round(self.remaining_rutting_msa, 1),
            "remaining_life_msa": round(self.remaining_life_msa, 1),
            "design_msa": self.design_msa,
            "overlay_required": self.overlay_required,
            "confirm_with_iitpave": self.confirm_with_iitpave,
        }


def evaluate_fwd_sections(
    sections: List[FWDSection],
    design_msa: float,
    *,
    nu_bituminous: float = 0.5,
    nu_granular: float = 0.4,
    nu_subgrade: float = 0.4,
) -> "FWDOverlayResult":
    """Remaining structural life + overlay decision per homogeneous sub-section.

    Reproduces the FWD-report workflow (IRC:115-2014): the mechanistic life from
    the current (15th-percentile) layer moduli is the remaining life; an overlay
    is required where that falls below the design traffic. Poisson defaults are
    the IRC:115 values (0.5 / 0.4 / 0.4)."""
    if design_msa <= 0:
        raise ValueError("design_msa must be positive.")
    rows: List[FWDOverlayRow] = []
    for sec in sections:
        layer = LayerModel(
            e_bituminous_mpa=sec.e_bituminous_mpa, e_granular_mpa=sec.e_granular_mpa,
            e_subgrade_mpa=sec.e_subgrade_mpa, h_bituminous_mm=sec.h_bituminous_mm,
            h_granular_mm=sec.h_granular_mm, nu_bituminous=nu_bituminous,
            nu_granular=nu_granular, nu_subgrade=nu_subgrade,
        )
        s = compute_strains(layer, standard="irc115")
        remaining = min(s.fatigue_life_msa, s.rutting_life_msa)
        rows.append(FWDOverlayRow(
            section_id=sec.section_id, chainage_from=sec.chainage_from,
            chainage_to=sec.chainage_to,
            tensile_microstrain=s.tensile_microstrain,
            vertical_microstrain=s.vertical_microstrain,
            remaining_fatigue_msa=s.fatigue_life_msa,
            remaining_rutting_msa=s.rutting_life_msa,
            remaining_life_msa=remaining, design_msa=design_msa,
            overlay_required=remaining < design_msa,
            # Within +-15% of the threshold the Odemark approximation cannot
            # decide reliably -> flag for a confirmatory IITPAVE run.
            confirm_with_iitpave=abs(remaining - design_msa) <= 0.15 * design_msa,
        ))
    return FWDOverlayResult(design_msa=design_msa, rows=rows)


@dataclass
class FWDOverlayResult:
    """Network-level FWD overlay assessment across homogeneous sub-sections."""

    design_msa: float
    rows: List[FWDOverlayRow]

    @property
    def overlay_sections(self) -> List[str]:
        return [r.section_id for r in self.rows if r.overlay_required]

    @property
    def borderline_sections(self) -> List[str]:
        return [r.section_id for r in self.rows if r.confirm_with_iitpave]

    @property
    def min_remaining_msa(self) -> float:
        return min((r.remaining_life_msa for r in self.rows), default=0.0)

    def as_dict(self) -> dict:
        n_overlay = len(self.overlay_sections)
        n_confirm = len(self.borderline_sections)
        confirm_txt = (
            f" {n_confirm} borderline section(s) are within 15% of the threshold -- "
            f"confirm these with IITPAVE." if n_confirm else ""
        )
        return {
            "design_msa": self.design_msa,
            "n_sections": len(self.rows),
            "n_overlay_required": n_overlay,
            "overlay_sections": self.overlay_sections,
            "borderline_sections": self.borderline_sections,
            "min_remaining_msa": round(self.min_remaining_msa, 1),
            "verdict": (
                f"All {len(self.rows)} sub-section(s) carry the {self.design_msa:.0f} MSA "
                f"design life; no overlay required (screening)." + confirm_txt
                if n_overlay == 0 else
                f"{n_overlay} of {len(self.rows)} sub-section(s) fall below the "
                f"{self.design_msa:.0f} MSA design life; overlay required (screening)." + confirm_txt
            ),
            "sections": [r.as_dict() for r in self.rows],
        }


def granular_modulus_from_subgrade(e_subgrade: float, h_granular: float) -> float:
    """IRC:37 granular composite modulus, M_R = 0.2*h^0.45*M_R(subgrade), capped."""
    return min(0.2 * (max(1.0, h_granular) ** 0.45) * e_subgrade, 1000.0)


def design_pavement_mechanistic(
    *,
    cbr: float,
    design_msa: float,
    design_life_years: int = 15,
    e_bituminous_mpa: float = 3000.0,
    bituminous_min_mm: float = 50.0,
    bituminous_max_mm: float = 250.0,
    granular_min_mm: float = 150.0,
    granular_max_mm: float = 800.0,
    bituminous_cost_ratio: float = 8.0,
    step_mm: float = 5.0,
) -> MechanisticDesign:
    """Size the lowest-cost IRC:37 section whose mechanistic fatigue AND rutting
    lives both meet the design traffic (IITPAVE-style search).

    Granular thickness carries the subgrade-rutting demand; bituminous carries
    fatigue. The objective is construction cost, not raw thickness: bituminous is
    ~`bituminous_cost_ratio` x dearer per mm than granular, so (like real IRC:37
    designs) the cheap granular layer does the structural work and the bituminous
    layer is kept to what fatigue needs. `e_bituminous_mpa` is the mix modulus at
    the design temperature (VG40 BC+DBM ~ 3000 MPa at 35 C, IRC:37 indicative)."""
    if cbr <= 0:
        raise ValueError("cbr must be positive.")
    if design_msa <= 0:
        raise ValueError("design_msa must be positive.")
    if not (1 <= design_life_years <= 100):
        raise ValueError("design_life_years out of range [1, 100].")

    def cost(h_bt: float, h_gran: float) -> float:
        return h_bt * bituminous_cost_ratio + h_gran

    e_sub = subgrade_modulus_mpa(cbr)
    best: Optional[MechanisticDesign] = None
    best_cost = float("inf")
    h_gran = granular_min_mm
    while h_gran <= granular_max_mm:
        e_gran = granular_modulus_from_subgrade(e_sub, h_gran)
        h_bt = bituminous_min_mm
        while h_bt <= bituminous_max_mm:
            layer = LayerModel(
                e_bituminous_mpa=e_bituminous_mpa, e_granular_mpa=e_gran,
                e_subgrade_mpa=e_sub, h_bituminous_mm=h_bt, h_granular_mm=h_gran,
            )
            s = compute_strains(layer)
            if s.fatigue_life_msa >= design_msa and s.rutting_life_msa >= design_msa:
                c = cost(h_bt, h_gran)
                if c < best_cost:
                    best_cost = c
                    best = MechanisticDesign(
                        cbr=float(cbr), design_msa=float(design_msa),
                        design_life_years=int(design_life_years),
                        subgrade_modulus_mpa=e_sub, e_bituminous_mpa=e_bituminous_mpa,
                        e_granular_mpa=e_gran, bituminous_mm=round(h_bt, 0),
                        granular_mm=round(h_gran, 0), total_mm=round(h_bt + h_gran, 0),
                        strains=s, rationale="",
                    )
                break  # thinnest bituminous for this granular thickness
            h_bt += step_mm
        h_gran += step_mm

    if best is None:  # design traffic beyond the search envelope
        layer = LayerModel(
            e_bituminous_mpa=e_bituminous_mpa,
            e_granular_mpa=granular_modulus_from_subgrade(e_sub, granular_max_mm),
            e_subgrade_mpa=e_sub, h_bituminous_mm=bituminous_max_mm,
            h_granular_mm=granular_max_mm,
        )
        s = compute_strains(layer)
        raise ValueError(
            f"no section within {bituminous_max_mm:.0f}mm bituminous / "
            f"{granular_max_mm:.0f}mm granular carries {design_msa:.0f} MSA "
            f"(max section reaches only {s.governing_life_msa:.0f} MSA); "
            f"raise the search limits or revisit the subgrade (CBR {cbr})."
        )

    best.rationale = (
        f"IITPAVE-style (Odemark--Boussinesq) design for CBR {cbr:.1f}% "
        f"(subgrade M_R {e_sub:.0f} MPa) and {design_msa:.0f} MSA: "
        f"{best.bituminous_mm:.0f} mm bituminous over {best.granular_mm:.0f} mm "
        f"granular (total {best.total_mm:.0f} mm) gives eps_t "
        f"{best.strains.tensile_microstrain:.0f} / eps_v "
        f"{best.strains.vertical_microstrain:.0f} microstrain -> fatigue "
        f"{best.strains.fatigue_life_msa:.0f} MSA, rutting "
        f"{best.strains.rutting_life_msa:.0f} MSA (governed by "
        f"{best.strains.governing_mode}). Approximate -- confirm with IITPAVE."
    )
    return best
