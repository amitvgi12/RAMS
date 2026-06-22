"""
Mechanistic (IITPAVE-style Odemark-Boussinesq) layered-elastic analysis.
"""
import unittest

from rams import api
from rams.iitpave import (
    LayerModel,
    compute_strains,
    contact_radius_mm,
    design_pavement_mechanistic,
    evaluate_section,
)


class TestStrains(unittest.TestCase):
    def test_contact_radius(self):
        # 20 kN at 0.56 MPa -> ~106 mm equivalent circular contact radius.
        self.assertAlmostEqual(contact_radius_mm(), 106.6, delta=1.0)

    def test_magnitudes_in_irc_range(self):
        s = compute_strains(LayerModel(3000, 300, 70, 120, 400))
        self.assertTrue(50 < s.tensile_microstrain < 600, s.tensile_microstrain)
        self.assertTrue(100 < s.vertical_microstrain < 800, s.vertical_microstrain)
        self.assertGreater(s.governing_life_msa, 0)

    def test_thicker_section_lower_strain(self):
        thin = compute_strains(LayerModel(3000, 300, 70, 100, 300))
        thick = compute_strains(LayerModel(3000, 300, 70, 150, 450))
        self.assertLess(thick.tensile_microstrain, thin.tensile_microstrain)
        self.assertLess(thick.vertical_microstrain, thin.vertical_microstrain)
        self.assertGreater(thick.governing_life_msa, thin.governing_life_msa)

    def test_weaker_subgrade_shorter_life(self):
        # Subgrade strain (and hence rutting life) must respond to subgrade strength.
        weak = compute_strains(LayerModel(3000, 250, 40, 120, 400))
        strong = compute_strains(LayerModel(3000, 250, 90, 120, 400))
        self.assertGreater(weak.vertical_microstrain, strong.vertical_microstrain)
        self.assertGreater(strong.rutting_life_msa, weak.rutting_life_msa)

    def test_governing_mode_label(self):
        s = compute_strains(LayerModel(3000, 300, 70, 100, 300))
        self.assertIn(s.governing_mode, ("fatigue cracking", "subgrade rutting"))
        self.assertEqual(s.governing_life_msa, min(s.fatigue_life_msa, s.rutting_life_msa))


class TestEvaluateSection(unittest.TestCase):
    def test_capacity_and_residual(self):
        layer = LayerModel(977, 200, 70, 300, 350)  # FWD-style design moduli
        a = evaluate_section(layer, annual_msa=10, traffic_growth_rate=0.05,
                             cumulative_msa=20, design_msa=150)
        self.assertGreater(a.strains.governing_life_msa, 0)
        self.assertAlmostEqual(a.remaining_msa,
                               max(0.0, a.strains.governing_life_msa - 20), places=2)
        self.assertIsNotNone(a.residual_years)
        self.assertTrue(a.adequate)  # 300/350 strong section carries 150 MSA

    def test_deficient_section_flagged(self):
        weak = LayerModel(800, 120, 45, 80, 250)
        a = evaluate_section(weak, annual_msa=8, design_msa=100)
        self.assertFalse(a.adequate)
        self.assertIn("DEFICIENT", a.rationale)

    def test_no_traffic_residual_none(self):
        a = evaluate_section(LayerModel(3000, 250, 70, 150, 450), annual_msa=0)
        self.assertIsNone(a.residual_years)


class TestMechanisticDesign(unittest.TestCase):
    def test_design_meets_both_criteria(self):
        d = design_pavement_mechanistic(cbr=8, design_msa=50)
        self.assertGreaterEqual(d.strains.fatigue_life_msa, 50)
        self.assertGreaterEqual(d.strains.rutting_life_msa, 50)
        self.assertAlmostEqual(d.total_mm, d.bituminous_mm + d.granular_mm, places=0)

    def test_heavier_traffic_thicker(self):
        light = design_pavement_mechanistic(cbr=8, design_msa=20)
        heavy = design_pavement_mechanistic(cbr=8, design_msa=150)
        self.assertGreater(heavy.total_mm, light.total_mm)

    def test_weaker_subgrade_more_granular(self):
        weak = design_pavement_mechanistic(cbr=3, design_msa=50)
        strong = design_pavement_mechanistic(cbr=12, design_msa=50)
        self.assertGreater(weak.granular_mm, strong.granular_mm)

    def test_cheap_granular_does_structural_work(self):
        # Granular (cheap) should dominate the section, not bituminous (dear).
        d = design_pavement_mechanistic(cbr=8, design_msa=50)
        self.assertGreater(d.granular_mm, d.bituminous_mm)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            design_pavement_mechanistic(cbr=0, design_msa=50)
        with self.assertRaises(ValueError):
            design_pavement_mechanistic(cbr=8, design_msa=0)


class TestIITPAVEApi(unittest.TestCase):
    def test_design_method_iitpave(self):
        out = api.pavement_design({"cbr": 8, "design_msa": 50, "method": "iitpave"})
        self.assertEqual(out["method"], "iitpave")
        self.assertIn("strains", out)
        self.assertIn("bituminous_mm", out["layers"])
        self.assertGreaterEqual(out["strains"]["fatigue_life_msa"], 50)

    def test_design_method_catalogue_default(self):
        out = api.pavement_design({"cbr": 8, "design_msa": 50})
        self.assertEqual(out["method"], "catalogue")

    def test_iitpave_evaluate_endpoint(self):
        out = api.iitpave_evaluate({
            "e_bituminous": 977, "e_granular": 200, "e_subgrade": 70,
            "h_bituminous": 300, "h_granular": 350,
            "annual_msa": 10, "growth": 0.05, "cumulative_msa": 20, "design_msa": 150,
        })
        self.assertIn("strains", out)
        self.assertTrue(out["adequate"])
        self.assertEqual(out["layer"]["h_bituminous_mm"], 300)


if __name__ == "__main__":
    unittest.main()
