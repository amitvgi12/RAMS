"""
Budget optimisation + API-layer tests (Lead QA + Security Lead).
"""
import unittest

from rams import api
from rams.batch import forecast_network
from rams.config import MonsoonZone
from rams.models import SegmentInput
from rams.optimize import BudgetParams, optimize_budget


def demo_segments():
    return [
        SegmentInput(**{**dict(base_iri=1.6, base_rut=2.5, base_crack=0.5,
                               traffic_growth_rate=0.05, monsoon_zone=MonsoonZone.HIGH),
                        **s}).validate()
        for s in (
            dict(segment_id="A", annual_msa=8.0, length_km=10.0),
            dict(segment_id="B", annual_msa=5.0, length_km=12.0),
            dict(segment_id="C", annual_msa=2.0, length_km=20.0),
        )
    ]


class TestOptimizer(unittest.TestCase):
    def test_schedules_within_budget_and_window(self):
        segs = demo_segments()
        fcs = list(forecast_network(segs, 10))
        params = BudgetParams(annual_budget=600.0, horizon_years=10, base_unit_cost=30.0)
        plan = optimize_budget(segs, fcs, params)
        # No year may exceed the annual budget.
        for y, spend in plan.spend_by_year.items():
            self.assertLessEqual(spend, params.annual_budget + 1e-6)
        # Each scheduled treatment must fall inside its segment's window.
        win = {fc.segment_id: fc.plan for fc in fcs}
        for s in plan.scheduled:
            p = win[s.segment_id]
            self.assertGreaterEqual(s.year, p.preventive_window_year)
            if p.window_expired_year is not None:
                self.assertLessEqual(s.year, p.window_expired_year - 1)

    def test_busiest_corridor_prioritised_under_scarcity(self):
        segs = demo_segments()
        fcs = list(forecast_network(segs, 10))
        # Tiny budget: only one segment per year can be funded.
        params = BudgetParams(annual_budget=300.0, horizon_years=10, base_unit_cost=30.0)
        plan = optimize_budget(segs, fcs, params)
        funded_ids = {s.segment_id for s in plan.scheduled}
        # 'A' has the highest exposure (8 MSA x 10 km) -> must be funded.
        self.assertIn("A", funded_ids)

    def test_zero_budget_funds_nothing(self):
        segs = demo_segments()
        fcs = list(forecast_network(segs, 10))
        plan = optimize_budget(segs, fcs, BudgetParams(annual_budget=0.0))
        self.assertEqual(plan.scheduled, [])
        self.assertTrue(plan.unfunded)

    def test_invalid_params_rejected(self):
        with self.assertRaises(ValueError):
            BudgetParams(annual_budget=-1.0)
        with self.assertRaises(ValueError):
            BudgetParams(annual_budget=100.0, base_unit_cost=0.0)


class TestApiLayer(unittest.TestCase):
    def test_forecast_single_shape(self):
        out = api.forecast_single({"zone": "HIGH"})
        self.assertEqual(len(out["untreated"]), 10)
        self.assertEqual(len(out["treated"]), 10)
        self.assertIn("rationale", out["plan"])
        self.assertIn("preventive_upper", out["bands"])

    def test_forecast_single_rejects_bad_zone(self):
        with self.assertRaises(ValueError):
            api.forecast_single({"zone": "TYPO"})

    def test_network_and_budget_shape(self):
        out = api.network_and_budget(api.default_network() | {"annual_budget": 600})
        self.assertEqual(len(out["segments"]), 8)
        self.assertIn("net_savings", out["budget"])
        self.assertIn("scheduled", out["budget"])

    def test_network_rejects_empty(self):
        with self.assertRaises(ValueError):
            api.network_and_budget({"segments": []})

    def test_network_rejects_too_many(self):
        big = {"segments": [{"segment_id": str(i)} for i in range(api.MAX_NETWORK_SEGMENTS + 1)]}
        with self.assertRaises(ValueError):
            api.network_and_budget(big)


if __name__ == "__main__":
    unittest.main()
