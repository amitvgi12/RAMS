"""
HDM-4 mechanistic rut model + Indian intervention-trigger tests.

These verify the selectable rut model, its three-component structure, FWD/SNP
sensitivity (the structural-data integration), the paper-calibrated presets,
and the MSA / rut / crack / IRI intervention triggers. The default-model golden
values are guarded in the existing suites; here we check HDM-4 *behaviour*
(structure, monotonicity, sensitivity), not magic numbers tied to a fixed spec.
"""
import unittest

from rams import api
from rams.config import RutModelType
from rams.engine import IndianPavementDeteriorationEngine as Engine
from rams.engine import forecast_segment
from rams.hdm4 import (
    DEFAULT_HDM4,
    HDM4_DENSE_GRADED,
    HDM4_POROUS,
    annual_rut_increment,
    preset,
)
from rams.models import SegmentInput
from rams.config import MonsoonZone
from rams.triggers import (
    InterventionTriggers,
    TriggerSeverity,
    evaluate_triggers,
    msa_category,
)


def hdm4_engine(**over):
    base = dict(
        base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
        traffic_growth_rate=0.06, monsoon_zone="HIGH",
        rut_model=RutModelType.HDM4, hdm4_calibration=HDM4_DENSE_GRADED,
    )
    base.update(over)
    return Engine(**base)


class TestHdm4Model(unittest.TestCase):
    def test_default_model_unchanged_golden_year1(self):
        # The default law must still produce the locked golden year-1 rut (3.5).
        e = Engine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
        self.assertAlmostEqual(e.run_lifecycle_forecast(1)[0].rutting_mm, 3.5, places=1)

    def test_hdm4_runs_and_records_breakdown(self):
        e = hdm4_engine()
        tl = e.run_lifecycle_forecast(10)
        self.assertEqual(len(e.rut_breakdown), 10)
        # Components sum to the recorded total.
        b = e.rut_breakdown[0]
        self.assertAlmostEqual(
            b["densification"] + b["structural"] + b["plastic"], b["total"], places=3
        )

    def test_densification_is_year1_only(self):
        e = hdm4_engine()
        e.run_lifecycle_forecast(5)
        self.assertGreater(e.rut_breakdown[0]["densification"], 0.0)
        self.assertEqual(e.rut_breakdown[1]["densification"], 0.0)
        self.assertEqual(e.rut_breakdown[4]["densification"], 0.0)

    def test_rut_monotonic_non_decreasing(self):
        e = hdm4_engine()
        tl = e.run_lifecycle_forecast(10)
        ruts = [y.rutting_mm for y in tl]
        self.assertTrue(all(b >= a for a, b in zip(ruts, ruts[1:])))

    def test_higher_deflection_more_rut(self):
        weak = hdm4_engine(deflection_mm=1.5).run_lifecycle_forecast(10)[-1].rutting_mm
        sound = hdm4_engine(deflection_mm=0.3).run_lifecycle_forecast(10)[-1].rutting_mm
        self.assertGreater(weak, sound)

    def test_lower_structural_number_more_rut(self):
        weak = hdm4_engine(structural_number=2.5).run_lifecycle_forecast(10)[-1].rutting_mm
        strong = hdm4_engine(structural_number=6.0).run_lifecycle_forecast(10)[-1].rutting_mm
        self.assertGreater(weak, strong)

    def test_porous_preset_has_no_plastic_and_less_rut(self):
        dense = hdm4_engine(hdm4_calibration=HDM4_DENSE_GRADED).run_lifecycle_forecast(10)[-1].rutting_mm
        porous = hdm4_engine(hdm4_calibration=HDM4_POROUS).run_lifecycle_forecast(10)[-1].rutting_mm
        self.assertLess(porous, dense)
        e = hdm4_engine(hdm4_calibration=HDM4_POROUS)
        e.run_lifecycle_forecast(3)
        self.assertEqual(e.rut_breakdown[0]["plastic"], 0.0)  # Krpd=0

    def test_zero_traffic_zero_increment(self):
        inc = annual_rut_increment(
            DEFAULT_HDM4, ye4=0.0, age=1, deflection_mm=0.8, structural_number=4.0,
            compaction_pct=98.0, cds=1.0, heavy_speed_kmh=50.0, surfacing_thickness_mm=100.0,
        )
        self.assertEqual(inc.total, 0.0)

    def test_preset_lookup_and_bad_name(self):
        self.assertIs(preset("dense"), HDM4_DENSE_GRADED)
        self.assertIs(preset("POROUS"), HDM4_POROUS)
        with self.assertRaises(ValueError):
            preset("concrete")

    def test_forecast_segment_selects_hdm4(self):
        seg = SegmentInput(
            base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
            traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
            deflection_mm=0.9, structural_number=4.0,
        )
        default_tl = forecast_segment(seg, 10)
        hdm4_tl = forecast_segment(seg, 10, rut_model=RutModelType.HDM4)
        self.assertNotEqual(default_tl[-1].rutting_mm, hdm4_tl[-1].rutting_mm)


class TestRutModelType(unittest.TestCase):
    def test_from_str_case_insensitive(self):
        self.assertEqual(RutModelType.from_str("hdm4"), RutModelType.HDM4)
        self.assertEqual(RutModelType.from_str(" Default "), RutModelType.DEFAULT)

    def test_from_str_bad_raises(self):
        with self.assertRaises(ValueError):
            RutModelType.from_str("ai")


class TestStructuralInputBounds(unittest.TestCase):
    def test_rejects_out_of_range_deflection(self):
        with self.assertRaises(ValueError):
            SegmentInput(
                base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
                traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
                deflection_mm=99.0,
            ).validate()

    def test_rejects_out_of_range_snp(self):
        with self.assertRaises(ValueError):
            SegmentInput(
                base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
                traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
                structural_number=0.0,
            ).validate()

    def test_defaults_validate(self):
        v = SegmentInput(
            base_iri=1.5, base_rut=2.0, base_crack=0.0, annual_msa=4.5,
            traffic_growth_rate=0.06, monsoon_zone=MonsoonZone.HIGH,
        ).validate()
        self.assertEqual(v.deflection_mm, 0.5)
        self.assertEqual(v.structural_number, 4.0)


class TestTriggers(unittest.TestCase):
    def _yr(self, *, year=1, rut=0.0, crack=0.0, iri=1.0, cmsa=5.0):
        from rams.models import YearResult
        return YearResult(year=year, cumulative_msa=cmsa, iri=iri,
                          rutting_mm=rut, cracking_pct=crack, irc82_pci=4.0)

    def test_rut_functional_then_structural(self):
        funct = evaluate_triggers(self._yr(rut=12.0), cumulative_msa=5.0)
        self.assertTrue(any(t.name == "rutting" and t.severity is TriggerSeverity.FUNCTIONAL for t in funct))
        struct = evaluate_triggers(self._yr(rut=25.0), cumulative_msa=5.0)
        self.assertTrue(any(t.name == "rutting" and t.severity is TriggerSeverity.STRUCTURAL for t in struct))

    def test_msa_fatigue_trigger(self):
        # 26 of 30 MSA design = 87% > 80% default fraction -> structural.
        fired = evaluate_triggers(self._yr(cmsa=26.0), cumulative_msa=26.0, design_msa=30.0)
        self.assertTrue(any(t.name == "traffic_msa" and t.severity is TriggerSeverity.STRUCTURAL for t in fired))

    def test_msa_trigger_not_fired_early(self):
        fired = evaluate_triggers(self._yr(cmsa=5.0), cumulative_msa=5.0, design_msa=30.0)
        self.assertFalse(any(t.name == "traffic_msa" for t in fired))

    def test_deflection_trigger(self):
        fired = evaluate_triggers(self._yr(), cumulative_msa=5.0, deflection_mm=1.2)
        self.assertTrue(any(t.name == "deflection" for t in fired))

    def test_clean_year_no_triggers(self):
        fired = evaluate_triggers(self._yr(rut=3.0, crack=1.0, iri=1.5), cumulative_msa=5.0)
        self.assertEqual(fired, [])

    def test_msa_category(self):
        self.assertEqual(msa_category(3.0), "<5 MSA (low volume)")
        self.assertIn("MSA", msa_category(40.0))
        self.assertIn(">150", msa_category(300.0))

    def test_custom_thresholds(self):
        tr = InterventionTriggers(rut_functional_mm=5.0)
        fired = evaluate_triggers(self._yr(rut=6.0), cumulative_msa=5.0, triggers=tr)
        self.assertTrue(any(t.name == "rutting" for t in fired))


class TestApiModelSelection(unittest.TestCase):
    def test_forecast_default_has_empty_breakdown(self):
        out = api.forecast_single({"zone": "HIGH", "years": 5, "model": "default"})
        self.assertEqual(out["model"]["rut_model"], "DEFAULT")
        self.assertEqual(out["model"]["rut_breakdown"], [])

    def test_forecast_hdm4_has_breakdown_and_triggers(self):
        out = api.forecast_single({
            "zone": "HIGH", "years": 10, "model": "hdm4", "pavement": "dense",
            "deflection": 0.9, "snp": 4.0, "design_msa": 30,
        })
        self.assertEqual(out["model"]["rut_model"], "HDM4")
        self.assertEqual(len(out["model"]["rut_breakdown"]), 10)
        self.assertEqual(len(out["triggers"]), 10)
        # At least one MSA structural trigger fires within the horizon.
        all_fired = [f for t in out["triggers"] for f in t["fired"]]
        self.assertTrue(any(f["name"] == "traffic_msa" for f in all_fired))

    def test_forecast_bad_model_raises(self):
        with self.assertRaises(ValueError):
            api.forecast_single({"zone": "HIGH", "model": "xyz"})


if __name__ == "__main__":
    unittest.main()
