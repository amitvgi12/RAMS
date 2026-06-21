"""
IRC:37 flexible-pavement structural design (design.py) + /api/design.
"""
import math
import unittest

from rams import api
from rams.design import (
    DEFAULT_CATALOGUE,
    design_pavement,
    fatigue_life_msa,
    granular_modulus_mpa,
    rutting_life_msa,
    subgrade_modulus_mpa,
)


class TestSubgradeModulus(unittest.TestCase):
    def test_low_cbr_linear_branch(self):
        # CBR <= 5 -> M_R = 10 * CBR
        self.assertAlmostEqual(subgrade_modulus_mpa(3.0), 30.0, places=6)
        self.assertAlmostEqual(subgrade_modulus_mpa(5.0), 50.0, places=6)

    def test_high_cbr_power_branch(self):
        # CBR > 5 -> 17.6 * CBR^0.64
        self.assertAlmostEqual(subgrade_modulus_mpa(8.0), 17.6 * math.pow(8.0, 0.64), places=6)
        self.assertGreater(subgrade_modulus_mpa(10.0), subgrade_modulus_mpa(8.0))

    def test_invalid_cbr_raises(self):
        with self.assertRaises(ValueError):
            subgrade_modulus_mpa(0)
        with self.assertRaises(ValueError):
            subgrade_modulus_mpa(-1)


class TestModuliAndPerformance(unittest.TestCase):
    def test_granular_modulus_monotonic_and_capped(self):
        thin = granular_modulus_mpa(50.0, 200.0)
        thick = granular_modulus_mpa(50.0, 400.0)
        self.assertGreater(thick, thin)  # more granular cover -> higher composite modulus
        self.assertLessEqual(granular_modulus_mpa(1000.0, 5000.0), 1000.0)  # cap honoured

    def test_fatigue_life_decreases_with_strain(self):
        low = fatigue_life_msa(1.5e-4, 3000.0)
        high = fatigue_life_msa(3.0e-4, 3000.0)
        self.assertGreater(low, high)  # higher tensile strain -> shorter fatigue life

    def test_rutting_life_decreases_with_strain(self):
        low = rutting_life_msa(3.0e-4)
        high = rutting_life_msa(6.0e-4)
        self.assertGreater(low, high)

    def test_reliability_lowers_allowable_life(self):
        # 90% reliability is more conservative (fewer allowable repetitions) than 80%.
        self.assertLess(
            rutting_life_msa(4.0e-4, reliability=90),
            rutting_life_msa(4.0e-4, reliability=80),
        )


class TestDesignPavement(unittest.TestCase):
    def test_more_traffic_thicker_bituminous(self):
        light = design_pavement(cbr=8.0, design_msa=10.0)
        heavy = design_pavement(cbr=8.0, design_msa=100.0)
        self.assertGreater(heavy.bituminous_mm, light.bituminous_mm)

    def test_weaker_subgrade_thicker_granular(self):
        weak = design_pavement(cbr=3.0, design_msa=30.0)
        strong = design_pavement(cbr=12.0, design_msa=30.0)
        self.assertGreater(weak.granular_mm, strong.granular_mm)

    def test_total_is_sum_of_layers(self):
        d = design_pavement(cbr=6.0, design_msa=50.0)
        self.assertAlmostEqual(d.total_mm, d.bituminous_mm + d.granular_mm, places=0)
        self.assertAlmostEqual(d.bituminous_mm, d.bc_mm + d.dbm_mm, places=0)
        self.assertAlmostEqual(d.granular_mm, d.wmm_mm + d.gsb_mm, places=0)

    def test_reliability_auto_selects_on_traffic(self):
        self.assertEqual(design_pavement(cbr=8.0, design_msa=10.0).reliability, 80)
        self.assertEqual(design_pavement(cbr=8.0, design_msa=30.0).reliability, 90)
        # explicit override wins
        self.assertEqual(
            design_pavement(cbr=8.0, design_msa=10.0, reliability=90).reliability, 90
        )

    def test_floors_respected(self):
        tiny = design_pavement(cbr=20.0, design_msa=1.0)
        self.assertGreaterEqual(tiny.bituminous_mm, DEFAULT_CATALOGUE.bit_min)
        self.assertGreaterEqual(tiny.granular_mm, DEFAULT_CATALOGUE.gran_min)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            design_pavement(cbr=0, design_msa=30.0)
        with self.assertRaises(ValueError):
            design_pavement(cbr=8.0, design_msa=0)
        with self.assertRaises(ValueError):
            design_pavement(cbr=8.0, design_msa=30.0, design_life_years=0)
        with self.assertRaises(ValueError):
            design_pavement(cbr=8.0, design_msa=30.0, reliability=75)

    def test_as_dict_shape(self):
        d = design_pavement(cbr=8.0, design_msa=129.0).as_dict()
        self.assertEqual(
            set(d["layers"]),
            {"bc_mm", "dbm_mm", "bituminous_mm", "wmm_mm", "gsb_mm", "granular_mm"},
        )
        self.assertIn("subgrade_modulus_mpa", d)
        self.assertIn("rationale", d)


class TestDesignAPI(unittest.TestCase):
    def test_direct_design_msa(self):
        out = api.pavement_design({"cbr": 8.0, "design_msa": 50.0, "design_life_years": 15})
        self.assertEqual(out["design_msa"], 50.0)
        self.assertGreater(out["total_mm"], 0)
        self.assertNotIn("traffic", out)

    def test_cvpd_derived_design(self):
        out = api.pavement_design({"cbr": 6.0, "cvpd": 4500, "vdf": 4.5, "design_life_years": 15})
        self.assertIn("traffic", out)
        self.assertGreater(out["design_msa"], 100)  # heavy corridor

    def test_missing_traffic_raises(self):
        with self.assertRaises(ValueError):
            api.pavement_design({"cbr": 8.0})


if __name__ == "__main__":
    unittest.main()
