"""
Maintenance decision-logic tests (Lead QA).

Validates Section 4: band classification, preventive-window detection,
window-expiry detection and treatment recommendation.
"""
import unittest

from rams.engine import IndianPavementDeteriorationEngine
from rams.maintenance import (
    MaintenanceFlag,
    MaintenancePolicy,
    TREATMENT_CATALOG,
    build_maintenance_plan,
)
from rams.models import YearResult


def fake_timeline(pcis):
    return [
        YearResult(year=i + 1, cumulative_msa=0.0, iri=0.0, rutting_mm=0.0,
                   cracking_pct=0.0, irc82_pci=p)
        for i, p in enumerate(pcis)
    ]


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.policy = MaintenancePolicy()

    def test_bands(self):
        self.assertEqual(self.policy.classify(3.5), MaintenanceFlag.ROUTINE)
        self.assertEqual(self.policy.classify(3.20), MaintenanceFlag.ROUTINE)
        self.assertEqual(self.policy.classify(2.9), MaintenanceFlag.PREVENTIVE)
        self.assertEqual(self.policy.classify(2.50), MaintenanceFlag.PREVENTIVE)
        self.assertEqual(self.policy.classify(2.49), MaintenanceFlag.STRUCTURAL)

    def test_invalid_policy_rejected(self):
        with self.assertRaises(ValueError):
            MaintenancePolicy(preventive_upper=2.0, structural_lower=2.5)


class TestPlanFromSpecEngine(unittest.TestCase):
    def test_window_year_5_expiry_year_7(self):
        # Matches the verified golden lifecycle.
        e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        tl = e.run_lifecycle_forecast(10)
        plan = build_maintenance_plan(tl)
        self.assertEqual(plan.preventive_window_year, 5)
        self.assertEqual(plan.window_expired_year, 7)
        self.assertEqual(plan.recommended_year, 5)
        self.assertEqual(
            plan.recommended_treatment, TREATMENT_CATALOG["MICROSURFACING"]
        )


class TestPlanEdgeCases(unittest.TestCase):
    def test_routine_only(self):
        plan = build_maintenance_plan(fake_timeline([3.9, 3.8, 3.7]))
        self.assertIsNone(plan.preventive_window_year)
        self.assertIsNone(plan.window_expired_year)
        self.assertIsNone(plan.recommended_treatment)

    def test_straight_to_structural(self):
        plan = build_maintenance_plan(fake_timeline([3.9, 3.5, 2.0, 1.5]))
        self.assertEqual(plan.window_expired_year, 3)
        self.assertEqual(
            plan.recommended_treatment, TREATMENT_CATALOG["MILL_AND_OVERLAY"]
        )

    def test_empty_timeline_raises(self):
        with self.assertRaises(ValueError):
            build_maintenance_plan([])

    def test_treatment_relative_costs_ordered(self):
        # Structural must be the most expensive (drives budget optimisation).
        routine = TREATMENT_CATALOG["ROUTINE_CRACK_SEAL"].relative_cost
        prev = TREATMENT_CATALOG["MICROSURFACING"].relative_cost
        struct = TREATMENT_CATALOG["MILL_AND_OVERLAY"].relative_cost
        self.assertLess(routine, prev)
        self.assertLess(prev, struct)


if __name__ == "__main__":
    unittest.main()
