"""
MLIT-PMS Maintenance Control Index (MCI) -- a paper-derived cross-reference.

Source:
    Taniguchi & Yoshida, "Calibrating HDM-4 Rutting Model on National Highways
    in Japan" (Public Works Research Institute). The Japanese MLIT-PMS scores a
    pavement's integrated condition with the Maintenance Control Index:

        MCI = 10 - 1.48*C^0.3 - 0.29*D^0.7 - 0.47*sigma^0.2

        C     : amount of cracking (%)
        D     : rut depth (mm)
        sigma : longitudinal roughness (mm)

    Management bands (paper, Section 3):
        MCI > 5  : not needing repair (desirable management level)
        3..5     : needing repair
        MCI < 3  : needing immediate repair

    The paper's overlay trigger is a cut-and-overlay once rut depth exceeds the
    30 mm management reference.

Why it lives here, separate from the IRC:82 PCI engine:
    MCI is an *alternative* composite to the engine's IRC:82 PCI, exposed so an
    analyst importing MLIT-style pavement-databank records (rut/crack/roughness)
    can read the same integrated index the source PMS uses. It never feeds the
    IRC:82 maintenance bands -- it is reported alongside them.

Fidelity caveat:
    The engine carries roughness as IRI (mm/m, IRC convention), whereas the MCI
    sigma term is a longitudinal roughness in mm. When an explicit roughness is
    not supplied we pass IRI through as a proxy, so `compute_mci` results that
    use IRI are an approximation -- flagged as such by callers. Supply a true
    `roughness_mm` (the XML/PDF schema accepts one) for a faithful MCI.
"""
from __future__ import annotations

import math
from enum import Enum

# Paper, page 7: cut-and-overlay once the rut depth exceeds 30 mm.
RUT_OVERLAY_THRESHOLD_MM = 30.0

# MCI band breakpoints (paper, Section 3).
MCI_DESIRABLE_MIN = 5.0   # MCI > 5 -> no repair needed
MCI_REPAIR_MIN = 3.0      # 3..5 -> repair; < 3 -> immediate repair


class MCIBand(str, Enum):
    """MLIT-PMS management level for a given MCI."""

    DESIRABLE = "DESIRABLE"            # MCI > 5
    NEEDS_REPAIR = "NEEDS_REPAIR"      # 3 <= MCI <= 5
    IMMEDIATE_REPAIR = "IMMEDIATE_REPAIR"  # MCI < 3


def compute_mci(rut_mm: float, cracking_pct: float, roughness_mm: float) -> float:
    """Maintenance Control Index per the MLIT-PMS formula.

    All distresses are non-negative (the engine/validation guarantee this);
    we clamp at 0.0 defensively so a stray negative cannot raise on a
    fractional power. The index is rounded to 2 dp like the IRC:82 PCI.
    """
    c = max(0.0, float(cracking_pct))
    d = max(0.0, float(rut_mm))
    s = max(0.0, float(roughness_mm))
    mci = (
        10.0
        - 1.48 * math.pow(c, 0.3)
        - 0.29 * math.pow(d, 0.7)
        - 0.47 * math.pow(s, 0.2)
    )
    return round(mci, 2)


def mci_band(mci: float) -> MCIBand:
    """Classify an MCI value into the paper's management bands."""
    if mci > MCI_DESIRABLE_MIN:
        return MCIBand.DESIRABLE
    if mci >= MCI_REPAIR_MIN:
        return MCIBand.NEEDS_REPAIR
    return MCIBand.IMMEDIATE_REPAIR
