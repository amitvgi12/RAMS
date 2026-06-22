"""
Validation of the mechanistic engine against REAL IITPAVE outputs, the
IRC:115-2014 fatigue model, and the FWD remaining-life / overlay workflow.

Golden values are taken from:
  * IRC:37-2018 Annex II worked example II.3 (CBR 7%, 131 MSA design)
  * the NH-152D FWD evaluation report (IRC:115-2014, 15th-percentile moduli)
The Odemark-Boussinesq approximation is calibrated to track these to ~10-12%.
"""
import unittest

from rams import api
from rams.design import fatigue_life_msa_irc115
from rams.iitpave import (
    FWDSection,
    LayerModel,
    compute_strains,
    evaluate_fwd_sections,
    evaluate_section,
)


def _rel(a, b):
    return abs(a - b) / b


class TestIRC115Fatigue(unittest.TestCase):
    def test_reproduces_report_fatigue_life(self):
        # NH-152D Sec-1: eps_t=0.0001254, MR=870 -> report Nf = 330.5 MSA.
        nf = fatigue_life_msa_irc115(0.0001254, 870)
        self.assertLess(_rel(nf, 330.5), 0.02)

    def test_no_c_factor_differs_from_irc37(self):
        # IRC:115 (0.711e-4, no C) != IRC:37 fatigue for the same strain/modulus.
        from rams.design import fatigue_life_msa
        self.assertNotAlmostEqual(
            fatigue_life_msa_irc115(1.5e-4, 3000),
            fatigue_life_msa(1.5e-4, 3000, reliability=90), places=1)


class TestStrainCalibration(unittest.TestCase):
    def test_irc37_worked_example_ii3(self):
        # E=3000/200/62, h=190/480, Poisson 0.35 -> IITPAVE eps_t=146, eps_v=243 ue.
        s = compute_strains(LayerModel(3000, 200, 62, 190, 480), standard="irc37")
        self.assertLess(_rel(s.tensile_microstrain, 146), 0.12)
        self.assertLess(_rel(s.vertical_microstrain, 243), 0.12)
        # Both allowable for 131 MSA at 90% reliability -> section is adequate.
        self.assertGreaterEqual(s.governing_life_msa, 131)

    def test_nh152d_section_irc115(self):
        # E=870/348/77, h=300/350, Poisson 0.5/0.4/0.4 -> IITPAVE eps_t=125.4, eps_v=213.
        lyr = LayerModel(870, 348, 77, 300, 350,
                         nu_bituminous=0.5, nu_granular=0.4, nu_subgrade=0.4)
        s = compute_strains(lyr, standard="irc115")
        self.assertLess(_rel(s.tensile_microstrain, 125.4), 0.10)
        self.assertLess(_rel(s.vertical_microstrain, 213), 0.10)
        self.assertLess(_rel(s.governing_life_msa, 330.5), 0.15)  # report remaining life

    def test_tensile_correction_differs_by_standard(self):
        lyr = LayerModel(1000, 300, 77, 300, 350,
                         nu_bituminous=0.5, nu_granular=0.4, nu_subgrade=0.4)
        # IRC:115 uses a larger tensile correction (1.38) than IRC:37 (1.30).
        s37 = compute_strains(lyr, standard="irc37")
        s115 = compute_strains(lyr, standard="irc115")
        self.assertGreater(s115.tensile_microstrain, s37.tensile_microstrain)


class TestEvaluateSectionStandard(unittest.TestCase):
    def test_irc115_section_capacity(self):
        lyr = LayerModel(870, 348, 77, 300, 350,
                         nu_bituminous=0.5, nu_granular=0.4, nu_subgrade=0.4)
        a = evaluate_section(lyr, design_msa=300, standard="irc115")
        self.assertTrue(a.adequate)               # ~309 MSA >= 300
        self.assertLess(_rel(a.strains.governing_life_msa, 330.5), 0.15)


class TestFWDOverlay(unittest.TestCase):
    def _nh152d(self):
        rows = [  # real NH-152D 15th-percentile moduli (Table 6.4)
            ("LHS-1", 870, 348), ("LHS-3", 1581, 235), ("LHS-7", 1040, 367),
            ("RHS-2", 801, 352), ("RHS-4", 1100, 365), ("RHS-7", 865, 335),
        ]
        return [FWDSection(i, eb, eg, 77, 300, 350) for i, eb, eg in rows]

    def test_overlay_assessment(self):
        res = evaluate_fwd_sections(self._nh152d(), 300)
        self.assertEqual(len(res.rows), 6)
        # Strong sub-sections (e.g. RHS-4) comfortably exceed 300 MSA.
        by_id = {r.section_id: r for r in res.rows}
        self.assertGreater(by_id["RHS-4"].remaining_life_msa, 300)
        self.assertFalse(by_id["RHS-4"].overlay_required)
        # Marginal sub-sections within 15% of the threshold are flagged.
        self.assertTrue(any(r.confirm_with_iitpave for r in res.rows))

    def test_design_validation_and_dict(self):
        with self.assertRaises(ValueError):
            evaluate_fwd_sections(self._nh152d(), 0)
        d = evaluate_fwd_sections(self._nh152d(), 300).as_dict()
        self.assertEqual(d["n_sections"], 6)
        self.assertIn("verdict", d)
        self.assertIn("borderline_sections", d)


class TestFWDApi(unittest.TestCase):
    def test_endpoint(self):
        out = api.fwd_overlay({
            "design_msa": 300,
            "sections": [
                {"section_id": "S1", "e_bituminous": 870, "e_granular": 348,
                 "e_subgrade": 77, "h_bituminous": 300, "h_granular": 350},
                {"section_id": "S2", "e_bituminous": 801, "e_granular": 352,
                 "e_subgrade": 77, "h_bituminous": 300, "h_granular": 350},
            ],
        })
        self.assertEqual(out["n_sections"], 2)
        self.assertIn("sections", out)

    def test_bad_payload(self):
        with self.assertRaises(ValueError):
            api.fwd_overlay({"design_msa": 300, "sections": []})
        with self.assertRaises(ValueError):
            api.fwd_overlay({"design_msa": 0, "sections": [{"e_bituminous": 1}]})

    def test_iitpave_standard_param(self):
        out = api.iitpave_evaluate({
            "e_bituminous": 870, "e_granular": 348, "e_subgrade": 77,
            "h_bituminous": 300, "h_granular": 350, "design_msa": 300,
            "standard": "irc115",
        })
        self.assertIn("strains", out)
        self.assertTrue(out["adequate"])


if __name__ == "__main__":
    unittest.main()
