"""
Multi-file upload + multi-sheet workbook ingestion (merge surveys by chainage).
"""
import base64
import io
import unittest
import zipfile

from rams import api
from rams.ingest import ingest_multi_files, ingest_segments_xlsx_bytes, ingest_workbook_parts
from tests.test_xlsx import _NS, _col, _esc

_RUT_CSV = "Start_Chainage,End_Chainage,Lane,Rutting\n0,100,L1,8.0\n100,200,L1,4.0\n"
_CRK_CSV = "Start_Chainage,End_Chainage,Lane,Condition\n0,100,L1,Good (5-10%)\n100,200,L1,Very Good (<5%)\n"


def make_multisheet_xlsx(sheets):
    """Build an .xlsx with several worksheets. sheets = [(name, headers, rows)]."""
    strings, idx = [], {}

    def s_index(v):
        if v not in idx:
            idx[v] = len(strings)
            strings.append(v)
        return idx[v]

    def cell(c, r, val):
        ref = f"{c}{r}"
        if isinstance(val, str):
            return f'<c r="{ref}" t="s"><v>{s_index(val)}</v></c>'
        return f'<c r="{ref}"><v>{val}</v></c>'

    sheet_xmls = []
    for headers, rows in (s[1:] for s in sheets):
        body = ['<row r="1">' + "".join(cell(_col(i), 1, str(h)) for i, h in enumerate(headers)) + "</row>"]
        for ri, row in enumerate(rows, start=2):
            body.append(f'<row r="{ri}">' + "".join(cell(_col(i), ri, v) for i, v in enumerate(row)) + "</row>")
        sheet_xmls.append(f'<?xml version="1.0"?><worksheet xmlns="{_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>')

    n = len(sheets)
    sst = (f'<?xml version="1.0"?><sst xmlns="{_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{_esc(x)}</t></si>" for x in strings) + "</sst>")
    sheet_tags = "".join(f'<sheet name="{_esc(sheets[i][0])}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i in range(n))
    workbook = (f'<?xml version="1.0"?><workbook xmlns="{_NS}" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<sheets>{sheet_tags}</sheets></workbook>')
    rels = ('<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i+1}" '
                      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      f'Target="worksheets/sheet{i+1}.xml"/>' for i in range(n))
            + "</Relationships>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        for i, xml in enumerate(sheet_xmls):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", xml)
    return buf.getvalue()


def make_grouped_header_xlsx():
    """A wide sheet with a 2-row grouped header (horizontal merges) + a duplicate
    working block, mirroring real vendor condition templates (no fixed schema)."""
    NS = _NS
    strs, idx = [], {}

    def si(v):
        if v not in idx:
            idx[v] = len(strs)
            strs.append(v)
        return idx[v]

    def c(ref, val):
        if isinstance(val, str):
            return f'<c r="{ref}" t="s"><v>{si(val)}</v></c>'
        return f'<c r="{ref}"><v>{val}</v></c>'

    # row1 group labels (A1:A2,B1:B2 vertical; C1:D1 'Rut Depth'; E1:F1 'Roughness BI')
    row1 = c("A1", "Start_Chainage") + c("B1", "Lane Direction") + c("C1", "Rut Depth") + c("E1", "Roughness BI")
    row2 = c("C2", "L1") + c("D2", "R1") + c("E2", "L1") + c("F2", "R1")
    # data: L1 rut 8 / R1 rut 6 ; L1 BI 3000 / R1 BI 2500
    row3 = c("A3", "0") + c("C3", 8.0) + c("D3", 6.0) + c("E3", 3000.0) + c("F3", 2500.0)
    merges = ('<mergeCells count="4"><mergeCell ref="A1:A2"/><mergeCell ref="B1:B2"/>'
              '<mergeCell ref="C1:D1"/><mergeCell ref="E1:F1"/></mergeCells>')
    sheet = (f'<?xml version="1.0"?><worksheet xmlns="{NS}"><sheetData>'
             f'<row r="1">{row1}</row><row r="2">{row2}</row><row r="3">{row3}</row>'
             f'</sheetData>{merges}</worksheet>')
    sst = (f'<?xml version="1.0"?><sst xmlns="{NS}" count="{len(strs)}" uniqueCount="{len(strs)}">'
           + "".join(f"<si><t>{_esc(x)}</t></si>" for x in strs) + "</sst>")
    workbook = (f'<?xml version="1.0"?><workbook xmlns="{NS}" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Cond" sheetId="1" r:id="rId1"/></sheets></workbook>')
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


class TestGenericHeaders(unittest.TestCase):
    def test_grouped_2row_header_and_lanes(self):
        # Generic merged 2-row header -> composite 'rut_depth_l1' etc, expanded per lane.
        res = ingest_segments_xlsx_bytes(make_grouped_header_xlsx())
        by = {s.segment_id: s for s in res.segments}
        self.assertIn("CH0/L1", by)
        self.assertAlmostEqual(by["CH0/L1"].base_rut, 8.0)
        self.assertAlmostEqual(by["CH0/L1"].base_iri, 3.0)   # BI 3000 / 1000
        self.assertAlmostEqual(by["CH0/R1"].base_rut, 6.0)

    def test_duplicate_columns_first_nonempty_wins(self):
        from tests.test_xlsx import make_xlsx
        # A real 'rutting' value then a later duplicate working column of 0.
        data = make_xlsx(["start_chainage", "end_chainage", "lane", "rutting", "rutting"],
                         [["0", "100", "L1", 8.5, 0]])
        res = ingest_segments_xlsx_bytes(data)
        self.assertAlmostEqual(res.segments[0].base_rut, 8.5)   # not the later 0

    def test_expand_wide_lanes_first_wins(self):
        from rams.survey import expand_wide_lanes
        rows = [{"start_chainage": "0", "l1_rut_depth_(100m)": "5.0", "l1_rut_depth": "0"}]
        out = expand_wide_lanes(rows)
        l1 = [r for r in out if r.get("lane") == "L1"][0]
        self.assertEqual(l1["rutting"], "5.0")   # real block wins over the 0 flag


class TestMultiSheet(unittest.TestCase):
    def test_skips_nonsurvey_first_sheet(self):
        # First sheet is junk, second is a rutting survey -> no crash, survey used.
        data = make_multisheet_xlsx([
            ("Summary", ["Note", "Value"], [["road", "NH-x"]]),
            ("Rutting", ["Start_Chainage", "End_Chainage", "Lane", "Rutting"],
             [["0", "100", "L1", 8.0], ["100", "200", "L1", 4.0]]),
        ])
        res = ingest_segments_xlsx_bytes(data)
        self.assertEqual(len(res.segments), 2)
        self.assertAlmostEqual(res.segments[0].base_rut, 8.0)

    def test_merges_distress_sheets_within_one_workbook(self):
        # Two survey sheets (rut + crack) merge by chainage in a single file.
        data = make_multisheet_xlsx([
            ("Rutting", ["Start_Chainage", "End_Chainage", "Lane", "Rutting"],
             [["0", "100", "L1", 9.0]]),
            ("Cracking", ["Start_Chainage", "End_Chainage", "Lane", "Condition"],
             [["0", "100", "L1", "Fair (10-20%)"]]),
        ])
        res = ingest_segments_xlsx_bytes(data)
        self.assertEqual(len(res.segments), 1)
        self.assertAlmostEqual(res.segments[0].base_rut, 9.0)
        self.assertAlmostEqual(res.segments[0].base_crack, 15.0)

    def test_all_junk_sheets_raise(self):
        data = make_multisheet_xlsx([("Junk", ["a", "b"], [["1", "2"]])])
        with self.assertRaises(ValueError) as ctx:
            ingest_segments_xlsx_bytes(data)
        self.assertIn("no parseable data", str(ctx.exception))

    def test_workbook_parts_returns_four(self):
        data = make_multisheet_xlsx([("Rutting", ["start_chainage", "lane", "rutting"],
                                      [["0", "L1", 5.0]])])
        parts = ingest_workbook_parts(data)
        self.assertEqual(len(parts), 4)  # std, std_errors, rowsets, infos


class TestMultiFile(unittest.TestCase):
    def test_merge_csv_files(self):
        res, infos = ingest_multi_files([("rut.csv", _RUT_CSV.encode()),
                                         ("crk.csv", _CRK_CSV.encode())])
        self.assertEqual(len(res.segments), 2)            # two chainages, merged
        s0 = {s.segment_id: s for s in res.segments}["CH0-100/L1"]
        self.assertAlmostEqual(s0.base_rut, 8.0)
        self.assertAlmostEqual(s0.base_crack, 7.5)
        self.assertEqual(len(infos), 2)

    def test_mixed_distress_xlsx_files(self):
        rut = make_multisheet_xlsx([("R", ["start_chainage", "end_chainage", "lane", "rutting"],
                                     [["0", "100", "L1", 7.0]])])
        rough = make_multisheet_xlsx([("G", ["start_chainage", "end_chainage", "lane", "lane_iri"],
                                       [["0", "100", "L1", 3.4]])])
        res, _ = ingest_multi_files([("rut.xlsx", rut), ("rough.xlsx", rough)])
        self.assertEqual(len(res.segments), 1)
        self.assertAlmostEqual(res.segments[0].base_rut, 7.0)
        self.assertAlmostEqual(res.segments[0].base_iri, 3.4)


class TestMultiApi(unittest.TestCase):
    def test_ingest_multi_endpoint(self):
        files = [{"name": "rut.csv", "content_b64": base64.b64encode(_RUT_CSV.encode()).decode()},
                 {"name": "crk.csv", "content_b64": base64.b64encode(_CRK_CSV.encode()).decode()}]
        out = api.ingest_multi({"files": files})
        self.assertEqual(out["count"], 2)
        self.assertEqual(len(out["files"]), 2)
        self.assertEqual(out["format"], "multi")

    def test_ingest_multi_bad_payload(self):
        with self.assertRaises(ValueError):
            api.ingest_multi({"files": []})
        with self.assertRaises(ValueError):
            api.ingest_multi({"files": [{"name": "x.csv"}]})         # no content_b64
        with self.assertRaises(ValueError):
            api.ingest_multi({"files": [{"name": "x.csv", "content_b64": "!!notb64"}]})


if __name__ == "__main__":
    unittest.main()
