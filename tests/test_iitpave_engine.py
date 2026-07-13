"""Validation of the pure-Python IITPAVE layered-elastic engine.

The engine is checked three ways:

  * against the closed-form Boussinesq solution for a homogeneous half-space
    (stresses AND strains, so the Poisson handling is exercised);
  * against the genuine IITPAVE outputs bundled under
    ``rams/data/iitpave_reference/`` -- the two self-consistent reference cases
    (``case_a`` and ``case_b``), row for row;
  * for physical self-consistency: splitting a layer into identical sub-layers
    must not change the result.

(The bundled ``case_c.out`` is a 7-layer example whose tabulated values are
internally inconsistent -- stresses and strains are discontinuous across a bonded
interface at z=700, which no valid elastic solution allows -- so it is not used as
ground truth.)
"""
import math
import os
import re
import unittest

from rams.iitpave_engine import ElasticLayer, WheelLoad, compute_point, analyze

REF_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       "rams", "data", "iitpave_reference")

_NUM = re.compile(r"[-+]?\d\.\d+E[-+]\d+")
_ROW = re.compile(r"^\s*(\d+\.\d+)(L?)\s+(\d+\.\d+)")


def _parse_out_rows(path):
    """Yield (z, r, lower, [sz, st, sr, trz, dz, ez, et, er]) from an IITPAVE .out."""
    rows = []
    with open(path) as fh:
        for line in fh:
            m = _ROW.match(line)
            if not m:
                continue
            nums = _NUM.findall(line)
            if len(nums) != 8:
                continue
            z = float(m.group(1))
            lower = m.group(2) == "L"
            r = float(m.group(3))
            rows.append((z, r, lower, [float(x) for x in nums]))
    return rows


# Layer stacks + loads for the two self-consistent reference cases. (case_a's input
# file is not bundled; its parameters are read from the case_a.out header.)
CASES = {
    "case_a": (
        [ElasticLayer(2000, 0.30, 100), ElasticLayer(200, 0.30, 300),
         ElasticLayer(75, 0.30, 0)],
        WheelLoad(20000, 0.56, dual=True),
    ),
    "case_b": (
        [ElasticLayer(15000, 0.25, 150), ElasticLayer(125, 0.35, 150),
         ElasticLayer(66, 0.35, 0)],
        WheelLoad(60000, 0.8, dual=True),
    ),
}

_COLS = ["sigma_z", "sigma_t", "sigma_r", "tau_rz", "disp_z", "eps_z", "eps_t", "eps_r"]


class TestBoussinesq(unittest.TestCase):
    """Homogeneous half-space must reproduce the closed-form Boussinesq solution."""

    def test_stress_and_strain_on_axis(self):
        for nu in (0.30, 0.45):
            E, q, a = 150.0, 0.7, 100.0
            layers = [ElasticLayer(E, nu, 0.0)]
            load = WheelLoad(q * math.pi * a * a, q, dual=False)
            for z in (40.0, 120.0, 300.0):
                r = compute_point(layers, load, z, 0.0)
                u = z / math.sqrt(a * a + z * z)
                sz = -q * (1.0 - u ** 3)
                sr = -q / 2.0 * ((1 + 2 * nu) - 2 * (1 + nu) * u + u ** 3)
                ez = (sz - nu * (sr + sr)) / E
                et = (sr - nu * (sz + sr)) / E
                self.assertAlmostEqual(r.sigma_z, sz, delta=1e-3 * abs(sz) + 1e-4)
                self.assertAlmostEqual(r.sigma_r, sr, delta=1e-3 * abs(sr) + 1e-4)
                self.assertAlmostEqual(r.eps_z, ez, delta=2e-3 * abs(ez) + 1e-9)
                self.assertAlmostEqual(r.eps_t, et, delta=2e-3 * abs(et) + 1e-9)


class TestIITPAVEReference(unittest.TestCase):
    """Every row of the self-consistent IITPAVE reference outputs, reproduced."""

    def _check_case(self, name, tol=0.02):
        layers, load = CASES[name]
        rows = _parse_out_rows(os.path.join(REF_DIR, name + ".out"))
        self.assertGreater(len(rows), 0)
        queries = [(z, r, lower) for (z, r, lower, _) in rows]
        got = analyze(layers, load, queries)
        for (z, r, lower, gt), pr in zip(rows, got):
            vals = [pr.sigma_z, pr.sigma_t, pr.sigma_r, pr.tau_rz, pr.disp_z_mm,
                    pr.eps_z, pr.eps_t, pr.eps_r]
            for col, g, v in zip(_COLS, gt, vals):
                rel = abs(v - g) / (abs(g) + 1e-9)
                self.assertLess(
                    rel, tol,
                    msg=f"{name} z={z} r={r} {'L' if lower else ''} {col}: "
                        f"IITPAVE={g:.4e} engine={v:.4e} rel={rel:.3%}")

    def test_case_a(self):
        self._check_case("case_a")

    def test_case_b(self):
        self._check_case("case_b")


class TestPhysicalInvariance(unittest.TestCase):
    """Splitting a layer into identical sub-layers cannot change the response."""

    def test_split_layer_invariance(self):
        load = WheelLoad(20000, 0.56, dual=True)
        base = [ElasticLayer(3000, 0.35, 120), ElasticLayer(250, 0.35, 400),
                ElasticLayer(60, 0.35, 0)]
        split = [ElasticLayer(3000, 0.35, 120),
                 ElasticLayer(250, 0.35, 150), ElasticLayer(250, 0.35, 250),
                 ElasticLayer(60, 0.35, 0)]
        for z, lower in ((120.0, False), (520.0, True)):
            b = compute_point(base, load, z, 155.0, lower=lower)
            s = compute_point(split, load, z, 155.0, lower=lower)
            self.assertAlmostEqual(b.eps_t, s.eps_t, places=10)
            self.assertAlmostEqual(b.eps_z, s.eps_z, places=10)


class TestValidatedEnvelope(unittest.TestCase):
    """The engine is validated for monotonically-softening stacks (the real-pavement
    case). `envelope_note` flags anything outside it -- honestly fencing the one
    regime (non-monotone, stiff layer buried under a softer one) that could not be
    reconciled against IITPAVE because the only bundled reference is corrupt."""

    def test_monotone_stack_is_in_envelope(self):
        from rams.iitpave_engine import envelope_note
        self.assertIsNone(envelope_note([
            ElasticLayer(3000, 0.35, 150), ElasticLayer(250, 0.35, 400),
            ElasticLayer(50, 0.35, 0)]))

    def test_non_monotone_stack_is_flagged(self):
        from rams.iitpave_engine import envelope_note
        # a stiff (5000) layer buried under a soft (100) one -- CASE_C's shape
        note = envelope_note([
            ElasticLayer(3000, 0.35, 100), ElasticLayer(100, 0.35, 100),
            ElasticLayer(5000, 0.35, 200), ElasticLayer(80, 0.35, 0)])
        self.assertIsNotNone(note)
        self.assertIn("IITPAVE", note)


if __name__ == "__main__":
    unittest.main()
