"""
NSV chainage-survey ingestion (ROMDAS / Hawkeye-style network survey vendors).

Real Indian condition surveys do not arrive in RAMS' canonical schema: a network
survey ships one *distress per file*, keyed to chainage and GPS, with vendor
column names and **text condition bands** rather than raw values. A typical
multi-distress NSV survey set looks like:

    Rutting    : Start_Chainage, End_Chainage, ..., Lane, Rutting (mm)
    Roughness  : ...,                                Lane, BI m/km, Lane IRI (m/km)
    Cracking   : ...,                                Lane, Condition ("Good (5-10%)"), Rating
    Potholes   : ...,                                Lane, PotHoles ("Very Good (0)")

This module maps those vendor schemas onto `SegmentInput`. Two entry points:

  * `segments_from_survey(rows, ...)` -- map ONE distress file; the surveyed
    distress is populated, the rest fall back to supplied defaults (so a single
    rutting file still loads and forecasts).
  * `merge_surveys([rows, ...], ...)` -- join several distress files by
    (chainage, lane) into one fully-populated segment per 100 m sub-section,
    which is what a real network forecast needs.

Text bands like "Good (5-10%)" are converted to a representative numeric value
(`<5` -> 2.5, `5-10` -> 7.5, `>20` -> 25). Rows that cannot be mapped are
isolated as errors, never aborting the import -- the same trust-boundary stance
as the rest of RAMS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .batch import IngestResult
from .config import DEFAULT_BOUNDS, InputBounds, MonsoonZone
from .models import SegmentInput


@dataclass
class SurveyDefaults:
    """Values used to fill fields a single-distress survey does not carry."""

    base_iri: float = 2.0
    base_rut: float = 2.0
    base_crack: float = 0.0
    base_potholes: float = 0.0
    annual_msa: float = 4.5
    traffic_growth_rate: float = 0.05
    monsoon_zone: str = "MEDIUM"


def _nk(s) -> str:
    """Normalise a key the same way ingestion does (whitespace/newline -> '_')."""
    return re.sub(r"\s+", "_", str(s).strip().lower())


def _norm_row(row: Dict[str, str]) -> Dict[str, str]:
    return {_nk(k): v for k, v in row.items()}


# Distress detected from the (normalised) header set, in priority order.
def detect_distress(headers) -> Optional[str]:
    """Return 'rutting' | 'roughness' | 'cracking' | 'potholes' | None."""
    h = {_nk(x) for x in headers}
    if "rutting" in h or "rut" in h:
        return "rutting"
    if ("lane_iri" in h or "iri" in h
            or any(("roughness" in k) or k.startswith("bi") or "_bi" in k for k in h)):
        return "roughness"
    if "potholes" in h:
        return "potholes"
    if "cracking" in h or "crack" in h:
        return "cracking"
    # A bare 'condition' band (with a chainage) is a cracking survey -- roughness,
    # rutting and potholes are matched above, so this only catches cracking.
    if "condition" in h and ("rating" in h or "start_chainage" in h or "chainage" in h):
        return "cracking"
    return None


def is_survey(headers) -> bool:
    """True if the header set looks like a chainage NSV survey (not RAMS native)."""
    h = {_nk(x) for x in headers}
    return ("start_chainage" in h or "chainage" in h) and detect_distress(headers) is not None


def detect_defect(headers) -> Optional[str]:
    """Recognise a per-defect inventory log (one row per pothole / crack).

    Returns 'pothole_defect' | 'crack_defect' | None. These are aggregated per
    chainage (count + area) rather than read row-as-segment.
    """
    h = {_nk(x) for x in headers}
    if "chainage" not in h and "start_chainage" not in h:
        return None
    has_area = any("area" in k for k in h)
    has_depth = any("depth" in k for k in h)
    if has_area and (has_depth or "severity" in h):
        return "pothole_defect"
    if any("width" in k for k in h) and (any("classification" in k for k in h)
                                         or any("crack" in k for k in h)):
        return "crack_defect"
    return None


def aggregate_defects(rows: List[Dict[str, str]], kind: str,
                      *, lane_width_m: float = 3.5, bin_m: float = 100.0) -> List[Dict[str, str]]:
    """Aggregate a per-defect log into per-(100 m chainage, lane) survey rows.

    Pothole/crack areas are summed and expressed as a percent of the sub-section
    carriageway area (length x lane width); the count is carried too. Defect
    chainages (point values) are binned to `bin_m` sub-sections.
    """
    norm = [_norm_row(r) for r in rows]
    agg: Dict[tuple, Dict[str, float]] = {}
    for r in norm:
        ch = _num(r, "chainage", "start_chainage")
        if ch is None:
            continue
        start = int(ch // bin_m * bin_m)
        lane = str(r.get("lane", "") or "").strip() or "L1"
        rec = agg.setdefault((start, lane), {"count": 0.0, "area": 0.0})
        rec["count"] += 1.0
        if kind == "pothole_defect":
            a = _num(r, "area_(in_sq_m)", "area", "area_(sq_m)")
            if a is not None:
                rec["area"] += a
        else:  # crack_defect: area = length(m) x width(mm -> m)
            length, width = _num(r, "length"), _num(r, "width")
            if length is not None and width is not None:
                rec["area"] += length * (width / 1000.0)
    seg_area = bin_m * lane_width_m
    out: List[Dict[str, str]] = []
    for (start, lane), rec in agg.items():
        pct = min(100.0, rec["area"] / seg_area * 100.0) if seg_area else 0.0
        row = {"start_chainage": str(start), "end_chainage": str(start + int(bin_m)),
               "lane": lane, "defect_count": str(int(rec["count"]))}
        row["potholes" if kind == "pothole_defect" else "crack"] = str(round(pct, 3))
        out.append(row)
    return out


# A lane token (l1/l2/r1/r2) as a whole word, at the start or end of the column
# name -- matches both 'L1 Lane Roughness BI' (prefix) and 'Rut Depth L1' (suffix
# from a combined group header), while ignoring embedded tokens like 'MaxL1'.
_LANE_RE = re.compile(r"(?:^|_)([lr]\d)(?=_|$)", re.IGNORECASE)


def expand_wide_lanes(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Expand a wide per-lane sheet into one row per lane.

    Vendor condition sheets often carry every distress for each lane in its own
    column (`L1 Lane Roughness BI`, `L1 % Crack Area`, `L1 Rut Depth`, ...). This
    turns each such row into up to four rows (one per lane), mapping the lane's
    columns onto the canonical distress keys (`bi_m/km`, `crack`, `rutting`).
    Pass-through if the sheet is not in the wide per-lane format.
    """
    if not rows:
        return rows
    lane_cols = [k for k in rows[0] if _LANE_RE.search(k)]
    if not lane_cols:
        return rows
    out: List[Dict[str, str]] = []
    for r in rows:
        base = {k: v for k, v in r.items() if k not in lane_cols}
        lanes: Dict[str, Dict[str, str]] = {}
        for k in lane_cols:
            lanes.setdefault(_LANE_RE.search(k).group(1).upper(), {})[k] = r.get(k)
        for lane, cols in lanes.items():
            nr = dict(base)
            nr["lane"] = lane
            for k, v in cols.items():            # cols are in column order
                if str(v).strip() in ("", "None"):
                    continue
                kl = k.lower()
                # setdefault -> the first (left-most) data block wins, so a later
                # vendor working/flag column never overwrites a real measurement.
                if "roughness" in kl and "bi" in kl:
                    nr.setdefault("bi_m/km", v)
                elif "crack" in kl:
                    nr.setdefault("crack", v)
                elif "rut" in kl:
                    nr.setdefault("rutting", v)
                # ravelling / generic area% have no RAMS-condition equivalent -> ignored
            out.append(nr)
    return out or rows


# Back-compat alias (older callers).
expand_wide_roughness = expand_wide_lanes


def band_value(text: str) -> Optional[float]:
    """Representative numeric value from a condition band like 'Good (5-10%)'.

    '<5' -> 2.5 ; '5-10' -> 7.5 ; '>20' -> 25 ; '(0)' -> 0.
    """
    if text is None:
        return None
    s = str(text)
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", s)]
    if not nums:
        return None
    if "<" in s:
        return nums[0] / 2.0
    if ">" in s:
        return nums[0] * 1.25
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2.0
    return nums[0]


def _num(row: Dict[str, str], *keys) -> Optional[float]:
    for k in keys:
        if k in row and str(row[k]).strip() not in ("", "None"):
            try:
                return float(row[k])
            except (TypeError, ValueError):
                return None
    return None


def _chainage_id(row: Dict[str, str]) -> Tuple[str, str, float]:
    """Return (segment_id, lane, length_km) from chainage + lane columns."""
    start = _num(row, "start_chainage")
    end = _num(row, "end_chainage")
    if start is None:
        start = _num(row, "chainage")               # single-chainage vendor format
    lane = str(row.get("lane", "") or "").strip() or "L1"
    if start is not None and end is not None and end > start:
        length_km = (end - start) / 1000.0
        sid = f"CH{int(start)}-{int(end)}/{lane}"
    elif start is not None:
        length_km = 0.1
        sid = f"CH{int(start)}/{lane}"
    else:
        length_km = 0.1
        sid = f"{row.get('key', 'SEG')}/{lane}"
    return sid, lane, length_km


def _distress_value(row: Dict[str, str], distress: str) -> Dict[str, float]:
    """Pull the one distress this survey row carries, as SegmentInput kwargs."""
    if distress == "rutting":
        v = _num(row, "rutting", "rut")
        return {"base_rut": v} if v is not None else {}
    if distress == "roughness":
        v = _num(row, "lane_iri", "iri")            # IRI in m/km == mm/m
        if v is None:
            bi = _num(row, "bi_m/km")               # bump integrator mm/km
            v = bi / 1000.0 if bi is not None else None  # ~ IRI proxy
        return {"base_iri": v} if v is not None else {}
    if distress == "cracking":
        v = _num(row, "cracking", "crack")
        if v is None:
            v = band_value(row.get("condition"))
        return {"base_crack": v} if v is not None else {}
    if distress == "potholes":
        v = _num(row, "potholes")
        if v is None:
            v = band_value(row.get("potholes"))
        if v is None:
            v = band_value(row.get("condition"))
        return {"base_potholes": v} if v is not None else {}
    return {}


def _all_distress_values(row: Dict[str, str], distress: Optional[str]) -> Dict[str, float]:
    """Extract every distress a row carries (a wide/merged sheet may hold several).

    Explicit numeric columns are always read; an ambiguous text `condition` band
    is attributed only to the sheet's primary distress, so a potholes sheet's
    rating column is never mistaken for cracking.
    """
    out: Dict[str, float] = {}
    v = _num(row, "rutting", "rut")
    if v is not None:
        out["base_rut"] = v
    iri = _num(row, "lane_iri", "iri")
    if iri is None:
        bi = _num(row, "bi_m/km")
        iri = bi / 1000.0 if bi is not None else None
    if iri is not None:
        out["base_iri"] = iri
    v = _num(row, "cracking", "crack")
    if v is not None:
        out["base_crack"] = v
    v = _num(row, "potholes")
    if v is None:
        v = band_value(row.get("potholes"))
    if v is not None:
        out["base_potholes"] = v
    band = band_value(row.get("condition")) if row.get("condition") is not None else None
    if band is not None:
        if distress == "cracking" and "base_crack" not in out:
            out["base_crack"] = band
        elif distress == "potholes" and "base_potholes" not in out:
            out["base_potholes"] = band
    return out


def _has_chainage(row: Dict[str, str]) -> bool:
    return _num(row, "start_chainage", "chainage") is not None


def _build_segment(
    sid: str, length_km: float, fields: Dict[str, float],
    defaults: SurveyDefaults, bounds: InputBounds,
) -> SegmentInput:
    return SegmentInput(
        base_iri=fields.get("base_iri", defaults.base_iri),
        base_rut=fields.get("base_rut", defaults.base_rut),
        base_crack=fields.get("base_crack", defaults.base_crack),
        annual_msa=defaults.annual_msa,
        traffic_growth_rate=defaults.traffic_growth_rate,
        monsoon_zone=MonsoonZone.from_str(defaults.monsoon_zone),
        segment_id=sid,
        length_km=max(bounds.length_min, min(bounds.length_max, length_km)),
        base_potholes=fields.get("base_potholes", defaults.base_potholes),
    ).validate(bounds)


def segments_from_survey(
    rows: List[Dict[str, str]],
    defaults: Optional[SurveyDefaults] = None,
    bounds: InputBounds = DEFAULT_BOUNDS,
) -> IngestResult:
    """Map one distress survey (list of lowercased-header dict rows) to segments."""
    defaults = defaults or SurveyDefaults()
    if not rows:
        raise ValueError("survey has no data rows.")
    rows = expand_wide_lanes([_norm_row(r) for r in rows])
    distress = detect_distress(rows[0].keys())
    if distress is None:
        raise ValueError("not a recognised NSV survey (no rutting/roughness/cracking/potholes column).")

    segments: List[SegmentInput] = []
    errors: List[Tuple[int, str]] = []
    for i, row in enumerate(rows, start=1):
        if not _has_chainage(row):
            continue  # skip blank / non-data rows
        try:
            sid, _lane, length_km = _chainage_id(row)
            fields = _all_distress_values(row, distress)
            segments.append(_build_segment(sid, length_km, fields, defaults, bounds))
        except (ValueError, KeyError) as exc:
            errors.append((i, str(exc)))
    return IngestResult(segments=segments, errors=errors)


def merge_surveys(
    surveys: List[List[Dict[str, str]]],
    defaults: Optional[SurveyDefaults] = None,
    bounds: InputBounds = DEFAULT_BOUNDS,
) -> IngestResult:
    """Join several distress surveys by (chainage, lane) into full segments.

    Each input is one distress file's rows. Records are merged on their
    chainage+lane id, so the resulting segment carries IRI, rut, cracking and
    potholes together -- the fully-populated condition a forecast needs.
    """
    defaults = defaults or SurveyDefaults()
    merged: "Dict[str, dict]" = {}
    order: List[str] = []
    for rows in surveys:
        if not rows:
            continue
        rows = expand_wide_lanes([_norm_row(r) for r in rows])
        distress = detect_distress(rows[0].keys())
        if distress is None:
            continue
        for row in rows:
            if not _has_chainage(row):
                continue
            sid, _lane, length_km = _chainage_id(row)
            rec = merged.get(sid)
            if rec is None:
                rec = {"length_km": length_km, "fields": {}}
                merged[sid] = rec
                order.append(sid)
            rec["fields"].update(_all_distress_values(row, distress))

    segments: List[SegmentInput] = []
    errors: List[Tuple[int, str]] = []
    for i, sid in enumerate(order, start=1):
        rec = merged[sid]
        try:
            segments.append(_build_segment(sid, rec["length_km"], rec["fields"], defaults, bounds))
        except (ValueError, KeyError) as exc:
            errors.append((i, str(exc)))
    return IngestResult(segments=segments, errors=errors)
