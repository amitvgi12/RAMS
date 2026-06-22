"""
XLSX survey/FWD ingestion (stdlib zipfile + ElementTree, no third-party deps).

An .xlsx is a ZIP of XML parts; these tests build minimal-but-valid workbooks
in-memory and exercise the loader, the format dispatcher, the per-row error
isolation, the security guards, and the /api ingestion paths.
"""
import base64
import io
import os
import tempfile
import unittest
import zipfile

from rams import api
from rams.ingest import ingest_segments, ingest_segments_xlsx, ingest_segments_xlsx_bytes

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_HEADERS = ["segment_id", "base_iri", "base_rut", "base_crack", "annual_msa",
            "traffic_growth_rate", "monsoon_zone", "length_km", "deflection_mm"]
_ROWS = [
    ["NH66-KL-012", 1.5, 2.0, 0.0, 4.5, 0.06, "HIGH", 12.0, 0.85],
    ["SH-RJ-077", 3.0, 6.0, 8.0, 2.0, 0.03, "LOW", 20.0, 1.10],
]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col(i: int) -> str:
    return chr(ord("A") + i)


def make_xlsx(headers, rows) -> bytes:
    """Build a minimal valid .xlsx (shared strings + one worksheet) in memory."""
    strings, idx = [], {}

    def s_index(val: str) -> int:
        if val not in idx:
            idx[val] = len(strings)
            strings.append(val)
        return idx[val]

    for h in headers:
        s_index(str(h))
    for row in rows:
        for v in row:
            if isinstance(v, str):
                s_index(v)

    def cell(c: str, r: int, val) -> str:
        ref = f"{c}{r}"
        if isinstance(val, str):
            return f'<c r="{ref}" t="s"><v>{s_index(val)}</v></c>'
        return f'<c r="{ref}"><v>{val}</v></c>'

    body = [f'<row r="1">' + "".join(cell(_col(i), 1, str(h)) for i, h in enumerate(headers)) + "</row>"]
    for ri, row in enumerate(rows, start=2):
        body.append(f'<row r="{ri}">' + "".join(cell(_col(i), ri, v) for i, v in enumerate(row)) + "</row>")
    sheet = f'<?xml version="1.0"?><worksheet xmlns="{_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    sst = (f'<?xml version="1.0"?><sst xmlns="{_NS}" count="{len(strings)}" '
           f'uniqueCount="{len(strings)}">' + "".join(f"<si><t>{_esc(x)}</t></si>" for x in strings) + "</sst>")
    workbook = (f'<?xml version="1.0"?><workbook xmlns="{_NS}" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


class TestXlsxIngest(unittest.TestCase):
    def test_parses_rows(self):
        res = ingest_segments_xlsx_bytes(make_xlsx(_HEADERS, _ROWS))
        self.assertEqual(len(res.segments), 2)
        self.assertEqual(res.errors, [])
        s = res.segments[0]
        self.assertEqual(s.segment_id, "NH66-KL-012")
        self.assertAlmostEqual(s.base_iri, 1.5)
        self.assertAlmostEqual(s.annual_msa, 4.5)
        self.assertEqual(s.monsoon_zone.value, "HIGH")
        self.assertAlmostEqual(s.length_km, 12.0)

    def test_header_aliases(self):
        # 'id' -> segment_id, 'deflection' -> deflection_mm (via _norm_key)
        headers = ["id", "base_iri", "base_rut", "base_crack", "annual_msa",
                   "traffic_growth_rate", "monsoon_zone", "deflection"]
        rows = [["A-1", 1.6, 2.5, 1.0, 4.5, 0.05, "MEDIUM", 0.7]]
        res = ingest_segments_xlsx_bytes(make_xlsx(headers, rows))
        self.assertEqual(len(res.segments), 1)
        self.assertEqual(res.segments[0].segment_id, "A-1")
        self.assertAlmostEqual(res.segments[0].deflection_mm, 0.7)

    def test_missing_required_column_raises(self):
        headers = ["segment_id", "base_iri", "base_rut", "base_crack",
                   "annual_msa", "traffic_growth_rate"]  # no monsoon_zone
        with self.assertRaises(ValueError) as ctx:
            ingest_segments_xlsx_bytes(make_xlsx(headers, [["A", 1.5, 2, 0, 4.5, 0.05]]))
        # All sheets are scanned now; a workbook with no standard/survey sheet
        # reports that no worksheet was parseable.
        self.assertIn("no parseable data", str(ctx.exception))

    def test_bad_row_is_isolated(self):
        rows = [
            ["GOOD", 1.5, 2.0, 0.0, 4.5, 0.06, "HIGH", 12.0, 0.85],
            ["BAD", 1.5, 2.0, 0.0, 4.5, 0.06, "NOZONE", 5.0, 0.85],  # invalid zone
        ]
        res = ingest_segments_xlsx_bytes(make_xlsx(_HEADERS, rows))
        self.assertEqual(len(res.segments), 1)
        self.assertEqual(len(res.errors), 1)
        self.assertEqual(res.segments[0].segment_id, "GOOD")

    def test_not_a_zip_raises(self):
        with self.assertRaises(ValueError):
            ingest_segments_xlsx_bytes(b"this is plainly not a zip/xlsx file")

    def test_doctype_in_part_rejected(self):
        # Inject a DTD into the worksheet -> XXE guard must reject the workbook.
        good = make_xlsx(_HEADERS, _ROWS)
        zin = zipfile.ZipFile(io.BytesIO(good))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zout:
            for name in zin.namelist():
                data = zin.read(name)
                if name == "xl/worksheets/sheet1.xml":
                    data = data.replace(b"<?xml version=\"1.0\"?>",
                                        b"<?xml version=\"1.0\"?><!DOCTYPE x>")
                zout.writestr(name, data)
        with self.assertRaises(ValueError) as ctx:
            ingest_segments_xlsx_bytes(buf.getvalue())
        self.assertIn("DOCTYPE", str(ctx.exception))

    def test_dispatcher_and_file_path(self):
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        with os.fdopen(fd, "wb") as fh:
            fh.write(make_xlsx(_HEADERS, _ROWS))
        self.addCleanup(os.remove, path)
        self.assertEqual(len(ingest_segments(path).segments), 2)         # extension dispatch
        out = api.ingest_file(path, "xlsx")                              # streaming upload path
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["format"], "xlsx")
        self.assertGreater(out["segments"][0]["structural_number"], 0)   # derived from deflection


class TestXlsxApi(unittest.TestCase):
    def test_ingest_data_base64(self):
        b64 = base64.b64encode(make_xlsx(_HEADERS, _ROWS)).decode("ascii")
        out = api.ingest_data({"format": "xlsx", "content_b64": b64})
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["format"], "xlsx")

    def test_ingest_data_requires_b64(self):
        with self.assertRaises(ValueError):
            api.ingest_data({"format": "xlsx"})

    def test_bad_format_message_lists_xlsx(self):
        with self.assertRaises(ValueError) as ctx:
            api.ingest_data({"format": "json", "content": "x"})
        self.assertIn("xlsx", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
