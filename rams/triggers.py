"""
Explicit Indian intervention triggers (rut / crack / roughness / MSA).

This layer answers a different question from the IRC:82 PCI bands: not "what is
the overall condition score" but "which *specific* defect or traffic threshold
has been crossed, and what does Indian practice say to do about it". It is an
additive diagnostic -- it does not replace the PCI decision bands.

Thresholds reference (defaults; every value is overridable per concession/agency):
    * IRC:37-2018  -- design of flexible pavements; design traffic in MSA and the
      traffic categories used to size the pavement. When cumulative traffic since
      the last structural renewal approaches the design MSA, the fatigue life is
      consumed and a structural overlay is due *regardless of surface condition*.
    * IRC:81-1997  -- Benkelman-beam deflection survey & overlay (strengthening)
      design. High rebound deflection => structural strengthening.
    * IRC:82-2015  -- maintenance of bituminous roads (functional triggers).
    * IRC:SP:16 / MoRTH & NH O&M -- riding-quality (IRI) and rut/crack defect
      limits used in concession O&M (e.g. rut > 10 mm and cracking > 10% area are
      common corrective-maintenance defect limits).

The HDM-4 paper's own management reference -- cut-and-overlay once rut depth
exceeds 30 mm -- is carried as `rut_structural_mm`'s upper companion in
rams.mci (RUT_OVERLAY_THRESHOLD_MM); the Indian functional limit here is tighter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from .models import YearResult

# IRC:37 design-traffic categories (cumulative MSA over the design life). Used to
# describe the structural class a section was built for and to flag fatigue-life
# consumption.
IRC37_MSA_CATEGORIES = (
    (5.0, "<5 MSA (low volume)"),
    (10.0, "5-10 MSA"),
    (20.0, "10-20 MSA"),
    (30.0, "20-30 MSA"),
    (50.0, "30-50 MSA"),
    (100.0, "50-100 MSA"),
    (150.0, "100-150 MSA"),
    (float("inf"), ">150 MSA (heavy corridor)"),
)


def msa_category(msa: float) -> str:
    """Map a cumulative/design MSA value to its IRC:37 traffic category label."""
    for upper, label in IRC37_MSA_CATEGORIES:
        if msa < upper:
            return label
    return IRC37_MSA_CATEGORIES[-1][1]


class TriggerSeverity(str, Enum):
    FUNCTIONAL = "FUNCTIONAL"    # corrective / preventive surface treatment
    STRUCTURAL = "STRUCTURAL"    # overlay / strengthening / reconstruction


@dataclass(frozen=True)
class InterventionTriggers:
    """Indian condition + traffic intervention thresholds (defaults overridable)."""

    # Rutting (mm).
    rut_functional_mm: float = 10.0     # IRC:82 / NH O&M corrective limit
    rut_structural_mm: float = 20.0     # structural overlay candidate

    # Cracking (% area).
    crack_functional_pct: float = 10.0  # surface sealing / corrective
    crack_structural_pct: float = 20.0  # fatigue cracking -> structural

    # Roughness (engine IRI, mm/m).
    iri_functional: float = 2.5         # ~2500 mm/km riding-quality limit
    iri_structural: float = 4.0         # severe unevenness

    # FWD/Benkelman rebound deflection (mm) -- IRC:81 strengthening trigger.
    deflection_structural_mm: float = 1.0

    # Skid resistance (SFC) -- below this, a skid-resistance surface treatment is
    # due (IRC:SP:16 / safety). Only evaluated when a skid model is run.
    skid_functional_sfc: float = 0.40

    # Potholing (area %) -- above this, immediate patching is due. Only evaluated
    # when a pothole model is run.
    potholes_functional_pct: float = 2.0
    potholes_structural_pct: float = 10.0

    # Traffic / fatigue (IRC:37). Structural renewal when cumulative MSA since
    # the last overlay reaches `design_life_fraction` of the design MSA.
    design_life_fraction: float = 0.8


@dataclass(frozen=True)
class FiredTrigger:
    name: str
    severity: TriggerSeverity
    value: float
    threshold: float
    irc_reference: str
    reason: str


def evaluate_triggers(
    year: YearResult,
    *,
    cumulative_msa: float,
    design_msa: Optional[float] = None,
    deflection_mm: Optional[float] = None,
    triggers: InterventionTriggers = InterventionTriggers(),
) -> List[FiredTrigger]:
    """Return every intervention trigger crossed in a given forecast year.

    `design_msa` (the IRC:37 design traffic the section was built for) enables
    the fatigue-life / MSA trigger; omit it to skip that check. `deflection_mm`
    enables the IRC:81 deflection trigger.
    """
    fired: List[FiredTrigger] = []

    def add(name, sev, val, thr, ref, reason):
        fired.append(FiredTrigger(name, sev, round(val, 2), thr, ref, reason))

    # --- Rutting -----------------------------------------------------------
    if year.rutting_mm >= triggers.rut_structural_mm:
        add("rutting", TriggerSeverity.STRUCTURAL, year.rutting_mm, triggers.rut_structural_mm,
            "IRC:82 / IRC:81", "deep rutting -> structural overlay / profile correction")
    elif year.rutting_mm >= triggers.rut_functional_mm:
        add("rutting", TriggerSeverity.FUNCTIONAL, year.rutting_mm, triggers.rut_functional_mm,
            "IRC:82", "rut depth past corrective limit -> mill & fill / microsurfacing")

    # --- Cracking ----------------------------------------------------------
    if year.cracking_pct >= triggers.crack_structural_pct:
        add("cracking", TriggerSeverity.STRUCTURAL, year.cracking_pct, triggers.crack_structural_pct,
            "IRC:37 / IRC:81", "fatigue cracking area -> structural strengthening")
    elif year.cracking_pct >= triggers.crack_functional_pct:
        add("cracking", TriggerSeverity.FUNCTIONAL, year.cracking_pct, triggers.crack_functional_pct,
            "IRC:82", "cracking past corrective limit -> crack sealing / surface dressing")

    # --- Roughness (IRI) ---------------------------------------------------
    if year.iri >= triggers.iri_structural:
        add("roughness", TriggerSeverity.STRUCTURAL, year.iri, triggers.iri_structural,
            "IRC:SP:16", "severe unevenness -> overlay / reprofiling")
    elif year.iri >= triggers.iri_functional:
        add("roughness", TriggerSeverity.FUNCTIONAL, year.iri, triggers.iri_functional,
            "IRC:SP:16 / NH O&M", "riding quality past limit -> corrective resurfacing")

    # --- FWD/Benkelman deflection (IRC:81) ---------------------------------
    if deflection_mm is not None and deflection_mm >= triggers.deflection_structural_mm:
        add("deflection", TriggerSeverity.STRUCTURAL, deflection_mm, triggers.deflection_structural_mm,
            "IRC:81", "high rebound deflection -> structural strengthening (overlay design)")

    # --- Skid resistance (safety) ------------------------------------------
    if year.skid is not None and year.skid <= triggers.skid_functional_sfc:
        add("skid", TriggerSeverity.FUNCTIONAL, year.skid, triggers.skid_functional_sfc,
            "IRC:SP:16 / safety", "low skid resistance -> surface dressing / restore SFC")

    # --- Potholing ---------------------------------------------------------
    if year.potholes is not None:
        if year.potholes >= triggers.potholes_structural_pct:
            add("potholes", TriggerSeverity.STRUCTURAL, year.potholes, triggers.potholes_structural_pct,
                "IRC:82 / MoRTH", "extensive potholing -> overlay / reconstruction")
        elif year.potholes >= triggers.potholes_functional_pct:
            add("potholes", TriggerSeverity.FUNCTIONAL, year.potholes, triggers.potholes_functional_pct,
                "IRC:82 / MoRTH 3000", "potholes past limit -> immediate patching")

    # --- Traffic / fatigue life (IRC:37) -----------------------------------
    if design_msa is not None and design_msa > 0:
        consumed = cumulative_msa / design_msa
        if consumed >= triggers.design_life_fraction:
            add("traffic_msa", TriggerSeverity.STRUCTURAL, cumulative_msa,
                round(triggers.design_life_fraction * design_msa, 2), "IRC:37",
                f"cumulative {cumulative_msa:.1f} MSA has consumed "
                f"{consumed * 100:.0f}% of the {design_msa:.0f} MSA design life "
                f"-> plan structural renewal")

    return fired


DEFAULT_TRIGGERS = InterventionTriggers()
