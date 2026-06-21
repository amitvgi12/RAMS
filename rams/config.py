"""
Calibration constants, enumerations and validation bounds for the
Indian Pavement Deterioration Engine.

Architecture note (Solution Architect):
    Every magic number used by the deterioration laws lives here, not inside
    the engine. Field engineers recalibrate a road network by editing this
    file (or supplying a `Calibration` override) -- never by touching the
    simulation logic. This keeps the math auditable against IRC:82 / MoRTH
    and makes the engine deterministic and unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict


class RutModelType(str, Enum):
    """Which rutting law the engine uses to advance rut depth each year.

    DEFAULT : the IRC:82-style power law (rut_factor * MSA**exp * monsoon).
    HDM4    : the mechanistic HDM-4 delta-RDM model (densification + structural
              + plastic deformation), driven by FWD deflection / structural
              number. See rams/hdm4.py.
    """

    DEFAULT = "DEFAULT"
    HDM4 = "HDM4"

    @classmethod
    def from_str(cls, value: str) -> "RutModelType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown rut model {value!r}. Expected one of: {allowed}."
            ) from None


class CrackModelType(str, Enum):
    """Which cracking law the engine uses.

    DEFAULT : the IRC:82-style S-curve on cumulative MSA (unchanged).
    MLIT    : the paper's empirical year-on-year recursion C_{i+1}=a+b*C_i.
    """

    DEFAULT = "DEFAULT"
    MLIT = "MLIT"

    @classmethod
    def from_str(cls, value: str) -> "CrackModelType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown crack model {value!r}. Expected one of: {allowed}.") from None


class RoughnessModelType(str, Enum):
    """Which roughness (IRI) law the engine uses.

    DEFAULT : the IRI structural+traffic law (unchanged).
    HDM4    : the HDM-4 incremental roughness model, coupled to the year's
              cracking and rutting increments + a structural + environmental term.
    """

    DEFAULT = "DEFAULT"
    HDM4 = "HDM4"

    @classmethod
    def from_str(cls, value: str) -> "RoughnessModelType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown roughness model {value!r}. Expected one of: {allowed}.") from None


class SkidModelType(str, Enum):
    """Whether/how the engine models skid resistance (side-force coefficient).

    NONE : skid is not tracked (the engine's historical behaviour).
    HDM4 : the HDM-4-style aggregate-polishing decay of SFC toward a terminal
           value (skid *decreases* with traffic, unlike the other distresses).
    """

    NONE = "NONE"
    HDM4 = "HDM4"

    @classmethod
    def from_str(cls, value: str) -> "SkidModelType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown skid model {value!r}. Expected one of: {allowed}.") from None


class PotholeModelType(str, Enum):
    """Whether/how the engine models potholing (area %).

    NONE : potholes are not tracked (the engine's historical behaviour).
    HDM4 : the HDM-4-style potholing model -- potholes initiate once cracking
           passes a threshold, then progress with traffic (wet-climate weighted).
    """

    NONE = "NONE"
    HDM4 = "HDM4"

    @classmethod
    def from_str(cls, value: str) -> "PotholeModelType":
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown pothole model {value!r}. Expected one of: {allowed}.") from None


class MonsoonZone(str, Enum):
    """Environmental exposure class driving subgrade-softening penalty."""

    HIGH = "HIGH"      # Kerala, Northeast, Coastal Maharashtra
    MEDIUM = "MEDIUM"  # Indo-Gangetic plains, Karnataka
    LOW = "LOW"        # Western Rajasthan desert zones

    @classmethod
    def from_str(cls, value: str) -> "MonsoonZone":
        """Strict parse. Raises ValueError on anything unexpected.

        Security/QA note: the original prototype used dict.get(zone, MEDIUM),
        silently mapping typos (e.g. 'HGIH') to MEDIUM. Silent fallback hides
        data-entry errors in a system that schedules public spending, so we
        fail loud instead.
        """
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            allowed = ", ".join(z.value for z in cls)
            raise ValueError(
                f"Unknown monsoon_zone {value!r}. Expected one of: {allowed}."
            ) from None


# --- Deterioration-law calibration -----------------------------------------

@dataclass(frozen=True)
class Calibration:
    """Immutable bundle of all tunable model coefficients.

    Defaults reproduce the documented National-Highway model. Override any
    field to recalibrate for State Highways, Major District Roads, etc.
    """

    # Environmental penalty (subgrade softening) per monsoon zone.
    monsoon_multipliers: Dict[MonsoonZone, float] = field(
        default_factory=lambda: {
            MonsoonZone.HIGH: 1.45,
            MonsoonZone.MEDIUM: 1.15,
            MonsoonZone.LOW: 1.00,
        }
    )

    # IRI progression: delta = iri_struct*IRI + iri_traffic*MSA*monsoon
    iri_structural_factor: float = 0.04
    iri_traffic_factor: float = 0.015
    iri_cap: float = 12.0          # impassable road

    # Rutting progression: delta = rut_factor * MSA**rut_exponent * monsoon
    rut_factor: float = 0.35
    rut_exponent: float = 0.7
    rut_cap: float = 35.0          # mm

    # Cracking progression (S-curve via power of cumulative MSA after lag).
    crack_lag_years: int = 3       # binder oxidation lag phase
    crack_factor: float = 1.2
    crack_exponent: float = 0.6
    crack_lag_factor: float = 0.1  # gentle pre-lag growth on annual MSA
    crack_cap: float = 100.0       # percent

    def monsoon_multiplier(self, zone: MonsoonZone) -> float:
        return self.monsoon_multipliers[zone]


# --- IRC:82 composite Pavement Condition scoring ---------------------------

@dataclass(frozen=True)
class IRC82Scoring:
    """Deduct-value thresholds and weights for the 0-4 composite PCI."""

    score_max: float = 4.0
    score_min: float = 1.0

    iri_free_threshold: float = 2.0   # IRI below this = perfect
    iri_deduct_rate: float = 0.6

    rut_free_threshold: float = 5.0   # mm below this = perfect
    rut_deduct_rate: float = 0.25

    crack_free_threshold: float = 5.0  # % below this = perfect
    crack_deduct_rate: float = 0.15

    weight_iri: float = 0.40
    weight_rut: float = 0.35
    weight_crack: float = 0.25

    def __post_init__(self) -> None:
        total = self.weight_iri + self.weight_rut + self.weight_crack
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"IRC:82 weights must sum to 1.0 (got {total}).")


# --- Input validation bounds (Security Lead) -------------------------------
# Hard physical/operational guard-rails. Reject inputs outside these ranges
# before they ever reach the math, to prevent NaN/inf propagation, absurd
# resource use, or silently meaningless forecasts.

@dataclass(frozen=True)
class InputBounds:
    iri_min: float = 0.0
    iri_max: float = 20.0      # already beyond the impassable cap
    rut_min: float = 0.0
    rut_max: float = 60.0
    crack_min: float = 0.0
    crack_max: float = 100.0
    msa_min: float = 0.0
    msa_max: float = 1000.0    # extreme but finite ceiling
    growth_min: float = -0.5   # allow modest negative (traffic decline)
    growth_max: float = 1.0    # 100%/yr is already implausible
    length_min: float = 0.01   # a segment must have positive length (km)
    length_max: float = 100.0  # homogeneous segments are short by definition
    horizon_min: int = 1
    horizon_max: int = 100     # DoS guard on the simulation loop

    # --- HDM-4 structural inputs (FWD / pavement composition) --------------
    # Optional fields, only consumed by the HDM-4 rut model. Defaults are sane
    # for a typical Indian flexible NH so HDM-4 runs without FWD data, but real
    # Benkelman/FWD deflection and structural number should be supplied.
    deflection_min: float = 0.0    # mm (Benkelman/FWD rebound)
    deflection_max: float = 5.0
    snp_min: float = 0.5           # adjusted structural number
    snp_max: float = 12.0
    compaction_min: float = 80.0   # % relative compaction
    compaction_max: float = 105.0
    surfacing_min: float = 0.0     # mm total bituminous thickness
    surfacing_max: float = 600.0
    cds_min: float = 0.5           # construction-defects indicator
    cds_max: float = 1.5
    speed_min: float = 5.0         # km/h heavy-vehicle speed
    speed_max: float = 120.0
    skid_min: float = 0.0          # side-force coefficient (SFC, fraction)
    skid_max: float = 1.0
    potholes_min: float = 0.0      # potholing area (%)
    potholes_max: float = 100.0


DEFAULT_CALIBRATION = Calibration()
DEFAULT_SCORING = IRC82Scoring()
DEFAULT_BOUNDS = InputBounds()
