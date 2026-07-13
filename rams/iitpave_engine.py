"""
Pure-Python multi-layer linear-elastic pavement solver -- a portable re-implementation
of IITPAVE (the IRC:37-2018 layered-elastic tool).

IITPAVE (bundled in ``IRC_37_IITPAVE.rar``) is a Windows Fortran program that solves
the classic **Burmister n-layer elastic** problem: a uniform circular load (single or
dual wheel) on a system of bonded, isotropic, homogeneous elastic layers resting on an
elastic half-space. It returns, at any (z, r) point, the stress tensor
(sigma_z, sigma_t, sigma_r, tau_rz), the vertical deflection and the three normal
strains -- exactly the quantities IRC:37 needs for the two critical strains:

  * horizontal tensile strain at the bottom of the bituminous layer (eps_t) -> fatigue,
  * vertical compressive strain on top of the subgrade (eps_v)              -> rutting.

RAMS cannot ship / run the Windows binary on its Linux hosting, and deliberately depends
on **no third-party packages** (no numpy/scipy). So this module reproduces the IITPAVE
computation from first principles in the standard library:

  1. Axisymmetric elasticity is solved in the Hankel-transform domain. Each layer's
     transformed response is governed by a biharmonic stress function with four unknown
     constants (A, B, C, D); the half-space drops the two growing terms.
  2. The unknowns are fixed, at every Hankel parameter ``m``, by the surface load, zero
     surface shear, and continuity of (sigma_z, tau_rz, u_z, u_r) across every bonded
     interface -- a (4n-2) linear system solved by Gaussian elimination.
  3. Physical stresses/strains are recovered by numerically inverting the Hankel
     transform (integrating the transformed response against J0 / J1 Bessel kernels).
  4. A dual wheel is the superposition of two such single-wheel fields along the line
     joining the wheel centres (IRC standard 310 mm c/c spacing).

The exponentials are carried in a depth-scaled form (``e^{-m*zeta}`` / ``e^{-m*(h-zeta)}``)
so nothing overflows even for thick, stiff layers. Validated against the ground-truth
IITPAVE outputs bundled under ``rams/data/iitpave_reference/`` (see
``tests/test_iitpave_engine.py``): stresses and strains match to ~3 significant figures.

This is the genuine mechanistic method IRC:37 mandates -- it replaces the earlier
Odemark--Boussinesq approximation (kept in ``iitpave.py`` only as a fallback). Every
coefficient (Poisson ratios, load, dual spacing) is data, like the rest of RAMS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# IRC:37 standard dual-wheel geometry (centre-to-centre spacing of the dual set).
DUAL_SPACING_MM = 310.0


# --------------------------------------------------------------------------- #
#  Bessel functions J0, J1 (Abramowitz & Stegun 9.4, ~1e-7 accuracy)          #
# --------------------------------------------------------------------------- #

def bessel_j0(x: float) -> float:
    ax = abs(x)
    if ax < 8.0:
        y = x * x
        p1 = (57568490574.0 + y * (-13362590354.0 + y * (651619640.7 + y *
              (-11214424.18 + y * (77392.33017 + y * (-184.9052456))))))
        p2 = (57568490411.0 + y * (1029532985.0 + y * (9494680.718 + y *
              (59272.64853 + y * (267.8532712 + y)))))
        return p1 / p2
    z = 8.0 / ax
    y = z * z
    xx = ax - 0.785398164
    p1 = (1.0 + y * (-0.1098628627e-2 + y * (0.2734510407e-4 + y *
          (-0.2073370639e-5 + y * 0.2093887211e-6))))
    p2 = (-0.1562499995e-1 + y * (0.1430488765e-3 + y * (-0.6911147651e-5 + y *
          (0.7621095161e-6 + y * (-0.934935152e-7)))))
    return math.sqrt(0.636619772 / ax) * (math.cos(xx) * p1 - z * math.sin(xx) * p2)


def bessel_j1(x: float) -> float:
    ax = abs(x)
    if ax < 8.0:
        y = x * x
        p1 = x * (72362614232.0 + y * (-7895059235.0 + y * (242396853.1 + y *
             (-2972611.439 + y * (15704.48260 + y * (-30.16036606))))))
        p2 = (144725228442.0 + y * (2300535178.0 + y * (18583304.74 + y *
             (99447.43394 + y * (376.9991397 + y)))))
        return p1 / p2
    z = 8.0 / ax
    y = z * z
    xx = ax - 2.356194491
    p1 = (1.0 + y * (0.183105e-2 + y * (-0.3516396496e-4 + y *
          (0.2457520174e-5 + y * (-0.240337019e-6)))))
    p2 = (0.04687499995 + y * (-0.2002690873e-3 + y * (0.8449199096e-5 + y *
          (-0.88228987e-6 + y * 0.105787412e-6))))
    ans = math.sqrt(0.636619772 / ax) * (math.cos(xx) * p1 - z * math.sin(xx) * p2)
    return ans if x >= 0.0 else -ans


def _j1_over_x(x: float) -> float:
    """J1(x)/x, numerically stable at x->0 (limit 1/2)."""
    if abs(x) < 1e-4:
        # J1(x)/x = 1/2 - x^2/16 + ...
        return 0.5 - x * x / 16.0
    return bessel_j1(x) / x


# --------------------------------------------------------------------------- #
#  Model definition                                                           #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ElasticLayer:
    """One isotropic elastic layer. The last layer in a system is the half-space."""

    E_mpa: float
    nu: float
    thickness_mm: float          # ignored for the bottom half-space


def envelope_note(layers: Sequence[ElasticLayer]) -> Optional[str]:
    """Return a caveat if a layer stack falls outside the engine's *validated*
    envelope, else None.

    The engine is validated (to ~0.1%) against the bundled IITPAVE outputs and the
    closed-form Boussinesq solution for **monotonically softening** stacks -- the
    real-pavement case, where each layer is weaker than the one above (bituminous >
    granular > subgrade). Every stack RAMS builds is of that form, so this returns
    None in production. It has NOT been reconciled against IITPAVE for non-monotone
    profiles with a stiff layer buried under a softer one (the only bundled
    reference for that, ``case_c.out``, is internally inconsistent -- it violates
    interface continuity -- so it could not be used). For such a stack, confirm the
    result against IITPAVE proper.
    """
    finite = [l for l in layers]
    moduli = [l.E_mpa for l in finite]
    for upper, lower in zip(moduli, moduli[1:]):
        if lower > upper * 1.05:                      # a stiffer layer below a softer one
            return ("layer stack is not monotonically softening (a stiffer layer sits "
                    "below a softer one); outside the engine's validated envelope -- "
                    "confirm against IITPAVE.")
    return None


@dataclass(frozen=True)
class WheelLoad:
    """A circular contact load. ``dual`` superposes a second identical wheel."""

    load_n: float
    pressure_mpa: float
    dual: bool = True
    spacing_mm: float = DUAL_SPACING_MM

    @property
    def contact_radius_mm(self) -> float:
        return math.sqrt(self.load_n / (math.pi * self.pressure_mpa))


@dataclass
class PointResponse:
    """Stresses (MPa), deflection (mm) and strains at one (z, r) point.

    Sign convention matches IITPAVE: tension positive, compression negative for
    stress; a positive normal strain is tensile. ``sigma_z`` is vertical.
    """

    z_mm: float
    r_mm: float
    sigma_z: float
    sigma_t: float
    sigma_r: float
    tau_rz: float
    disp_z_mm: float
    eps_z: float
    eps_t: float
    eps_r: float


# --------------------------------------------------------------------------- #
#  Core: transformed-domain constants at one Hankel parameter m               #
# --------------------------------------------------------------------------- #
#
#  Per layer the biharmonic stress function transform is
#     Phi(zeta) = (A + B*zeta) e^{-m*zeta} + (C + D*zeta) e^{+m*zeta}
#  with zeta the local depth from the top of the layer. To avoid overflow the
#  growing part is stored scaled: the stored C, D are  C*e^{m*h}, D*e^{m*h}, and
#  the growing exponential is evaluated as e2 = e^{-m*(h-zeta)} <= 1. For the
#  half-space C = D = 0 (no growing term).
#
#  The transformed physical quantities are linear in (A, B, C, D). Each is
#  returned as a 4-vector of coefficients [cA, cB, cC, cD] so the boundary/
#  continuity system can be assembled generically.


def _coeffs_at(m: float, nu: float, zeta: float, e1: float, e2: float):
    """Coefficient 4-vectors [cA, cB, cC, cD] for the transformed
    (sigma_z, tau_rz, 2G*u_z, 2G*u_r) at local depth ``zeta``.

    ``e1 = e^{-m*zeta}``, ``e2 = e^{-m*(h-zeta)}`` (0 for the half-space).
    """
    m2 = m * m
    m3 = m2 * m
    mz = m * zeta
    # sigma_z transform (kernel J0)
    sz = (m3 * e1,
          m2 * e1 * (1.0 - 2.0 * nu + mz),
          -m3 * e2,
          m2 * e2 * (1.0 - 2.0 * nu - mz))
    # tau_rz transform (kernel J1)
    trz = (m3 * e1,
           -m2 * e1 * (2.0 * nu - mz),
           m3 * e2,
           m2 * e2 * (2.0 * nu + mz))
    # 2G * u_z transform (kernel J0)
    uz = (-m2 * e1,
          m * e1 * (4.0 * nu - 2.0 - mz),
          -m2 * e2,
          m * e2 * (2.0 - 4.0 * nu - mz))
    # 2G * u_r transform (kernel J1)
    ur = (-m2 * e1,
          m * e1 * (1.0 - mz),
          m2 * e2,
          m * e2 * (1.0 + mz))
    return sz, trz, uz, ur


def _sigma_horizontal_coeffs(m: float, nu: float, zeta: float, e1: float, e2: float):
    """Coefficient 4-vectors for sigma_r and sigma_t, each split into a part that
    multiplies J0(m r) and a part that multiplies J1(m r)/r.

    Returns (sr_J0, sr_J1r, st_J0, st_J1r), each a 4-vector.
    """
    m2 = m * m
    m3 = m2 * m
    mz = m * zeta
    # (nu*psi' + m^2 Phi')  -> J0 part of sigma_r
    sr_j0 = (-m3 * e1,
             m2 * e1 * (1.0 + 2.0 * nu - mz),
             m3 * e2,
             m2 * e2 * (1.0 + 2.0 * nu + mz))
    # nu*psi'  -> J0 part of sigma_t
    st_j0 = (0.0, 2.0 * nu * m2 * e1, 0.0, 2.0 * nu * m2 * e2)
    # Phi' coefficients
    phi = (-m * e1, e1 * (1.0 - mz), m * e2, e2 * (1.0 + mz))
    # sigma_r J1/r part = -m*Phi' ; sigma_t J1/r part = +m*Phi'
    sr_j1r = tuple(-m * c for c in phi)
    st_j1r = tuple(m * c for c in phi)
    return sr_j0, sr_j1r, st_j0, st_j1r


class _LayeredSystem:
    """Solves the Burmister constants at a given Hankel parameter m and evaluates
    the transformed response, for a fixed layer stack and load."""

    def __init__(self, layers: Sequence[ElasticLayer], load_intensity: float,
                 contact_radius: float):
        self.layers = list(layers)
        self.n = len(self.layers)
        self.q = load_intensity          # contact pressure (MPa)
        self.a = contact_radius          # contact radius (mm)
        self.twoG = [l.E_mpa / (1.0 + l.nu) for l in self.layers]  # 2G = E/(1+nu)
        # depth of the top of each layer
        self.z_top = [0.0]
        for l in self.layers[:-1]:
            self.z_top.append(self.z_top[-1] + l.thickness_mm)

    def _n_unknowns(self) -> int:
        return 4 * (self.n - 1) + 2

    def solve_constants(self, m: float) -> List[Tuple[float, float, float, float]]:
        """Return [(A,B,C,D), ...] per layer (C=D=0 for the half-space) at param m."""
        n = self.n
        N = self._n_unknowns()
        # column offset for each layer's unknowns
        off = [4 * i for i in range(n)]  # layer i unknowns start at off[i]
        # half-space (layer n-1) has only A,B at off[n-1] = 4(n-1)
        M = [[0.0] * N for _ in range(N)]
        rhs = [0.0] * N
        row = 0

        def e_pair(i: float, zeta: float):
            h = self.layers[i].thickness_mm
            if i == n - 1:
                return math.exp(-m * zeta), 0.0        # half-space: no growing term
            return math.exp(-m * zeta), math.exp(-m * (h - zeta))

        def put(r: int, i: int, vec, scale: float = 1.0):
            base = off[i]
            ncol = 2 if i == n - 1 else 4
            for k in range(ncol):
                M[r][base + k] += scale * vec[k]

        # --- surface (layer 0, zeta=0): sigma_z = -q*a*J1(m a), tau_rz = 0 ---
        e1, e2 = e_pair(0, 0.0)
        sz, trz, uz, ur = _coeffs_at(m, self.layers[0].nu, 0.0, e1, e2)
        put(row, 0, sz); rhs[row] = -self.q * self.a * bessel_j1(m * self.a); row += 1
        put(row, 0, trz); rhs[row] = 0.0; row += 1

        # --- interfaces i | i+1 : continuity of sigma_z, tau_rz, u_z, u_r ---
        for i in range(n - 1):
            h = self.layers[i].thickness_mm
            e1b, e2b = e_pair(i, h)          # bottom of layer i
            e1t, e2t = e_pair(i + 1, 0.0)    # top of layer i+1
            szb, trzb, uzb, urb = _coeffs_at(m, self.layers[i].nu, h, e1b, e2b)
            szt, trzt, uzt, urt = _coeffs_at(m, self.layers[i + 1].nu, 0.0, e1t, e2t)
            # sigma_z continuous
            put(row, i, szb); put(row, i + 1, szt, -1.0); row += 1
            # tau_rz continuous
            put(row, i, trzb); put(row, i + 1, trzt, -1.0); row += 1
            # u_z continuous:  (2G u_z)_i / 2G_i  =  (2G u_z)_{i+1} / 2G_{i+1}
            put(row, i, uzb, 1.0 / self.twoG[i])
            put(row, i + 1, uzt, -1.0 / self.twoG[i + 1]); row += 1
            # u_r continuous
            put(row, i, urb, 1.0 / self.twoG[i])
            put(row, i + 1, urt, -1.0 / self.twoG[i + 1]); row += 1

        x = _solve_linear(M, rhs)
        out = []
        for i in range(n):
            if i == n - 1:
                out.append((x[off[i]], x[off[i] + 1], 0.0, 0.0))
            else:
                out.append((x[off[i]], x[off[i] + 1], x[off[i] + 2], x[off[i] + 3]))
        return out

    def transformed_response(self, m: float, layer_idx: int, zeta: float,
                             consts: Tuple[float, float, float, float]):
        """Transformed (kernel) amplitudes at (layer, zeta) for a solved const set.

        Returns (sz_J0, trz_J1, uz_J0, ur_J1, sr_J0, sr_J1r, st_J0, st_J1r) --
        amplitudes to be multiplied by the respective Bessel kernel and integrated.
        """
        nu = self.layers[layer_idx].nu
        h = self.layers[layer_idx].thickness_mm
        if layer_idx == self.n - 1:
            e1, e2 = math.exp(-m * zeta), 0.0
        else:
            e1, e2 = math.exp(-m * zeta), math.exp(-m * (h - zeta))
        sz, trz, uz, ur = _coeffs_at(m, nu, zeta, e1, e2)
        sr_j0, sr_j1r, st_j0, st_j1r = _sigma_horizontal_coeffs(m, nu, zeta, e1, e2)

        def dot(vec):
            return (vec[0] * consts[0] + vec[1] * consts[1]
                    + vec[2] * consts[2] + vec[3] * consts[3])

        return (dot(sz), dot(trz), dot(uz), dot(ur),
                dot(sr_j0), dot(sr_j1r), dot(st_j0), dot(st_j1r))


def _solve_linear(A: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting (dense, small systems)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-300:
            M[piv][col] = 1e-300
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for r in range(col + 1, n):
            f = M[r][col] / pivval
            if f != 0.0:
                for c in range(col, n + 1):
                    M[r][c] -= f * M[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n] - sum(M[i][c] * x[c] for c in range(i + 1, n))
        x[i] = s / M[i][i]
    return x


# --------------------------------------------------------------------------- #
#  Hankel inversion: integrate transformed response against J0 / J1 kernels   #
# --------------------------------------------------------------------------- #

# 8-point Gauss-Legendre nodes/weights on [-1, 1]
_GL8_X = (-0.9602898564975363, -0.7966664774136267, -0.5255324099163290,
          -0.1834346424956498, 0.1834346424956498, 0.5255324099163290,
          0.7966664774136267, 0.9602898564975363)
_GL8_W = (0.1012285362903763, 0.2223810344533745, 0.3137066458778873,
          0.3626837833783620, 0.3626837833783620, 0.3137066458778873,
          0.2223810344533745, 0.1012285362903763)


def _single_wheel_point(system: _LayeredSystem, z_mm: float, r_mm: float,
                        layer_idx: int, zeta: float) -> PointResponse:
    """Response at one (z, r) point for a single circular load, by numerically
    inverting the Hankel transform."""
    a = system.a
    # Oscillation length scale of the kernels (load J1(m a) and field J0/J1(m r)).
    scale = max(a, r_mm, 1.0)
    dm = math.pi / (16.0 * scale)              # ~32 samples per Bessel period
    # Depth that drives exponential decay of the integrand (response is smooth at
    # depth); guard a tiny floor so a surface point still integrates.
    decay_depth = max(z_mm, 0.05 * a, 1.0)
    m_max = 30.0 / decay_depth
    n_panels = max(200, int(math.ceil(m_max / dm)))

    acc = [0.0] * 8   # sz, trz, uz, ur, sr_j0, sr_j1r, st_j0, st_j1r
    prev_const_m = None
    m0 = 1e-9
    for p in range(n_panels):
        ma = m0 + p * dm
        mb = ma + dm
        mid = 0.5 * (ma + mb)
        half = 0.5 * (mb - ma)
        panel = [0.0] * 8
        for xg, wg in zip(_GL8_X, _GL8_W):
            m = mid + half * xg
            consts = system.solve_constants(m)
            amp = system.transformed_response(m, layer_idx, zeta, consts[layer_idx])
            j0 = bessel_j0(m * r_mm)
            j1 = bessel_j1(m * r_mm)
            j1r = _j1_over_x(m * r_mm) * m      # J1(m r)/r  = m * J1(x)/x
            w = wg * half
            panel[0] += w * amp[0] * j0         # sigma_z
            panel[1] += w * amp[1] * j1         # tau_rz
            panel[2] += w * amp[2] * j0         # 2G u_z
            panel[3] += w * amp[3] * j1         # 2G u_r
            panel[4] += w * amp[4] * j0         # sigma_r  J0 part
            panel[5] += w * amp[5] * j1r        # sigma_r  J1/r part
            panel[6] += w * amp[6] * j0         # sigma_t  J0 part
            panel[7] += w * amp[7] * j1r        # sigma_t  J1/r part
        for k in range(8):
            acc[k] += panel[k]
        # early stop once the tail is negligible relative to the running integral
        if p > 40 and p % 8 == 0:
            mag = max(abs(acc[0]), abs(acc[4]), abs(acc[6]), 1e-30)
            if max(abs(panel[k]) for k in range(8)) < 1e-9 * mag:
                break

    twoG = system.twoG[layer_idx]
    sigma_z = acc[0]
    tau_rz = acc[1]
    disp_z = acc[2] / twoG
    sigma_r = acc[4] + acc[5]
    sigma_t = acc[6] + acc[7]
    E = system.layers[layer_idx].E_mpa
    nu = system.layers[layer_idx].nu
    eps_z = (sigma_z - nu * (sigma_r + sigma_t)) / E
    eps_r = (sigma_r - nu * (sigma_z + sigma_t)) / E
    eps_t = (sigma_t - nu * (sigma_z + sigma_r)) / E
    return PointResponse(z_mm=z_mm, r_mm=r_mm, sigma_z=sigma_z, sigma_t=sigma_t,
                         sigma_r=sigma_r, tau_rz=tau_rz, disp_z_mm=disp_z,
                         eps_z=eps_z, eps_t=eps_t, eps_r=eps_r)


# --------------------------------------------------------------------------- #
#  Public API                                                                 #
# --------------------------------------------------------------------------- #

def _locate_layer(z_top: List[float], layers: Sequence[ElasticLayer], z: float,
                  lower: bool) -> Tuple[int, float]:
    """Return (layer_idx, zeta) for depth z. At an interface, ``lower`` selects the
    layer below (zeta=0) vs above (zeta=h of the upper layer)."""
    n = len(layers)
    interfaces = z_top[1:]  # depths of interfaces (below layer 0..n-2)
    for i, zt in enumerate(interfaces):
        if abs(z - zt) < 1e-6:
            if lower:
                return i + 1, 0.0
            return i, layers[i].thickness_mm
    # strictly inside some layer
    for i in range(n):
        top = z_top[i]
        if i < n - 1:
            bot = z_top[i + 1]
            if top - 1e-9 <= z < bot - 1e-9 or (top <= z <= bot):
                if z < bot - 1e-9:
                    return i, z - top
        else:
            return i, z - top   # half-space
    return n - 1, z - z_top[-1]


# Integration resolution presets: (gauss-samples-per-Bessel-period, panel floor,
# cap on the radius used to set the panel width). FINE reproduces the full IITPAVE
# stress tensor (incl. the sharp tau_rz / sigma_r features) to ~0.1%; FAST resolves
# the smooth critical strains (eps_t, eps_v) to <0.01% at ~4x the speed, for the
# design search inner loop.
RES_FINE = (16.0, 200, 1.0e9)
RES_FAST = (6.0, 60, 160.0)


def analyze(layers: Sequence[ElasticLayer], load: WheelLoad,
            queries: Sequence[Tuple[float, float, bool]],
            *, resolution: Tuple[float, int, float] = RES_FINE) -> List[PointResponse]:
    """Response at many (z, r, lower) query points in a single Hankel sweep.

    The Burmister constants depend only on the layer stack and the transform
    parameter ``m`` -- not on the query point -- so the (expensive) per-``m`` linear
    solve is done once and reused for every query. This is the fast path used by the
    IRC:37 design search, which evaluates the same section at several depths /
    positions. Equivalent to calling :func:`compute_point` per query, but ~N times
    cheaper for N queries. ``resolution`` trades accuracy for speed (see the presets).
    """
    gauss_per_period, panel_floor, r_dm_cap = resolution
    system = _LayeredSystem(layers, load.pressure_mpa, load.contact_radius_mm)
    a = system.a
    # Expand every query into the single-wheel evaluations it needs (two wheels for
    # a dual load), tagged with the layer/zeta of its depth.
    # eval spec: (query_index, wheel_sign, layer_idx, zeta, r)
    specs = []
    for qi, (z, r, lower) in enumerate(queries):
        li, zeta = _locate_layer(system.z_top, layers, z, lower)
        specs.append((qi, +1, li, zeta, abs(r)))
        if load.dual:
            specs.append((qi, -1, li, zeta, abs(load.spacing_mm - r)))
    # accumulators per spec: [sz, trz, uz, ur, sr_j0, sr_j1r, st_j0, st_j1r]
    acc = [[0.0] * 8 for _ in specs]

    # m-grid: cover the shallowest query (largest m_max) at the finest oscillation.
    r_max = min(max((s[4] for s in specs), default=a), r_dm_cap)
    scale = max(a, r_max, 1.0)
    dm = math.pi / (gauss_per_period * scale)
    z_min = min((q[0] for q in queries), default=a)
    decay_depth = max(z_min, 0.05 * a, 1.0)
    m_max = 30.0 / decay_depth
    n_panels = max(panel_floor, int(math.ceil(m_max / dm)))

    m0 = 1e-9
    for p in range(n_panels):
        ma = m0 + p * dm
        mid = ma + 0.5 * dm
        half = 0.5 * dm
        for xg, wg in zip(_GL8_X, _GL8_W):
            m = mid + half * xg
            w = wg * half
            consts = system.solve_constants(m)
            for si, (qi, sign, li, zeta, r) in enumerate(specs):
                amp = system.transformed_response(m, li, zeta, consts[li])
                j0 = bessel_j0(m * r)
                j1 = bessel_j1(m * r)
                j1r = _j1_over_x(m * r) * m
                A = acc[si]
                A[0] += w * amp[0] * j0
                A[1] += w * amp[1] * j1
                A[2] += w * amp[2] * j0
                A[3] += w * amp[3] * j1
                A[4] += w * amp[4] * j0
                A[5] += w * amp[5] * j1r
                A[6] += w * amp[6] * j0
                A[7] += w * amp[7] * j1r

    # combine wheel contributions per query and build responses
    results: List[Optional[PointResponse]] = [None] * len(queries)
    for si, (qi, sign, li, zeta, r) in enumerate(specs):
        z, rq, lower = queries[qi]
        A = acc[si]
        sigma_z = A[0]
        sigma_r = A[4] + A[5]
        sigma_t = A[6] + A[7]
        tau_rz = A[1]
        disp_z = A[2] / system.twoG[li]
        if results[qi] is None:
            results[qi] = [sigma_z, sigma_t, sigma_r, tau_rz, disp_z, li]
        else:
            acc_r = results[qi]
            acc_r[0] += sigma_z
            acc_r[1] += sigma_t
            acc_r[2] += sigma_r
            acc_r[3] += tau_rz          # IITPAVE dual-shear convention (sum)
            acc_r[4] += disp_z
    out: List[PointResponse] = []
    for qi, (z, r, lower) in enumerate(queries):
        sigma_z, sigma_t, sigma_r, tau_rz, disp_z, li = results[qi]
        E = system.layers[li].E_mpa
        nu = system.layers[li].nu
        eps_z = (sigma_z - nu * (sigma_r + sigma_t)) / E
        eps_r = (sigma_r - nu * (sigma_z + sigma_t)) / E
        eps_t = (sigma_t - nu * (sigma_z + sigma_r)) / E
        out.append(PointResponse(z_mm=z, r_mm=r, sigma_z=sigma_z, sigma_t=sigma_t,
                                 sigma_r=sigma_r, tau_rz=tau_rz, disp_z_mm=disp_z,
                                 eps_z=eps_z, eps_t=eps_t, eps_r=eps_r))
    return out


def compute_point(layers: Sequence[ElasticLayer], load: WheelLoad,
                  z_mm: float, r_mm: float, *, lower: bool = False) -> PointResponse:
    """Full stress/strain state at (z, r) for a single- or dual-wheel load.

    ``r_mm`` is measured from the centre of one wheel. For a dual wheel the second
    identical wheel sits at ``load.spacing_mm`` along the line of the two centres,
    and the two axisymmetric fields are superposed along that line (sigma_z, sigma_r
    as sigma_xx, sigma_t as sigma_yy add directly; tau_rz from the second wheel acts
    on the opposite side and subtracts).

    ``lower=True`` reads the response just below an interface (subgrade side), i.e.
    the "L" rows of IITPAVE output.

    Note on ``tau_rz`` for a dual wheel: IITPAVE tabulates the sum of each wheel's
    own radial shear, not the tensor-net tau_xz (which cancels at the mid-point by
    symmetry). We reproduce that convention. It is immaterial to design: the normal
    strains -- which drive fatigue and rutting -- come from the normal stresses only.
    """
    system = _LayeredSystem(layers, load.pressure_mpa, load.contact_radius_mm)
    layer_idx, zeta = _locate_layer(system.z_top, layers, z_mm, lower)
    r1 = abs(r_mm)
    resp = _single_wheel_point(system, z_mm, r1, layer_idx, zeta)
    if not load.dual:
        return resp
    # second wheel along the connecting line, distance |spacing - r| from the point
    r2 = abs(load.spacing_mm - r_mm)
    resp2 = _single_wheel_point(system, z_mm, r2, layer_idx, zeta)
    sigma_z = resp.sigma_z + resp2.sigma_z
    sigma_r = resp.sigma_r + resp2.sigma_r
    sigma_t = resp.sigma_t + resp2.sigma_t
    tau_rz = resp.tau_rz + resp2.tau_rz    # IITPAVE convention (see docstring)
    disp_z = resp.disp_z_mm + resp2.disp_z_mm
    E = system.layers[layer_idx].E_mpa
    nu = system.layers[layer_idx].nu
    eps_z = (sigma_z - nu * (sigma_r + sigma_t)) / E
    eps_r = (sigma_r - nu * (sigma_z + sigma_t)) / E
    eps_t = (sigma_t - nu * (sigma_z + sigma_r)) / E
    return PointResponse(z_mm=z_mm, r_mm=r_mm, sigma_z=sigma_z, sigma_t=sigma_t,
                         sigma_r=sigma_r, tau_rz=tau_rz, disp_z_mm=disp_z,
                         eps_z=eps_z, eps_t=eps_t, eps_r=eps_r)
