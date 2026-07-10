"""
Pure request/response functions powering the web dashboard.

Kept free of any HTTP/socket concerns so they are unit-testable and reusable
(CLI, server, notebooks). Each function takes a plain dict (already parsed
from JSON) and returns a JSON-serialisable dict. All raise ValueError on bad
input, which the server maps to HTTP 400.
"""
from __future__ import annotations

import base64
import binascii
from typing import Dict, List

from .batch import forecast_network
from .calibrate import (
    PotholeObservation,
    RoughnessObservation,
    RutObservation,
    SkidObservation,
    calibrate_hdm4_potholes,
    calibrate_hdm4_roughness,
    calibrate_hdm4_rut,
    calibrate_hdm4_skid,
    calibrate_mlit_cracking,
    load_observations_csv,
)
from .config import (
    CrackModelType,
    MonsoonZone,
    PotholeModelType,
    RoughnessModelType,
    RutModelType,
    SkidModelType,
)
from .distress import (
    DEFAULT_HDM4_POTHOLE,
    DEFAULT_HDM4_ROUGHNESS,
    DEFAULT_HDM4_SKID,
    mlit_crack_preset,
)
from .design import design_pavement
from .engine import IndianPavementDeteriorationEngine
from .fwd import snp_from_deflection
from .iitpave import (
    FWDSection,
    LayerModel,
    design_pavement_mechanistic,
    evaluate_fwd_sections,
    evaluate_section,
)
from .pbmc import PBMCParams, estimate_pbmc, estimate_pbmc_network
from .hdm4 import preset as hdm4_preset
from .residual import handback_assessment, remaining_fatigue_life
from .traffic import default_vdf, design_msa, lane_distribution_factor
from .ingest import (
    _extract_pdf_text,
    _xlsx_sheets,
    ingest_segments_csv,
    ingest_segments_csv_text,
    ingest_segments_pdf,
    ingest_segments_pdf_bytes,
    ingest_segments_xlsx,
    ingest_segments_xlsx_bytes,
)
from .lifecycle import simulate_managed_lifecycle
from .maintenance import MaintenancePolicy, annotate_timeline, build_maintenance_plan
from .mci import compute_mci, mci_band
from .models import SegmentInput
from .optimize import BudgetParams, optimize_budget, recommend_budget_to_clear
from .triggers import evaluate_triggers

_POLICY = MaintenancePolicy()

# Bounded so a hostile/fat-fingered request can't request a huge network.
MAX_NETWORK_SEGMENTS = 5_000

# Demo network used to prefill the dashboard (mirrors examples/sample_network.csv).
DEFAULT_NETWORK: List[dict] = [
    {"segment_id": "NH66-KL-012", "base_iri": 1.5, "base_rut": 2.0, "base_crack": 0.0, "annual_msa": 4.5, "traffic_growth_rate": 0.06, "monsoon_zone": "HIGH", "length_km": 12.0},
    {"segment_id": "NH48-KA-204", "base_iri": 2.2, "base_rut": 4.0, "base_crack": 3.0, "annual_msa": 6.0, "traffic_growth_rate": 0.05, "monsoon_zone": "MEDIUM", "length_km": 8.5},
    {"segment_id": "SH-RJ-077", "base_iri": 3.0, "base_rut": 6.0, "base_crack": 8.0, "annual_msa": 2.0, "traffic_growth_rate": 0.03, "monsoon_zone": "LOW", "length_km": 20.0},
    {"segment_id": "NH16-MH-330", "base_iri": 1.8, "base_rut": 3.0, "base_crack": 1.0, "annual_msa": 8.0, "traffic_growth_rate": 0.07, "monsoon_zone": "HIGH", "length_km": 6.0},
    {"segment_id": "MDR-UP-451", "base_iri": 2.6, "base_rut": 5.0, "base_crack": 5.0, "annual_msa": 3.5, "traffic_growth_rate": 0.04, "monsoon_zone": "MEDIUM", "length_km": 15.0},
    {"segment_id": "NH44-TN-118", "base_iri": 2.0, "base_rut": 3.5, "base_crack": 2.0, "annual_msa": 5.5, "traffic_growth_rate": 0.05, "monsoon_zone": "MEDIUM", "length_km": 10.0},
    {"segment_id": "SH-AS-090", "base_iri": 2.8, "base_rut": 5.5, "base_crack": 6.0, "annual_msa": 3.0, "traffic_growth_rate": 0.04, "monsoon_zone": "HIGH", "length_km": 18.0},
    {"segment_id": "NH52-RJ-260", "base_iri": 1.6, "base_rut": 2.5, "base_crack": 0.5, "annual_msa": 4.0, "traffic_growth_rate": 0.05, "monsoon_zone": "LOW", "length_km": 14.0},
]


def default_network() -> dict:
    """Demo network for prefilling the dashboard."""
    return {"segments": DEFAULT_NETWORK}


def _f(payload: dict, key: str, default: float) -> float:
    """Coerce a single JSON field to float with a clear error."""
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"field {key!r} must be numeric.") from None


def _i(payload: dict, key: str, default: int) -> int:
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"field {key!r} must be an integer.") from None


def bands() -> dict:
    """Decision-band thresholds, so the client renders the same shading."""
    return {
        "preventive_upper": _POLICY.preventive_upper,
        "structural_lower": _POLICY.structural_lower,
        "score_min": 1.0,
        "score_max": 4.0,
    }


def forecast_single(payload: dict) -> dict:
    """Untreated forecast + treated (managed) trajectory + maintenance plan.

    Optional payload fields select the rut model:
      model    : "default" (IRC:82 power law) | "hdm4" (mechanistic delta-RDM)
      pavement : "dense" | "porous"  (HDM-4 calibration preset)
      deflection, snp, comp, hs, cds, speed : HDM-4 structural / FWD inputs
      design_msa : IRC:37 design traffic, enables the MSA fatigue-life trigger
    """
    horizon = _i(payload, "years", 10)
    rut_model = RutModelType.from_str(str(payload.get("model", "default")))
    crack_model = CrackModelType.from_str(str(payload.get("crack_model", "default")))
    roughness_model = RoughnessModelType.from_str(str(payload.get("roughness_model", "default")))
    skid_model = SkidModelType.from_str(str(payload.get("skid_model", "none")))
    pothole_model = PotholeModelType.from_str(str(payload.get("pothole_model", "none")))
    pavement = str(payload.get("pavement", "dense"))
    hdm4_cal = hdm4_preset(pavement)
    mlit_crack = mlit_crack_preset(pavement)
    model_kw = dict(
        rut_model=rut_model, hdm4_calibration=hdm4_cal,
        crack_model=crack_model, mlit_crack=mlit_crack,
        roughness_model=roughness_model, hdm4_roughness=DEFAULT_HDM4_ROUGHNESS,
        skid_model=skid_model, hdm4_skid=DEFAULT_HDM4_SKID,
        pothole_model=pothole_model, hdm4_pothole=DEFAULT_HDM4_POTHOLE,
    )
    deflection = _f(payload, "deflection", 0.5)
    # Optionally back-calculate the structural number from the FWD deflection
    # (a raw deflection survey carries no SNP).
    derived_snp = snp_from_deflection(deflection) if payload.get("derive_snp") else None
    structural_kw = dict(
        deflection_mm=deflection,
        structural_number=derived_snp if derived_snp is not None else _f(payload, "snp", 4.0),
        compaction_pct=_f(payload, "comp", 98.0),
        surfacing_thickness_mm=_f(payload, "hs", 100.0),
        cds=_f(payload, "cds", 1.0),
        heavy_speed_kmh=_f(payload, "speed", 50.0),
        base_skid=_f(payload, "base_skid", 0.55),
        base_potholes=_f(payload, "base_potholes", 0.0),
    )
    engine = IndianPavementDeteriorationEngine(
        base_iri=_f(payload, "iri", 1.5),
        base_rut=_f(payload, "rut", 2.0),
        base_crack=_f(payload, "crack", 0.0),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=str(payload.get("zone", "HIGH")),
        **model_kw,
        **structural_kw,
    )
    timeline = engine.run_lifecycle_forecast(horizon)
    plan = build_maintenance_plan(timeline, _POLICY)
    annotate_timeline(timeline, _POLICY)

    # Treated trajectory for the comparison chart (same model + structural inputs).
    seg = SegmentInput(
        base_iri=_f(payload, "iri", 1.5),
        base_rut=_f(payload, "rut", 2.0),
        base_crack=_f(payload, "crack", 0.0),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=MonsoonZone.from_str(str(payload.get("zone", "HIGH"))),
        segment_id=str(payload.get("id", "SEGMENT")),
        length_km=_f(payload, "length_km", 1.0),
        **structural_kw,
    )
    managed = simulate_managed_lifecycle(
        seg, horizon, policy=_POLICY, width_m=_f(payload, "width_m", 7.0), **model_kw
    )

    # Indian intervention triggers (rut / crack / IRI / FWD deflection / MSA).
    design_msa = payload.get("design_msa")
    design_msa = float(design_msa) if design_msa not in (None, "") else None
    triggers = [
        {
            "year": yr.year,
            "fired": [
                {
                    "name": t.name, "severity": t.severity.value,
                    "value": t.value, "threshold": t.threshold,
                    "irc_reference": t.irc_reference, "reason": t.reason,
                }
                for t in evaluate_triggers(
                    yr, cumulative_msa=yr.cumulative_msa, design_msa=design_msa,
                    deflection_mm=deflection,
                )
            ],
        }
        for yr in timeline
    ]

    # MLIT-PMS Maintenance Control Index alongside the IRC:82 PCI (paper
    # cross-reference). IRI stands in for the paper's longitudinal-roughness
    # sigma -- an approximation, surfaced as a secondary indicator only.
    mci = [
        {
            "year": yr.year,
            "mci": compute_mci(yr.rutting_mm, yr.cracking_pct, yr.iri),
            "band": mci_band(
                compute_mci(yr.rutting_mm, yr.cracking_pct, yr.iri)
            ).value,
            "rut_over_30mm": yr.rutting_mm > 30.0,
        }
        for yr in timeline
    ]

    # Skid resistance (SFC) trajectory, when a skid model is active.
    skid = (
        [{"year": yr.year, "skid": yr.skid,
          "below_limit": yr.skid is not None and yr.skid <= 0.40}
         for yr in timeline]
        if skid_model is SkidModelType.HDM4 else []
    )

    # Potholing (area %) trajectory, when a pothole model is active.
    potholes = (
        [{"year": yr.year, "potholes": yr.potholes,
          "over_limit": yr.potholes is not None and yr.potholes >= 2.0}
         for yr in timeline]
        if pothole_model is PotholeModelType.HDM4 else []
    )

    return {
        "bands": bands(),
        "model": {
            "rut_model": rut_model.value,
            "crack_model": crack_model.value,
            "roughness_model": roughness_model.value,
            "skid_model": skid_model.value,
            "pothole_model": pothole_model.value,
            "label": (
                hdm4_cal.label if rut_model is RutModelType.HDM4
                else "default IRC:82 power law"
            ),
            "crack_label": (
                mlit_crack.label if crack_model is CrackModelType.MLIT
                else "default IRC:82 S-curve"
            ),
            "roughness_label": (
                DEFAULT_HDM4_ROUGHNESS.label if roughness_model is RoughnessModelType.HDM4
                else "default IRI structural+traffic law"
            ),
            "skid_label": (
                DEFAULT_HDM4_SKID.label if skid_model is SkidModelType.HDM4
                else "skid not modelled"
            ),
            "pothole_label": (
                DEFAULT_HDM4_POTHOLE.label if pothole_model is PotholeModelType.HDM4
                else "potholes not modelled"
            ),
            "rut_breakdown": engine.rut_breakdown,  # [] unless HDM-4
            "structural_number": structural_kw["structural_number"],
            "snp_derived_from_fwd": derived_snp is not None,
        },
        "triggers": triggers,
        "skid": skid,
        "potholes": potholes,
        "untreated": [yr.as_row() for yr in timeline],
        "mci": mci,
        "flags": [f.value for f in plan.flags_by_year],
        "treated": [yr.as_row() for yr in managed.timeline],
        "plan": {
            "preventive_window_year": plan.preventive_window_year,
            "window_expired_year": plan.window_expired_year,
            "recommended_year": plan.recommended_year,
            "recommended_treatment": (
                plan.recommended_treatment.name if plan.recommended_treatment else None
            ),
            "morth_reference": (
                plan.recommended_treatment.morth_reference
                if plan.recommended_treatment else None
            ),
            "rationale": plan.rationale,
        },
        "interventions": [
            {
                "year": iv.year, "treatment": iv.treatment.name, "cost": iv.cost,
                "pci_before": iv.pci_before, "pci_after": iv.pci_after,
            }
            for iv in managed.interventions
        ],
        "managed_total_cost": managed.total_cost,
    }


def survey_sections(payload: dict) -> dict:
    """Group an uploaded chainage survey into homogeneous sections (tabular).

    Accepts a `segments` list (as returned by /api/upload) plus optional
    `years`, `min_length_km`, and `key` ("pci"|"rut"|"iri"|"crack"). Returns the
    per-section breakdown with aggregate condition, PCI band and preventive window.
    """
    from .sections import section_survey
    segments = _segments_from_payload(payload)
    result = section_survey(
        segments,
        horizon_years=_i(payload, "years", 10),
        min_length_km=_f(payload, "min_length_km", 0.5),
        key=str(payload.get("key", "pci")).strip().lower(),
    )
    return result.as_dict()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def export_report(payload: dict, fmt: str):
    """Build a downloadable report. `report`: "sections" (default) or "lca".
    Returns (bytes, mime, filename)."""
    fmt = str(fmt).strip().lower()
    if fmt not in ("xlsx", "pdf"):
        raise ValueError("'format' must be xlsx or pdf.")
    kind = str(payload.get("report", "sections")).strip().lower()

    if kind == "lca":
        from .export import lca_to_pdf, lca_to_xlsx
        result = _lca_from_payload(payload)
        if fmt == "xlsx":
            return lca_to_xlsx(result), _XLSX_MIME, "rams_lca_matrix.xlsx"
        return lca_to_pdf(result), "application/pdf", "rams_lca_matrix.pdf"

    from .sections import section_survey
    from .export import sections_to_pdf, sections_to_xlsx
    result = section_survey(
        _segments_from_payload(payload),
        horizon_years=_i(payload, "years", 10),
        min_length_km=_f(payload, "min_length_km", 0.5),
        key=str(payload.get("key", "pci")).strip().lower(),
    )
    if fmt == "xlsx":
        return sections_to_xlsx(result), _XLSX_MIME, "rams_sections.xlsx"
    return sections_to_pdf(result), "application/pdf", "rams_sections.pdf"


def _lca_from_payload(payload: dict):
    """Build an LCAResult from a single-segment payload (shared by lca + export)."""
    from .lca import lca_matrix
    seg = SegmentInput(
        base_iri=_f(payload, "iri", 2.5), base_rut=_f(payload, "rut", 4.0),
        base_crack=_f(payload, "crack", 3.0), annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=MonsoonZone.from_str(str(payload.get("zone", "MEDIUM"))),
        segment_id=str(payload.get("id", "SEGMENT")),
        length_km=_f(payload, "length_km", 1.0),
        deflection_mm=_f(payload, "deflection", 0.5),
        structural_number=_f(payload, "snp", 4.0),
    )
    return lca_matrix(
        seg, _i(payload, "years", 15), width_m=_f(payload, "width_m", 7.0),
        discount_rate=_f(payload, "discount_rate", 0.08),
        rut_model=RutModelType.from_str(str(payload.get("model", "default"))),
        hdm4_calibration=hdm4_preset(str(payload.get("pavement", "dense"))),
        crack_model=CrackModelType.from_str(str(payload.get("crack_model", "default"))),
        roughness_model=RoughnessModelType.from_str(str(payload.get("roughness_model", "default"))),
    )


def lca(payload: dict) -> dict:
    """Life-cycle decision matrix + MoRTH costs over a user-given horizon.

    Single segment (same condition fields as /api/forecast) -> per-year matrix
    triggering routine/preventive/overlay/reconstruction, priced from the MoRTH
    Standard Data Book, with total cost, NPV and EUAC. Optional: years, width_m,
    discount_rate, plus the rut/crack/roughness model selectors.
    """
    return _lca_from_payload(payload).as_dict()


def _segments_from_payload(payload: dict) -> List[SegmentInput]:
    rows = payload.get("segments")
    if not isinstance(rows, list) or not rows:
        raise ValueError("'segments' must be a non-empty list.")
    if len(rows) > MAX_NETWORK_SEGMENTS:
        raise ValueError(f"too many segments (max {MAX_NETWORK_SEGMENTS}).")
    segments: List[SegmentInput] = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"segment #{i} must be an object.")
        # Optional structural/FWD fields, forwarded only when present so the
        # network can be forecast with HDM-4 using per-segment deflection/SNP.
        structural = {
            k: row[k] for k in
            ("deflection_mm", "structural_number", "compaction_pct",
             "surfacing_thickness_mm", "cds", "heavy_speed_kmh")
            if row.get(k) not in (None, "")
        }
        segments.append(
            SegmentInput(
                segment_id=str(row.get("segment_id", f"SEG-{i}")),
                base_iri=row.get("base_iri", 1.5),
                base_rut=row.get("base_rut", 2.0),
                base_crack=row.get("base_crack", 0.0),
                annual_msa=row.get("annual_msa", 4.5),
                traffic_growth_rate=row.get("traffic_growth_rate", 0.05),
                monsoon_zone=MonsoonZone.from_str(str(row.get("monsoon_zone", "MEDIUM"))),
                length_km=row.get("length_km", 1.0),
                **structural,
            ).validate()
        )
    return segments


def ingest_data(payload: dict) -> dict:
    """Parse an uploaded CSV/XLSX/PDF network file into segment rows.

    Request shape (one of `content` for CSV text, `content_b64` for binary):
        {"format": "csv"|"xlsx"|"pdf",
         "content": "<text>",            # csv
         "content_b64": "<base64>"}      # xlsx / pdf (binary)

    Returns the parsed (validated) segments ready to feed `/api/network`,
    plus any per-row errors.
    """
    fmt = str(payload.get("format", "")).strip().lower()
    if fmt == "csv":
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("'content' (text) is required for csv import.")
        result = ingest_segments_csv_text(content)
    elif fmt in ("pdf", "xlsx"):
        b64 = payload.get("content_b64")
        if not isinstance(b64, str) or not b64.strip():
            raise ValueError(f"'content_b64' (base64) is required for {fmt} import.")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("'content_b64' is not valid base64.") from None
        result = (
            ingest_segments_xlsx_bytes(data)
            if fmt == "xlsx"
            else ingest_segments_pdf_bytes(data)
        )
    else:
        raise ValueError("'format' must be one of: csv, xlsx, pdf.")

    return _ingest_payload(result, fmt)


def _ingest_payload(result, fmt: str) -> dict:
    """Shape an IngestResult for the portal (shared by JSON + file upload)."""
    if len(result.segments) > MAX_NETWORK_SEGMENTS:
        raise ValueError(
            f"file parsed {len(result.segments)} segments; the dashboard handles up "
            f"to {MAX_NETWORK_SEGMENTS}. For a full raw NSV survey, use the CLI batch "
            f"path (python -m rams.cli --csv <file>), which streams the whole network."
        )
    return {
        "format": fmt,
        "count": len(result.segments),
        "segments": [
            {
                "segment_id": s.segment_id,
                "base_iri": s.base_iri,
                "base_rut": s.base_rut,
                "base_crack": s.base_crack,
                "annual_msa": s.annual_msa,
                "traffic_growth_rate": s.traffic_growth_rate,
                "monsoon_zone": s.monsoon_zone.value,
                "length_km": s.length_km,
                "deflection_mm": s.deflection_mm,
                "structural_number": s.structural_number,
            }
            for s in result.segments
        ],
        "errors": [{"row": r, "message": m} for r, m in result.errors],
    }


def ingest_multi(payload: dict) -> dict:
    """Ingest several uploaded files at once and merge their surveys by chainage.

    Request: {"files": [{"name": "...", "content_b64": "..."}], ...}. Separate
    rutting / roughness / cracking / pothole exports (or a multi-sheet workbook)
    merge into one fully-populated condition. Returns the merged segments plus a
    per-file/sheet status list.
    """
    from .ingest import ingest_multi_files
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("'files' must be a non-empty list.")
    if len(files) > 50:
        raise ValueError("too many files (max 50).")
    decoded = []
    for i, f in enumerate(files, start=1):
        if not isinstance(f, dict):
            raise ValueError(f"file #{i} must be an object.")
        name = str(f.get("name", f"file{i}"))
        b64 = f.get("content_b64")
        if not isinstance(b64, str) or not b64.strip():
            raise ValueError(f"file #{i} ({name}) is missing 'content_b64'.")
        try:
            decoded.append((name, base64.b64decode(b64, validate=True)))
        except (binascii.Error, ValueError):
            raise ValueError(f"file #{i} ({name}) is not valid base64.") from None
    result, infos = ingest_multi_files(decoded)
    out = _ingest_payload(result, "multi")
    out["files"] = [{"name": n, "status": s} for n, s in infos]
    return out


def ingest_file(path: str, fmt: str) -> dict:
    """Ingest an uploaded file already streamed to `path` (large-file path).

    Avoids base64/JSON buffering: the server streams the raw body to a temp file
    and calls this. CSV is row-streamed; XLSX/PDF are buffered up to the cap.
    """
    fmt = str(fmt).strip().lower()
    loaders = {
        "csv": ingest_segments_csv,
        "xlsx": ingest_segments_xlsx,
        "pdf": ingest_segments_pdf,
    }
    if fmt not in loaders:
        raise ValueError("'format' must be one of: csv, xlsx, pdf.")
    return _ingest_payload(loaders[fmt](path), fmt)


def traffic_msa(payload: dict) -> dict:
    """IRC:37 design + first-year MSA from CVPD and VDF (Indian overloading).

    Payload: cvpd, and either vdf or (terrain -> indicative VDF); plus optional
    growth, design_life_years, and carriageway (-> lane distribution factor).
    """
    cvpd = _f(payload, "cvpd", 0.0)
    terrain = str(payload.get("terrain", "plain"))
    vdf = payload.get("vdf")
    vdf = float(vdf) if vdf not in (None, "") else default_vdf(cvpd, terrain)
    carriageway = str(payload.get("carriageway", "two_lane"))
    result = design_msa(
        cvpd, vdf=vdf,
        growth_rate=_f(payload, "growth", 0.05),
        design_life_years=_i(payload, "design_life_years", 15),
        lane_distribution=lane_distribution_factor(carriageway),
    )
    out = result.as_dict()
    out["terrain"] = terrain
    out["carriageway"] = carriageway
    return out


def residual_life(payload: dict) -> dict:
    """IRC:81/IRC:37 remaining structural life (+ optional handback verdict)."""
    res = remaining_fatigue_life(
        deflection_mm=_f(payload, "deflection", 0.8),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        cumulative_msa=_f(payload, "cumulative_msa", 0.0),
        design_msa=(
            _f(payload, "design_msa", 0.0)
            if payload.get("design_msa") not in (None, "") else None
        ),
    )
    out = {"residual": res.as_dict()}
    req = payload.get("required_residual_msa")
    if req not in (None, ""):
        out["handback"] = handback_assessment(
            res, required_residual_msa=float(req)
        ).as_dict()
    return out


def _split_layers(bituminous_mm: float, granular_mm: float) -> dict:
    """Split bituminous/granular totals into BC/DBM and WMM/GSB for display."""
    bc = min(40.0, bituminous_mm)
    wmm = min(250.0, max(0.0, granular_mm - 150.0))
    return {
        "bc_mm": round(bc, 0), "dbm_mm": round(bituminous_mm - bc, 0),
        "bituminous_mm": round(bituminous_mm, 0),
        "wmm_mm": round(wmm, 0), "gsb_mm": round(granular_mm - wmm, 0),
        "granular_mm": round(granular_mm, 0),
    }


def pavement_design(payload: dict) -> dict:
    """IRC:37 flexible-pavement structural design from CBR + design traffic.

    Payload: `cbr`, and either `design_msa` directly or CVPD inputs
    (`cvpd`, `vdf`|`terrain`, `growth`, `design_life_years`, `carriageway`) to
    derive it via IRC:37; optional `reliability` (80|90) and
    `method`: "catalogue" (default) | "iitpave" (mechanistic layered-elastic).
    """
    cbr = _f(payload, "cbr", 8.0)
    design_life = _i(payload, "design_life_years", 15)
    d_msa = payload.get("design_msa")
    traffic = None
    if d_msa in (None, "", 0):
        cvpd = payload.get("cvpd")
        if cvpd in (None, ""):
            raise ValueError("provide 'design_msa', or 'cvpd' to derive it via IRC:37.")
        terrain = str(payload.get("terrain", "plain"))
        vdf = payload.get("vdf")
        vdf = float(vdf) if vdf not in (None, "") else default_vdf(float(cvpd), terrain)
        traffic = design_msa(
            float(cvpd), vdf=vdf, growth_rate=_f(payload, "growth", 0.05),
            design_life_years=design_life,
            lane_distribution=lane_distribution_factor(str(payload.get("carriageway", "two_lane"))),
        )
        d_msa = traffic.design_msa
    else:
        d_msa = float(d_msa)

    method = str(payload.get("method", "catalogue")).strip().lower()
    if method in ("iitpave", "mechanistic"):
        mech = design_pavement_mechanistic(
            cbr=cbr, design_msa=d_msa, design_life_years=design_life,
            e_bituminous_mpa=_f(payload, "e_bituminous", 3000.0),
        )
        out = mech.as_dict()
        out["method"] = "iitpave"
        out["layers"] = _split_layers(mech.bituminous_mm, mech.granular_mm)
    else:
        reliability = payload.get("reliability")
        reliability = int(reliability) if reliability not in (None, "") else None
        design = design_pavement(
            cbr=cbr, design_msa=d_msa, design_life_years=design_life, reliability=reliability,
        )
        out = design.as_dict()
        out["method"] = "catalogue"
    if traffic is not None:
        out["traffic"] = traffic.as_dict()
    return out


def iitpave_evaluate(payload: dict) -> dict:
    """Mechanistic (IITPAVE-style) assessment of an existing section from its
    layer moduli + thicknesses, e.g. FWD back-calculated 15th-percentile moduli.

    Payload: e_bituminous, e_granular, e_subgrade (MPa); h_bituminous, h_granular
    (mm); optional annual_msa, growth, cumulative_msa, design_msa.
    """
    standard = str(payload.get("standard", "irc37")).strip().lower()
    # IRC:115-2014 (FWD remaining-life) uses Poisson 0.5/0.4/0.4; IRC:37 uses 0.35.
    nu = (0.5, 0.4, 0.4) if standard == "irc115" else (0.35, 0.35, 0.35)
    layer = LayerModel(
        e_bituminous_mpa=_f(payload, "e_bituminous", 3000.0),
        e_granular_mpa=_f(payload, "e_granular", 250.0),
        e_subgrade_mpa=_f(payload, "e_subgrade", 70.0),
        h_bituminous_mm=_f(payload, "h_bituminous", 150.0),
        h_granular_mm=_f(payload, "h_granular", 450.0),
        nu_bituminous=nu[0], nu_granular=nu[1], nu_subgrade=nu[2],
    )
    design = payload.get("design_msa")
    design = float(design) if design not in (None, "", 0) else None
    assessment = evaluate_section(
        layer,
        annual_msa=_f(payload, "annual_msa", 0.0),
        traffic_growth_rate=_f(payload, "growth", 0.0),
        cumulative_msa=_f(payload, "cumulative_msa", 0.0),
        design_msa=design,
        standard=standard,
    )
    out = assessment.as_dict()
    out["layer"] = {
        "e_bituminous_mpa": layer.e_bituminous_mpa,
        "e_granular_mpa": layer.e_granular_mpa,
        "e_subgrade_mpa": layer.e_subgrade_mpa,
        "h_bituminous_mm": layer.h_bituminous_mm,
        "h_granular_mm": layer.h_granular_mm,
    }
    return out


def fwd_overlay(payload: dict) -> dict:
    """FWD remaining-life + overlay across homogeneous sub-sections (IRC:115-2014).

    Payload: `design_msa`, and `sections` -- a list of objects with
    section_id, e_bituminous, e_granular, e_subgrade, h_bituminous, h_granular
    (and optional chainage_from / chainage_to). Mirrors an FWD evaluation report
    (back-calculated 15th-percentile moduli -> remaining life -> overlay).
    """
    rows = payload.get("sections")
    if isinstance(rows, list) and rows:
        rows = [_apply_aliases({_norm_calib_key(k): v for k, v in r.items()}, _FWD_ALIASES)
                for r in rows if isinstance(r, dict)]
    else:
        rows = _fwd_rows(payload)  # csv text / xlsx / pdf upload
    design = _f(payload, "design_msa", 0.0)
    if design <= 0:
        raise ValueError("'design_msa' must be positive.")
    # A condition survey misuploaded here would have no moduli -- guide, don't choke.
    if not any(r.get("e_bituminous") not in (None, "") for r in rows) and _looks_like_survey(rows):
        raise ValueError(
            "This looks like a condition survey, not an FWD report. The overlay check "
            "needs back-calculated layer moduli (e_bituminous, e_granular, e_subgrade) "
            "and crust thickness from an FWD evaluation -- a condition survey has none. "
            "Use this file in the Segment Forecast / Sections tabs instead.")
    if len(rows) > MAX_NETWORK_SEGMENTS:
        raise ValueError(f"too many sections (max {MAX_NETWORK_SEGMENTS}).")

    sections, skipped = [], 0
    for i, row in enumerate(rows, start=1):
        try:
            sections.append(FWDSection(
                section_id=str(row.get("section_id") or f"Sec-{i}"),
                e_bituminous_mpa=_fnum(row, "e_bituminous"),  # primary; required
                e_granular_mpa=_fnum(row, "e_granular", _FWD_DEFAULTS["e_granular"]),
                e_subgrade_mpa=_fnum(row, "e_subgrade", _FWD_DEFAULTS["e_subgrade"]),
                h_bituminous_mm=_fnum(row, "h_bituminous", _FWD_DEFAULTS["h_bituminous"]),
                h_granular_mm=_fnum(row, "h_granular", _FWD_DEFAULTS["h_granular"]),
                chainage_from=(float(row["chainage_from"]) if row.get("chainage_from") not in (None, "") else None),
                chainage_to=(float(row["chainage_to"]) if row.get("chainage_to") not in (None, "") else None),
            ))
        except _MissingCol:
            skipped += 1
    if not sections:
        if _looks_like_survey(rows):
            raise ValueError(
                "This looks like a condition survey, not an FWD report. The overlay check "
                "needs back-calculated layer moduli (e_bituminous, e_granular, e_subgrade) "
                "and crust thickness from an FWD evaluation -- a condition survey has none. "
                "Use this file in the Segment Forecast / Sections tabs instead."
            )
        found = sorted({k for r in rows for k in r.keys() if k})
        raise ValueError(
            "No usable sections. Each needs e_bituminous (and optionally e_granular, "
            "e_subgrade, h_bituminous, h_granular -- these default if absent). Columns "
            "found: " + (", ".join(found[:40]) if found else "(none)") + ".")
    result = evaluate_fwd_sections(sections, design).as_dict()
    result["skipped"] = skipped
    return result


def _pbmc_params_from_payload(payload: dict) -> PBMCParams:
    return PBMCParams(
        term_years=_i(payload, "term_years", 5),
        performance_pci=_f(payload, "performance_pci", 3.0),
        routine_rate_per_km_year=_f(payload, "routine_rate_per_km_year", 1.5),
        base_unit_cost=_f(payload, "base_unit_cost", 30.0),
        escalation_rate=_f(payload, "escalation_rate", 0.05),
        contingency_pct=_f(payload, "contingency_pct", 0.10),
        overhead_pct=_f(payload, "overhead_pct", 0.10),
        discount_rate=_f(payload, "discount_rate", 0.08),
    )


def pbmc(payload: dict) -> dict:
    """Price a 5-7y Performance-Based Maintenance Contract.

    With a `segments` list -> a network aggregate; otherwise a single segment
    from the same condition fields as `/api/forecast`. Optional commercial
    fields: term_years, performance_pci, routine_rate_per_km_year,
    escalation_rate, contingency_pct, overhead_pct, discount_rate.
    """
    params = _pbmc_params_from_payload(payload)
    rut_model = RutModelType.from_str(str(payload.get("model", "default")))
    pavement = str(payload.get("pavement", "dense"))
    engine_kw = dict(
        rut_model=rut_model, hdm4_calibration=hdm4_preset(pavement),
        crack_model=CrackModelType.from_str(str(payload.get("crack_model", "default"))),
        roughness_model=RoughnessModelType.from_str(str(payload.get("roughness_model", "default"))),
    )
    if isinstance(payload.get("segments"), list):
        segments = _segments_from_payload(payload)
        return estimate_pbmc_network(segments, params, **engine_kw).as_dict()

    seg = SegmentInput(
        base_iri=_f(payload, "iri", 1.5),
        base_rut=_f(payload, "rut", 2.0),
        base_crack=_f(payload, "crack", 0.0),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=MonsoonZone.from_str(str(payload.get("zone", "HIGH"))),
        segment_id=str(payload.get("id", "SEGMENT")),
        length_km=_f(payload, "length_km", 1.0),
        deflection_mm=_f(payload, "deflection", 0.5),
        structural_number=_f(payload, "snp", 4.0),
    )
    return estimate_pbmc(seg, params, **engine_kw).as_dict()


def _norm_calib_key(key: str) -> str:
    """Lower-case, collapse whitespace/newlines to '_' (matches xlsx header norm)."""
    import re
    return re.sub(r"\s+", "_", str(key or "").strip().lower())


# Each calibration column and the alternative header names it may appear under,
# so every model "picks the related fields" regardless of the file's template.
# Exact (normalised) matches only -- never alias an absolute reading to an
# increment (e.g. rut_depth is NOT a rut increment), which would fit garbage.
_CALIB_ALIASES = {
    "measured_rut_increment_mm": [
        "rut_increment_mm", "rut_increment", "measured_rut_increment",
        "delta_rut_mm", "delta_rut", "rut_change_mm", "drut_mm"],
    "ye4": ["esa_msa", "cumulative_esa", "cumulative_msa", "esa", "ye4_msa"],
    "age": ["age_years", "pavement_age", "age_yr"],
    "structural_number": ["snp", "sn", "snc"],
    "deflection_mm": ["deflection", "d0_mm", "peak_deflection_mm"],
    "measured_iri_increment": [
        "iri_increment", "measured_iri_increment", "delta_iri", "iri_change", "diri"],
    "iri": ["roughness_iri", "iri_m_km"],
    "d_msa": ["delta_msa", "msa_year", "yearly_msa"],
    "d_crack_pct": ["delta_crack_pct", "crack_increment_pct"],
    "d_rut_mm": ["delta_rut_mm", "rut_increment_mm"],
    "crack_prev": ["crack_year1", "crack_t0", "crack_start", "crack_before", "crack_prev_pct"],
    "crack_next": ["crack_year2", "crack_t1", "crack_end", "crack_after", "crack_next_pct"],
    "measured_sfc_decrement": [
        "sfc_decrement", "delta_sfc", "sfc_change", "skid_decrement"],
    "sfc": ["skid", "side_force_coefficient", "sfc_value"],
    "measured_pothole_increment": [
        "pothole_increment", "delta_pothole", "pothole_change", "pothole_increment_pct"],
    "cracking_pct": ["crack_pct", "crack_area_pct", "percent_crack_area", "cracking_percent"],
}

# Markers that identify a single condition survey (vs a calibration observation file).
_SURVEY_MARKERS = (
    "rut_depth", "crack_area", "roughness", "lane_roughness", "_bi_",
    "pothole", "ravelling", "deflection", "chainage", "severity")


def _apply_aliases(row: dict, alias_map: dict) -> dict:
    """Map a row's columns onto canonical names via `alias_map` (exact normalised
    matches). Existing canonical keys win; aliases only fill what is absent."""
    out = dict(row)
    for canon, aliases in alias_map.items():
        if out.get(canon) not in (None, ""):
            continue
        for alt in aliases:
            if row.get(alt) not in (None, ""):
                out[canon] = row[alt]
                break
    return out


def _canonicalize_calib_row(row: dict) -> dict:
    return _apply_aliases(row, _CALIB_ALIASES)


def _looks_like_survey(rows: List[dict]) -> bool:
    """True if the columns look like a one-time condition survey (not paired/observation data)."""
    found = {k for r in rows for k in r.keys() if k}
    return any(any(m in c for m in _SURVEY_MARKERS) for c in found)


# FWD overlay columns and their alternative header names (any report template).
_FWD_ALIASES = {
    "section_id": ["section", "sec_id", "homogeneous_section", "sub_section"],
    "e_bituminous": ["e_bit", "e_bc", "e_bituminous_mpa", "modulus_bituminous",
                     "bituminous_modulus", "e1"],
    "e_granular": ["e_gran", "e_base", "e_granular_mpa", "granular_modulus", "e2"],
    "e_subgrade": ["e_sg", "e_subgrade_mpa", "subgrade_modulus", "mr_subgrade",
                   "m_r_subgrade", "e3"],
    "h_bituminous": ["h_bit", "h_bc", "thickness_bituminous", "bituminous_thickness_mm", "h1"],
    "h_granular": ["h_gran", "h_base", "thickness_granular", "granular_thickness_mm", "h2"],
    "chainage_from": ["from_chainage", "start_chainage", "chainage_start"],
    "chainage_to": ["to_chainage", "end_chainage", "chainage_end"],
}
# Defaults for FWD columns a report may omit (typical NH crust; editable per call).
_FWD_DEFAULTS = {"e_granular": 250.0, "e_subgrade": 50.0,
                 "h_bituminous": 150.0, "h_granular": 450.0}


def _dictrows_from_csv_text(text: str, target_col: Optional[str] = None) -> List[dict]:
    """DictReader CSV text. If `target_col` is given, skip preamble lines until a
    header row that actually contains it (handives PDF text / banner lines)."""
    import csv as _csv
    import io

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if target_col:
        for i, ln in enumerate(lines):
            cells = [_norm_calib_key(c) for c in ln.split(",")]
            if target_col in cells:
                lines = lines[i:]
                break
    rows = list(_csv.DictReader(io.StringIO("\n".join(lines))))
    return [{_norm_calib_key(k): v for k, v in r.items()} for r in rows]


def _file_rows(payload: dict, target_col: Optional[str] = None) -> List[dict]:
    """Tabular rows from `csv` text **or** an uploaded csv/xlsx/pdf (base64).

    Keys are normalised (lower-case, whitespace -> '_'). Reuses the same XLSX/PDF
    parsers as the network importer, so any report/workbook works. No domain
    aliasing here -- callers apply their own (calibration / FWD) alias map.
    """
    b64 = payload.get("content_b64")
    if isinstance(b64, str) and b64.strip():
        try:
            raw = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("content_b64 is not valid base64.") from None
        fmt = str(payload.get("format", "")).strip().lower()
        if fmt == "xlsx" or raw[:2] == b"PK":
            rows: List[dict] = []
            for _name, _hdr, sheet_rows in _xlsx_sheets(raw, 100_000):
                rows.extend(sheet_rows)  # keys already normalised by _xlsx_sheets
            if not rows:
                raise ValueError("no rows found in the XLSX.")
        elif fmt == "pdf" or raw[:5] == b"%PDF-":
            rows = _dictrows_from_csv_text(_extract_pdf_text(raw), target_col)
            if not rows:
                raise ValueError(
                    "no table found in the PDF text layer. The parser reads a "
                    "digitally-generated, comma-delimited table" +
                    (f" containing a '{target_col}' column" if target_col else "") +
                    "; a formatted/scanned report whose columns are aligned with "
                    "spaces will not parse -- paste the table as CSV, or upload a "
                    ".csv / .xlsx instead.")
        else:
            rows = _dictrows_from_csv_text(raw.decode("utf-8-sig", "replace"))
    else:
        text = payload.get("csv")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("provide observations as `csv` text or upload a .csv/.xlsx/.pdf file.")
        rows = _dictrows_from_csv_text(text)
    if not rows:
        raise ValueError("no data rows found.")
    if len(rows) > 100_000:
        raise ValueError("too many rows (max 100000).")
    return rows


def _calib_rows(payload: dict, target_col: Optional[str] = None) -> List[dict]:
    """Calibration observations (multi-format) with the calibration alias map applied."""
    return [_canonicalize_calib_row(r) for r in _file_rows(payload, target_col)]


def _fwd_rows(payload: dict) -> List[dict]:
    """FWD sub-section rows (multi-format) with the FWD alias map applied."""
    return [_apply_aliases(r, _FWD_ALIASES) for r in _file_rows(payload, "e_bituminous")]


# Sentinel: column is required (no default) for a calibration observation.
_REQUIRED = object()


class _MissingCol(Exception):
    def __init__(self, col: str):
        self.col = col
        super().__init__(col)


def _fnum(row: dict, key: str, default=_REQUIRED) -> float:
    """Float a cell; fall back to `default` for missing/blank/non-numeric.

    When no default is given the column is required -> raises `_MissingCol`, which
    the caller uses to skip just that row (not abort the whole file)."""
    v = row.get(key)
    if v is None or str(v).strip() == "":
        if default is _REQUIRED:
            raise _MissingCol(key)
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError):
        if default is _REQUIRED:
            raise _MissingCol(key)
        return float(default)


def _calib_build(rows: List[dict], required: List[str], build):
    """Build observations row-by-row, skipping rows missing a required column.

    Returns (observations, skipped_count). Raises a clear, actionable error
    naming the required columns and the columns actually found if nothing parses
    (e.g. the chosen model does not match the uploaded file)."""
    obs, skipped = [], 0
    for r in rows:
        try:
            obs.append(build(r))
        except _MissingCol:
            skipped += 1
    if not obs:
        if _looks_like_survey(rows):
            raise ValueError(
                "This looks like a single condition survey, not calibration data. "
                "Calibration fits a deterioration *rate*, so it needs repeat surveys of "
                "the same segment (year-on-year " + required[0] + "), or per-segment age + "
                "traffic. A one-time NSV snapshot only has current condition. "
                "Use this file in the Segment Forecast / Network & Budget / LCA tabs "
                "(it already feeds them); for calibration, upload before/after observations "
                "with column '" + required[0] + "'."
            )
        found = sorted({k for r in rows for k in r.keys() if k})
        raise ValueError(
            "No usable rows for this model. It needs column(s): "
            + ", ".join(required)
            + ". Columns found in the file: "
            + (", ".join(found[:40]) + (", …" if len(found) > 40 else ""))
            + ". Pick the model that matches your file, or add the missing column."
        )
    return obs, skipped


def _csv_rows(payload: dict) -> List[dict]:
    """Back-compat shim: calibration rows from the `csv` text field or a file."""
    return _calib_rows(payload)


def calibrate(payload: dict) -> dict:
    """Fit a deterioration model from field data.

    `kind`: "rut" (default) | "cracking" | "roughness". Data via `csv` text
    (header + rows) or, for rut, an `observations` list.
    """
    kind = str(payload.get("kind", "rut")).strip().lower()

    if kind == "rut":
        rows = payload.get("observations")
        if not isinstance(rows, list) or not rows:
            rows = _calib_rows(payload, "measured_rut_increment_mm")
        else:
            rows = [{_norm_calib_key(k): v for k, v in r.items()} for r in rows]
        obs, skipped = _calib_build(
            rows, ["measured_rut_increment_mm", "ye4", "age"],
            lambda r: RutObservation(
                measured_rut_increment_mm=_fnum(r, "measured_rut_increment_mm"),
                ye4=_fnum(r, "ye4"), age=int(_fnum(r, "age")),
                deflection_mm=_fnum(r, "deflection_mm", 0.5),
                structural_number=_fnum(r, "structural_number", 4.0),
                compaction_pct=_fnum(r, "compaction_pct", 98.0),
                cds=_fnum(r, "cds", 1.0),
                heavy_speed_kmh=_fnum(r, "heavy_speed_kmh", 50.0),
                surfacing_thickness_mm=_fnum(r, "surfacing_thickness_mm", 100.0),
            ))
        res = calibrate_hdm4_rut(obs)
        return {
            "kind": "rut",
            "k_rid": res.k_rid, "k_rst": res.k_rst, "k_rpd": res.k_rpd,
            "r_squared": res.r_squared, "rmse_before": res.rmse_before,
            "rmse_after": res.rmse_after, "n": res.n, "skipped": skipped,
            "fixed_to_zero": list(res.fixed_to_zero),
            "label": res.calibration.label, "summary": res.summary(),
        }

    if kind == "cracking":
        rows = _calib_rows(payload, "crack_next")
        obs, skipped = _calib_build(
            rows, ["crack_prev", "crack_next"],
            lambda r: (_fnum(r, "crack_prev"), _fnum(r, "crack_next")))
        res = calibrate_mlit_cracking(obs)
        return {
            "kind": "cracking", "a": res.a, "b": res.b,
            "r_squared": res.r_squared, "n": res.n, "skipped": skipped,
            "label": res.model.label, "summary": res.summary(),
        }

    if kind == "roughness":
        rows = _calib_rows(payload, "measured_iri_increment")
        obs, skipped = _calib_build(
            rows, ["measured_iri_increment", "iri", "age"],
            lambda r: RoughnessObservation(
                measured_iri_increment=_fnum(r, "measured_iri_increment"),
                iri=_fnum(r, "iri"), structural_number=_fnum(r, "structural_number", 4.0),
                age=int(_fnum(r, "age")), d_msa=_fnum(r, "d_msa", 1.0),
                d_crack_pct=_fnum(r, "d_crack_pct", 0.0), d_rut_mm=_fnum(r, "d_rut_mm", 0.0),
            ))
        res = calibrate_hdm4_roughness(obs)
        return {
            "kind": "roughness", "env_coeff": res.env_coeff, "struct_a0": res.struct_a0,
            "crack_coeff": res.crack_coeff, "rut_coeff": res.rut_coeff,
            "r_squared": res.r_squared, "rmse": res.rmse, "n": res.n, "skipped": skipped,
            "label": res.model.label, "summary": res.summary(),
        }

    if kind == "skid":
        rows = _calib_rows(payload, "measured_sfc_decrement")
        obs, skipped = _calib_build(
            rows, ["measured_sfc_decrement", "sfc"],
            lambda r: SkidObservation(
                measured_sfc_decrement=_fnum(r, "measured_sfc_decrement"),
                sfc=_fnum(r, "sfc"), d_msa=_fnum(r, "d_msa", 1.0),
            ))
        res = calibrate_hdm4_skid(obs)
        return {
            "kind": "skid", "decay_k": res.decay_k, "sfc_min": res.sfc_min,
            "r_squared": res.r_squared, "n": res.n, "skipped": skipped,
            "label": res.model.label, "summary": res.summary(),
        }

    if kind in ("pothole", "potholes"):
        rows = _calib_rows(payload, "measured_pothole_increment")
        obs, skipped = _calib_build(
            rows, ["measured_pothole_increment", "cracking_pct"],
            lambda r: PotholeObservation(
                measured_pothole_increment=_fnum(r, "measured_pothole_increment"),
                cracking_pct=_fnum(r, "cracking_pct"), d_msa=_fnum(r, "d_msa", 1.0),
            ))
        res = calibrate_hdm4_potholes(obs)
        return {
            "kind": "potholes", "rate": res.rate,
            "crack_threshold_pct": res.crack_threshold_pct,
            "r_squared": res.r_squared, "n": res.n, "skipped": skipped,
            "label": res.model.label, "summary": res.summary(),
        }

    raise ValueError("'kind' must be one of: rut, cracking, roughness, skid, potholes.")


def network_and_budget(payload: dict) -> dict:
    """Forecast a network and run the multi-year budget optimiser over it.

    Optional `model` ("default"|"hdm4") and `pavement` ("dense"|"porous") select
    the rut model for every segment; under HDM-4 each segment uses its own FWD
    deflection / structural number.
    """
    horizon = _i(payload, "years", 10)
    rut_model = RutModelType.from_str(str(payload.get("model", "default")))
    crack_model = CrackModelType.from_str(str(payload.get("crack_model", "default")))
    roughness_model = RoughnessModelType.from_str(str(payload.get("roughness_model", "default")))
    pavement = str(payload.get("pavement", "dense"))
    hdm4_cal = hdm4_preset(pavement)
    segments = _segments_from_payload(payload)
    forecasts = list(
        forecast_network(
            segments, horizon, _POLICY,
            rut_model=rut_model, hdm4_calibration=hdm4_cal,
            crack_model=crack_model, mlit_crack=mlit_crack_preset(pavement),
            roughness_model=roughness_model,
        )
    )

    params = BudgetParams(
        annual_budget=_f(payload, "annual_budget", 300.0),
        horizon_years=horizon,
        base_unit_cost=_f(payload, "base_unit_cost", 30.0),
    )
    budget = optimize_budget(segments, forecasts, params)
    clearing = recommend_budget_to_clear(segments, forecasts, params)

    # Per-segment remaining structural life + (optional) handback verdict.
    design_msa = payload.get("design_msa")
    design_msa = float(design_msa) if design_msa not in (None, "", 0) else None
    required = payload.get("required_residual_msa")
    required = float(required) if required not in (None, "") else None
    handback_counts = {"PASS": 0, "MARGINAL": 0, "FAIL": 0}

    rows = []
    for seg, fc in zip(segments, forecasts):
        res = remaining_fatigue_life(
            deflection_mm=seg.deflection_mm, annual_msa=seg.annual_msa,
            traffic_growth_rate=seg.traffic_growth_rate, cumulative_msa=0.0,
            design_msa=design_msa,
        )
        verdict = None
        if required is not None:
            hb = handback_assessment(res, required_residual_msa=required)
            verdict = hb.verdict.value
            handback_counts[verdict] += 1
        rows.append({
            "segment_id": fc.segment_id,
            "length_km": seg.length_km,
            "annual_msa": seg.annual_msa,
            "monsoon_zone": seg.monsoon_zone.value,
            "deflection_mm": seg.deflection_mm,
            "structural_number": seg.structural_number,
            "preventive_window_year": fc.plan.preventive_window_year,
            "window_expired_year": fc.plan.window_expired_year,
            "final_pci": fc.timeline[-1].irc82_pci,
            "residual_msa": round(res.governing_remaining_msa, 1),
            "residual_years": (round(res.residual_years, 1) if res.residual_years is not None else None),
            "residual_basis": res.governing_basis,
            "handback": verdict,
        })

    # Honest savings: only FUNDED segments avoid the structural premium;
    # unfunded segments still incur mill & overlay, so they save nothing.
    savings = budget.total_avoided_premium
    return {
        "model": {
            "rut_model": rut_model.value,
            "label": (
                hdm4_cal.label if rut_model is RutModelType.HDM4
                else "default IRC:82 power law"
            ),
        },
        "handback": (
            {"required_residual_msa": required, "counts": handback_counts,
             "failing": [r["segment_id"] for r in rows if r["handback"] == "FAIL"]}
            if required is not None else None
        ),
        "segments": rows,
        "budget": {
            "annual_budget": budget.annual_budget,
            "scheduled": [
                {
                    "segment_id": s.segment_id, "year": s.year,
                    "treatment": s.treatment, "cost": s.cost,
                    "avoided_premium": s.avoided_premium,
                }
                for s in budget.scheduled
            ],
            "unfunded": budget.unfunded,
            "spend_by_year": {str(k): v for k, v in budget.spend_by_year.items()},
            "total_spend": budget.total_spend,
            "total_avoided_premium": budget.total_avoided_premium,
            "do_nothing_structural_cost": budget.do_nothing_structural_cost,
            "net_savings": savings,
            "n_at_risk": clearing["n_at_risk"],
            "total_preventive_need": clearing["total_preventive_need"],
            "recommended_annual_budget": clearing["recommended_annual_budget"],
            "clears_at_current": clearing["clears_at_current"],
            "rationale": budget.rationale,
        },
    }
