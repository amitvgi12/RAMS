"""
FWD -> SNP back-calculation + network-level HDM-4 (per-segment FWD) tests.
"""
import unittest

from rams import api
from rams.batch import forecast_network
from rams.config import RutModelType
from rams.fwd import DEFAULT_DEFLECTION_TO_SNP, DeflectionToSNP, snp_from_deflection
from rams.ingest import ingest_segments_csv_text
from rams.models import SegmentInput
from rams.config import MonsoonZone


class TestFwdToSnp(unittest.TestCase):
    def test_monotonic_decreasing(self):
        # Weaker pavement (higher deflection) -> lower structural number.
        self.assertGreater(snp_from_deflection(0.5), snp_from_deflection(1.0))
        self.assertGreater(snp_from_deflection(1.0), snp_from_deflection(1.5))

    def test_reasonable_range(self):
        self.assertTrue(3.0 < snp_from_deflection(1.0) < 4.0)
        self.assertTrue(4.0 < snp_from_deflection(0.5) < 6.0)

    def test_clamped_to_bounds(self):
        # Extreme deflections cannot push SNP outside the engine's bounds.
        self.assertLessEqual(snp_from_deflection(0.001), 12.0)
        self.assertGreaterEqual(snp_from_deflection(50.0), 0.5)

    def test_custom_model(self):
        m = DeflectionToSNP(coeff_a=4.0, coeff_b=0.5)
        self.assertNotEqual(m.snp(1.0), DEFAULT_DEFLECTION_TO_SNP.snp(1.0))


class TestIngestionAutoDerivesSnp(unittest.TestCase):
    HEADER = ("segment_id,base_iri,base_rut,base_crack,annual_msa,"
              "traffic_growth_rate,monsoon_zone,deflection_mm")

    def test_snp_derived_when_absent(self):
        csv = self.HEADER + "\nA,2,3,1,5,0.05,HIGH,1.2\n"
        s = ingest_segments_csv_text(csv).segments[0]
        self.assertAlmostEqual(s.structural_number, snp_from_deflection(1.2), places=3)

    def test_explicit_snp_not_overridden(self):
        csv = self.HEADER + ",structural_number\nA,2,3,1,5,0.05,HIGH,1.2,5.5\n"
        s = ingest_segments_csv_text(csv).segments[0]
        self.assertEqual(s.structural_number, 5.5)

    def test_no_deflection_keeps_default_snp(self):
        csv = ("segment_id,base_iri,base_rut,base_crack,annual_msa,"
               "traffic_growth_rate,monsoon_zone\nA,2,3,1,5,0.05,HIGH\n")
        s = ingest_segments_csv_text(csv).segments[0]
        self.assertEqual(s.structural_number, 4.0)  # SegmentInput default


class TestNetworkHdm4(unittest.TestCase):
    def _segs(self):
        return [
            SegmentInput(1.5, 2.0, 0.0, 4.5, 0.06, MonsoonZone.HIGH, segment_id="A",
                         length_km=10.0, deflection_mm=0.7, structural_number=4.6),
            SegmentInput(2.5, 5.0, 5.0, 5.0, 0.05, MonsoonZone.MEDIUM, segment_id="B",
                         length_km=12.0, deflection_mm=1.2, structural_number=3.4),
        ]

    def test_network_default_vs_hdm4_differ(self):
        segs = self._segs()
        default = list(forecast_network(segs, 10))
        hdm4 = list(forecast_network(segs, 10, rut_model=RutModelType.HDM4))
        self.assertNotEqual(
            default[0].timeline[-1].rutting_mm, hdm4[0].timeline[-1].rutting_mm
        )

    def test_weaker_segment_ruts_more_under_hdm4(self):
        segs = self._segs()
        hdm4 = list(forecast_network(segs, 10, rut_model=RutModelType.HDM4))
        # B has higher deflection / lower SNP than A -> more rut.
        self.assertGreater(hdm4[1].timeline[-1].rutting_mm, hdm4[0].timeline[-1].rutting_mm)


class TestApiNetworkModel(unittest.TestCase):
    def _payload(self, model):
        return {
            "segments": [
                {"segment_id": "A", "base_iri": 1.5, "base_rut": 2.0, "base_crack": 0.0,
                 "annual_msa": 4.5, "traffic_growth_rate": 0.06, "monsoon_zone": "HIGH",
                 "length_km": 10.0, "deflection_mm": 0.7, "structural_number": 4.6},
                {"segment_id": "B", "base_iri": 2.5, "base_rut": 5.0, "base_crack": 5.0,
                 "annual_msa": 5.0, "traffic_growth_rate": 0.05, "monsoon_zone": "MEDIUM",
                 "length_km": 12.0, "deflection_mm": 1.2, "structural_number": 3.4},
            ],
            "annual_budget": 300, "years": 10, "model": model, "pavement": "dense",
        }

    def test_network_reports_model_and_structural(self):
        out = api.network_and_budget(self._payload("hdm4"))
        self.assertEqual(out["model"]["rut_model"], "HDM4")
        self.assertIn("deflection_mm", out["segments"][0])
        self.assertIn("structural_number", out["segments"][0])

    def test_network_default_model(self):
        out = api.network_and_budget(self._payload("default"))
        self.assertEqual(out["model"]["rut_model"], "DEFAULT")

    def test_forecast_derive_snp(self):
        out = api.forecast_single({"model": "hdm4", "deflection": 1.3, "derive_snp": True, "years": 5})
        self.assertTrue(out["model"]["snp_derived_from_fwd"])
        self.assertAlmostEqual(out["model"]["structural_number"], snp_from_deflection(1.3), places=3)

    def test_forecast_no_derive_keeps_snp(self):
        out = api.forecast_single({"model": "hdm4", "deflection": 1.3, "snp": 4.0, "years": 5})
        self.assertFalse(out["model"]["snp_derived_from_fwd"])
        self.assertEqual(out["model"]["structural_number"], 4.0)


if __name__ == "__main__":
    unittest.main()
