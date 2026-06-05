"""
Treatment re-simulation tests (Lead QA).

Verifies that applying MoRTH catalog reset values keeps a managed asset
healthier than the untreated baseline, and that costs/interventions are
recorded correctly.
"""
import unittest

from rams.config import MonsoonZone
from rams.engine import IndianPavementDeteriorationEngine
from rams.lifecycle import simulate_managed_lifecycle, treatment_cost
from rams.maintenance import TREATMENT_CATALOG
from rams.models import SegmentInput


def spec_segment():
    return SegmentInput(
        base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
        traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
        segment_id="SEG", length_km=10.0,
    )


class TestManagedLifecycle(unittest.TestCase):
    def test_managed_stays_healthier_than_untreated(self):
        untreated = IndianPavementDeteriorationEngine(
            1.5, 2.0, 0.0, 4.5, 0.06, "HIGH"
        ).run_lifecycle_forecast(10)
        managed = simulate_managed_lifecycle(spec_segment(), 10)
        # By the final year the managed PCI must beat the untreated PCI.
        self.assertGreater(managed.timeline[-1].irc82_pci, untreated[-1].irc82_pci)

    def test_interventions_recorded_with_cost(self):
        managed = simulate_managed_lifecycle(spec_segment(), 10)
        self.assertTrue(managed.interventions)  # high-MSA HIGH zone -> needs work
        for iv in managed.interventions:
            self.assertGreater(iv.cost, 0)
            self.assertGreaterEqual(iv.pci_after, iv.pci_before)
        self.assertAlmostEqual(
            managed.total_cost, round(sum(i.cost for i in managed.interventions), 2)
        )

    def test_treatment_interval_respected(self):
        managed = simulate_managed_lifecycle(
            spec_segment(), 10, min_treatment_interval=3
        )
        years = [iv.year for iv in managed.interventions]
        for a, b in zip(years, years[1:]):
            self.assertGreaterEqual(b - a, 3)

    def test_routine_segment_needs_no_treatment(self):
        calm = SegmentInput(
            base_iri=1.0, base_rut=1.0, base_crack=0.0, annual_msa=0.5,
            traffic_growth_rate=0.0, monsoon_zone=MonsoonZone.LOW, length_km=5.0,
        )
        managed = simulate_managed_lifecycle(calm, 10)
        self.assertEqual(managed.interventions, [])
        self.assertEqual(managed.total_cost, 0.0)


class TestTreatmentCost(unittest.TestCase):
    def test_cost_scales_with_length_and_relative_cost(self):
        micro = TREATMENT_CATALOG["MICROSURFACING"]
        self.assertEqual(treatment_cost(micro, 10.0, 30.0), 300.0)  # 1.0*30*10
        mill = TREATMENT_CATALOG["MILL_AND_OVERLAY"]
        self.assertEqual(treatment_cost(mill, 10.0, 30.0), 1500.0)  # 5.0*30*10


if __name__ == "__main__":
    unittest.main()
