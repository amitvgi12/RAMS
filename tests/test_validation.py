"""
Input-validation & security tests (Security Lead + Lead QA).

These guard the trust boundary: nothing invalid should reach the math.
"""
import math
import unittest

from rams.config import MonsoonZone
from rams.engine import IndianPavementDeteriorationEngine
from rams.models import SegmentInput


def good(**overrides):
    base = dict(
        base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
        traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
    )
    base.update(overrides)
    return SegmentInput(**base)


class TestRejectsBadNumbers(unittest.TestCase):
    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            good(base_iri=float("nan")).validate()

    def test_rejects_inf(self):
        with self.assertRaises(ValueError):
            good(annual_msa=float("inf")).validate()

    def test_rejects_negative_msa(self):
        with self.assertRaises(ValueError):
            good(annual_msa=-1.0).validate()

    def test_rejects_out_of_range_iri(self):
        with self.assertRaises(ValueError):
            good(base_iri=999.0).validate()

    def test_rejects_nonnumeric(self):
        with self.assertRaises(ValueError):
            good(base_rut="not-a-number").validate()


class TestMonsoonZoneStrict(unittest.TestCase):
    def test_unknown_zone_raises(self):
        # The prototype silently fell back to MEDIUM; we must fail loud.
        with self.assertRaises(ValueError):
            MonsoonZone.from_str("HGIH")

    def test_engine_rejects_bad_zone(self):
        with self.assertRaises(ValueError):
            IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "TYPO")

    def test_case_insensitive_and_trimmed(self):
        self.assertEqual(MonsoonZone.from_str("  high "), MonsoonZone.HIGH)


class TestHorizonGuard(unittest.TestCase):
    def test_rejects_zero_horizon(self):
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        with self.assertRaises(ValueError):
            e.run_lifecycle_forecast(0)

    def test_rejects_absurd_horizon(self):
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        with self.assertRaises(ValueError):
            e.run_lifecycle_forecast(10_000)

    def test_rejects_bool_horizon(self):
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        with self.assertRaises(ValueError):
            e.run_lifecycle_forecast(True)


class TestSegmentIdSanitisation(unittest.TestCase):
    def test_blank_id_defaults(self):
        self.assertEqual(good(segment_id="   ").validate().segment_id, "SEGMENT")

    def test_overlong_id_rejected(self):
        with self.assertRaises(ValueError):
            good(segment_id="x" * 200).validate()


if __name__ == "__main__":
    unittest.main()
