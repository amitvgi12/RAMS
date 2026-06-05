"""
Engine correctness tests (Lead QA).

Covers: golden lifecycle values, determinism, deterioration caps, the IRC:82
scoring boundaries, the cracking lag-phase boundary, monsoon multiplier
sensitivity, traffic-growth compounding, and the treatment reset hook.

Pure unittest -- runs with `python -m unittest` and zero third-party deps.
"""
import math
import unittest

from rams.config import MonsoonZone
from rams.engine import IndianPavementDeteriorationEngine


def spec_engine():
    return IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")


# Golden values produced by the verified stdlib engine (rounded as exported).
GOLDEN = [
    # year, cum_msa, iri, rut, crack, pci
    (1, 4.50, 1.66, 3.5, 0.5, 4.00),
    (2, 9.27, 1.83, 5.0, 0.9, 4.00),
    (3, 14.33, 2.01, 6.5, 1.4, 3.86),
    (4, 19.69, 2.21, 8.2, 8.6, 3.54),
    (5, 25.37, 2.42, 9.9, 17.0, 3.02),
    (6, 31.39, 2.65, 11.7, 26.4, 2.51),
    (7, 37.77, 2.89, 13.5, 37.1, 2.29),
    (8, 44.54, 3.16, 15.5, 48.8, 2.06),
    (9, 51.71, 3.44, 17.5, 61.6, 1.85),
    (10, 59.31, 3.74, 19.6, 75.5, 1.78),
]


class TestGoldenLifecycle(unittest.TestCase):
    def test_matches_golden_table(self):
        tl = spec_engine().run_lifecycle_forecast(10)
        self.assertEqual(len(tl), 10)
        for yr, (g_year, g_cum, g_iri, g_rut, g_crack, g_pci) in zip(tl, GOLDEN):
            r = yr.as_row()
            self.assertEqual(r["Year"], g_year)
            self.assertEqual(r["Cumulative_MSA"], g_cum)
            self.assertEqual(r["IRI"], g_iri)
            self.assertEqual(r["Rutting_mm"], g_rut)
            self.assertEqual(r["Cracking_Pct"], g_crack)
            self.assertEqual(r["IRC82_PCI"], g_pci)

    def test_determinism(self):
        a = [y.as_row() for y in spec_engine().run_lifecycle_forecast(10)]
        b = [y.as_row() for y in spec_engine().run_lifecycle_forecast(10)]
        self.assertEqual(a, b)


class TestDeteriorationCaps(unittest.TestCase):
    def test_kpis_never_exceed_caps(self):
        # Brutal loading for many years should saturate at the caps, not blow up.
        e = IndianPavementDeteriorationEngine(10.0, 30.0, 90.0, 500.0, 0.0, "HIGH")
        for _ in range(50):
            r = e.simulate_year()
            self.assertLessEqual(r.iri, e.calibration.iri_cap + 1e-9)
            self.assertLessEqual(r.rutting_mm, e.calibration.rut_cap + 1e-9)
            self.assertLessEqual(r.cracking_pct, e.calibration.crack_cap + 1e-9)

    def test_kpis_monotonic_nondecreasing_default(self):
        # With non-negative growth, distresses never improve on their own.
        e = spec_engine()
        prev = (e.iri, e.rut, e.crack)
        for _ in range(10):
            e.simulate_year()
            self.assertGreaterEqual(e.iri, prev[0] - 1e-9)
            self.assertGreaterEqual(e.rut, prev[1] - 1e-9)
            self.assertGreaterEqual(e.crack, prev[2] - 1e-9)
            prev = (e.iri, e.rut, e.crack)


class TestIRC82Scoring(unittest.TestCase):
    def test_perfect_condition(self):
        e = spec_engine()
        self.assertEqual(e.calculate_irc82_pci(1.0, 0.0, 0.0), 4.0)

    def test_score_floor_is_one(self):
        e = spec_engine()
        # Extreme distress: all sub-scores floor at 1.0 -> composite 1.0.
        self.assertEqual(e.calculate_irc82_pci(100.0, 100.0, 100.0), 1.0)

    def test_threshold_boundaries(self):
        e = spec_engine()
        # Exactly at free thresholds the deduction is zero (still perfect-ish).
        self.assertEqual(e.calculate_irc82_pci(1.99, 4.99, 4.99), 4.0)


class TestCrackingLagPhase(unittest.TestCase):
    def test_lag_then_accelerate(self):
        e = spec_engine()
        results = [e.simulate_year() for _ in range(5)]
        # Years 1-3 use the gentle lag growth; year 4 jumps (S-curve onset).
        delta_y3 = results[2].cracking_pct - results[1].cracking_pct
        delta_y4 = results[3].cracking_pct - results[2].cracking_pct
        self.assertGreater(delta_y4, delta_y3 * 3)


class TestMonsoonSensitivity(unittest.TestCase):
    def test_high_zone_deteriorates_faster(self):
        high = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        low = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "LOW")
        hi = high.run_lifecycle_forecast(10)[-1]
        lo = low.run_lifecycle_forecast(10)[-1]
        self.assertGreater(hi.rutting_mm, lo.rutting_mm)
        self.assertGreater(hi.iri, lo.iri)
        self.assertLessEqual(hi.irc82_pci, lo.irc82_pci)


class TestTrafficGrowth(unittest.TestCase):
    def test_annual_msa_compounds(self):
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 10.0, 0.10, "LOW")
        e.simulate_year()
        self.assertTrue(math.isclose(e.annual_msa, 11.0, rel_tol=1e-9))
        e.simulate_year()
        self.assertTrue(math.isclose(e.annual_msa, 12.1, rel_tol=1e-9))

    def test_cumulative_msa_accumulates(self):
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.0, "LOW")
        tl = e.run_lifecycle_forecast(3)
        self.assertAlmostEqual(tl[-1].cumulative_msa, 13.5, places=6)


class TestTreatmentReset(unittest.TestCase):
    def test_apply_reset_clamps_and_sets(self):
        e = spec_engine()
        e.run_lifecycle_forecast(6)
        e.apply_reset(iri=1.8, rut=2.0, crack=0.0)
        self.assertEqual(e.iri, 1.8)
        self.assertEqual(e.rut, 2.0)
        self.assertEqual(e.crack, 0.0)

    def test_reset_none_leaves_value(self):
        e = spec_engine()
        e.run_lifecycle_forecast(6)
        before = e.rut
        e.apply_reset(crack=0.0)  # rut untouched
        self.assertEqual(e.rut, before)


if __name__ == "__main__":
    unittest.main()
