"""Validity of the professional PDF layout engine (rams.pdfdoc)."""
import re
import unittest

from rams.pdfdoc import PdfDoc, _md_runs, text_width
from rams.ingest import _extract_pdf_text


def _build() -> bytes:
    d = PdfDoc(doc_title="Test Doc")
    d.cover("RAMS Guide", "A professional subtitle that may be long enough to wrap nicely",
            intro="Intro paragraph.", meta="meta line")
    d.heading("Section One", 1)
    d.paragraph("A normal paragraph of body text that should wrap across the page width "
                "because it is comfortably longer than a single line of Helvetica.")
    d.rich_paragraph(_md_runs("Inline **bold** and `code` runs render in their own fonts."))
    d.bullet("First bullet point with some text.")
    d.bullet("Second bullet.", indent=1)
    d.heading("A Table", 2)
    d.table(["Feature", "Required", "Optional"],
            [["FWD overlay", "e_bituminous", "e_granular, e_subgrade, h_bituminous"],
             ["Calibrate", "crack_prev, crack_next", "-"]])
    d.callout("An important callout note about repeat surveys.")
    d.code_block(["python -m rams.server --port 8000", "# open http://127.0.0.1:8000"])
    return d.render()


class TestPdfDoc(unittest.TestCase):
    def test_valid_pdf(self):
        data = _build()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"xref", data)
        self.assertIn(b"/Helvetica-Bold", data)
        self.assertIn(b"/Courier", data)
        self.assertIn(b"WinAnsiEncoding", data)

    def test_count_matches_kids(self):
        data = _build()
        count = int(re.search(rb"/Count (\d+)", data).group(1))
        kids = re.search(rb"/Kids \[([^\]]*)\]", data).group(1).split(b"0 R")
        self.assertEqual(count, len([k for k in kids if k.strip()]))
        self.assertGreaterEqual(count, 2)  # cover + at least one body page

    def test_text_recoverable(self):
        txt = _extract_pdf_text(_build())
        for kw in ("RAMS Guide", "Section One", "FWD overlay", "e_bituminous"):
            self.assertIn(kw, txt)

    def test_text_width_monotonic(self):
        self.assertLess(text_width("ab", "N", 10), text_width("abcd", "N", 10))
        self.assertGreater(text_width("WWW", "B", 10), text_width("iii", "B", 10))

    def test_md_runs_splits_styles(self):
        runs = _md_runs("plain **bold** `code` end")
        styles = [s for _, s in runs]
        self.assertIn("B", styles)
        self.assertIn("C", styles)
        self.assertIn("N", styles)


if __name__ == "__main__":
    unittest.main()
