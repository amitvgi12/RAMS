"""
IRC:37 traffic (CVPD/VDF -> MSA) + large-file streaming upload tests.
"""
import os
import tempfile
import unittest

from rams import api
from rams.traffic import (
    LANE_DISTRIBUTION,
    default_vdf,
    design_msa,
    lane_distribution_factor,
)


class TestIRC37Traffic(unittest.TestCase):
    def test_design_and_annual_msa(self):
        # NH-44 example: CVPD 4500, VDF 4.5, 5% growth, 15y, two-lane (D=0.75).
        t = design_msa(4500, vdf=4.5, growth_rate=0.05, design_life_years=15, lane_distribution=0.75)
        self.assertAlmostEqual(t.annual_msa, 4500 * 365 * 0.75 * 4.5 / 1e6, places=3)
        self.assertGreater(t.design_msa, 100)  # heavy corridor, >100 MSA

    def test_zero_growth_linear(self):
        t = design_msa(1000, vdf=3.0, growth_rate=0.0, design_life_years=10, lane_distribution=0.75)
        self.assertAlmostEqual(t.design_msa, 1000 * 365 * 10 * 0.75 * 3.0 / 1e6, places=3)

    def test_higher_vdf_more_damage(self):
        low = design_msa(3000, vdf=2.0).design_msa
        high = design_msa(3000, vdf=6.0).design_msa  # overloaded corridor
        self.assertGreater(high, low)

    def test_default_vdf_bands(self):
        self.assertLess(default_vdf(100, "plain"), default_vdf(5000, "plain"))
        self.assertLess(default_vdf(5000, "hilly"), default_vdf(5000, "plain"))

    def test_lane_distribution(self):
        self.assertEqual(lane_distribution_factor("two_lane"), 0.75)
        self.assertEqual(lane_distribution_factor("four_lane"), 0.40)
        self.assertIn("single", LANE_DISTRIBUTION)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            design_msa(-1, vdf=4.0)
        with self.assertRaises(ValueError):
            design_msa(1000, vdf=0.0)
        with self.assertRaises(ValueError):
            lane_distribution_factor("eight_lane")
        with self.assertRaises(ValueError):
            default_vdf(1000, "coastal")

    def test_api_traffic_endpoint(self):
        out = api.traffic_msa({"cvpd": 4500, "vdf": 4.5, "carriageway": "two_lane",
                               "design_life_years": 15, "growth": 0.05})
        self.assertGreater(out["design_msa"], 100)
        self.assertEqual(out["carriageway"], "two_lane")

    def test_api_traffic_default_vdf(self):
        out = api.traffic_msa({"cvpd": 4500, "terrain": "plain"})  # no vdf -> indicative
        self.assertEqual(out["vdf"], default_vdf(4500, "plain"))


class TestIngestFile(unittest.TestCase):
    HEADER = ("segment_id,base_iri,base_rut,base_crack,annual_msa,"
              "traffic_growth_rate,monsoon_zone,length_km,deflection_mm\n")

    def _write(self, text, suffix=".csv"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_ingest_file_csv(self):
        path = self._write(self.HEADER + "A,1.6,2.5,1,4.5,0.05,HIGH,2,0.8\nB,2,3,1,5,0.05,LOW,1,0.6\n")
        out = api.ingest_file(path, "csv")
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["format"], "csv")
        self.assertGreater(out["segments"][0]["structural_number"], 0)  # derived from deflection

    def test_ingest_file_bad_format(self):
        with self.assertRaises(ValueError):
            api.ingest_file("x.json", "json")

    def test_ingest_file_too_many_segments_points_to_cli(self):
        rows = "".join(f"S{i},1.6,2.5,1,4.5,0.05,HIGH,2,0.8\n" for i in range(api.MAX_NETWORK_SEGMENTS + 5))
        path = self._write(self.HEADER + rows)
        with self.assertRaises(ValueError) as ctx:
            api.ingest_file(path, "csv")
        self.assertIn("CLI", str(ctx.exception))  # message steers to the streaming CLI path


if __name__ == "__main__":
    unittest.main()
