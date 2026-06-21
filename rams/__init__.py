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
    RutModelType,
)
from .calibrate import (
    CalibrationResult,
    CrackCalibrationResult,
    PotholeCalibrationResult,
    PotholeObservation,
    RoughnessCalibrationResult,
    RoughnessObservation,
    RutObservation,
    SkidCalibrationResult,
    SkidObservation,
    calibrate_hdm4_potholes,
    calibrate_hdm4_roughness,
    calibrate_hdm4_rut,
    calibrate_hdm4_skid,
    calibrate_mlit_cracking,
    cracking_pairs_from_series,
    load_observations_csv,
    observations_from_rut_series,
)
from .config import (
    CrackModelType,
    PotholeModelType,
    RoughnessModelType,
    SkidModelType,
)
from .distress import (
    DEFAULT_HDM4_POTHOLE,
    DEFAULT_HDM4_ROUGHNESS,
    DEFAULT_HDM4_SKID,
    DEFAULT_MLIT_CRACK,
    MLIT_CRACK_DENSE,
    MLIT_CRACK_POROUS,
    HDM4PotholeModel,
    HDM4RoughnessModel,
    HDM4SkidModel,
    MLITCrackModel,
)
from .engine import IndianPavementDeteriorationEngine, forecast_segment
from .fwd import DEFAULT_DEFLECTION_TO_SNP, DeflectionToSNP, snp_from_deflection
from .hdm4 import (
    DEFAULT_HDM4,
    HDM4_DENSE_GRADED,
    HDM4_POROUS,
    HDM4RutCalibration,
    annual_rut_increment,
)
from .ingest import (
    ingest_segments,
    ingest_segments_csv,
    ingest_segments_pdf,
    ingest_segments_xml,
)
from .lifecycle import (
    Intervention,
    ManagedLifecycle,
    simulate_managed_lifecycle,
    treatment_cost,
)
from .mci import RUT_OVERLAY_THRESHOLD_MM, MCIBand, compute_mci, mci_band
from .residual import (
    DEFAULT_DEFLECTION_LIFE,
    DeflectionLifeModel,
    HandbackVerdict,
    ResidualLife,
    handback_assessment,
    remaining_fatigue_life,
)
from .traffic import (
    LANE_DISTRIBUTION,
    TrafficLoading,
    default_vdf,
    design_msa,
    lane_distribution_factor,
)
from .triggers import (
    DEFAULT_TRIGGERS,
    InterventionTriggers,
    TriggerSeverity,
    evaluate_triggers,
    msa_category,
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
from .design import (
    DEFAULT_CATALOGUE,
    DEFAULT_PERFORMANCE,
    DesignCatalogue,
    PavementDesign,
    PerformanceModel,
    design_pavement,
    fatigue_life_msa,
    granular_modulus_mpa,
    rutting_life_msa,
    subgrade_modulus_mpa,
)
from .pbmc import (
    DEFAULT_MONSOON_ROUTINE_FACTOR,
    PBMCEstimate,
    PBMCNetworkEstimate,
    PBMCParams,
    PBMCYear,
    estimate_pbmc,
    estimate_pbmc_network,
)

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
    "ingest_segments",
    "ingest_segments_csv",
    "ingest_segments_xml",
    "ingest_segments_pdf",
    "compute_mci",
    "mci_band",
    "MCIBand",
    "RUT_OVERLAY_THRESHOLD_MM",
    "RutModelType",
    "CrackModelType",
    "RoughnessModelType",
    "SkidModelType",
    "PotholeModelType",
    "HDM4RutCalibration",
    "HDM4_DENSE_GRADED",
    "HDM4_POROUS",
    "DEFAULT_HDM4",
    "annual_rut_increment",
    "MLITCrackModel",
    "MLIT_CRACK_DENSE",
    "MLIT_CRACK_POROUS",
    "DEFAULT_MLIT_CRACK",
    "HDM4RoughnessModel",
    "DEFAULT_HDM4_ROUGHNESS",
    "calibrate_mlit_cracking",
    "calibrate_hdm4_roughness",
    "CrackCalibrationResult",
    "RoughnessCalibrationResult",
    "RoughnessObservation",
    "cracking_pairs_from_series",
    "evaluate_triggers",
    "InterventionTriggers",
    "TriggerSeverity",
    "DEFAULT_TRIGGERS",
    "msa_category",
    "design_msa",
    "default_vdf",
    "lane_distribution_factor",
    "TrafficLoading",
    "LANE_DISTRIBUTION",
    "calibrate_hdm4_rut",
    "RutObservation",
    "CalibrationResult",
    "load_observations_csv",
    "observations_from_rut_series",
    "remaining_fatigue_life",
    "handback_assessment",
    "ResidualLife",
    "DeflectionLifeModel",
    "HandbackVerdict",
    "DEFAULT_DEFLECTION_LIFE",
    "snp_from_deflection",
    "DeflectionToSNP",
    "DEFAULT_DEFLECTION_TO_SNP",
    "build_maintenance_plan",
    "simulate_managed_lifecycle",
    "ManagedLifecycle",
    "Intervention",
    "treatment_cost",
    "optimize_budget",
    "BudgetParams",
    "BudgetPlan",
    "design_pavement",
    "PavementDesign",
    "DesignCatalogue",
    "DEFAULT_CATALOGUE",
    "PerformanceModel",
    "DEFAULT_PERFORMANCE",
    "subgrade_modulus_mpa",
    "granular_modulus_mpa",
    "fatigue_life_msa",
    "rutting_life_msa",
    "estimate_pbmc",
    "estimate_pbmc_network",
    "PBMCParams",
    "PBMCEstimate",
    "PBMCNetworkEstimate",
    "PBMCYear",
    "DEFAULT_MONSOON_ROUTINE_FACTOR",
    "__version__",
]
