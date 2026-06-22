"""
Homogeneous sectioning (cumulative-difference) + XLSX/PDF export.
"""
import io
import unittest
import zipfile

from rams import api
from rams.config import MonsoonZone
from rams.export import pdf_bytes, sections_to_pdf, sections_to_xlsx, xlsx_bytes
from rams.models import SegmentInput
from rams.sections import homogeneous_sections, section_survey


def _survey(n_good=10, n_bad=10, rut_good=2.0, rut_bad=16.0):
    """An ordered chainage survey: a good run followed by a degraded run."""
    segs, ch = [], 100000
    for i in range(n_good + n_bad):
        rut = rut_good if i < n_good else rut_bad
        segs.append(SegmentInput(
            base_iri=2.0, base_rut=rut, base_crack=0.0, annual_msa=4.5,
            traffic_growth_rate=0.05, monsoon_zone=MonsoonZone.MEDIUM,
            segment_id=f"CH{ch}-{ch+100}/L1", length_km=0.1,
        ))
        ch += 100
    return segs


class TestSectioning(unittest.TestCase):
    def test_splits_distinct_condition(self):
        secs = homogeneous_sections(_survey(), key="rut", min_length_km=0.5)
        self.assertGreaterEqual(len(secs), 2)
        # The good run scores a higher PCI than the degraded run.
        self.assertGreater(secs[0].base_pci, secs[-1].base_pci)

    def test_chainage_parsed(self):
        secs = homogeneous_sections(_survey(), key="rut")
        self.assertEqual(secs[0].chainage_from, 100000.0)
        self.assertEqual(secs[-1].chainage_to, 102000.0)

    def test_min_length_merges_short_runs(self):
        # Alternating every point would over-split; min length forces merges.
        segs, ch = [], 0
        for i in range(20):
            segs.append(SegmentInput(2.0, 4.0 if i % 2 else 12.0, 0.0, 4.5, 0.05,
                                     MonsoonZone.MEDIUM, segment_id=f"CH{ch}-{ch+100}/L1",
                                     length_km=0.1))
            ch += 100
        secs = homogeneous_sections(segs, key="rut", min_length_km=0.5)
        self.assertTrue(all(s.length_km >= 0.5 - 1e-9 for s in secs[:-1]))

    def test_section_survey_summary(self):
        res = section_survey(_survey(), key="rut").as_dict()
        self.assertEqual(res["n_points"], 20)
        self.assertIn("band_counts", res)
        self.assertEqual(res["n_sections"], len(res["sections"]))

    def test_empty(self):
        self.assertEqual(homogeneous_sections([]), [])


class TestExportWriters(unittest.TestCase):
    def test_xlsx_is_valid_zip(self):
        data = xlsx_bytes(["A", "B"], [["x", 1], ["y", 2.5]])
        self.assertEqual(data[:2], b"PK")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(z.testzip())
        self.assertIn("xl/worksheets/sheet1.xml", z.namelist())
        self.assertIn("[Content_Types].xml", z.namelist())

    def test_pdf_is_well_formed(self):
        data = pdf_bytes("Title", ["line one", "line two"])
        self.assertEqual(data[:4], b"%PDF")
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Catalog", data)
        # every xref offset points at its object header
        import re
        sx = data.rfind(b"startxref")
        off = int(data[sx + 9:data.find(b"%%EOF", sx)].strip())
        self.assertEqual(data[off:off + 4], b"xref")

    def test_section_reports(self):
        res = section_survey(_survey(), key="rut")
        xb = sections_to_xlsx(res)
        pb = sections_to_pdf(res)
        self.assertEqual(xb[:2], b"PK")
        self.assertEqual(pb[:4], b"%PDF")


class TestSectionsApi(unittest.TestCase):
    def _payload(self):
        return {"segments": [
            {"segment_id": s.segment_id, "base_iri": s.base_iri, "base_rut": s.base_rut,
             "base_crack": s.base_crack, "annual_msa": s.annual_msa,
             "traffic_growth_rate": s.traffic_growth_rate,
             "monsoon_zone": s.monsoon_zone.value, "length_km": s.length_km}
            for s in _survey()
        ], "years": 10, "key": "rut"}

    def test_survey_sections_endpoint(self):
        out = api.survey_sections(self._payload())
        self.assertGreaterEqual(out["n_sections"], 2)
        self.assertEqual(out["n_points"], 20)

    def test_export_xlsx_and_pdf(self):
        xb, mime_x, name_x = api.export_report(self._payload(), "xlsx")
        self.assertEqual(xb[:2], b"PK")
        self.assertTrue(name_x.endswith(".xlsx"))
        pb, mime_p, name_p = api.export_report(self._payload(), "pdf")
        self.assertEqual(pb[:4], b"%PDF")
        self.assertEqual(mime_p, "application/pdf")

    def test_export_bad_format(self):
        with self.assertRaises(ValueError):
            api.export_report(self._payload(), "docx")


if __name__ == "__main__":
    unittest.main()
