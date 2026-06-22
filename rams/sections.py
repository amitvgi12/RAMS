"""
Dynamic segmentation: group a per-chainage NSV survey into homogeneous sections.

A raw network survey is hundreds/thousands of 100 m sub-segments. Maintenance is
planned on *homogeneous sections* -- contiguous stretches of similar condition.
This module delineates them with the **cumulative-difference method** (IRC:115-2014
clause 6.2.3 / AASHTO): the cumulative area of (value - network mean) is tracked
along the road, and a new section starts wherever that trend reverses (i.e. the
local value crosses the mean). Short runs are merged into their neighbour so a
section is never below `min_length_km`.

Each homogeneous section is then aggregated (length-weighted mean distress),
scored with the IRC:82 PCI, classified into a MoRTH maintenance band, and
forecast forward so the per-section table carries the preventive window and the
recommended treatment -- the tabular deliverable a PMS planner works from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from .config import DEFAULT_SCORING, MonsoonZone
from .engine import IndianPavementDeteriorationEngine, forecast_segment
from .maintenance import MaintenancePolicy, build_maintenance_plan
from .models import SegmentInput

_CHAINAGE_RE = re.compile(r"(\d+)\s*[-+to]+\s*(\d+)", re.IGNORECASE)


def _chainage(seg: SegmentInput) -> Optional[float]:
    """Best-effort start chainage parsed from a segment id like 'CH154400-154500/L1'."""
    m = _CHAINAGE_RE.search(str(seg.segment_id))
    return float(m.group(1)) if m else None


# One reusable engine purely to score IRC:82 PCI from a condition triple.
_SCORER = IndianPavementDeteriorationEngine(
    base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
    traffic_growth_rate=0.05, monsoon_zone="MEDIUM", scoring=DEFAULT_SCORING,
)


def base_pci(iri: float, rut: float, crack: float) -> float:
    return _SCORER.calculate_irc82_pci(iri, rut, crack)


@dataclass
class HomogeneousSection:
    """One delineated homogeneous section with its aggregate condition + plan."""

    section_id: str
    chainage_from: Optional[float]
    chainage_to: Optional[float]
    length_km: float
    n_points: int
    mean_iri: float
    mean_rut: float
    mean_crack: float
    mean_potholes: float
    base_pci: float
    band: str                       # ROUTINE | PREVENTIVE | STRUCTURAL
    treatment: str
    preventive_window_year: Optional[int]
    window_expired_year: Optional[int]
    final_pci: float

    def as_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "chainage_from": self.chainage_from,
            "chainage_to": self.chainage_to,
            "length_km": round(self.length_km, 3),
            "n_points": self.n_points,
            "mean_iri": round(self.mean_iri, 2),
            "mean_rut": round(self.mean_rut, 2),
            "mean_crack": round(self.mean_crack, 1),
            "mean_potholes": round(self.mean_potholes, 2),
            "base_pci": round(self.base_pci, 2),
            "band": self.band,
            "treatment": self.treatment,
            "preventive_window_year": self.preventive_window_year,
            "window_expired_year": self.window_expired_year,
            "final_pci": round(self.final_pci, 2),
        }


def _split_indices(values: List[float], lengths: List[float], min_length_km: float) -> List[int]:
    """Cumulative-difference boundaries: indices where value crosses the network
    mean, then short runs merged *forward* so every section is >= min_length_km."""
    n = len(values)
    if n <= 1:
        return [0, n]
    total_len = sum(lengths) or float(n)
    mean = sum(v * L for v, L in zip(values, lengths)) / total_len

    # Raw boundaries where the deviation (value - mean) changes sign.
    raw = [0]
    prev_sign = 0
    for i, v in enumerate(values):
        sign = 1 if v >= mean else -1
        if prev_sign and sign != prev_sign:
            raw.append(i)
        prev_sign = sign
    raw.append(n)

    # Greedily accumulate runs until each section reaches min_length_km (merges
    # short runs forward); the final boundary is always n.
    out = [0]
    for hi in raw[1:]:
        if sum(lengths[out[-1]:hi]) >= min_length_km or hi == n:
            out.append(hi)
    if out[-1] != n:
        out.append(n)
    # If the last section is itself too short, fold it into the previous one.
    if len(out) > 2 and sum(lengths[out[-2]:out[-1]]) < min_length_km:
        out.pop(-2)
    return out


def homogeneous_sections(
    segments: List[SegmentInput],
    *,
    horizon_years: int = 10,
    policy: Optional[MaintenancePolicy] = None,
    min_length_km: float = 0.5,
    key: str = "pci",
) -> List[HomogeneousSection]:
    """Delineate homogeneous sections from an ordered per-chainage survey.

    `key` selects the variable the cumulative-difference method runs on:
    "pci" (default, the composite condition), "rut", "iri" or "crack".
    """
    policy = policy or MaintenancePolicy()
    if not segments:
        return []

    pci = [base_pci(s.base_iri, s.base_rut, s.base_crack) for s in segments]
    lengths = [s.length_km for s in segments]
    key_vals = {
        "pci": pci,
        "rut": [s.base_rut for s in segments],
        "iri": [s.base_iri for s in segments],
        "crack": [s.base_crack for s in segments],
    }.get(key, pci)

    bounds = _split_indices(key_vals, lengths, min_length_km)

    sections: List[HomogeneousSection] = []
    for k in range(len(bounds) - 1):
        lo, hi = bounds[k], bounds[k + 1]
        members = segments[lo:hi]
        seg_len = sum(lengths[lo:hi]) or len(members) * 0.1

        def wmean(attr: str) -> float:
            num = sum(getattr(m, attr) * (m.length_km or 0.1) for m in members)
            return num / (seg_len if seg_len else len(members))

        m_iri, m_rut, m_crack = wmean("base_iri"), wmean("base_rut"), wmean("base_crack")
        m_pot = wmean("base_potholes")
        pci0 = base_pci(m_iri, m_rut, m_crack)
        flag = policy.classify(pci0)
        treatment = policy.recommended_treatment(pci0)

        # Forecast the section's mean condition forward for the per-section plan.
        # (Length only weights cost downstream, not the deterioration -- clamp it
        # to the validation range so a long section never trips the bound.)
        rep = members[0]
        sec_seg = SegmentInput(
            base_iri=m_iri, base_rut=m_rut, base_crack=m_crack,
            annual_msa=rep.annual_msa, traffic_growth_rate=rep.traffic_growth_rate,
            monsoon_zone=rep.monsoon_zone, length_km=max(0.01, min(seg_len, 100.0)),
        )
        timeline = forecast_segment(sec_seg, horizon_years)
        plan = build_maintenance_plan(timeline, policy)

        cf, ct = _chainage(members[0]), _chainage(members[-1])
        if ct is not None and members[-1] is not members[0]:
            ct2 = _CHAINAGE_RE.search(str(members[-1].segment_id))
            ct = float(ct2.group(2)) if ct2 else ct
        sections.append(HomogeneousSection(
            section_id=f"Sec-{k + 1}",
            chainage_from=cf, chainage_to=ct,
            length_km=seg_len, n_points=len(members),
            mean_iri=m_iri, mean_rut=m_rut, mean_crack=m_crack, mean_potholes=m_pot,
            base_pci=pci0, band=flag.value, treatment=treatment.name,
            preventive_window_year=plan.preventive_window_year,
            window_expired_year=plan.window_expired_year,
            final_pci=timeline[-1].irc82_pci,
        ))
    return sections


@dataclass
class SectioningResult:
    """Homogeneous-section breakdown of an uploaded survey."""

    n_points: int
    total_length_km: float
    horizon_years: int
    key: str
    sections: List[HomogeneousSection]

    def as_dict(self) -> dict:
        bands = {"ROUTINE": 0, "PREVENTIVE": 0, "STRUCTURAL": 0}
        for s in self.sections:
            bands[s.band] = bands.get(s.band, 0) + 1
        return {
            "n_points": self.n_points,
            "total_length_km": round(self.total_length_km, 2),
            "horizon_years": self.horizon_years,
            "key": self.key,
            "n_sections": len(self.sections),
            "band_counts": bands,
            "sections": [s.as_dict() for s in self.sections],
        }


def section_survey(
    segments: List[SegmentInput],
    *,
    horizon_years: int = 10,
    min_length_km: float = 0.5,
    key: str = "pci",
    policy: Optional[MaintenancePolicy] = None,
) -> SectioningResult:
    """Top-level: ordered survey -> homogeneous sections + summary."""
    secs = homogeneous_sections(
        segments, horizon_years=horizon_years, policy=policy,
        min_length_km=min_length_km, key=key,
    )
    return SectioningResult(
        n_points=len(segments),
        total_length_km=sum(s.length_km for s in segments),
        horizon_years=horizon_years, key=key, sections=secs,
    )
