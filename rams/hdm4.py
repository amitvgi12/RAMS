"""
HDM-4 mechanistic rutting model (delta-RDM) -- a selectable alternative to the
engine's default IRC:82-style power law.

Source / motivation:
    Taniguchi & Yoshida, "Calibrating HDM-4 Rutting Model on National Highways
    in Japan" (PWRI). HDM-4 builds the annual rut-depth increment from three
    physically-distinct components, each with its own calibration factor K:

        delta-RDM = K_rid * RDO  +  K_rst * RDST  +  K_rpd * RDPD

    where (HDM-4 Vol.4 form, as reproduced in the paper):

        RDO  (initial densification) = a0 * YE4^(a1 + a2*DEF) * SNP^a3 * COMP^a4
        RDST (structural deformation) = a0 * SNP^a1 * YE4^a2 * COMP^a3
        RDPD (plastic deformation)    = a0 * CDS^3 * YE4 * Sh^a1 * HS^a2

    Inputs:
        YE4  : annual equivalent standard axles, millions/lane  (== segment MSA/yr)
        DEF  : mean Benkelman-beam / FWD-derived rebound deflection (mm)  <-- FWD
        SNP  : adjusted structural number of the pavement                  <-- FWD/DCP
        COMP : relative compaction (%)
        CDS  : construction-defects indicator (0.5 sound .. 1.5 defective)
        Sh   : speed of heavy vehicles (km/h) -- site severity term
        HS   : total bituminous surfacing thickness (mm)

Calibration factors:
    The PAPER calibrates the three K factors against Japanese NH field data; the
    a-coefficients are the HDM-4 structural-model form parameters. Presets below
    carry the paper's K factors. The a-coefficients here are HDM-4-form defaults
    tuned to realistic flexible-pavement magnitudes (a few mm/yr); they are NOT a
    substitute for an agency calibration against local NSV+FWD data -- every
    coefficient is overridable for exactly that reason.

Faithfulness notes:
    * Initial densification is a one-off that occurs in the first year after
      construction/overlay; it is applied at age==1 only and is zero thereafter.
    * The studded-tyre surface-wear term (RDW) is omitted -- not relevant to
      Indian conditions, and the paper omits it too.
    * Environmental moisture enters structurally through DEF/SNP (a weaker, wetter
      subgrade shows up as higher deflection), so HDM-4 mode does NOT additionally
      apply the monsoon multiplier the default law uses -- to avoid double counting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HDM4RutCalibration:
    """All HDM-4 rut coefficients: 3 calibration factors + the model a-params.

    Defaults are the HDM-4-form structural parameters; presets override the
    three K factors with the paper's Japanese NH calibration.
    """

    # --- calibration factors (the paper's regression output) ---------------
    k_rid: float = 1.0   # initial densification
    k_rst: float = 1.0   # structural deformation
    k_rpd: float = 1.0   # plastic (asphalt) deformation

    # --- RDO: a0 * YE4^(a1 + a2*DEF) * SNP^a3 * COMP^a4 ---------------------
    rid_a0: float = 51740.0
    rid_a1: float = 0.09
    rid_a2: float = 0.0384
    rid_a3: float = -0.502
    rid_a4: float = -2.3

    # --- RDST: a0 * SNP^a1 * YE4^a2 * COMP^a3 -------------------------------
    rst_a0: float = 0.817
    rst_a1: float = -1.4585
    rst_a2: float = 0.5
    rst_a3: float = 0.0

    # --- RDPD: a0 * CDS^3 * YE4 * Sh^a1 * HS^a2 -----------------------------
    rpd_a0: float = 0.17
    rpd_a1: float = 0.0      # Sh exponent (neutral by default; carry for calibration)
    rpd_a2: float = 0.0      # HS exponent (neutral by default; carry for calibration)

    label: str = "HDM-4 (uncalibrated defaults)"

    # --- components (each returns mm) --------------------------------------

    def densification(self, ye4: float, deflection: float, snp: float, comp: float) -> float:
        if ye4 <= 0.0:
            return 0.0
        exp = self.rid_a1 + self.rid_a2 * max(0.0, deflection)
        return self.k_rid * (
            self.rid_a0
            * math.pow(ye4, exp)
            * math.pow(max(1e-6, snp), self.rid_a3)
            * math.pow(max(1e-6, comp), self.rid_a4)
        )

    def structural(self, ye4: float, snp: float, comp: float) -> float:
        if ye4 <= 0.0:
            return 0.0
        return self.k_rst * (
            self.rst_a0
            * math.pow(max(1e-6, snp), self.rst_a1)
            * math.pow(ye4, self.rst_a2)
            * math.pow(max(1e-6, comp), self.rst_a3)
        )

    def plastic(self, ye4: float, cds: float, sh: float, hs: float) -> float:
        if ye4 <= 0.0:
            return 0.0
        return self.k_rpd * (
            self.rpd_a0
            * math.pow(max(0.0, cds), 3.0)
            * ye4
            * math.pow(max(1e-6, sh), self.rpd_a1)
            * math.pow(max(1e-6, hs), self.rpd_a2)
        )


@dataclass(frozen=True)
class RutIncrement:
    """One year's HDM-4 rut increment, broken into its physical components (mm)."""

    densification: float
    structural: float
    plastic: float

    @property
    def total(self) -> float:
        return self.densification + self.structural + self.plastic

    def as_dict(self) -> dict:
        return {
            "densification": round(self.densification, 3),
            "structural": round(self.structural, 3),
            "plastic": round(self.plastic, 3),
            "total": round(self.total, 3),
        }


def annual_rut_increment(
    cal: HDM4RutCalibration,
    *,
    ye4: float,
    age: int,
    deflection_mm: float,
    structural_number: float,
    compaction_pct: float,
    cds: float,
    heavy_speed_kmh: float,
    surfacing_thickness_mm: float,
) -> RutIncrement:
    """Compute the HDM-4 rut increment for one analysis year.

    `ye4` is the year's traffic in million ESAL/lane (the segment's annual MSA).
    Densification is applied only in the first year (age == 1).
    """
    rdo = cal.densification(ye4, deflection_mm, structural_number, compaction_pct) if age == 1 else 0.0
    rdst = cal.structural(ye4, structural_number, compaction_pct)
    rdpd = cal.plastic(ye4, cds, heavy_speed_kmh, surfacing_thickness_mm)
    return RutIncrement(densification=rdo, structural=rdst, plastic=rdpd)


# --- Paper-calibrated presets (Taniguchi & Yoshida, PWRI) ------------------
# Dense-graded AC: best-fit K factors (R^2 = 0.16 on the Japanese NH data set).
HDM4_DENSE_GRADED = HDM4RutCalibration(
    k_rid=3.26,
    k_rst=3.11,
    k_rpd=0.59,
    label="HDM-4 (dense-graded AC, Japan NH calibration: Krid=3.26 Krst=3.11 Krpd=0.59)",
)

# Porous AC: the paper's physically-admissible variant with Krpd forced to 0
# (the unconstrained fit gave Krpd<0, i.e. rut shrinking with age -- rejected).
HDM4_POROUS = HDM4RutCalibration(
    k_rid=1.48,
    k_rst=0.83,
    k_rpd=0.0,
    label="HDM-4 (porous AC, Japan NH calibration: Krid=1.48 Krst=0.83 Krpd=0)",
)

DEFAULT_HDM4 = HDM4_DENSE_GRADED

PRESETS = {
    "dense": HDM4_DENSE_GRADED,
    "porous": HDM4_POROUS,
}


def preset(name: str) -> HDM4RutCalibration:
    """Look up a calibration preset by short name ('dense' | 'porous')."""
    key = str(name).strip().lower()
    if key not in PRESETS:
        raise ValueError(
            f"unknown HDM-4 preset {name!r}; expected one of: {', '.join(PRESETS)}."
        )
    return PRESETS[key]
