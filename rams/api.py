"""
Pure request/response functions powering the web dashboard.

Kept free of any HTTP/socket concerns so they are unit-testable and reusable
(CLI, server, notebooks). Each function takes a plain dict (already parsed
from JSON) and returns a JSON-serialisable dict. All raise ValueError on bad
input, which the server maps to HTTP 400.
"""
from __future__ import annotations

from typing import Dict, List

from .batch import forecast_network
from .config import MonsoonZone
from .engine import IndianPavementDeteriorationEngine
from .lifecycle import simulate_managed_lifecycle
from .maintenance import MaintenancePolicy, annotate_timeline, build_maintenance_plan
from .models import SegmentInput
from .optimize import BudgetParams, optimize_budget

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
    """Untreated forecast + treated (managed) trajectory + maintenance plan."""
    horizon = _i(payload, "years", 10)
    engine = IndianPavementDeteriorationEngine(
        base_iri=_f(payload, "iri", 1.5),
        base_rut=_f(payload, "rut", 2.0),
        base_crack=_f(payload, "crack", 0.0),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=str(payload.get("zone", "HIGH")),
    )
    timeline = engine.run_lifecycle_forecast(horizon)
    plan = build_maintenance_plan(timeline, _POLICY)
    annotate_timeline(timeline, _POLICY)

    # Treated trajectory for the comparison chart.
    seg = SegmentInput(
        base_iri=_f(payload, "iri", 1.5),
        base_rut=_f(payload, "rut", 2.0),
        base_crack=_f(payload, "crack", 0.0),
        annual_msa=_f(payload, "msa", 4.5),
        traffic_growth_rate=_f(payload, "growth", 0.06),
        monsoon_zone=MonsoonZone.from_str(str(payload.get("zone", "HIGH"))),
        segment_id=str(payload.get("id", "SEGMENT")),
        length_km=_f(payload, "length_km", 1.0),
    )
    managed = simulate_managed_lifecycle(seg, horizon, policy=_POLICY)

    return {
        "bands": bands(),
        "untreated": [yr.as_row() for yr in timeline],
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
            ).validate()
        )
    return segments


def network_and_budget(payload: dict) -> dict:
    """Forecast a network and run the multi-year budget optimiser over it."""
    horizon = _i(payload, "years", 10)
    segments = _segments_from_payload(payload)
    forecasts = list(forecast_network(segments, horizon, _POLICY))

    params = BudgetParams(
        annual_budget=_f(payload, "annual_budget", 300.0),
        horizon_years=horizon,
        base_unit_cost=_f(payload, "base_unit_cost", 30.0),
    )
    budget = optimize_budget(segments, forecasts, params)

    rows = []
    for seg, fc in zip(segments, forecasts):
        rows.append({
            "segment_id": fc.segment_id,
            "length_km": seg.length_km,
            "annual_msa": seg.annual_msa,
            "monsoon_zone": seg.monsoon_zone.value,
            "preventive_window_year": fc.plan.preventive_window_year,
            "window_expired_year": fc.plan.window_expired_year,
            "final_pci": fc.timeline[-1].irc82_pci,
        })

    # Honest savings: only FUNDED segments avoid the structural premium;
    # unfunded segments still incur mill & overlay, so they save nothing.
    savings = budget.total_avoided_premium
    return {
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
            "rationale": budget.rationale,
        },
    }
