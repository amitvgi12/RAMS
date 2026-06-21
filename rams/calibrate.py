"""
HDM-4 rut-model calibration harness.

Reproduces the method in Taniguchi & Yoshida (PWRI): the HDM-4 rut increment

    delta-RDM = K_rid*RDO + K_rst*RDST + K_rpd*RDPD

is *linear in the three calibration factors* once the components RDO/RDST/RDPD
are evaluated from the known inputs (YE4, DEF, SNP, COMP, CDS, Sh, HS). So given
field-measured annual rut increments we recover [K_rid, K_rst, K_rpd] by ordinary
least-squares multiple regression -- exactly what the paper did.

The paper also hit the case where the unconstrained fit gave a *negative* plastic
factor (K_rpd < 0), which is physically impossible (rut shrinking with traffic);
they refit with K_rpd forced to 0. `enforce_nonnegative=True` (the default)
reproduces that: any negative factor is dropped to 0 and the model refit on the
remaining components.

Pure standard library: a small Gaussian-elimination solver runs the normal
equations, so there is no numpy dependency (consistent with the rest of RAMS).
"""
from __future__ import annotations

import csv
import dataclasses
import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .distress import (
    HDM4PotholeModel,
    HDM4RoughnessModel,
    HDM4SkidModel,
    MLITCrackModel,
)
from .hdm4 import HDM4RutCalibration

# Unit-K calibration: its component methods return RDO/RDST/RDPD with K==1, which
# is exactly the design matrix the regression needs.
_UNIT = HDM4RutCalibration()


@dataclass
class RutObservation:
    """One (segment, year) field record used to calibrate the rut model.

    `measured_rut_increment_mm` is the *increase* in rut depth over that year.
    `age` (years since construction/last overlay) gates initial densification,
    which HDM-4 applies in the first year only.
    """

    measured_rut_increment_mm: float
    ye4: float                       # that year's traffic, million ESAL/lane
    age: int
    deflection_mm: float = 0.5
    structural_number: float = 4.0
    compaction_pct: float = 98.0
    cds: float = 1.0
    heavy_speed_kmh: float = 50.0
    surfacing_thickness_mm: float = 100.0

    def components(self) -> Tuple[float, float, float]:
        """Unit-K (RDO, RDST, RDPD) for this record -- the design-matrix row."""
        rdo = (
            _UNIT.densification(self.ye4, self.deflection_mm, self.structural_number, self.compaction_pct)
            if self.age == 1
            else 0.0
        )
        rdst = _UNIT.structural(self.ye4, self.structural_number, self.compaction_pct)
        rdpd = _UNIT.plastic(self.ye4, self.cds, self.heavy_speed_kmh, self.surfacing_thickness_mm)
        return rdo, rdst, rdpd


@dataclass
class CalibrationResult:
    """Fitted calibration factors plus goodness-of-fit, mirroring the paper."""

    k_rid: float
    k_rst: float
    k_rpd: float
    r_squared: float
    rmse_before: float       # RMSE of the `base` (pre-calibration) factors
    rmse_after: float        # RMSE of the fitted factors
    n: int
    fixed_to_zero: Tuple[str, ...]   # components dropped by the non-negativity rule
    calibration: HDM4RutCalibration  # ready-to-use, fitted

    def summary(self) -> str:
        zeroed = (
            f"; forced to 0: {', '.join(self.fixed_to_zero)}" if self.fixed_to_zero else ""
        )
        return (
            f"Calibrated on {self.n} obs: Krid={self.k_rid:.3f} Krst={self.k_rst:.3f} "
            f"Krpd={self.k_rpd:.3f} (R^2={self.r_squared:.4f}); RMSE "
            f"{self.rmse_before:.3f} -> {self.rmse_after:.3f}{zeroed}"
        )


# --- small pure-stdlib least-squares ---------------------------------------

def _solve(matrix: List[List[float]], rhs: List[float]) -> List[float]:
    """Solve a small square linear system by Gaussian elimination w/ pivoting."""
    n = len(matrix)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError(
                "singular normal equations (collinear / degenerate predictors) -- "
                "cannot calibrate; check the observation set."
            )
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / aug[col][col]
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][n] / aug[i][i] for i in range(n)]


def _ols(design: Sequence[Sequence[float]], y: Sequence[float]) -> List[float]:
    """Ordinary least squares via the normal equations (X^T X) b = X^T y."""
    k = len(design[0])
    xtx = [[sum(row[a] * row[b] for row in design) for b in range(k)] for a in range(k)]
    xty = [sum(design[i][a] * y[i] for i in range(len(y))) for a in range(k)]
    return _solve(xtx, xty)


_COMPONENT_NAMES = ("k_rid", "k_rst", "k_rpd")


def calibrate_hdm4_rut(
    observations: Iterable[RutObservation],
    *,
    base: HDM4RutCalibration = _UNIT,
    enforce_nonnegative: bool = True,
    label: Optional[str] = None,
) -> CalibrationResult:
    """Fit [K_rid, K_rst, K_rpd] to measured rut increments by OLS regression.

    `base` supplies the a-coefficients (the HDM-4 model form) and the
    "before calibration" factors used for `rmse_before`. With
    `enforce_nonnegative` any factor that regresses negative is forced to 0 and
    the model refit (the paper's K_rpd<0 handling).
    """
    obs = list(observations)
    if len(obs) < 3:
        raise ValueError("need at least 3 observations to calibrate 3 factors.")

    full_design = [list(o.components()) for o in obs]
    y = [o.measured_rut_increment_mm for o in obs]

    active = [0, 1, 2]
    coeffs = {0: 0.0, 1: 0.0, 2: 0.0}
    while active:
        if len(obs) < len(active):
            raise ValueError("more predictors than observations; supply more data.")
        sub = [[row[i] for i in active] for row in full_design]
        beta = _ols(sub, y)
        fitted = dict(zip(active, beta))
        if enforce_nonnegative:
            worst = min(active, key=lambda i: fitted[i])
            if fitted[worst] < 0:
                active = [i for i in active if i != worst]  # drop & refit at 0
                coeffs[worst] = 0.0
                continue
        coeffs.update(fitted)
        break

    if not active:
        raise ValueError(
            "no non-negative combination of components fits the data "
            "(all factors regressed <= 0). Check the observations -- e.g. "
            "negative measured rut increments, or wrong age/densification gating."
        )

    k_rid, k_rst, k_rpd = coeffs[0], coeffs[1], coeffs[2]
    fixed = tuple(_COMPONENT_NAMES[i] for i in (0, 1, 2) if i not in active)

    def predict(kr: float, ks: float, kp: float) -> List[float]:
        return [kr * r[0] + ks * r[1] + kp * r[2] for r in full_design]

    pred_after = predict(k_rid, k_rst, k_rpd)
    pred_before = predict(base.k_rid, base.k_rst, base.k_rpd)
    rmse_after = _rmse(y, pred_after)
    rmse_before = _rmse(y, pred_before)
    r2 = _r_squared(y, pred_after)

    fitted_cal = dataclasses.replace(
        base,
        k_rid=round(k_rid, 4),
        k_rst=round(k_rst, 4),
        k_rpd=round(k_rpd, 4),
        label=label or f"HDM-4 (calibrated on {len(obs)} obs)",
    )
    return CalibrationResult(
        k_rid=round(k_rid, 4),
        k_rst=round(k_rst, 4),
        k_rpd=round(k_rpd, 4),
        r_squared=round(r2, 4),
        rmse_before=round(rmse_before, 4),
        rmse_after=round(rmse_after, 4),
        n=len(obs),
        fixed_to_zero=fixed,
        calibration=fitted_cal,
    )


def _rmse(y: Sequence[float], pred: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(y, pred)) / len(y))


def _r_squared(y: Sequence[float], pred: Sequence[float]) -> float:
    mean = sum(y) / len(y)
    ss_tot = sum((a - mean) ** 2 for a in y)
    ss_res = sum((a - b) ** 2 for a, b in zip(y, pred))
    if ss_tot < 1e-12:
        return 0.0
    return 1.0 - ss_res / ss_tot


# --- CSV ingestion of field observations -----------------------------------

_OBS_REQUIRED = ("ye4", "age", "deflection_mm", "structural_number", "measured_rut_increment_mm")


def load_observations_csv(path: str) -> List[RutObservation]:
    """Load rut-calibration observations from a CSV with `_OBS_REQUIRED` columns.

    Optional columns: compaction_pct, cds, heavy_speed_kmh, surfacing_thickness_mm.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in _OBS_REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"observations CSV missing required columns: {missing}")
        obs: List[RutObservation] = []
        for i, row in enumerate(reader, start=1):
            try:
                obs.append(
                    RutObservation(
                        measured_rut_increment_mm=float(row["measured_rut_increment_mm"]),
                        ye4=float(row["ye4"]),
                        age=int(float(row["age"])),
                        deflection_mm=float(row["deflection_mm"]),
                        structural_number=float(row["structural_number"]),
                        compaction_pct=float(row.get("compaction_pct") or 98.0),
                        cds=float(row.get("cds") or 1.0),
                        heavy_speed_kmh=float(row.get("heavy_speed_kmh") or 50.0),
                        surfacing_thickness_mm=float(row.get("surfacing_thickness_mm") or 100.0),
                    )
                )
            except (ValueError, KeyError) as exc:
                raise ValueError(f"observation row {i}: {exc}") from None
    return obs


# --- cracking calibration (MLIT recursion C_{i+1} = a + b*C_i) -------------

@dataclass
class CrackCalibrationResult:
    a: float
    b: float
    r_squared: float
    n: int
    model: MLITCrackModel

    def summary(self) -> str:
        return (
            f"MLIT cracking fit on {self.n} pairs: C+1 = {self.a:.3f} + {self.b:.3f}*C "
            f"(R^2={self.r_squared:.4f})"
        )


def calibrate_mlit_cracking(
    pairs: Iterable[Tuple[float, float]], *, label: Optional[str] = None
) -> CrackCalibrationResult:
    """Fit the MLIT cracking recursion C_next = a + b*C_prev by OLS.

    `pairs` are (C_i, C_{i+1}) observations -- consecutive yearly cracking
    measurements on the same segment. Linear regression with intercept.
    """
    pts = [(float(c0), float(c1)) for c0, c1 in pairs]
    if len(pts) < 2:
        raise ValueError("need at least 2 (C_i, C_{i+1}) pairs to fit a line.")
    design = [[1.0, c0] for c0, _ in pts]
    y = [c1 for _, c1 in pts]
    a, b = _ols(design, y)
    pred = [a + b * c0 for c0, _ in pts]
    return CrackCalibrationResult(
        a=round(a, 4), b=round(b, 4), r_squared=round(_r_squared(y, pred), 4),
        n=len(pts),
        model=MLITCrackModel(round(a, 4), round(b, 4), label or f"MLIT cracking (fit, n={len(pts)})"),
    )


def cracking_pairs_from_series(readings: Sequence[Tuple[int, float]]) -> List[Tuple[float, float]]:
    """Turn a per-segment (age, cracking_pct) series into (C_i, C_{i+1}) pairs."""
    ordered = sorted(readings, key=lambda r: r[0])
    return [(c0, c1) for (_, c0), (_, c1) in zip(ordered, ordered[1:])]


# --- roughness calibration (HDM-4 incremental IRI) -------------------------

@dataclass
class RoughnessObservation:
    """One year's roughness record for fitting the HDM-4 roughness model."""

    measured_iri_increment: float
    iri: float
    structural_number: float
    age: int
    d_msa: float
    d_crack_pct: float
    d_rut_mm: float


@dataclass
class RoughnessCalibrationResult:
    env_coeff: float
    struct_a0: float
    crack_coeff: float
    rut_coeff: float
    r_squared: float
    rmse: float
    n: int
    model: HDM4RoughnessModel

    def summary(self) -> str:
        return (
            f"HDM-4 roughness fit on {self.n} obs: env={self.env_coeff:.4f} "
            f"struct_a0={self.struct_a0:.2f} Kc={self.crack_coeff:.4f} "
            f"Kr={self.rut_coeff:.4f} (R^2={self.r_squared:.4f}, RMSE={self.rmse:.4f})"
        )


def calibrate_hdm4_roughness(
    observations: Iterable[RoughnessObservation],
    *,
    base: HDM4RoughnessModel = HDM4RoughnessModel(),
    label: Optional[str] = None,
) -> RoughnessCalibrationResult:
    """Fit the linear roughness multipliers [env, struct_a0, Kc, Kr] by OLS.

    The structural term's exponents (age, SNP power) come from `base` and are
    held fixed -- the regression recovers the four linear coefficients that make
    the HDM-4 incremental-roughness form fit measured dIRI.
    """
    obs = list(observations)
    if len(obs) < 4:
        raise ValueError("need at least 4 observations to fit 4 roughness coefficients.")
    design = []
    y = []
    for o in obs:
        x_env = max(0.0, o.iri)
        x_struct = (
            math.exp(base.struct_age_m * o.age)
            * math.pow(1.0 + max(0.0, o.structural_number), base.struct_snp_pow)
            * max(0.0, o.d_msa)
        )
        design.append([x_env, x_struct, max(0.0, o.d_crack_pct), max(0.0, o.d_rut_mm)])
        y.append(o.measured_iri_increment)
    env, a0, kc, kr = _ols(design, y)
    pred = [r[0] * env + r[1] * a0 + r[2] * kc + r[3] * kr for r in design]
    model = dataclasses.replace(
        base, env_coeff=round(env, 5), struct_a0=round(a0, 3),
        crack_coeff=round(kc, 5), rut_coeff=round(kr, 5),
        label=label or f"HDM-4 roughness (fit, n={len(obs)})",
    )
    return RoughnessCalibrationResult(
        env_coeff=round(env, 5), struct_a0=round(a0, 3),
        crack_coeff=round(kc, 5), rut_coeff=round(kr, 5),
        r_squared=round(_r_squared(y, pred), 4), rmse=round(_rmse(y, pred), 4),
        n=len(obs), model=model,
    )


# --- skid calibration (HDM-4 polishing decay) ------------------------------

@dataclass
class SkidObservation:
    """One year's skid record for fitting the polishing rate decay_k."""

    measured_sfc_decrement: float   # dSFC over the year (<= 0)
    sfc: float                      # SFC at the start of the year
    d_msa: float


@dataclass
class SkidCalibrationResult:
    decay_k: float
    sfc_min: float
    r_squared: float
    n: int
    model: HDM4SkidModel

    def summary(self) -> str:
        return (
            f"HDM-4 skid fit on {self.n} obs: decay_k={self.decay_k:.5f} "
            f"(sfc_min={self.sfc_min:.3f}, R^2={self.r_squared:.4f})"
        )


def calibrate_hdm4_skid(
    observations: Iterable[SkidObservation],
    *,
    base: HDM4SkidModel = HDM4SkidModel(),
    label: Optional[str] = None,
) -> SkidCalibrationResult:
    """Fit the polishing rate decay_k by single-predictor OLS (no intercept).

    dSFC = -decay_k * (SFC - sfc_min) * dMSA  -- linear in decay_k, with sfc_min
    held at `base.sfc_min`.
    """
    obs = list(observations)
    if len(obs) < 2:
        raise ValueError("need at least 2 observations to fit decay_k.")
    # Single-predictor regression through the origin: y = decay_k * x.
    xs = [-(o.sfc - base.sfc_min) * o.d_msa for o in obs]
    ys = [o.measured_sfc_decrement for o in obs]
    sxx = sum(x * x for x in xs)
    if sxx < 1e-12:
        raise ValueError("degenerate skid data (no SFC headroom or no traffic).")
    decay_k = sum(x * y for x, y in zip(xs, ys)) / sxx
    pred = [decay_k * x for x in xs]
    return SkidCalibrationResult(
        decay_k=round(decay_k, 6), sfc_min=base.sfc_min,
        r_squared=round(_r_squared(ys, pred), 4), n=len(obs),
        model=dataclasses.replace(
            base, decay_k=round(decay_k, 6),
            label=label or f"HDM-4 skid (fit, n={len(obs)})",
        ),
    )


# --- pothole calibration (HDM-4 crack-initiated progression) ---------------

@dataclass
class PotholeObservation:
    """One year's potholing record for fitting the progression `rate`."""

    measured_pothole_increment: float   # dAPOT over the year (>= 0)
    cracking_pct: float                 # cracking at the start of the year
    d_msa: float


@dataclass
class PotholeCalibrationResult:
    rate: float
    crack_threshold_pct: float
    r_squared: float
    n: int
    model: HDM4PotholeModel

    def summary(self) -> str:
        return (
            f"HDM-4 potholing fit on {self.n} obs: rate={self.rate:.4f} "
            f"(crack_threshold={self.crack_threshold_pct:.1f}%, R^2={self.r_squared:.4f})"
        )


def calibrate_hdm4_potholes(
    observations: Iterable[PotholeObservation],
    *,
    base: HDM4PotholeModel = HDM4PotholeModel(),
    label: Optional[str] = None,
) -> PotholeCalibrationResult:
    """Fit the potholing progression `rate` by single-predictor OLS (no intercept).

    dAPOT = rate * (max(0, CRACK - threshold)/100) * dMSA * water_factor.
    `crack_threshold_pct` and `water_factor` are held at `base`'s values.
    """
    obs = list(observations)
    if len(obs) < 2:
        raise ValueError("need at least 2 observations to fit the pothole rate.")
    xs = [
        (max(0.0, o.cracking_pct - base.crack_threshold_pct) / 100.0) * o.d_msa * base.water_factor
        for o in obs
    ]
    ys = [o.measured_pothole_increment for o in obs]
    sxx = sum(x * x for x in xs)
    if sxx < 1e-12:
        raise ValueError("degenerate pothole data (no cracking above threshold, or no traffic).")
    rate = sum(x * y for x, y in zip(xs, ys)) / sxx
    pred = [rate * x for x in xs]
    return PotholeCalibrationResult(
        rate=round(rate, 5), crack_threshold_pct=base.crack_threshold_pct,
        r_squared=round(_r_squared(ys, pred), 4), n=len(obs),
        model=dataclasses.replace(
            base, rate=round(rate, 5),
            label=label or f"HDM-4 potholing (fit, n={len(obs)})",
        ),
    )


def observations_from_rut_series(
    readings: Sequence[Tuple[int, float]],
    *,
    ye4: float,
    deflection_mm: float,
    structural_number: float,
    compaction_pct: float = 98.0,
    cds: float = 1.0,
    heavy_speed_kmh: float = 50.0,
    surfacing_thickness_mm: float = 100.0,
) -> List[RutObservation]:
    """Turn a per-segment series of (age, cumulative_rut_mm) into year-on-year
    increment observations (field rut is usually recorded as total depth)."""
    ordered = sorted(readings, key=lambda r: r[0])
    obs: List[RutObservation] = []
    for (a0, r0), (a1, r1) in zip(ordered, ordered[1:]):
        obs.append(
            RutObservation(
                measured_rut_increment_mm=r1 - r0,
                ye4=ye4,
                age=a1,
                deflection_mm=deflection_mm,
                structural_number=structural_number,
                compaction_pct=compaction_pct,
                cds=cds,
                heavy_speed_kmh=heavy_speed_kmh,
                surfacing_thickness_mm=surfacing_thickness_mm,
            )
        )
    return obs
