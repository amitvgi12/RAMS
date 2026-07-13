"""
XML / PDF ingestion + MLIT-PMS MCI tests.

Covers the new multi-format pavement-databank import path (CSV stays tested in
test_report.py) and the paper-derived Maintenance Control Index. The security
focus mirrors the rest of RAMS: the XML loader must reject DTDs (XXE / billion
-laughs) and isolate per-record errors so one bad segment never aborts a whole
network import.
"""
import math
import os
import tempfile
import unittest

from rams import api
from rams.ingest import (
    ingest_segments,
    ingest_segments_pdf_bytes,
    _decode_pdf_literal,
    _looks_like_fwd_report,
)
from rams.mci import (
    RUT_OVERLAY_THRESHOLD_MM,
    MCIBand,
    compute_mci,
    mci_band,
)

REQUIRED = (
    "segment_id,base_iri,base_rut,base_crack,"
    "annual_msa,traffic_growth_rate,monsoon_zone,length_km"
)


def make_pdf(lines):
    """Build a minimal single-stream PDF whose text layer holds `lines`.

    Uncompressed content stream of one Td/Tj pair per line, which the stdlib
    extractor decodes back to newline-separated text. No third-party writer.
    """
    ops = ["BT", "/F1 10 Tf", "72 720 Td"]
    for i, ln in enumerate(lines):
        esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i:
            ops.append("0 -14 Td")
        ops.append("(%s) Tj" % esc)
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref,
    )
    return out


def make_table_pdf(rows, x0=50, y0=700, dy=18, colw=130, size=10):
    """PDF whose table cells are individually positioned, with every *word* placed
    as its own Tm/Tj fragment (like a real report -- multi-word header cells such as
    "Bituminous Layer" are two fragments). Exercises the coordinate-based row/cell
    reconstruction honestly, not with pre-joined single-string cells."""
    ops = ["BT", "/F1 %d Tf" % size]
    for r, cells in enumerate(rows):
        y = y0 - r * dy
        for c, cell in enumerate(cells):
            x = x0 + c * colw
            for w in str(cell).split(" "):
                if not w:
                    continue
                esc = w.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append("1 0 0 1 %d %d Tm (%s) Tj" % (x, y, esc))
                x += len(w) * size * 0.55 + 3          # advance past the word + a space
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref)
    return out


# --- PDF --------------------------------------------------------------------

class TestPdfIngestion(unittest.TestCase):
    def test_text_pdf_round_trip(self):
        pdf = make_pdf(
            [
                REQUIRED,
                "A,1.5,2.0,0.0,4.5,0.06,HIGH,12.0",
                "B,2.2,4.0,3.0,6.0,0.05,MEDIUM,8.5",
            ]
        )
        res = ingest_segments_pdf_bytes(pdf)
        self.assertEqual([s.segment_id for s in res.segments], ["A", "B"])
        self.assertEqual(res.errors, [])

    def test_bad_row_isolated(self):
        pdf = make_pdf(
            [
                REQUIRED,
                "A,1.5,2.0,0.0,4.5,0.06,HIGH,12.0",
                "BAD,1.5,2,0,-9,0.06,HIGH,1",  # negative MSA
            ]
        )
        res = ingest_segments_pdf_bytes(pdf)
        self.assertEqual(len(res.segments), 1)
        self.assertEqual(len(res.errors), 1)

    def test_pdf_without_table_header_raises(self):
        pdf = make_pdf(["Annual Condition Survey", "no table here"])
        with self.assertRaises(ValueError):
            ingest_segments_pdf_bytes(pdf)

    def test_image_only_pdf_raises(self):
        with self.assertRaises(ValueError):
            ingest_segments_pdf_bytes(b"%PDF-1.4\n%%EOF")

    def test_fwd_report_pdf_points_to_fwd_tool(self):
        # An FWD moduli report has none of the condition-survey columns; the error
        # should recognise it and route the user to the FWD overlay tool.
        pdf = make_pdf([
            "Falling Weight Deflectometer (FWD) Evaluation Report",
            "Back-calculated layer moduli and central deflection D0",
            "Chainage  E1  E2  E3  Deflection",
        ])
        with self.assertRaises(ValueError) as ctx:
            ingest_segments_pdf_bytes(pdf)
        self.assertIn("FWD remaining-life", str(ctx.exception))

    def test_looks_like_fwd_report(self):
        self.assertTrue(_looks_like_fwd_report(
            "FWD deflection back-calculated moduli report"))
        self.assertFalse(_looks_like_fwd_report(
            "Annual condition survey: IRI, rutting, cracking by chainage"))

    def test_pdf_literal_non_octal_digit_escape(self):
        # \8 and \9 are NOT octal escapes (octal is 0-7); the PDF spec says the
        # backslash is ignored and the digit is literal. Previously this crashed
        # with "invalid literal for int() with base 8: b'8'".
        self.assertEqual(_decode_pdf_literal(rb"IRI \8.5 m/km"), "IRI 8.5 m/km")
        self.assertEqual(_decode_pdf_literal(rb"\9"), "9")
        self.assertEqual(_decode_pdf_literal(rb"\101"), "A")     # valid octal still works
        self.assertEqual(_decode_pdf_literal(rb"\78"), chr(7) + "8")


def make_prose_pdf(sentence, x0=60, y=700, size=10):
    """A single line of justified prose with each word individually positioned."""
    ops = ["BT", "/F1 %d Tf" % size]
    x = x0
    for w in sentence.split(" "):
        esc = w.replace("(", "\\(").replace(")", "\\)")
        ops.append("1 0 0 1 %g %d Tm (%s) Tj" % (x, y, esc))
        x += len(w) * size * 0.55 + 3
    ops.append("ET")
    content = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1, xref)
    return out


class TestReportTableExtraction(unittest.TestCase):
    """Coordinate-based row reconstruction + fuzzy FWD parsing for report PDFs.

    De-rigged: table cells are built from individually-placed *words* (not
    pre-joined strings), so these exercise the real failure modes.
    """

    def test_prose_paragraph_stays_readable(self):
        # Regression: positioned prose must NOT explode into tab-separated fragments.
        from rams.ingest import _extract_pdf_text
        text = _extract_pdf_text(make_prose_pdf(
            "The subgrade modulus estimated from the above methods"))
        self.assertNotIn("\t", text)
        self.assertIn("subgrade modulus estimated", text)

    def test_multiword_header_maps_correct_moduli(self):
        # "Bituminous Layer" is two fragments but must stay ONE cell, so the moduli
        # map to the right columns (this is the silent-wrong bug from the autopsy).
        import base64
        from rams.api import _fwd_rows
        pdf = make_table_pdf([
            ["Sub-Section", "Bituminous Layer", "Granular Layer", "Subgrade"],
            ["LHS-Sec-1", "977", "60", "70"],
        ])
        row = _fwd_rows({"format": "pdf", "content_b64": base64.b64encode(pdf).decode()})[0]
        self.assertEqual(row.get("e_bituminous"), "977")
        self.assertEqual(row.get("e_granular"), "60")
        self.assertEqual(row.get("e_subgrade"), "70")   # not dropped / defaulted

    def test_stacked_multirow_header_fails_loud(self):
        # A merged/stacked 2-row header cannot align to the data columns; the parser
        # must REFUSE (clear error), never silently shift values into wrong columns.
        import base64
        pdf = make_table_pdf([
            ["", "Bituminous", "Granular", "Subgrade"],   # header line 1
            ["Sub-Section", "Layer", "Layer", ""],        # header line 2 (stacked)
            ["LHS-Sec-1", "977", "60", "70"],
        ])
        with self.assertRaises(ValueError) as ctx:
            api.fwd_overlay({"design_msa": 300, "format": "pdf",
                             "content_b64": base64.b64encode(pdf).decode()})
        self.assertIn("did not line up", str(ctx.exception))

    def test_prose_line_is_not_mistaken_for_a_header(self):
        from rams.api import _header_matches, _split_delimited
        prose = "The back calculated moduli of the bituminous granular layers"
        # words become separate cells, but a sentence resolves to < 3 schema
        # columns, so it must not be picked as the FWD header.
        self.assertFalse(_header_matches(_split_delimited(prose.replace(" ", "\t")),
                                         "e_bituminous"))
        header = "Sub-Section\tBituminous Layer\tGranular Layer\tSubgrade"
        self.assertTrue(_header_matches(_split_delimited(header), "e_bituminous"))


# --- dispatcher -------------------------------------------------------------

class TestDispatcher(unittest.TestCase):
    def test_dispatch_by_extension(self):
        res = ingest_segments("examples/sample_network.csv")
        self.assertEqual(len(res.segments), 8)

    def test_unsupported_extension_raises(self):
        with self.assertRaises(ValueError):
            ingest_segments("network.json")


# --- MCI --------------------------------------------------------------------

class TestMCI(unittest.TestCase):
    def test_matches_paper_formula(self):
        # MCI = 10 - 1.48*C^0.3 - 0.29*D^0.7 - 0.47*sigma^0.2
        c, d, s = 3.0, 6.0, 4.0
        expect = 10 - 1.48 * c ** 0.3 - 0.29 * d ** 0.7 - 0.47 * s ** 0.2
        self.assertAlmostEqual(compute_mci(d, c, s), round(expect, 2), places=2)

    def test_zero_distress_is_high(self):
        self.assertEqual(compute_mci(0.0, 0.0, 0.0), 10.0)

    def test_bands(self):
        self.assertEqual(mci_band(8.0), MCIBand.DESIRABLE)
        self.assertEqual(mci_band(4.0), MCIBand.NEEDS_REPAIR)
        self.assertEqual(mci_band(2.0), MCIBand.IMMEDIATE_REPAIR)

    def test_band_breakpoints(self):
        # >5 desirable; exactly 5 falls into the repair band; <3 immediate.
        self.assertEqual(mci_band(5.0), MCIBand.NEEDS_REPAIR)
        self.assertEqual(mci_band(3.0), MCIBand.NEEDS_REPAIR)
        self.assertEqual(mci_band(2.99), MCIBand.IMMEDIATE_REPAIR)

    def test_overlay_threshold_constant(self):
        self.assertEqual(RUT_OVERLAY_THRESHOLD_MM, 30.0)

    def test_monotonic_in_rutting(self):
        self.assertGreater(compute_mci(2.0, 1.0, 1.0), compute_mci(20.0, 1.0, 1.0))


# --- portal API -------------------------------------------------------------

class TestApiIngest(unittest.TestCase):
    def test_csv_text_import(self):
        csv_text = REQUIRED + "\nA,1.5,2,0,4.5,0.06,HIGH,1\n"
        out = api.ingest_data({"format": "csv", "content": csv_text})
        self.assertEqual(out["count"], 1)

    def test_pdf_import_base64(self):
        import base64

        pdf = make_pdf([REQUIRED, "A,1.5,2.0,0.0,4.5,0.06,HIGH,12.0"])
        out = api.ingest_data(
            {"format": "pdf", "content_b64": base64.b64encode(pdf).decode()}
        )
        self.assertEqual(out["count"], 1)

    def test_bad_format_raises(self):
        with self.assertRaises(ValueError):
            api.ingest_data({"format": "json", "content": "{}"})

    def test_bad_base64_raises(self):
        with self.assertRaises(ValueError):
            api.ingest_data({"format": "pdf", "content_b64": "!!notb64!!"})

    def test_forecast_includes_mci(self):
        out = api.forecast_single({"zone": "HIGH", "years": 10})
        self.assertEqual(len(out["mci"]), 10)
        self.assertIn("band", out["mci"][0])
        self.assertIn("rut_over_30mm", out["mci"][0])


if __name__ == "__main__":
    unittest.main()
