"""
Multi-Year Budget Optimization.

Allocates a constrained annual maintenance budget across competing segments to
maximise network value, exploiting the preventive "window of maximum return"
that the deterioration engine projects for each segment.

Decision model (deterministic, explainable -- appropriate for public spend):
  * Each segment has a preventive window [window_start, deadline], where
    deadline = window_expired_year - 1 (the last year a cheap microsurface
    still applies). Funding microsurfacing inside the window avoids the ~5x
    structural mill & overlay later -- that cost difference is the realised
    *benefit* (avoided premium).
  * Because every preventive treatment shares the same benefit/cost ratio,
    the scarce-budget decision is *which* segments to protect first. We rank
    by traffic exposure (annual_msa x length_km): protect the busiest
    corridors first, then break ties by the tightest deadline.
  * Greedy allocation: walk segments in priority order; schedule each in the
    earliest year within its window that still has budget. Segments that cannot
    be funded in-window are reported as `unfunded` (they will require structural
    treatment) so planners see exactly what a budget shortfall costs.

This is a transparent greedy heuristic, not an ILP optimum; the trade-off is
auditability. The hooks (per-year schedule, benefit, deadlines) are sufficient
to swap in an ILP solver later without changing the API.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .batch import SegmentForecast
from .lifecycle import treatment_cost
from .maintenance import TREATMENT_CATALOG
from .models import SegmentInput


@dataclass
class BudgetParams:
    annual_budget: float          # cost units available per year (e.g. Rs lakh)
    horizon_years: int = 10
    base_unit_cost: float = 30.0  # cost of 1.0 relative-cost-unit per km

    def __post_init__(self) -> None:
        if self.annual_budget < 0:
            raise ValueError("annual_budget must be non-negative.")
        if not (1 <= self.horizon_years <= 100):
            raise ValueError("horizon_years out of range [1, 100].")
        if self.base_unit_cost <= 0:
            raise ValueError("base_unit_cost must be positive.")


@dataclass
class ScheduledTreatment:
    segment_id: str
    year: int
    treatment: str
    cost: float
    avoided_premium: float  # benefit realised by acting in the window


@dataclass
class BudgetPlan:
    scheduled: List[ScheduledTreatment]
    unfunded: List[str]                 # segment ids that missed their window
    spend_by_year: Dict[int, float]
    total_spend: float
    total_avoided_premium: float        # realised benefit
    do_nothing_structural_cost: float   # what failing to act would cost
    annual_budget: float
    rationale: str = ""


@dataclass
class _Candidate:
    segment_id: str
    window_start: int
    deadline: int               # last year a preventive treatment still helps
    micro_cost: float
    mill_cost: float
    exposure: float             # annual_msa x length_km (priority key)

    @property
    def avoided_premium(self) -> float:
        return round(self.mill_cost - self.micro_cost, 2)


def _build_candidate(
    seg: SegmentInput, forecast: SegmentForecast, params: BudgetParams
) -> Optional[_Candidate]:
    plan = forecast.plan
    if plan.preventive_window_year is None:
        return None  # never needs preventive within horizon
    deadline = (
        plan.window_expired_year - 1
        if plan.window_expired_year is not None
        else params.horizon_years
    )
    if deadline < plan.preventive_window_year:
        deadline = plan.preventive_window_year  # degenerate but keep it actionable
    micro = treatment_cost(TREATMENT_CATALOG["MICROSURFACING"], seg.length_km, params.base_unit_cost)
    mill = treatment_cost(TREATMENT_CATALOG["MILL_AND_OVERLAY"], seg.length_km, params.base_unit_cost)
    return _Candidate(
        segment_id=seg.segment_id,
        window_start=plan.preventive_window_year,
        deadline=min(deadline, params.horizon_years),
        micro_cost=micro,
        mill_cost=mill,
        exposure=round(seg.annual_msa * seg.length_km, 2),
    )


def optimize_budget(
    segments: List[SegmentInput],
    forecasts: List[SegmentForecast],
    params: BudgetParams,
) -> BudgetPlan:
    """Greedy multi-year allocation. `segments` and `forecasts` align by index."""
    by_id = {s.segment_id: s for s in segments}
    candidates: List[_Candidate] = []
    do_nothing_cost = 0.0
    for fc in forecasts:
        seg = by_id.get(fc.segment_id)
        if seg is None:
            continue
        cand = _build_candidate(seg, fc, params)
        if cand is not None:
            candidates.append(cand)
            do_nothing_cost += cand.mill_cost  # untreated -> structural later

    # Rank: busiest corridors first, then tightest deadline, then cheapest.
    candidates.sort(key=lambda c: (-c.exposure, c.deadline, c.micro_cost))

    remaining = {y: params.annual_budget for y in range(1, params.horizon_years + 1)}
    scheduled: List[ScheduledTreatment] = []
    unfunded: List[str] = []
    total_spend = 0.0
    total_benefit = 0.0

    for c in candidates:
        placed = False
        for year in range(c.window_start, c.deadline + 1):
            if remaining.get(year, 0.0) >= c.micro_cost:
                remaining[year] -= c.micro_cost
                total_spend += c.micro_cost
                total_benefit += c.avoided_premium
                scheduled.append(
                    ScheduledTreatment(
                        segment_id=c.segment_id, year=year,
                        treatment=TREATMENT_CATALOG["MICROSURFACING"].name,
                        cost=c.micro_cost, avoided_premium=c.avoided_premium,
                    )
                )
                placed = True
                break
        if not placed:
            unfunded.append(c.segment_id)

    spend_by_year = {
        y: round(params.annual_budget - remaining[y], 2)
        for y in range(1, params.horizon_years + 1)
    }
    scheduled.sort(key=lambda s: (s.year, s.segment_id))

    funded = len(scheduled)
    rationale = (
        f"Funded preventive microsurfacing on {funded} of "
        f"{len(candidates)} at-risk segment(s) within budget, avoiding "
        f"{round(total_benefit, 2)} cost-units of future structural work. "
        f"{len(unfunded)} segment(s) could not be funded in-window and will "
        f"require structural mill & overlay."
    )

    return BudgetPlan(
        scheduled=scheduled,
        unfunded=unfunded,
        spend_by_year=spend_by_year,
        total_spend=round(total_spend, 2),
        total_avoided_premium=round(total_benefit, 2),
        do_nothing_structural_cost=round(do_nothing_cost, 2),
        annual_budget=params.annual_budget,
        rationale=rationale,
    )


def recommend_budget_to_clear(
    segments: List[SegmentInput],
    forecasts: List[SegmentForecast],
    params: BudgetParams,
    iters: int = 24,
) -> Dict[str, object]:
    """Smallest flat annual budget that funds every at-risk segment in-window.

    "Unfunded" segments are simply those the annual budget could not cover before
    their preventive window closed -- a budget-vs-need gap, not an error. This
    answers the natural follow-up: *what budget makes the unfunded list empty?*

    Re-uses the supplied forecasts (the expensive step); only the cheap greedy
    allocation is re-run, so the binary search is inexpensive even for large
    networks. Returns the total preventive need, the number of at-risk segments,
    whether the current budget already clears it, and the recommended budget.
    """
    # Total preventive need = everything funded under an unconstrained budget.
    full = optimize_budget(segments, forecasts, replace(params, annual_budget=1.0e15))
    total_need = full.total_spend
    n_at_risk = len(full.scheduled)

    base = optimize_budget(segments, forecasts, params)
    if not base.unfunded:
        return {
            "total_preventive_need": round(total_need, 2),
            "n_at_risk": n_at_risk,
            "recommended_annual_budget": round(params.annual_budget, 2),
            "clears_at_current": True,
        }

    lo, hi = params.annual_budget, max(total_need, params.annual_budget)
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if optimize_budget(segments, forecasts, replace(params, annual_budget=mid)).unfunded:
            lo = mid
        else:
            hi = mid
    return {
        "total_preventive_need": round(total_need, 2),
        "n_at_risk": n_at_risk,
        "recommended_annual_budget": round(hi, 2),
        "clears_at_current": False,
    }
