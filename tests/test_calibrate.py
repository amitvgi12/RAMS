"""
HDM-4 calibration-harness + remaining-fatigue-life tests.

Calibration: synthetic field data is generated from KNOWN K factors, and the OLS
harness must recover them; the non-negativity refit (the paper's K_rpd<0 case)
and degenerate-data guards are checked too. Residual life: IRC:81 deflection
capacity vs IRC:37 traffic budget, the governing minimum, and the HAM handback
verdict.
"""
import math
import os
import random
import tempfile
import unittest

from rams import api
from rams.calibrate import (
    RutObservation,
    _UNIT,
    calibrate_hdm4_rut,
    load_observations_csv,
    observations_from_rut_series,
)
from rams.residual import (
    DEFAULT_DEFLECTION_LIFE,
    DeflectionLifeModel,
    HandbackVerdict,
    handback_assessment,
    remaining_fatigue_life,
)


def synth(true, n=50, noise=0.02, seed=1, krpd_term=True):
    """Generate n observations from known factors `true`=(rid,rst,rpd)."""
    rng = random.Random(seed)
    obs = []
    for _ in range(n):
        ye4 = rng.uniform(2.5, 9.0)
        age = rng.choice([1, 2, 3, 4, 5])
        deff = rng.uniform(0.4, 1.4)
        snp = rng.uniform(3.0, 6.0)
        rdo = _UNIT.densification(ye4, deff, snp, 98.0) if age == 1 else 0.0
        rdst = _UNIT.structural(ye4, snp, 98.0)
        rdpd = _UNIT.plastic(ye4, 1.0, 50.0, 100.0) if krpd_term else 0.0
        meas = true[0] * rdo + true[1] * rdst + true[2] * rdpd + rng.gauss(0, noise)
        obs.append(RutObservation(meas, ye4, age, deflection_mm=deff, structural_number=snp))
    return obs


class TestCalibration(unittest.TestCase):
    def test_recovers_known_factors(self):
        res = calibrate_hdm4_rut(synth((3.26, 3.11, 0.59), n=60, noise=0.01))
        self.assertAlmostEqual(res.k_rid, 3.26, delta=0.1)
        self.assertAlmostEqual(res.k_rst, 3.11, delta=0.1)
        self.assertAlmostEqual(res.k_rpd, 0.59, delta=0.1)
        self.assertGreater(res.r_squared, 0.95)

    def test_rmse_improves_vs_unit_base(self):
        res = calibrate_hdm4_rut(synth((2.5, 1.8, 0.4), n=50, noise=0.02))
        self.assertLessEqual(res.rmse_after, res.rmse_before)

    def test_nonnegativity_forces_plastic_to_zero(self):
        # True K_rpd == 0 (plastic irrelevant) -> harness keeps it ~0, fit stays positive.
        res = calibrate_hdm4_rut(synth((1.48, 0.83, 0.0), n=50, noise=0.03))
        self.assertGreaterEqual(res.k_rpd, 0.0)
        self.assertAlmostEqual(res.k_rid, 1.48, delta=0.2)
        self.assertAlmostEqual(res.k_rst, 0.83, delta=0.2)

    def test_returns_usable_calibration(self):
        res = calibrate_hdm4_rut(synth((2.0, 1.5, 0.3), n=40))
        self.assertEqual(res.calibration.k_rid, res.k_rid)
        # a-coefficients preserved from the base model form.
        self.assertEqual(res.calibration.rid_a0, _UNIT.rid_a0)

    def test_too_few_observations_raises(self):
        with self.assertRaises(ValueError):
            calibrate_hdm4_rut(synth((2.0, 1.5, 0.3), n=2))

    def test_degenerate_negative_targets_raise(self):
        # All-negative measured increments cannot be fit by a non-negative model.
        obs = [RutObservation(-5.0, 5.0, 2, deflection_mm=0.8, structural_number=4.0) for _ in range(10)]
        with self.assertRaises(ValueError):
            calibrate_hdm4_rut(obs)

    def test_csv_round_trip(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        rows = ["ye4,age,deflection_mm,structural_number,measured_rut_increment_mm"]
        for o in synth((2.5, 1.8, 0.4), n=20):
            rows.append(f"{o.ye4},{o.age},{o.deflection_mm},{o.structural_number},{o.measured_rut_increment_mm}")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(rows) + "\n")
        self.addCleanup(os.remove, path)
        obs = load_observations_csv(path)
        self.assertEqual(len(obs), 20)
        res = calibrate_hdm4_rut(obs)
        self.assertGreater(res.r_squared, 0.9)

    def test_observations_from_series_differences(self):
        obs = observations_from_rut_series(
            [(1, 3.5), (2, 4.6), (3, 5.8)], ye4=4.5, deflection_mm=0.8, structural_number=4.0
        )
        self.assertEqual(len(obs), 2)
        self.assertAlmostEqual(obs[0].measured_rut_increment_mm, 1.1, places=3)
        self.assertEqual(obs[0].age, 2)

    def test_sample_observations_file(self):
        obs = load_observations_csv("examples/sample_observations.csv")
        res = calibrate_hdm4_rut(obs)
        self.assertGreater(res.r_squared, 0.9)
        self.assertGreater(res.k_rid, 0)


class TestResidualLife(unittest.TestCase):
    def test_deflection_model_monotonic(self):
        m = DEFAULT_DEFLECTION_LIFE
        self.assertGreater(m.allowable_msa(0.5), m.allowable_msa(1.0))
        self.assertGreater(m.allowable_msa(1.0), m.allowable_msa(1.5))

    def test_deflection_inverse(self):
        m = DEFAULT_DEFLECTION_LIFE
        d = m.deflection_for_msa(20.0)
        self.assertAlmostEqual(m.allowable_msa(d), 20.0, places=2)

    def test_traffic_budget_governs_when_sound(self):
        r = remaining_fatigue_life(
            deflection_mm=0.5, annual_msa=4.5, traffic_growth_rate=0.06,
            cumulative_msa=10.0, design_msa=30.0,
        )
        # Sound (low deflection) -> traffic budget (20 MSA) is the binding limit.
        self.assertEqual(r.governing_basis, "traffic budget (IRC:37)")
        self.assertAlmostEqual(r.governing_remaining_msa, 20.0, places=1)
        self.assertIsNotNone(r.residual_years)

    def test_deflection_governs_when_weak(self):
        r = remaining_fatigue_life(
            deflection_mm=1.3, annual_msa=6.0, traffic_growth_rate=0.05,
            cumulative_msa=5.0, design_msa=30.0,
        )
        self.assertEqual(r.governing_basis, "deflection (IRC:81)")
        self.assertLess(r.governing_remaining_msa, 25.0)

    def test_zero_traffic_residual_years_none(self):
        r = remaining_fatigue_life(deflection_mm=0.6, annual_msa=0.0, design_msa=30.0)
        self.assertIsNone(r.residual_years)

    def test_handback_pass_marginal_fail(self):
        strong = remaining_fatigue_life(deflection_mm=0.4, annual_msa=4.0, design_msa=50.0)
        self.assertEqual(handback_assessment(strong, required_residual_msa=20.0).verdict, HandbackVerdict.PASS)

        weak = remaining_fatigue_life(deflection_mm=1.4, annual_msa=6.0, cumulative_msa=10.0, design_msa=30.0)
        h = handback_assessment(weak, required_residual_msa=20.0)
        self.assertEqual(h.verdict, HandbackVerdict.FAIL)
        self.assertGreater(h.shortfall_msa, 0)
        self.assertIsNotNone(h.overlay_target_deflection_mm)

    def test_handback_overlay_target_meets_requirement(self):
        weak = remaining_fatigue_life(deflection_mm=1.5, annual_msa=5.0, design_msa=40.0)
        h = handback_assessment(weak, required_residual_msa=15.0)
        # The suggested overlay deflection should restore >= the required capacity.
        self.assertGreaterEqual(
            DEFAULT_DEFLECTION_LIFE.allowable_msa(h.overlay_target_deflection_mm), 15.0 - 1e-6
        )


class TestApiCalibrateResidual(unittest.TestCase):
    def test_api_calibrate_from_list(self):
        obs = synth((2.5, 1.8, 0.4), n=40, noise=0.02)
        rows = [
            {"measured_rut_increment_mm": o.measured_rut_increment_mm, "ye4": o.ye4,
             "age": o.age, "deflection_mm": o.deflection_mm, "structural_number": o.structural_number}
            for o in obs
        ]
        out = api.calibrate({"observations": rows})
        self.assertGreater(out["r_squared"], 0.9)
        self.assertEqual(out["n"], 40)

    def test_api_calibrate_requires_data(self):
        with self.assertRaises(ValueError):
            api.calibrate({})

    def test_api_residual_with_handback(self):
        out = api.residual_life({
            "deflection": 1.2, "msa": 6.0, "growth": 0.05,
            "cumulative_msa": 15.0, "design_msa": 30.0, "required_residual_msa": 20.0,
        })
        self.assertIn("residual", out)
        self.assertIn("handback", out)
        self.assertIn(out["handback"]["verdict"], ("PASS", "MARGINAL", "FAIL"))

    def test_api_residual_without_design_msa(self):
        out = api.residual_life({"deflection": 0.8, "msa": 4.5})
        self.assertNotIn("handback", out)
        self.assertEqual(out["residual"]["governing_basis"], "deflection (IRC:81)")


class TestCalibrateRobustInput(unittest.TestCase):
    """Multi-format input + default-tolerant parsing for the Calibrate tab."""

    RUT = "ye4,age,measured_rut_increment_mm\n4.5,1,3.8\n4.8,2,1.1\n5.0,3,0.9\n5.2,4,0.7"

    def test_optional_columns_default(self):
        # Only target + ye4 + age present; optional predictors must default, not error.
        out = api.calibrate({"kind": "rut", "csv": self.RUT})
        self.assertEqual(out["n"], 4)
        self.assertEqual(out["skipped"], 0)

    def test_xlsx_upload(self):
        import base64
        from rams.export import xlsx_bytes
        xb = xlsx_bytes(
            ["ye4", "age", "measured_rut_increment_mm"],
            [["4.5", "1", "3.8"], ["4.8", "2", "1.1"], ["5.0", "3", "0.9"], ["5.2", "4", "0.7"]],
            "obs",
        )
        out = api.calibrate(
            {"kind": "rut", "content_b64": base64.b64encode(xb).decode(), "format": "xlsx"}
        )
        self.assertEqual(out["n"], 4)

    def test_header_normalisation(self):
        csv = "YE4, Age ,Measured Rut Increment mm\n4.5,1,3.8\n4.8,2,1.1\n5,3,0.9\n5.2,4,0.7"
        out = api.calibrate({"kind": "rut", "csv": csv})
        self.assertEqual(out["n"], 4)

    def test_bad_rows_skipped_not_aborted(self):
        csv = "ye4,age,measured_rut_increment_mm\n4.5,1,3.8\n4.8,2,\n5.0,3,0.9\n5.2,4,0.7"
        out = api.calibrate({"kind": "rut", "csv": csv})
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["skipped"], 1)

    def test_model_file_mismatch_clear_error(self):
        with self.assertRaises(ValueError) as cm:
            api.calibrate({"kind": "rut", "csv": "crack_prev,crack_next\n5,8\n8,12"})
        msg = str(cm.exception)
        self.assertIn("measured_rut_increment_mm", msg)
        self.assertIn("Columns found", msg)

    def test_column_aliases_picked_per_model(self):
        # Alternative template column names map to the canonical calibration inputs.
        csv = "Rut Increment mm,ESA MSA,age,SNP\n3.8,4.5,1,4.2\n1.1,4.8,2,4.2\n0.9,5,3,4.2\n0.7,5.2,4,4.2"
        out = api.calibrate({"kind": "rut", "csv": csv})
        self.assertEqual(out["n"], 4)
        out2 = api.calibrate({"kind": "cracking", "csv": "crack_t0,crack_t1\n0,0.4\n0.4,0.86\n0.86,1.4"})
        self.assertEqual(out2["n"], 3)

    def test_condition_survey_gets_actionable_message(self):
        # Survey-style columns (rut depth / crack area) -> snapshot guidance, not a wall.
        csv = ("l1_rut_depth_(in_mm),l1_%_crack_area,chainage\n"
               "4.6,8.2,154400\n5.1,0.0,154500")
        with self.assertRaises(ValueError) as cm:
            api.calibrate({"kind": "rut", "csv": csv})
        self.assertIn("condition survey", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
