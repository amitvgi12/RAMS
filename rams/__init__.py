"""
RAMS -- Indian Road Asset Management System: deterioration forecasting.

Public API:
    IndianPavementDeteriorationEngine -- single-segment year-by-year simulator
    forecast_segment                  -- validated functional wrapper
    SegmentInput / YearResult         -- typed I/O contracts
    MonsoonZone, Calibration, IRC82Scoring -- configuration
    MaintenancePolicy, TREATMENT_CATALOG   -- preventive-maintenance logic
"""
from __future__ import annotations

from .config import (
    DEFAULT_BOUNDS,
    DEFAULT_CALIBRATION,
    DEFAULT_SCORING,
    Calibration,
    InputBounds,
    IRC82Scoring,
    MonsoonZone,
)
from .engine import IndianPavementDeteriorationEngine, forecast_segment
from .lifecycle import (
    Intervention,
    ManagedLifecycle,
    simulate_managed_lifecycle,
    treatment_cost,
)
from .maintenance import (
    TREATMENT_CATALOG,
    MaintenanceFlag,
    MaintenancePolicy,
    Treatment,
    build_maintenance_plan,
)
from .models import SegmentInput, YearResult
from .optimize import BudgetParams, BudgetPlan, optimize_budget

__version__ = "1.0.0"

__all__ = [
    "IndianPavementDeteriorationEngine",
    "forecast_segment",
    "SegmentInput",
    "YearResult",
    "MonsoonZone",
    "Calibration",
    "IRC82Scoring",
    "InputBounds",
    "DEFAULT_CALIBRATION",
    "DEFAULT_SCORING",
    "DEFAULT_BOUNDS",
    "MaintenancePolicy",
    "MaintenanceFlag",
    "Treatment",
    "TREATMENT_CATALOG",
    "build_maintenance_plan",
    "simulate_managed_lifecycle",
    "ManagedLifecycle",
    "Intervention",
    "treatment_cost",
    "optimize_budget",
    "BudgetParams",
    "BudgetPlan",
    "__version__",
]
