"""
Reporting + ingestion tests (Lead QA + Security Lead).

Notably asserts that untrusted segment IDs are HTML-escaped in the report
(stored-XSS guard) and that malformed CSV rows are isolated, not fatal.
"""
import json
import os
import tempfile
import unittest

from rams.batch import forecast_network, ingest_segments_csv, network_summary
from rams.engine import IndianPavementDeteriorationEngine
from rams.maintenance import MaintenancePolicy, build_maintenance_plan
from rams.report import to_csv, to_html, to_json


def spec_timeline():
    e = IndianPavementDeteriorationEngine(1.5, 2.0, 0.0, 4.5, 0.06, "HIGH")
    tl = e.run_lifecycle_forecast(10)
    return tl, build_maintenance_plan(tl)


class TestExports(unittest.TestCase):
    def test_csv_has_header_and_rows(self):
        tl, _ = spec_timeline()
        out = to_csv(tl)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 11)  # header + 10 years
        self.assertIn("Year", lines[0])

    def test_json_roundtrips(self):
        tl, plan = spec_timeline()
        payload = json.loads(to_json(tl, plan))
        self.assertEqual(len(payload["timeline"]), 10)
        self.assertEqual(payload["maintenance_plan"]["preventive_window_year"], 5)


class TestHtmlSecurity(unittest.TestCase):
    def test_segment_id_is_escaped(self):
        tl, plan = spec_timeline()
        evil = '<script>alert(1)</script>'
        html_out = to_html(evil, tl, plan, MaintenancePolicy())
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)

    def test_html_is_self_contained(self):
        tl, plan = spec_timeline()
        html_out = to_html("SEG", tl, plan, MaintenancePolicy())
        # No external scripts/stylesheets/CDNs.
        self.assertNotIn("http://", html_out.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html_out)
        self.assertIn("<svg", html_out)


class TestCsvIngestion(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.remove, path)
        return path

    HEADER = "segment_id,base_iri,base_rut,base_crack,annual_msa,traffic_growth_rate,monsoon_zone\n"

    def test_good_rows_loaded(self):
        path = self._write(self.HEADER + "A,1.5,2,0,4.5,0.06,HIGH\nB,2,3,1,5,0.05,LOW\n")
        res = ingest_segments_csv(path)
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.errors, [])

    def test_bad_row_isolated(self):
        path = self._write(
            self.HEADER
            + "GOOD,1.5,2,0,4.5,0.06,HIGH\n"
            + "BAD,1.5,2,0,-9,0.06,HIGH\n"      # negative MSA
            + "ALSO_BAD,1.5,2,0,4.5,0.06,TYPO\n"  # bad zone
        )
        res = ingest_segments_csv(path)
        self.assertEqual(len(res.segments), 1)
        self.assertEqual(len(res.errors), 2)

    def test_missing_columns_raises(self):
        path = self._write("segment_id,base_iri\nA,1.5\n")
        with self.assertRaises(ValueError):
            ingest_segments_csv(path)

    def test_network_summary_counts(self):
        path = self._write(
            self.HEADER
            + "A,1.5,2,0,4.5,0.06,HIGH\n"
            + "B,1.5,2,0,1.0,0.0,LOW\n"
        )
        res = ingest_segments_csv(path)
        forecasts = list(forecast_network(res.segments, 10))
        summary = network_summary(forecasts)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(
            summary["routine_only"] + summary["needs_preventive"]
            + summary["window_expired"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
