"""
Alternative, selectable cracking and roughness models -- putting cracking and
roughness on the same calibratable footing as the rutting model.

The calibration paper (Taniguchi & Yoshida) explicitly flags this as the next
step: "this study only calibrated the rutting model ... it will be necessary to
calibrate the models for cracking, roughness and skid resistance".

Cracking -- MLIT-PMS empirical recursion (the paper's own model):

    C_{i+1} = a + b * C_i          (i = years in service)

    Dense-graded AC : a=0.40, b=1.16   (paper)
    Porous AC       : a=0.40, b=1.10   (paper)

Roughness -- HDM-4 incremental model, coupled to the *actual* distress
progression (so roughness is driven by the rut/crack the other models predict):

    dIRI = env*IRI + struct + Kc*dCRACK + Kr*dRUT

    env    : environmental coefficient * current IRI
    struct : a0 * e^(m*age) * (1+SNP)^p * dMSA   (HDM-4 structural deformation)
    Kc     : roughness gain per % cracking increment (HDM-4 ~0.0066 / ACRA)
    Kr     : roughness gain per mm rut increment    (HDM-4 ~0.088 / RDS)

These coefficients are HDM-4-form defaults tuned to realistic Indian-flexible
magnitudes; like every coefficient in RAMS they are calibration data (see
rams.calibrate for the cracking/roughness harnesses), not gospel. dRUT is used
as a proxy for the HDM-4 rut-depth standard-deviation change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# --- MLIT cracking recursion -----------------------------------------------

@dataclass(frozen=True)
class MLITCrackModel:
    """Empirical cracking recursion C_next = a + b * C_prev (paper)."""

    a: float
    b: float
    label: str = "MLIT cracking recursion"

    def next_crack(self, crack_prev: float) -> float:
        return self.a + self.b * max(0.0, crack_prev)


MLIT_CRACK_DENSE = MLITCrackModel(0.40, 1.16, "MLIT dense-graded AC (C+1=0.40+1.16C)")
MLIT_CRACK_POROUS = MLITCrackModel(0.40, 1.10, "MLIT porous AC (C+1=0.40+1.10C)")
DEFAULT_MLIT_CRACK = MLIT_CRACK_DENSE

_CRACK_PRESETS = {"dense": MLIT_CRACK_DENSE, "porous": MLIT_CRACK_POROUS}


def mlit_crack_preset(name: str) -> MLITCrackModel:
    key = str(name).strip().lower()
    if key not in _CRACK_PRESETS:
        raise ValueError(f"unknown MLIT crack preset {name!r}; expected: {', '.join(_CRACK_PRESETS)}.")
    return _CRACK_PRESETS[key]


# --- HDM-4 incremental roughness -------------------------------------------

@dataclass(frozen=True)
class HDM4RoughnessModel:
    """HDM-4 incremental roughness (IRI), coupled to crack/rut increments."""

    env_coeff: float = 0.023        # environmental: env_coeff * IRI
    struct_a0: float = 134.0        # HDM-4 structural roughness coefficient
    struct_age_m: float = 0.023     # age exponent in the structural term
    struct_snp_pow: float = -5.0    # (1+SNP) power -- stronger pavement, less roughness
    crack_coeff: float = 0.0066     # HDM-4: dIRI per unit cracking-area increment
    rut_coeff: float = 0.045        # dIRI per mm rut increment (proxy for d-RDS)
    label: str = "HDM-4 incremental roughness"

    def increment(
        self, *, iri: float, snp: float, age: int, d_msa: float,
        d_crack_pct: float, d_rut_mm: float,
    ) -> float:
        env = self.env_coeff * max(0.0, iri)
        struct = (
            self.struct_a0
            * math.exp(self.struct_age_m * age)
            * math.pow(1.0 + max(0.0, snp), self.struct_snp_pow)
            * max(0.0, d_msa)
        )
        crack = self.crack_coeff * max(0.0, d_crack_pct)
        rut = self.rut_coeff * max(0.0, d_rut_mm)
        return env + struct + crack + rut


DEFAULT_HDM4_ROUGHNESS = HDM4RoughnessModel()


# --- HDM-4 skid resistance (aggregate polishing) ---------------------------

@dataclass(frozen=True)
class HDM4SkidModel:
    """Skid resistance (side-force coefficient) decay from aggregate polishing.

    Unlike the other distresses, skid resistance *decreases* with traffic and
    relaxes toward a terminal polished value `sfc_min`:

        dSFC = -decay_k * (SFC - sfc_min) * dMSA      (SFC -> sfc_min)

    `decay_k` is the polishing rate (per SFC-unit per MSA) and is the parameter
    the calibration harness recovers; `sfc_min` is the terminal polished SFC.
    """

    sfc_min: float = 0.30
    decay_k: float = 0.012
    label: str = "HDM-4 skid (aggregate polishing)"

    def increment(self, sfc: float, d_msa: float) -> float:
        """Annual change in SFC (<= 0). Caller clamps at sfc_min."""
        return -self.decay_k * max(0.0, sfc - self.sfc_min) * max(0.0, d_msa)


DEFAULT_HDM4_SKID = HDM4SkidModel()


# --- HDM-4 potholing -------------------------------------------------------

@dataclass(frozen=True)
class HDM4PotholeModel:
    """Potholing (area %) -- initiates from cracking, then progresses with traffic.

    Potholes form where the surface has already cracked/ravelled, so potholing
    starts only once cracking passes `crack_threshold_pct`, then grows with the
    cracked-area excess and traffic, accelerated in wet climates:

        dAPOT = rate * (max(0, CRACK - threshold)/100) * dMSA * water_factor

    `rate` is what the calibration harness recovers; `water_factor >= 1` weights
    monsoon-prone segments. Bounded by `cap_pct`.
    """

    crack_threshold_pct: float = 20.0
    rate: float = 0.6
    water_factor: float = 1.0
    cap_pct: float = 50.0
    label: str = "HDM-4 potholing (crack-initiated)"

    def increment(self, crack_pct: float, d_msa: float) -> float:
        excess = max(0.0, crack_pct - self.crack_threshold_pct) / 100.0
        return self.rate * excess * max(0.0, d_msa) * self.water_factor


DEFAULT_HDM4_POTHOLE = HDM4PotholeModel()
