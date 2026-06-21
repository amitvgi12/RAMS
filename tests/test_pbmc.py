"""
Performance-Based Maintenance Contract estimator (pbmc.py) + /api/pbmc.
"""
import unittest

from rams import api
from rams.config import MonsoonZone
from rams.models import SegmentInput
from rams.pbmc import (
    PBMCParams,
    estimate_pbmc,
    estimate_pbmc_network,
)


def _seg(iri=1.5, rut=2.0, crack=0.0, msa=4.5, growth=0.06, zone=MonsoonZone.HIGH,
         sid="SEG", length=10.0):
    return SegmentInput(iri, rut, crack, msa, growth, zone, segment_id=sid, length_km=length)


class TestPBMCParams(unittest.TestCase):
    def test_guards(self):
        with self.assertRaises(ValueError):
            PBMCParams(term_years=0)
        with self.assertRaises(ValueError):
            PBMCParams(term_years=20)
        with self.assertRaises(ValueError):
            PBMCParams(performance_pci=5.0)
        with self.assertRaises(ValueError):
            PBMCParams(base_unit_cost=0)

    def test_policy_trigger_is_service_level(self):
        pol = PBMCParams(performance_pci=3.2).policy()
        self.assertEqual(pol.preventive_upper, 3.2)
        self.assertLess(pol.structural_lower, pol.preventive_upper)


class TestEstimatePBMC(unittest.TestCase):
    def test_term_length_matches(self):
        for term in (5, 6, 7):
            est = estimate_pbmc(_seg(), PBMCParams(term_years=term))
            self.assertEqual(len(est.years), term)
            self.assertEqual(est.term_years, term)

    def test_good_segment_no_initial_rectification(self):
        est = estimate_pbmc(_seg(), PBMCParams(term_years=5, performance_pci=3.0))
        self.assertEqual(est.initial_rectification, 0.0)
        self.assertIsNone(est.initial_treatment)
        self.assertGreater(est.contract_value, 0)

    def test_degraded_segment_triggers_initial_rectification(self):
        # Very rough/cracked road starts below a 3.2 service level -> rectify at handover.
        est = estimate_pbmc(_seg(iri=5.5, rut=14.0, crack=35.0),
                            PBMCParams(term_years=5, performance_pci=3.2))
        self.assertGreater(est.initial_rectification, 0.0)
        self.assertIsNotNone(est.initial_treatment)
        self.assertEqual(est.years[0].initial, est.initial_rectification)

    def test_renewals_scheduled_to_hold_service_level(self):
        est = estimate_pbmc(_seg(iri=3.0, rut=6.0, crack=8.0, msa=6.0, length=20.0),
                            PBMCParams(term_years=7, performance_pci=3.2, min_treatment_interval=2))
        self.assertGreater(len(est.interventions), 0)
        self.assertGreater(est.total_periodic, 0)

    def test_npv_below_nominal_with_positive_discount(self):
        est = estimate_pbmc(_seg(), PBMCParams(term_years=7, discount_rate=0.10))
        self.assertLess(est.npv, est.contract_value)

    def test_escalation_increases_yearly_cost(self):
        est = estimate_pbmc(_seg(), PBMCParams(term_years=5, escalation_rate=0.06))
        escalated = [y.escalated for y in est.years]
        self.assertEqual(escalated, sorted(escalated))  # non-decreasing under escalation
        self.assertGreater(escalated[-1], escalated[0])

    def test_monsoon_zone_raises_routine_cost(self):
        high = estimate_pbmc(_seg(zone=MonsoonZone.HIGH), PBMCParams(term_years=5))
        low = estimate_pbmc(_seg(zone=MonsoonZone.LOW), PBMCParams(term_years=5))
        self.assertGreater(high.total_routine, low.total_routine)

    def test_loadings_increase_contract_value(self):
        bare = estimate_pbmc(_seg(), PBMCParams(term_years=5, escalation_rate=0.0,
                                                contingency_pct=0.0, overhead_pct=0.0))
        loaded = estimate_pbmc(_seg(), PBMCParams(term_years=5, escalation_rate=0.0,
                                                  contingency_pct=0.10, overhead_pct=0.10))
        self.assertGreater(loaded.contract_value, bare.contract_value)
        # With no loadings/escalation the contract value equals the nominal total.
        self.assertAlmostEqual(bare.contract_value, bare.total_nominal, places=2)

    def test_cost_per_km(self):
        est = estimate_pbmc(_seg(length=10.0), PBMCParams(term_years=5))
        self.assertAlmostEqual(est.cost_per_km, est.contract_value / 10.0, places=4)


class TestPBMCNetwork(unittest.TestCase):
    def test_aggregate_sums_segments(self):
        segs = [_seg(sid="A", length=10.0), _seg(sid="B", rut=6.0, length=8.0)]
        net = estimate_pbmc_network(segs, PBMCParams(term_years=5))
        self.assertEqual(net.n_segments, 2)
        self.assertAlmostEqual(net.contract_value,
                               sum(e.contract_value for e in net.segments), places=2)
        self.assertAlmostEqual(net.total_length_km, 18.0, places=4)


class TestPBMCAPI(unittest.TestCase):
    def test_single(self):
        out = api.pbmc({"iri": 1.5, "rut": 2.0, "crack": 0.0, "msa": 4.5,
                        "growth": 0.06, "zone": "HIGH", "id": "X", "length_km": 12,
                        "term_years": 5})
        self.assertEqual(len(out["years"]), 5)
        self.assertGreater(out["contract_value"], 0)

    def test_network(self):
        out = api.pbmc({"segments": api.DEFAULT_NETWORK, "term_years": 7,
                        "performance_pci": 3.2})
        self.assertEqual(out["n_segments"], len(api.DEFAULT_NETWORK))
        self.assertIn("non_compliant", out)
        self.assertGreater(out["contract_value"], 0)


if __name__ == "__main__":
    unittest.main()
