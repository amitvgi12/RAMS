"""
NSV chainage-survey ingestion (ROMDAS/Hawkeye vendor schemas) + auto-detection.
"""
import unittest

from rams.ingest import ingest_segments_csv_text, ingest_segments_xlsx_bytes
from rams.survey import (
    SurveyDefaults,
    band_value,
    detect_distress,
    is_survey,
    merge_surveys,
    segments_from_survey,
)
from tests.test_xlsx import make_xlsx

_COMMON = ["start_chainage", "end_chainage", "lane"]


class TestBandParsing(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(band_value("Very Good (<5%)"), 2.5)
        self.assertEqual(band_value("Good (5-10%)"), 7.5)
        self.assertEqual(band_value("Poor (>20%)"), 25.0)
        self.assertEqual(band_value("Very Good (0)"), 0.0)
        self.assertIsNone(band_value("no numbers here"))


class TestDetection(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_distress(_COMMON + ["rutting"]), "rutting")
        self.assertEqual(detect_distress(_COMMON + ["lane iri", "bi m/km"]), "roughness")
        self.assertEqual(detect_distress(_COMMON + ["potholes"]), "potholes")
        self.assertEqual(detect_distress(_COMMON + ["condition", "rating"]), "cracking")
        self.assertIsNone(detect_distress(["foo", "bar"]))

    def test_is_survey(self):
        self.assertTrue(is_survey(_COMMON + ["rutting"]))
        self.assertFalse(is_survey(["segment_id", "base_iri"]))


class TestSegmentsFromSurvey(unittest.TestCase):
    def _rows(self, distress_col, value):
        return [{
            "start_chainage": "154400", "end_chainage": "154500", "lane": "L1",
            distress_col: value,
        }]

    def test_rutting(self):
        r = segments_from_survey(self._rows("rutting", "8.5"))
        self.assertEqual(len(r.segments), 1)
        self.assertAlmostEqual(r.segments[0].base_rut, 8.5)
        self.assertEqual(r.segments[0].segment_id, "CH154400-154500/L1")
        self.assertAlmostEqual(r.segments[0].length_km, 0.1)

    def test_roughness_lane_iri(self):
        r = segments_from_survey(self._rows("lane iri", "4.25"))
        self.assertAlmostEqual(r.segments[0].base_iri, 4.25)

    def test_cracking_from_band(self):
        rows = [{"start_chainage": "0", "end_chainage": "100", "lane": "L1",
                 "condition": "Good (5-10%)"}]
        r = segments_from_survey(rows)
        self.assertAlmostEqual(r.segments[0].base_crack, 7.5)

    def test_defaults_fill_unsurveyed(self):
        d = SurveyDefaults(annual_msa=8.0, monsoon_zone="HIGH")
        r = segments_from_survey(self._rows("rutting", "5.0"), d)
        self.assertAlmostEqual(r.segments[0].annual_msa, 8.0)
        self.assertEqual(r.segments[0].monsoon_zone.value, "HIGH")

    def test_not_a_survey_raises(self):
        with self.assertRaises(ValueError):
            segments_from_survey([{"foo": "1"}])


class TestMerge(unittest.TestCase):
    def test_merge_by_chainage(self):
        rut = [{"start_chainage": "0", "end_chainage": "100", "lane": "L1", "rutting": "6.0"}]
        rough = [{"start_chainage": "0", "end_chainage": "100", "lane": "L1", "lane iri": "3.5"}]
        crack = [{"start_chainage": "0", "end_chainage": "100", "lane": "L1",
                  "condition": "Fair (10-20%)"}]
        m = merge_surveys([rut, rough, crack])
        self.assertEqual(len(m.segments), 1)
        s = m.segments[0]
        self.assertAlmostEqual(s.base_rut, 6.0)
        self.assertAlmostEqual(s.base_iri, 3.5)
        self.assertAlmostEqual(s.base_crack, 15.0)


class TestAutoDetectIngest(unittest.TestCase):
    def test_xlsx_survey_autodetected(self):
        headers = ["Start_Chainage", "End_Chainage", "Lane", "Rutting"]
        rows = [["154400", "154500", "L1", 8.5], ["154500", "154600", "L1", 4.2]]
        res = ingest_segments_xlsx_bytes(make_xlsx(headers, rows))
        self.assertEqual(len(res.segments), 2)
        self.assertAlmostEqual(res.segments[0].base_rut, 8.5)

    def test_csv_survey_autodetected(self):
        text = ("Start_Chainage,End_Chainage,Lane,Condition\n"
                "0,100,L1,Good (5-10%)\n100,200,L1,Very Good (<5%)\n")
        res = ingest_segments_csv_text(text)
        self.assertEqual(len(res.segments), 2)
        self.assertAlmostEqual(res.segments[0].base_crack, 7.5)

    def test_csv_unknown_still_raises(self):
        with self.assertRaises(ValueError):
            ingest_segments_csv_text("foo,bar\n1,2\n")


if __name__ == "__main__":
    unittest.main()
