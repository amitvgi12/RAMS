"""
Cracking + roughness alternative models, their calibration, and the network
residual-life / handback wiring.
"""
import unittest

from rams import api
from rams.config import CrackModelType, RoughnessModelType
from rams.calibrate import (
    RoughnessObservation,
    calibrate_hdm4_roughness,
    calibrate_mlit_cracking,
    cracking_pairs_from_series,
)
from rams.distress import (
    DEFAULT_HDM4_ROUGHNESS,
    MLIT_CRACK_DENSE,
    MLIT_CRACK_POROUS,
    HDM4RoughnessModel,
    MLITCrackModel,
    mlit_crack_preset,
)
from rams.engine import IndianPavementDeteriorationEngine as Engine


def eng(**over):
    base = dict(base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
                traffic_growth_rate=0.06, monsoon_zone="HIGH")
    base.update(over)
    return Engine(**base)


class TestDefaultUnchanged(unittest.TestCase):
    def test_golden_year1_intact(self):
        y = eng().run_lifecycle_forecast(1)[0]
        self.assertAlmostEqual(y.rutting_mm, 3.5, places=1)
        self.assertEqual(y.irc82_pci, 4.00)

    def test_golden_full_curve_intact(self):
        tl = eng().run_lifecycle_forecast(10)
        self.assertEqual(round(tl[-1].iri, 2), 3.74)
        self.assertEqual(round(tl[-1].cracking_pct, 1), 75.5)
        self.assertEqual(tl[-1].irc82_pci, 1.78)


class TestMLITCracking(unittest.TestCase):
    def test_matches_recursion(self):
        e = eng(crack_model=CrackModelType.MLIT, mlit_crack=MLIT_CRACK_DENSE)
        cracks = [y.cracking_pct for y in e.run_lifecycle_forecast(5)]
        c, hand = 0.0, []
        for _ in range(5):
            c = 0.40 + 1.16 * c
            hand.append(round(c, 4))
        self.assertEqual([round(x, 4) for x in cracks], hand)

    def test_porous_grows_slower_than_dense(self):
        dense = eng(crack_model=CrackModelType.MLIT, mlit_crack=MLIT_CRACK_DENSE).run_lifecycle_forecast(10)
        porous = eng(crack_model=CrackModelType.MLIT, mlit_crack=MLIT_CRACK_POROUS).run_lifecycle_forecast(10)
        self.assertLess(porous[-1].cracking_pct, dense[-1].cracking_pct)

    def test_preset_lookup(self):
        self.assertIs(mlit_crack_preset("dense"), MLIT_CRACK_DENSE)
        self.assertIs(mlit_crack_preset("POROUS"), MLIT_CRACK_POROUS)
        with self.assertRaises(ValueError):
            mlit_crack_preset("concrete")

    def test_cap_respected(self):
        e = eng(base_crack=95.0, crack_model=CrackModelType.MLIT)
        self.assertLessEqual(e.run_lifecycle_forecast(10)[-1].cracking_pct, 100.0)


class TestHDM4Roughness(unittest.TestCase):
    def test_couples_to_rut_and_crack(self):
        # Higher rutting (weaker structure) -> faster roughness growth.
        from rams.config import RutModelType
        strong = eng(roughness_model=RoughnessModelType.HDM4, structural_number=6.0)
        weak = eng(roughness_model=RoughnessModelType.HDM4, structural_number=2.5,
                   rut_model=RutModelType.HDM4, deflection_mm=1.4)
        self.assertGreater(
            weak.run_lifecycle_forecast(10)[-1].iri,
            strong.run_lifecycle_forecast(10)[-1].iri,
        )

    def test_monotonic_non_decreasing(self):
        iri = [y.iri for y in eng(roughness_model=RoughnessModelType.HDM4).run_lifecycle_forecast(10)]
        self.assertTrue(all(b >= a for a, b in zip(iri, iri[1:])))

    def test_increment_components(self):
        m = DEFAULT_HDM4_ROUGHNESS
        # Each driver increases the increment.
        base = m.increment(iri=2.0, snp=4.0, age=3, d_msa=4.5, d_crack_pct=1.0, d_rut_mm=1.0)
        more_rut = m.increment(iri=2.0, snp=4.0, age=3, d_msa=4.5, d_crack_pct=1.0, d_rut_mm=3.0)
        self.assertGreater(more_rut, base)


class TestCrackCalibration(unittest.TestCase):
    def test_recovers_recursion(self):
        pairs, c = [], 0.0
        for _ in range(15):
            nxt = 0.40 + 1.16 * c
            pairs.append((c, nxt))
            c = nxt
        res = calibrate_mlit_cracking(pairs)
        self.assertAlmostEqual(res.a, 0.40, places=2)
        self.assertAlmostEqual(res.b, 1.16, places=2)
        self.assertGreater(res.r_squared, 0.99)

    def test_pairs_from_series(self):
        pairs = cracking_pairs_from_series([(1, 0.4), (2, 0.86), (3, 1.4)])
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], (0.4, 0.86))

    def test_too_few_raises(self):
        with self.assertRaises(ValueError):
            calibrate_mlit_cracking([(0.0, 0.4)])


class TestRoughnessCalibration(unittest.TestCase):
    def _obs(self, true, n=40):
        import math
        import random
        base = HDM4RoughnessModel()
        rng = random.Random(11)
        out = []
        for _ in range(n):
            iri = rng.uniform(1.5, 5); snp = rng.uniform(3, 6); age = rng.randint(1, 10)
            dm = rng.uniform(3, 9); dc = rng.uniform(0, 5); dr = rng.uniform(0.1, 3)
            xs = math.exp(base.struct_age_m * age) * (1 + snp) ** base.struct_snp_pow * dm
            y = true[0] * iri + true[1] * xs + true[2] * dc + true[3] * dr
            out.append(RoughnessObservation(y, iri, snp, age, dm, dc, dr))
        return out

    def test_recovers_coefficients(self):
        res = calibrate_hdm4_roughness(self._obs((0.03, 120.0, 0.008, 0.05)))
        self.assertAlmostEqual(res.env_coeff, 0.03, places=2)
        self.assertAlmostEqual(res.struct_a0, 120.0, delta=2.0)
        self.assertAlmostEqual(res.rut_coeff, 0.05, places=2)
        self.assertGreater(res.r_squared, 0.99)

    def test_too_few_raises(self):
        with self.assertRaises(ValueError):
            calibrate_hdm4_roughness(self._obs((0.03, 120, 0.008, 0.05), n=3))


class TestApiModelSelection(unittest.TestCase):
    def test_forecast_reports_all_three_models(self):
        out = api.forecast_single({
            "zone": "HIGH", "years": 10, "model": "hdm4",
            "crack_model": "mlit", "roughness_model": "hdm4", "pavement": "dense",
        })
        self.assertEqual(out["model"]["crack_model"], "MLIT")
        self.assertEqual(out["model"]["roughness_model"], "HDM4")
        self.assertIn("crack_label", out["model"])

    def test_calibrate_cracking_endpoint(self):
        csv = "crack_prev,crack_next\n0.0,0.40\n0.40,0.864\n0.864,1.40\n1.40,2.024"
        out = api.calibrate({"kind": "cracking", "csv": csv})
        self.assertEqual(out["kind"], "cracking")
        self.assertAlmostEqual(out["a"], 0.40, places=2)
        self.assertAlmostEqual(out["b"], 1.16, places=2)

    def test_calibrate_bad_kind(self):
        with self.assertRaises(ValueError):
            api.calibrate({"kind": "skid", "csv": "a,b\n1,2"})


class TestSkid(unittest.TestCase):
    def test_skid_none_by_default(self):
        from rams.config import SkidModelType
        y = eng().run_lifecycle_forecast(1)[0]
        self.assertIsNone(y.skid)

    def test_skid_decreases_with_traffic(self):
        from rams.config import SkidModelType
        sk = [y.skid for y in eng(skid_model=SkidModelType.HDM4, base_skid=0.55).run_lifecycle_forecast(10)]
        self.assertTrue(all(b <= a for a, b in zip(sk, sk[1:])))
        self.assertLess(sk[-1], 0.55)

    def test_skid_floored_at_terminal(self):
        from rams.config import SkidModelType
        sk = [y.skid for y in eng(skid_model=SkidModelType.HDM4, base_skid=0.55,
                                  annual_msa=50.0).run_lifecycle_forecast(20)]
        self.assertGreaterEqual(min(sk), 0.30)  # sfc_min

    def test_skid_calibration_recovers_decay_k(self):
        from rams.calibrate import SkidObservation, calibrate_hdm4_skid
        from rams.distress import HDM4SkidModel
        m = HDM4SkidModel(decay_k=0.015, sfc_min=0.30)
        obs, sfc = [], 0.55
        for _ in range(8):
            d = m.increment(sfc, 4.5)
            obs.append(SkidObservation(d, sfc, 4.5))
            sfc = max(0.30, sfc + d)
        res = calibrate_hdm4_skid(obs)
        self.assertAlmostEqual(res.decay_k, 0.015, places=3)

    def test_skid_trigger(self):
        from rams.models import YearResult
        from rams.triggers import evaluate_triggers, TriggerSeverity
        yr = YearResult(year=5, cumulative_msa=20, iri=1.5, rutting_mm=3, cracking_pct=2,
                        irc82_pci=4.0, skid=0.35)
        fired = evaluate_triggers(yr, cumulative_msa=20)
        self.assertTrue(any(t.name == "skid" for t in fired))

    def test_api_skid_array(self):
        out = api.forecast_single({"zone": "HIGH", "years": 5, "skid_model": "hdm4"})
        self.assertEqual(len(out["skid"]), 5)
        self.assertEqual(out["model"]["skid_model"], "HDM4")

    def test_api_skid_off_by_default(self):
        out = api.forecast_single({"zone": "HIGH", "years": 5})
        self.assertEqual(out["skid"], [])


class TestPotholes(unittest.TestCase):
    def test_potholes_none_by_default(self):
        self.assertIsNone(eng().run_lifecycle_forecast(1)[0].potholes)

    def test_potholes_initiate_from_cracking(self):
        from rams.config import PotholeModelType
        tl = eng(pothole_model=PotholeModelType.HDM4).run_lifecycle_forecast(10)
        # No potholes while cracking is below the 20% threshold...
        self.assertEqual(tl[0].potholes, 0.0)
        # ...but present once cracking has crossed it later in the horizon.
        self.assertGreater(tl[-1].potholes, 0.0)

    def test_potholes_monotonic_and_capped(self):
        from rams.config import PotholeModelType
        from rams.distress import HDM4PotholeModel
        e = eng(pothole_model=PotholeModelType.HDM4, base_crack=50.0,
                hdm4_pothole=HDM4PotholeModel(cap_pct=10.0))
        pot = [y.potholes for y in e.run_lifecycle_forecast(10)]
        self.assertTrue(all(b >= a for a, b in zip(pot, pot[1:])))
        self.assertLessEqual(max(pot), 10.0)

    def test_potholes_calibration_recovers_rate(self):
        from rams.calibrate import PotholeObservation, calibrate_hdm4_potholes
        from rams.distress import HDM4PotholeModel
        m = HDM4PotholeModel(rate=0.8, crack_threshold_pct=20.0)
        obs = []
        for crack in (25.0, 30.0, 40.0, 55.0):
            obs.append(PotholeObservation(m.increment(crack, 5.0), crack, 5.0))
        res = calibrate_hdm4_potholes(obs)
        self.assertAlmostEqual(res.rate, 0.8, places=3)

    def test_pothole_trigger(self):
        from rams.models import YearResult
        from rams.triggers import evaluate_triggers, TriggerSeverity
        yr = YearResult(year=8, cumulative_msa=40, iri=2, rutting_mm=8, cracking_pct=40,
                        irc82_pci=2.0, potholes=12.0)
        fired = evaluate_triggers(yr, cumulative_msa=40)
        self.assertTrue(any(t.name == "potholes" and t.severity is TriggerSeverity.STRUCTURAL for t in fired))

    def test_api_potholes_array_and_calibrate(self):
        out = api.forecast_single({"zone": "HIGH", "years": 10, "pothole_model": "hdm4"})
        self.assertEqual(len(out["potholes"]), 10)
        self.assertEqual(out["model"]["pothole_model"], "HDM4")
        csv = "measured_pothole_increment,cracking_pct,d_msa\n0.3,25,5\n0.6,35,5\n1.0,50,5"
        c = api.calibrate({"kind": "potholes", "csv": csv})
        self.assertEqual(c["kind"], "potholes")
        self.assertGreater(c["rate"], 0)


class TestSegmentImport(unittest.TestCase):
    def test_ingest_first_segment_has_form_fields(self):
        # The segment-forecast upload reuses /api/ingest; the first row must carry
        # everything the form needs (incl. FWD deflection / derived SNP).
        csv = ("segment_id,base_iri,base_rut,base_crack,annual_msa,traffic_growth_rate,"
               "monsoon_zone,deflection_mm\nS1,2.2,4,3,6,0.05,MEDIUM,0.9\n")
        out = api.ingest_data({"format": "csv", "content": csv})
        s = out["segments"][0]
        for k in ("base_iri", "base_rut", "base_crack", "annual_msa",
                  "traffic_growth_rate", "monsoon_zone", "deflection_mm", "structural_number"):
            self.assertIn(k, s)
        self.assertGreater(s["structural_number"], 0)  # derived from deflection


class TestNetworkResidualHandback(unittest.TestCase):
    def _payload(self, **extra):
        p = {
            "segments": [
                {"segment_id": "STRONG", "base_iri": 1.5, "base_rut": 2.0, "base_crack": 0.0,
                 "annual_msa": 4.0, "traffic_growth_rate": 0.05, "monsoon_zone": "LOW",
                 "length_km": 10.0, "deflection_mm": 0.5, "structural_number": 5.0},
                {"segment_id": "WEAK", "base_iri": 2.5, "base_rut": 5.0, "base_crack": 5.0,
                 "annual_msa": 6.0, "traffic_growth_rate": 0.06, "monsoon_zone": "HIGH",
                 "length_km": 12.0, "deflection_mm": 1.3, "structural_number": 3.1},
            ],
            "annual_budget": 300, "years": 10,
        }
        p.update(extra)
        return p

    def test_per_segment_residual_present(self):
        out = api.network_and_budget(self._payload(design_msa=30))
        for row in out["segments"]:
            self.assertIn("residual_msa", row)
            self.assertIn("residual_basis", row)

    def test_handback_flags_weak_segment(self):
        out = api.network_and_budget(self._payload(design_msa=30, required_residual_msa=10))
        self.assertIsNotNone(out["handback"])
        self.assertIn("WEAK", out["handback"]["failing"])
        self.assertNotIn("STRONG", out["handback"]["failing"])

    def test_no_handback_without_requirement(self):
        out = api.network_and_budget(self._payload(design_msa=30))
        self.assertIsNone(out["handback"])


if __name__ == "__main__":
    unittest.main()
