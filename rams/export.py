"""
Downloadable report writers -- XLSX and PDF -- in pure standard library.

Keeping the zero-dependency posture: an .xlsx is written as a ZIP of the minimal
Office Open XML parts, and a .pdf is written as a paginated Courier (monospace)
text table. Both take a header row + data rows and return raw bytes, ready to be
streamed as a download.
"""
from __future__ import annotations

import io
import zipfile
from typing import List, Sequence

# ---------------------------------------------------------------- XLSX --------

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PNS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_bytes(headers: Sequence, rows: Sequence[Sequence], sheet_name: str = "Report") -> bytes:
    """Minimal valid .xlsx from a header row + data rows (strings + numbers)."""
    strings: List[str] = []
    idx = {}

    def s_index(val: str) -> int:
        if val not in idx:
            idx[val] = len(strings)
            strings.append(val)
        return idx[val]

    def cell(col: str, r: int, val) -> str:
        ref = f"{col}{r}"
        if isinstance(val, bool):
            val = "Yes" if val else "No"
        if isinstance(val, (int, float)):
            return f'<c r="{ref}"><v>{val}</v></c>'
        return f'<c r="{ref}" t="s"><v>{s_index(str(val))}</v></c>'

    body = ['<row r="1">' + "".join(cell(_col_letter(i), 1, str(h))
                                    for i, h in enumerate(headers)) + "</row>"]
    for ri, row in enumerate(rows, start=2):
        body.append(f'<row r="{ri}">'
                    + "".join(cell(_col_letter(i), ri, v) for i, v in enumerate(row))
                    + "</row>")
    sheet = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<worksheet xmlns="{_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>')
    sst = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<sst xmlns="{_NS}" count="{len(strings)}" uniqueCount="{len(strings)}">'
           + "".join(f"<si><t>{_xml_escape(x)}</t></si>" for x in strings) + "</sst>")
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{_NS}" xmlns:r="{_RNS}"><sheets>'
                f'<sheet name="{_xml_escape(sheet_name[:31])}" sheetId="1" r:id="rId1"/>'
                f'</sheets></workbook>')
    wb_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<Relationships xmlns="{_PNS}">'
               f'<Relationship Id="rId1" Type="{_RNS}/worksheet" Target="worksheets/sheet1.xml"/>'
               f'<Relationship Id="rId2" Type="{_RNS}/sharedStrings" Target="sharedStrings.xml"/>'
               f'</Relationships>')
    root_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 f'<Relationships xmlns="{_PNS}">'
                 f'<Relationship Id="rId1" Type="{_RNS}/officeDocument" Target="xl/workbook.xml"/>'
                 f'</Relationships>')
    content_types = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     f'<Types xmlns="{_CT}">'
                     f'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     f'<Default Extension="xml" ContentType="application/xml"/>'
                     f'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     f'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                     f'</Types>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ----------------------------------------------------------------- PDF --------

def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_bytes(title: str, lines: Sequence[str], *, landscape: bool = True,
              font_size: int = 8) -> bytes:
    """Paginated Courier-text PDF (A4). `lines` are pre-formatted, fixed-width."""
    pw, ph = (842, 595) if landscape else (595, 842)
    margin, lead = 36, font_size + 3
    top = ph - margin
    per_page = max(1, int((ph - 2 * margin) / lead) - 2)

    all_lines = [title, ""] + list(lines)
    pages = [all_lines[i:i + per_page] for i in range(0, len(all_lines), per_page)] or [[title]]

    objs: List[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)  # 1-based object number

    catalog_n = add(b"")          # 1 -> filled later
    pages_n = add(b"")            # 2 -> filled later
    font_n = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    page_ns: List[int] = []
    for pg in pages:
        text = "".join(f"({_pdf_escape(ln)}) Tj T*\n" for ln in pg)
        stream = (f"BT /F1 {font_size} Tf {margin} {top} Td {lead} TL\n{text}ET").encode("latin-1", "replace")
        content_n = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page_obj = (
            f"<< /Type /Page /Parent {pages_n} 0 R /MediaBox [0 0 {pw} {ph}] "
            f"/Resources << /Font << /F1 {font_n} 0 R >> >> /Contents {content_n} 0 R >>"
        ).encode("latin-1")
        page_ns.append(add(page_obj))

    kids = " ".join(f"{n} 0 R" for n in page_ns)
    objs[catalog_n - 1] = f"<< /Type /Catalog /Pages {pages_n} 0 R >>".encode("latin-1")
    objs[pages_n - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ns)} >>".encode("latin-1")
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_n} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode("latin-1")
    return bytes(out)


# ------------------------------------------------ homogeneous-section reports --

_SECTION_HEADERS = ["Section", "From", "To", "Length_km", "Points", "Mean_IRI",
                    "Mean_Rut_mm", "Mean_Crack_pct", "PCI", "Band", "Treatment",
                    "Preventive_Year", "Final_PCI"]


def _section_rows(result) -> List[list]:
    rows = []
    for s in result.sections:
        rows.append([
            s.section_id, s.chainage_from, s.chainage_to, round(s.length_km, 3),
            s.n_points, round(s.mean_iri, 2), round(s.mean_rut, 2),
            round(s.mean_crack, 1), round(s.base_pci, 2), s.band, s.treatment,
            s.preventive_window_year if s.preventive_window_year is not None else "-",
            round(s.final_pci, 2),
        ])
    return rows


def sections_to_xlsx(result) -> bytes:
    """SectioningResult -> .xlsx bytes (one row per homogeneous section)."""
    return xlsx_bytes(_SECTION_HEADERS, _section_rows(result), "Homogeneous Sections")


_LCA_HEADERS = ["Year", "Cumulative_MSA", "IRI", "Rut_mm", "Crack_pct", "PCI",
                "Decision", "Treatment", "MoRTH_Reference", "Cost_Rs", "Cost_Lakh", "Deferred"]


def lca_to_xlsx(result) -> bytes:
    """LCAResult -> .xlsx bytes (one row per life-cycle year)."""
    rows = [[y.year, round(y.cumulative_msa, 2), round(y.iri, 2), round(y.rut, 1),
             round(y.crack, 1), round(y.pci, 2), y.decision, y.treatment,
             y.morth_reference, round(y.cost_inr, 0), round(y.cost_inr / 1e5, 2),
             "Yes" if y.deferred else ""]
            for y in result.years]
    return xlsx_bytes(_LCA_HEADERS, rows, "LCA Matrix")


def lca_to_pdf(result, title: str = "RAMS - Life-Cycle Decision Matrix (MoRTH SDB)") -> bytes:
    """LCAResult -> .pdf bytes (life-cycle matrix + economics)."""
    header = (f"{'Yr':>3}{'C.MSA':>8}{'IRI':>6}{'Rut':>6}{'Crk%':>6}{'PCI':>6}  "
              f"{'Decision':<15}{'Cost(lakh)':>11}")
    lines = [
        f"{result.segment_id}  |  {result.horizon_years} yr  |  {result.length_km:.1f} km x "
        f"{result.width_m:.1f} m  |  discount {result.discount_rate*100:.0f}%",
        f"Preventive {result.n_preventive} | Overlay {result.n_overlay} | "
        f"Reconstruction {result.n_reconstruction}",
        f"Total Rs {result.total_cost_inr/1e5:.1f} lakh | NPV Rs {result.npv_inr/1e5:.1f} lakh "
        f"| EUAC Rs {result.euac_inr/1e5:.1f} lakh/yr",
        "", header, "-" * len(header),
    ]
    for y in result.years:
        d = y.decision + ("*" if y.deferred else "")
        lines.append(f"{y.year:>3}{y.cumulative_msa:>8.1f}{y.iri:>6.2f}{y.rut:>6.1f}"
                     f"{y.crack:>6.1f}{y.pci:>6.2f}  {d:<15}{y.cost_inr/1e5:>11.2f}")
    lines += ["", "(* major treatment due but deferred by the minimum interval)",
              "Rates: MoRTH Standard Data Book (indicative) -- replace with current SDB/SoR."]
    return pdf_bytes(title, lines)


def sections_to_pdf(result, title: str = "RAMS - Homogeneous Section Forecast") -> bytes:
    """SectioningResult -> .pdf bytes (paginated section table)."""
    bands = result.as_dict()["band_counts"]
    header = (f"{'Section':<8}{'From':>9}{'To':>9}{'km':>7}{'pts':>5}{'IRI':>6}"
              f"{'Rut':>6}{'Crk%':>6}{'PCI':>6}  {'Band':<11}{'PrevYr':>7}  Treatment")
    lines = [
        f"Points: {result.n_points}   Sections: {len(result.sections)}   "
        f"Length: {result.total_length_km:.1f} km   Horizon: {result.horizon_years} yr   Key: {result.key}",
        f"Bands -> ROUTINE {bands.get('ROUTINE',0)} | PREVENTIVE {bands.get('PREVENTIVE',0)} "
        f"| STRUCTURAL {bands.get('STRUCTURAL',0)}",
        "",
        header, "-" * len(header),
    ]
    for s in result.sections:
        cf = f"{s.chainage_from:.0f}" if s.chainage_from is not None else "-"
        ct = f"{s.chainage_to:.0f}" if s.chainage_to is not None else "-"
        pv = str(s.preventive_window_year) if s.preventive_window_year is not None else "-"
        lines.append(
            f"{s.section_id:<8}{cf:>9}{ct:>9}{s.length_km:>7.1f}{s.n_points:>5}"
            f"{s.mean_iri:>6.2f}{s.mean_rut:>6.1f}{s.mean_crack:>6.1f}{s.base_pci:>6.2f}  "
            f"{s.band:<11}{pv:>7}  {s.treatment[:32]}"
        )
    return pdf_bytes(title, lines)
