"""
IRC:37 flexible-pavement structural design (new construction / strengthening).

This is the *design* stage of a pavement-management workflow -- it runs **before**
any field-condition data exists. Given the subgrade strength (CBR), the design
traffic (MSA, from `traffic.py`) and a design life, it sizes the pavement layers
and reports the IRC:37-2018 mechanistic-empirical performance checks.

Layers in an Indian flexible pavement (top to bottom):

    BC   -- bituminous concrete wearing course        )  bound (bituminous)
    DBM  -- dense bituminous macadam binder course     )
    WMM  -- wet-mix macadam granular base
    GSB  -- granular sub-base
    -------- compacted subgrade (characterised by CBR) --------

Two design philosophies coexist in IRC:37-2018 and both are exposed here:

  1. **Catalogue / parametric thickness design** (`design_pavement`). IRC:37 ships
     design catalogues (plates) giving layer thicknesses for combinations of
     design traffic and subgrade CBR. We encode that catalogue as a smooth,
     editable parametric fit (`DesignCatalogue`) -- the same "coefficients are
     data, not code" stance the rest of RAMS takes. These are *indicative* and
     must be confirmed by a mechanistic (IITPAVE) run before construction.

  2. **Mechanistic-empirical performance equations** (`fatigue_life_msa`,
     `rutting_life_msa`). These are the *exact* IRC:37-2018 closed forms: given
     the critical strains from a layered-elastic analysis (IITPAVE), they return
     the allowable repetitions for bottom-up fatigue cracking and subgrade
     rutting. RAMS does not ship a layered-elastic solver (that is a deliberate
     external integration, see docs/INDIAN_RAMS_STRATEGY.md), so these take the
     strains as inputs -- they let you verify a catalogue section if you have
     IITPAVE strains, and they document the governing failure modes.

All numbers are seeded to IRC:37-2018 practice and are overridable. None should
reach construction without a project-specific mechanistic design.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# --- Subgrade & granular moduli (IRC:37-2018 closed forms) ------------------

def subgrade_modulus_mpa(cbr: float) -> float:
    """Resilient modulus of the subgrade from its CBR (IRC:37-2018).

        M_RS = 10 * CBR              for CBR <= 5 %
        M_RS = 17.6 * CBR^0.64       for CBR > 5 %     (MPa)
    """
    c = float(cbr)
    if c <= 0:
        raise ValueError("cbr must be positive.")
    if c <= 5.0:
        return 10.0 * c
    return 17.6 * math.pow(c, 0.64)


def granular_modulus_mpa(support_modulus_mpa: float, granular_thickness_mm: float) -> float:
    """Effective modulus of the granular (WMM+GSB) layer over its support.

        M_R(granular) = 0.2 * h^0.45 * M_R(support)      (h in mm)

    IRC:37-2018 caps this composite modulus; we apply the customary 1000 MPa cap.
    """
    h = max(1.0, float(granular_thickness_mm))
    mr = 0.2 * math.pow(h, 0.45) * float(support_modulus_mpa)
    return min(mr, 1000.0)


# --- Mechanistic-empirical performance models (IRC:37-2018) -----------------
# Allowable standard-axle repetitions for the two governing failure modes.
# Both take the critical strain from a layered-elastic (IITPAVE) analysis.

@dataclass(frozen=True)
class PerformanceModel:
    """IRC:37-2018 fatigue + rutting coefficients, by reliability level.

    The 80%-reliability constants apply to design traffic < 20 MSA; the
    90%-reliability constants apply to design traffic >= 20 MSA.
    """

    # Fatigue (bottom-up cracking): Nf = fatigue_c * C * 1e-4 * (1/eps_t)^3.89 * (1/MRm)^0.854
    fatigue_c_80: float = 1.6064
    fatigue_c_90: float = 0.5161
    fatigue_eps_exp: float = 3.89
    fatigue_modulus_exp: float = 0.854
    # Rutting (subgrade): Nr = rut_c * (1/eps_v)^4.5337
    rut_c_80: float = 4.1656e-8
    rut_c_90: float = 1.4100e-8
    rut_eps_exp: float = 4.5337


DEFAULT_PERFORMANCE = PerformanceModel()


def _reliability_for_traffic(design_msa: float) -> int:
    """IRC:37-2018 reliability: 90% for >= 20 MSA, else 80%."""
    return 90 if design_msa >= 20.0 else 80


def fatigue_life_msa(
    tensile_strain: float,
    bituminous_modulus_mpa: float,
    *,
    va_pct: float = 3.0,
    vbe_pct: float = 11.5,
    reliability: int = 90,
    model: PerformanceModel = DEFAULT_PERFORMANCE,
) -> float:
    """Allowable repetitions (MSA) before bottom-up fatigue cracking, IRC:37-2018.

        Nf = C_f * C * 1e-4 * (1/eps_t)^3.89 * (1/M_Rm)^0.854
        C  = 10^M,  M = 4.84 * (Vbe/(Va+Vbe) - 0.69)

    `tensile_strain` is the horizontal tensile strain at the bottom of the
    bituminous layer; `va_pct`/`vbe_pct` are the air-void and effective-binder
    volumes of the mix (the IRC:37-2018 defaults give the standard mix factor).
    """
    eps = max(1e-9, float(tensile_strain))
    mrm = max(1.0, float(bituminous_modulus_mpa))
    cf = model.fatigue_c_90 if reliability >= 90 else model.fatigue_c_80
    m = 4.84 * (vbe_pct / (va_pct + vbe_pct) - 0.69)
    c = math.pow(10.0, m)
    nf = cf * c * 1e-4 * math.pow(1.0 / eps, model.fatigue_eps_exp) * math.pow(
        1.0 / mrm, model.fatigue_modulus_exp
    )
    return nf / 1.0e6  # standard axles -> MSA


def fatigue_life_msa_irc115(
    tensile_strain: float,
    bituminous_modulus_mpa: float,
    *,
    reliability: int = 90,
) -> float:
    """Allowable repetitions (MSA) for bottom-up fatigue, **IRC:115-2014**.

        Nf = 0.711e-4 * (1/eps_t)^3.89 * (1/MR)^0.854   (90% reliability)

    This is the model FWD remaining-life reports use (e.g. the NH-152D
    evaluation). Unlike the IRC:37-2018 form it carries no mix `C` factor, and
    `MR` is the (temperature-corrected) back-calculated bituminous modulus. The
    90% constant is validated against published reports; the 80% constant
    (1.6940e-4) is indicative -- confirm against the agency's adopted IRC:115.
    """
    eps = max(1e-9, float(tensile_strain))
    mr = max(1.0, float(bituminous_modulus_mpa))
    c = 0.711e-4 if reliability >= 90 else 1.6940e-4
    nf = c * math.pow(1.0 / eps, 3.89) * math.pow(1.0 / mr, 0.854)
    return nf / 1.0e6


def rutting_life_msa(
    vertical_strain: float,
    *,
    reliability: int = 90,
    model: PerformanceModel = DEFAULT_PERFORMANCE,
) -> float:
    """Allowable repetitions (MSA) before subgrade rutting, IRC:37-2018.

        Nr = C_r * (1/eps_v)^4.5337

    `vertical_strain` is the vertical compressive strain on top of the subgrade.
    """
    eps = max(1e-9, float(vertical_strain))
    cr = model.rut_c_90 if reliability >= 90 else model.rut_c_80
    nr = cr * math.pow(1.0 / eps, model.rut_eps_exp)
    return nr / 1.0e6


# --- Thickness design (IRC:37-2018 catalogue, parametric) -------------------

@dataclass(frozen=True)
class DesignCatalogue:
    """Smooth, editable fit to the IRC:37-2018 flexible-pavement catalogue.

    Bituminous thickness is traffic-driven (fatigue); granular thickness is
    subgrade-driven (rutting / drainage), with a mild traffic term. Floors are
    the IRC:37-2018 minimum layer thicknesses. These coefficients are the
    catalogue *as data* -- override them to match an agency's adopted plates.
    """

    # Bituminous (BC+DBM): bit = bit_a + bit_b*log10(MSA), clamped.
    bit_a: float = 50.0
    bit_b: float = 55.0
    bit_min: float = 50.0
    bit_max: float = 230.0
    bc_thickness_mm: float = 40.0     # wearing course; DBM is the remainder
    # Granular (WMM+GSB): gran = gran_a + gran_cbr*(ref_cbr - CBR) + gran_b*log10(MSA)
    gran_a: float = 450.0
    gran_cbr: float = 30.0
    ref_cbr: float = 8.0
    gran_b: float = 15.0
    gran_min: float = 350.0
    gran_max: float = 750.0
    wmm_thickness_mm: float = 250.0   # granular base; GSB is the remainder
    gsb_min_mm: float = 150.0
    bituminous_modulus_mpa: float = 3000.0  # VG40 BC+DBM at 35 C (IRC:37 indicative)

    def bituminous_mm(self, design_msa: float) -> float:
        msa = max(1.0, float(design_msa))
        t = self.bit_a + self.bit_b * math.log10(msa)
        return round(min(self.bit_max, max(self.bit_min, t)), 0)

    def granular_mm(self, cbr: float, design_msa: float) -> float:
        msa = max(1.0, float(design_msa))
        t = (
            self.gran_a
            + self.gran_cbr * (self.ref_cbr - float(cbr))
            + self.gran_b * math.log10(msa)
        )
        return round(min(self.gran_max, max(self.gran_min, t)), 0)


DEFAULT_CATALOGUE = DesignCatalogue()


@dataclass
class PavementDesign:
    """A complete IRC:37 flexible-pavement section for given CBR + design traffic."""

    cbr: float
    design_msa: float
    design_life_years: int
    reliability: int
    subgrade_modulus_mpa: float
    # Layer thicknesses (mm)
    bc_mm: float           # bituminous concrete wearing course
    dbm_mm: float          # dense bituminous macadam binder course
    bituminous_mm: float   # BC + DBM
    wmm_mm: float          # wet-mix macadam granular base
    gsb_mm: float          # granular sub-base
    granular_mm: float     # WMM + GSB
    total_mm: float        # full pavement thickness above subgrade
    rationale: str

    def as_dict(self) -> dict:
        return {
            "cbr": round(self.cbr, 2),
            "design_msa": round(self.design_msa, 2),
            "design_life_years": self.design_life_years,
            "reliability": self.reliability,
            "subgrade_modulus_mpa": round(self.subgrade_modulus_mpa, 1),
            "layers": {
                "bc_mm": self.bc_mm,
                "dbm_mm": self.dbm_mm,
                "bituminous_mm": self.bituminous_mm,
                "wmm_mm": self.wmm_mm,
                "gsb_mm": self.gsb_mm,
                "granular_mm": self.granular_mm,
            },
            "total_mm": self.total_mm,
            "rationale": self.rationale,
        }


def design_pavement(
    *,
    cbr: float,
    design_msa: float,
    design_life_years: int = 15,
    reliability: Optional[int] = None,
    catalogue: DesignCatalogue = DEFAULT_CATALOGUE,
) -> PavementDesign:
    """Size an IRC:37-2018 flexible pavement for a subgrade CBR and design MSA.

    Returns layer thicknesses (BC / DBM / WMM / GSB), the subgrade modulus, and
    the governing reliability. `design_msa` is the cumulative design traffic from
    `traffic.design_msa(...)`; `design_life_years` is carried through for the
    record and feeds the PBMC term planning downstream.
    """
    if cbr <= 0:
        raise ValueError("cbr must be positive.")
    if design_msa <= 0:
        raise ValueError("design_msa must be positive.")
    if not (1 <= design_life_years <= 100):
        raise ValueError("design_life_years out of range [1, 100].")
    rel = reliability if reliability is not None else _reliability_for_traffic(design_msa)
    if rel not in (80, 90):
        raise ValueError("reliability must be 80 or 90 (percent).")

    mrs = subgrade_modulus_mpa(cbr)
    bit = catalogue.bituminous_mm(design_msa)
    gran = catalogue.granular_mm(cbr, design_msa)

    bc = min(catalogue.bc_thickness_mm, bit)
    dbm = round(bit - bc, 0)
    wmm = min(catalogue.wmm_thickness_mm, gran - catalogue.gsb_min_mm)
    gsb = round(gran - wmm, 0)
    total = round(bit + gran, 0)

    rationale = (
        f"IRC:37-2018 section for CBR {cbr:.1f}% (subgrade M_R {mrs:.0f} MPa) and "
        f"{design_msa:.0f} MSA design traffic over {design_life_years} yr at {rel}% "
        f"reliability: {bit:.0f} mm bituminous (BC+DBM) over {gran:.0f} mm granular "
        f"(WMM+GSB), total {total:.0f} mm. Indicative catalogue values -- confirm "
        f"with a mechanistic (IITPAVE) strain check before construction."
    )
    return PavementDesign(
        cbr=float(cbr),
        design_msa=float(design_msa),
        design_life_years=int(design_life_years),
        reliability=rel,
        subgrade_modulus_mpa=mrs,
        bc_mm=round(bc, 0),
        dbm_mm=dbm,
        bituminous_mm=bit,
        wmm_mm=round(wmm, 0),
        gsb_mm=gsb,
        granular_mm=gran,
        total_mm=total,
        rationale=rationale,
    )
