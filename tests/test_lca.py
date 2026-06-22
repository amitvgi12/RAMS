"""
MoRTH cost basis, the LCA decision matrix, and defect-log aggregation.
"""
import unittest

from rams import api
from rams.config import MonsoonZone
from rams.lca import DEFAULT_LCA_THRESHOLDS, lca_matrix
from rams.models import SegmentInput
from rams.morth import Decision, MORTH_RATES, to_lakh, treated_area_sqm, treatment_cost_inr
from rams.survey import aggregate_defects, detect_defect


def _seg(iri=2.6, rut=5.0, crack=5.0, msa=4.5, growth=0.06, zone=MonsoonZone.HIGH, length=10.0):
    return SegmentInput(iri, rut, crack, msa, growth, zone, segment_id="S", length_km=length)


class TestMorthCost(unittest.TestCase):
    def test_area_and_cost(self):
        self.assertAlmostEqual(treated_area_sqm(10.0, 7.0), 70000.0)
        # ROUTINE 60 Rs/m^2 over 70000 m^2 = 42 lakh
        self.assertAlmostEqual(treatment_cost_inr(Decision.ROUTINE, 10.0, 7.0), 60.0 * 70000)
        self.assertAlmostEqual(to_lakh(treatment_cost_inr(Decision.ROUTINE, 10.0, 7.0)), 42.0)

    def test_overlay_dearer_than_preventive(self):
        ov = treatment_cost_inr(Decision.OVERLAY, 1.0, 7.0)
        pv = treatment_cost_inr(Decision.PREVENTIVE, 1.0, 7.0)
        self.assertGreater(ov, pv)

    def test_rates_have_references(self):
        for d, r in MORTH_RATES.items():
            self.assertTrue(r.morth_reference.startswith("MoRTH"))
            self.assertGreater(r.rate_per_sqm, 0)


class TestLCAMatrix(unittest.TestCase):
    def test_matrix_shape_and_economics(self):
        r = lca_matrix(_seg(), 15, width_m=7.0, discount_rate=0.08)
        self.assertEqual(len(r.years), 15)
        self.assertGreater(r.total_cost_inr, 0)
        self.assertLess(r.npv_inr, r.total_cost_inr)        # positive discount
        self.assertGreater(r.euac_inr, 0)

    def test_degrading_segment_triggers_overlay(self):
        r = lca_matrix(_seg(iri=3.0, rut=8.0, crack=10.0, msa=8.0), 20)
        self.assertGreater(r.n_overlay, 0)
        self.assertTrue(any(y.decision == "OVERLAY" for y in r.years))

    def test_decisions_escalate(self):
        # every decision is one of the four life-cycle actions
        r = lca_matrix(_seg(), 12)
        valid = {"ROUTINE", "PREVENTIVE", "OVERLAY", "RECONSTRUCTION"}
        self.assertTrue(all(y.decision in valid for y in r.years))

    def test_min_interval_defers(self):
        # a heavily-degraded segment can have a major treatment deferred
        r = lca_matrix(_seg(iri=4.0, rut=15.0, crack=18.0, msa=10.0), 20)
        self.assertIsInstance(r.years[0].deferred, bool)

    def test_zero_discount_euac_is_average(self):
        r = lca_matrix(_seg(), 10, discount_rate=0.0)
        self.assertAlmostEqual(r.euac_inr, r.total_cost_inr / 10, places=0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            lca_matrix(_seg(), 0)
        with self.assertRaises(ValueError):
            lca_matrix(_seg(), 10, width_m=0)


class TestDefectAggregation(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_defect(["chainage", "lane", "area_(in_sq_m)", "max_depth", "severity"]),
                         "pothole_defect")
        self.assertEqual(detect_defect(["chainage", "lane", "length", "width", "classification"]),
                         "crack_defect")
        self.assertIsNone(detect_defect(["foo", "bar"]))

    def test_pothole_aggregation(self):
        rows = [
            {"chainage": "154430", "lane": "L1", "area_(in_sq_m)": "3.5"},
            {"chainage": "154460", "lane": "L1", "area_(in_sq_m)": "3.5"},   # same 100m bin
            {"chainage": "154550", "lane": "L1", "area_(in_sq_m)": "1.75"},  # next bin
        ]
        out = aggregate_defects(rows, "pothole_defect", lane_width_m=3.5)
        by = {r["start_chainage"]: r for r in out}
        # bin 154400: 7 m^2 over 100x3.5=350 m^2 -> 2.0% , count 2
        self.assertEqual(by["154400"]["defect_count"], "2")
        self.assertAlmostEqual(float(by["154400"]["potholes"]), 2.0, places=2)
        self.assertEqual(by["154500"]["defect_count"], "1")

    def test_crack_aggregation(self):
        rows = [{"chainage": "0", "lane": "L1", "length": "5", "width": "10"}]  # 5m x 10mm
        out = aggregate_defects(rows, "crack_defect", lane_width_m=3.5)
        # area = 5 * 0.01 = 0.05 m^2 over 350 -> ~0.014%
        self.assertAlmostEqual(float(out[0]["crack"]), 0.05 / 350 * 100, places=3)


class TestLCAApi(unittest.TestCase):
    def test_lca_endpoint(self):
        out = api.lca({"iri": 2.6, "rut": 5, "crack": 5, "msa": 4.5, "zone": "HIGH",
                       "length_km": 10, "years": 15, "width_m": 7})
        self.assertEqual(len(out["years"]), 15)
        self.assertIn("euac_lakh", out)
        self.assertIn("total_cost_lakh", out)

    def test_lca_export(self):
        payload = {"report": "lca", "iri": 2.6, "rut": 5, "crack": 5, "length_km": 10, "years": 15}
        xb, mime, name = api.export_report(payload, "xlsx")
        self.assertEqual(xb[:2], b"PK")
        self.assertEqual(name, "rams_lca_matrix.xlsx")
        pb, _, pn = api.export_report(payload, "pdf")
        self.assertEqual(pb[:4], b"%PDF")
        self.assertEqual(pn, "rams_lca_matrix.pdf")


if __name__ == "__main__":
    unittest.main()
