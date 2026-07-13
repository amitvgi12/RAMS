"""
Multi-format network ingestion: CSV, XLSX and PDF -> validated SegmentInput.

Why this module exists:
    A RAMS portal must ingest condition/structural records however they arrive: a
    flat CSV, an XLSX workbook (NSV/condition survey, possibly multi-sheet) or a
    condition-survey PDF. All formats are funnelled through the same
    `SegmentInput.validate()` trust boundary.

Security posture (consistent with the rest of RAMS):
    * XLSX is a ZIP of XML parts read with the stdlib `zipfile` +
      `xml.etree.ElementTree`; any internal part carrying a DOCTYPE/DTD is
      rejected up front, blocking XXE / "billion laughs" without a third-party
      parser in a zero-dependency government deployment.
    * PDF text is recovered with a defensive, bounded stdlib extractor (or
      `pypdf` when installed). Only the text layer is read -- no embedded
      JavaScript, no external resources are followed.
    * Every loader caps rows (MAX_ROWS) and isolates per-record errors, so one
      malformed segment cannot abort a whole network import.
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
import zlib
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .batch import (
    MAX_ROWS,
    REQUIRED_COLUMNS,
    IngestResult,
    ingest_segments_csv,
    segment_from_mapping as _segment_from_mapping,
)
from .config import DEFAULT_BOUNDS, InputBounds, MonsoonZone
from .models import SegmentInput
from .survey import (
    aggregate_defects,
    detect_defect,
    detect_distress,
    expand_wide_lanes,
    is_survey,
    merge_surveys,
    segments_from_survey,
)

__all__ = [
    "ingest_segments",
    "ingest_segments_csv",
    "ingest_segments_csv_text",
    "ingest_segments_xlsx",
    "ingest_segments_xlsx_bytes",
    "ingest_segments_pdf",
    "ingest_segments_pdf_bytes",
    "ingest_multi_files",
    "ingest_workbook_parts",
    "IngestResult",
]

# Optional roughness column (the MLIT-PMS "sigma"); not required by the engine
# but carried through so a faithful MCI can be computed downstream.
_OPTIONAL_FIELDS = ("length_km", "roughness_mm")

# Optional HDM-4 structural / FWD columns, consumed only by the HDM-4 rut model.
# Mapped onto SegmentInput kwargs when present; absent ones fall back to defaults.
# Hard cap on bytes we will read from an XLSX/PDF blob (DoS guard). CSV is read
# row-streaming (no blob cap, just MAX_ROWS), so large NSV CSVs are fine; XLSX/PDF
# are fully buffered, hence this ceiling.
MAX_BLOB_BYTES = 64 * 1024 * 1024


# --- shared row -> validated SegmentInput (defined in batch, reused here) ---
# `_segment_from_mapping` is imported from rams.batch so every importer (CSV on
# disk, XLSX, PDF, pasted text) shares one row->segment contract, including the
# optional structural/FWD columns and FWD->SNP derivation.


def _ingest_mappings(
    rows: Iterable[Dict[str, str]], bounds: InputBounds, max_rows: int
) -> IngestResult:
    """Validate an iterable of row-mappings, isolating per-row errors."""
    segments: List[SegmentInput] = []
    errors: List[Tuple[int, str]] = []
    for i, row in enumerate(rows, start=1):
        if i > max_rows:
            errors.append((i, f"row limit {max_rows} exceeded; ingestion truncated"))
            break
        try:
            segments.append(_segment_from_mapping(row, bounds))
        except (ValueError, KeyError) as exc:
            errors.append((i, str(exc)))
    return IngestResult(segments=segments, errors=errors)


# --- CSV (in-memory text variant, for the web portal) -----------------------

def ingest_segments_csv_text(
    text: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Parse CSV *text* (not a file path) into validated segments.

    The on-disk `ingest_segments_csv` stays the canonical loader; this variant
    lets the portal ingest a pasted/uploaded CSV without a temp file.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in fields]
    if not missing:
        return _ingest_mappings(reader, bounds, max_rows)
    # Vendor NSV chainage survey? Normalise headers and try the survey mapper.
    norm = {c: _norm_key(c) for c in fields}
    if is_survey(norm.values()):
        rows = [{norm[k]: v for k, v in row.items() if k in norm} for row in reader]
        return segments_from_survey(rows, bounds=bounds)
    raise ValueError(f"CSV missing required columns: {missing}")


def _norm_key(key: str) -> str:
    """Normalise a column header: lower-case, collapse whitespace/newlines to one
    underscore, then map common aliases onto canonical names. So 'Start Chainage '
    -> 'start_chainage', 'Lane No.' -> 'lane', 'New NH\\nNumber' -> 'new_nh_number'."""
    k = re.sub(r"\s+", "_", str(key).strip().lower())
    return {
        "id": "segment_id",
        "sigma": "roughness_mm",
        "roughness": "roughness_mm",
        # chainage / lane aliases (vendor variants)
        "chainage_from": "start_chainage",
        "from_chainage": "start_chainage",
        "chainage_to": "end_chainage",
        "to_chainage": "end_chainage",
        "lane_no": "lane",
        "lane_no.": "lane",
        "lane_number": "lane",
        "lane/side": "lane",
        "direction": "lane",
        # FWD / structural aliases
        "deflection": "deflection_mm",
        "fwd_deflection": "deflection_mm",
        "fwd_deflection_mm": "deflection_mm",
        "benkelman": "deflection_mm",
        "snp": "structural_number",
        "sn": "structural_number",
        "comp": "compaction_pct",
        "compaction": "compaction_pct",
    }.get(k, k)


# --- XLSX (Office Open XML spreadsheet) -------------------------------------
# An .xlsx is a ZIP of XML parts. We read it with the stdlib zipfile + the same
# hardened ElementTree path used for XML -- no third-party (openpyxl/pandas)
# dependency, consistent with the zero-dependency government-deployment posture.
# The first worksheet's first row is the header (same columns as the CSV).

# Bound the *inflated* size of any single zip member and the member count, so a
# crafted "zip bomb" cannot exhaust memory on decompression.
_XLSX_MAX_PARTS = 1024
_XLSX_MAX_PART_BYTES = MAX_BLOB_BYTES


def ingest_segments_xlsx(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Load segments from an .xlsx survey/FWD workbook (file path)."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_BLOB_BYTES + 1)
    return ingest_segments_xlsx_bytes(data, bounds, max_rows)


def ingest_segments_xlsx_bytes(
    data: bytes, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Parse an .xlsx workbook (bytes) into validated segments.

    **All** worksheets are scanned: any sheet carrying the standard columns is
    ingested as segments, any sheet that looks like an NSV chainage survey
    (rutting/roughness/cracking/potholes) is parsed and the survey sheets are
    merged by chainage; sheets matching neither schema are skipped (not an error).
    Aliases (id, sigma, deflection, snp, 'Start Chainage', 'Lane No.', ...) are honoured.
    """
    raw = bytes(data)
    if len(raw) > MAX_BLOB_BYTES:
        raise ValueError("XLSX document too large.")
    std, std_errors, rowsets, infos = ingest_workbook_parts(raw, bounds, max_rows)
    segments = list(std)
    errors: List[Tuple[int, str]] = list(std_errors)
    if rowsets:
        merged = merge_surveys(rowsets, bounds=bounds)
        segments.extend(merged.segments)
        errors.extend(merged.errors)
    if not segments:
        detail = "; ".join(f"{n} [{s}]" for n, s in infos) or "no worksheets"
        raise ValueError(
            "no parseable data in any worksheet (need the standard columns or an "
            f"NSV chainage survey). Sheets: {detail}"
        )
    return IngestResult(segments=segments, errors=errors)


def ingest_workbook_parts(raw: bytes, bounds: InputBounds = DEFAULT_BOUNDS,
                          max_rows: int = MAX_ROWS):
    """Scan every worksheet -> (standard_segments, std_errors, survey_rowsets, sheet_infos).

    `survey_rowsets` is a list of normalised row-lists (one per survey sheet), so
    several distress sheets -- here or across files -- can be merged by chainage.
    `sheet_infos` is [(sheet_name, status)] for transparency in the UI.
    """
    std: List[SegmentInput] = []
    std_errors: List[Tuple[int, str]] = []
    rowsets: List[List[Dict[str, str]]] = []
    infos: List[Tuple[str, str]] = []
    for name, headers, rows in _xlsx_sheets(raw, max_rows):
        if not rows:
            infos.append((name, "empty"))
            continue
        hset = set(headers)
        if all(c in hset for c in REQUIRED_COLUMNS):
            res = _ingest_mappings(rows, bounds, max_rows)
            std.extend(res.segments)
            std_errors.extend(res.errors)
            infos.append((name, f"standard ({len(res.segments)} rows)"))
        elif is_survey(hset):
            rows = expand_wide_lanes(rows)
            rowsets.append(rows)
            infos.append((name, f"survey:{detect_distress(hset) or 'mixed'} ({len(rows)} rows)"))
        elif detect_defect(hset):
            kind = detect_defect(hset)
            agg = aggregate_defects(rows, kind)
            rowsets.append(agg)
            infos.append((name, f"defect:{kind} ({len(rows)} defects -> {len(agg)} sub-sections)"))
        else:
            infos.append((name, "skipped (unrecognised columns)"))
    return std, std_errors, rowsets, infos


def _xlsx_first_sheet_grid(raw: bytes, max_rows: int) -> List[Dict[str, str]]:
    """Return the first worksheet as a list of {column-letter: cell-text} rows."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("not a valid .xlsx file (bad ZIP container).") from None
    with zf:
        names = zf.namelist()
        if len(names) > _XLSX_MAX_PARTS:
            raise ValueError("XLSX has too many internal parts.")
        shared = _xlsx_shared_strings(zf, set(names))
        sheet = _xlsx_first_sheet_path(zf, set(names))
        root = _read_zip_xml(zf, sheet)
        sheet_data = root.find("{*}sheetData")
        if sheet_data is None:
            return []
        grid: List[Dict[str, str]] = []
        for row in sheet_data.findall("{*}row"):
            cells: Dict[str, str] = {}
            for c in row.findall("{*}c"):
                col = _xlsx_col_letters(c.get("r") or "")
                if col:
                    cells[col] = _xlsx_cell_value(c, shared)
            grid.append(cells)
            if len(grid) > max_rows + 1:  # header + capped data rows
                break
        return grid


def _col_to_idx(letters: str) -> int:
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return idx


def _idx_to_col(idx: int) -> str:
    s = ""
    while idx > 0:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_merges(root: ET.Element):
    """Merged-cell ranges as ((c1,r1),(c2,r2)) with 1-based column/row indices."""
    mc = root.find("{*}mergeCells")
    out = []
    if mc is None:
        return out
    for m in mc.findall("{*}mergeCell"):
        ref = m.get("ref") or ""
        if ":" not in ref:
            continue
        a, b = ref.split(":", 1)

        def cell(rf):
            mm = re.match(r"([A-Z]+)(\d+)", rf)
            return (_col_to_idx(mm.group(1)), int(mm.group(2))) if mm else (0, 0)
        out.append((cell(a), cell(b)))
    return out


def _expand_horizontal(cells: Dict[str, str], merges, row_no: int) -> Dict[str, str]:
    """Fill horizontal merges in a header row (a group label spread over columns)."""
    filled = dict(cells)
    for (c1, r1), (c2, r2) in merges:
        if r1 == row_no and r2 == row_no and c2 > c1:
            src = filled.get(_idx_to_col(c1), "")
            for ci in range(c1 + 1, c2 + 1):
                col = _idx_to_col(ci)
                if str(filled.get(col, "")).strip() == "":
                    filled[col] = src
    return filled


def _xlsx_sheets(raw: bytes, max_rows: int):
    """Every worksheet as (sheet_name, normalised_headers, normalised_dict_rows).

    Handles two real-world wrinkles generically (no per-template assumptions):
      * **multi-row / merged headers** -- if the top row has horizontal merges
        (group labels), the first two rows are combined into one composite header
        ('Roughness BI' + 'L1' -> 'roughness_bi_l1') and merges are expanded;
      * **duplicate columns** -- when several columns normalise to the same name
        (a vendor working block repeating headers), the *first non-empty* value
        wins, so a real measurement is never overwritten by a later flag column.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("not a valid .xlsx file (bad ZIP container).") from None
    out = []
    with zf:
        names = zf.namelist()
        if len(names) > _XLSX_MAX_PARTS:
            raise ValueError("XLSX has too many internal parts.")
        nameset = set(names)
        shared = _xlsx_shared_strings(zf, nameset)
        for sheet_name, path in _xlsx_all_sheet_paths(zf, nameset):
            root = _read_zip_xml(zf, path)
            merges = _xlsx_merges(root)
            sd = root.find("{*}sheetData")
            grid: List[Dict[str, str]] = []
            if sd is not None:
                for row in sd.findall("{*}row"):
                    cells: Dict[str, str] = {}
                    for c in row.findall("{*}c"):
                        col = _xlsx_col_letters(c.get("r") or "")
                        if col:
                            cells[col] = _xlsx_cell_value(c, shared)
                    grid.append(cells)
                    if len(grid) > max_rows + 2:
                        break
            if len(grid) < 2:
                out.append((sheet_name, [], []))
                continue

            # Two header rows when the top row carries a horizontal (group) merge.
            depth = 2 if any(r1 == 1 and r2 == 1 and c2 > c1
                             for (c1, r1), (c2, r2) in merges) and len(grid) >= 3 else 1
            hrows = [_expand_horizontal(grid[i], merges, i + 1) for i in range(depth)]
            all_cols = [c for c in grid[0]]
            for hr in hrows[1:]:
                all_cols += [c for c in hr if c not in all_cols]
            header: Dict[str, str] = {}
            for col in all_cols:
                parts = [str(hr.get(col, "")).strip() for hr in hrows]
                name = "_".join(p for p in parts if p)
                if name:
                    header[col] = _norm_key(name)

            rows = []
            for r in grid[depth:]:
                d: Dict[str, str] = {}
                for col, val in r.items():          # cells are in column order
                    key = header.get(col)
                    if key is None:
                        continue
                    if key not in d or str(d[key]).strip() in ("", "None"):
                        d[key] = val                # first non-empty wins on duplicates
                rows.append(d)
            out.append((sheet_name, list(dict.fromkeys(header.values())), rows))
    return out


def _xlsx_all_sheet_paths(zf: zipfile.ZipFile, names: set):
    """[(sheet_name, member_path)] for every worksheet, in workbook order."""
    try:
        wb = _read_zip_xml(zf, "xl/workbook.xml")
        rels = _read_zip_xml(zf, "xl/_rels/workbook.xml.rels")
        rid2tgt = {r.get("Id"): (r.get("Target") or "") for r in rels.findall("{*}Relationship")}
        sheets = wb.find("{*}sheets")
        out = []
        if sheets is not None:
            for s in sheets.findall("{*}sheet"):
                rid = _xlsx_attr(s, "id")
                tgt = (rid2tgt.get(rid, "") or "").lstrip("/")
                path = tgt if tgt.startswith("xl/") else "xl/" + tgt
                if path in names:
                    out.append((s.get("name") or path, path))
        if out:
            return out
    except ValueError:
        pass
    ws = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    return [(n.rsplit("/", 1)[-1], n) for n in ws]


def _read_zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    """Read and parse a ZIP member as XML, bounded and DTD-rejecting (XXE guard)."""
    try:
        with zf.open(name) as fh:
            blob = fh.read(_XLSX_MAX_PART_BYTES + 1)
    except KeyError:
        raise ValueError(f"XLSX is missing internal part {name!r}.") from None
    if len(blob) > _XLSX_MAX_PART_BYTES:
        raise ValueError(f"XLSX part {name!r} too large (possible zip bomb).")
    if re.search(rb"<!DOCTYPE", blob, re.IGNORECASE):
        raise ValueError("XLSX XML DOCTYPE/DTD is not permitted (XXE guard).")
    try:
        return ET.fromstring(blob)
    except ET.ParseError as exc:
        raise ValueError(f"malformed XLSX XML in {name!r}: {exc}") from None


def _xlsx_shared_strings(zf: zipfile.ZipFile, names: set) -> List[str]:
    """Read the shared-strings table (cells of type 's' index into this)."""
    if "xl/sharedStrings.xml" not in names:
        return []
    root = _read_zip_xml(zf, "xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in si.findall(".//{*}t")) for si in root.findall("{*}si")]


def _xlsx_first_sheet_path(zf: zipfile.ZipFile, names: set) -> str:
    """Resolve the first worksheet via workbook rels; fall back to a sensible default."""
    try:
        wb = _read_zip_xml(zf, "xl/workbook.xml")
        rels = _read_zip_xml(zf, "xl/_rels/workbook.xml.rels")
        sheets = wb.find("{*}sheets")
        first = sheets.find("{*}sheet") if sheets is not None else None
        rid = _xlsx_attr(first, "id") if first is not None else None
        if rid:
            for rel in rels.findall("{*}Relationship"):
                if rel.get("Id") == rid:
                    target = (rel.get("Target") or "").lstrip("/")
                    path = target if target.startswith("xl/") else "xl/" + target
                    if path in names:
                        return path
    except ValueError:
        pass  # fall through to the conventional defaults
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    worksheets = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
    if worksheets:
        return worksheets[0]
    raise ValueError("no worksheet found in XLSX.")


def _xlsx_attr(elem: ET.Element, local: str) -> Optional[str]:
    """Get an attribute by local name, ignoring its XML namespace (e.g. r:id)."""
    for key, val in elem.attrib.items():
        if key == local or key.endswith("}" + local):
            return val
    return None


def _xlsx_col_letters(ref: str) -> str:
    """'AB12' -> 'AB' (the column part of an A1-style cell reference)."""
    out = []
    for ch in ref:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out).upper()


def _xlsx_cell_value(cell: ET.Element, shared: List[str]) -> str:
    """Decode a cell to text, resolving shared strings and inline strings."""
    t = cell.get("t")
    if t == "s":  # shared string: <v> holds the index
        v = cell.find("{*}v")
        if v is not None and v.text is not None:
            idx = int(v.text)
            return shared[idx] if 0 <= idx < len(shared) else ""
        return ""
    if t == "inlineStr":  # inline string: <is><t>...</t></is>
        is_node = cell.find("{*}is")
        return "".join(tt.text or "" for tt in is_node.findall(".//{*}t")) if is_node is not None else ""
    v = cell.find("{*}v")  # number, boolean, or formula-string result
    return v.text if (v is not None and v.text is not None) else ""


# --- PDF --------------------------------------------------------------------

def ingest_segments_pdf(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Load segments from a (text-based) PDF condition report (file path)."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_BLOB_BYTES + 1)
    return ingest_segments_pdf_bytes(data, bounds, max_rows)


def ingest_segments_pdf_bytes(
    data: bytes, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Recover a delimited table from a digitally-generated PDF and ingest it.

    The PDF is expected to carry the same comma-delimited table as the CSV
    (a header row of REQUIRED_COLUMNS followed by data rows) in its text layer
    -- exactly what a PMS "print to PDF" of a databank query produces.

    Best-effort by design: this reads the text layer only. Scanned/image PDFs
    (no text layer) yield nothing and raise a clear error; those need OCR.
    """
    raw = bytes(data)
    if len(raw) > MAX_BLOB_BYTES:
        raise ValueError("PDF document too large.")
    text = _extract_pdf_text(raw)
    if not text.strip():
        raise ValueError(
            "no extractable text in PDF (is it a scanned/image PDF? OCR is required)."
        )
    rows = _csv_rows_from_text(text)
    if rows is None:
        hint = ""
        if _looks_like_fwd_report(text):
            hint = (" This looks like an FWD deflection/moduli report, which is "
                    "different data -- load it in the 'FWD remaining-life & overlay' "
                    "tool (Design tab), not the condition-survey importer.")
        raise ValueError(
            "could not read a condition-survey table from the PDF. This importer "
            "reads a digitally-generated, comma-delimited table with the columns: "
            f"{', '.join(REQUIRED_COLUMNS)}."
            + hint +
            " A formatted/scanned report PDF whose columns are aligned with spaces "
            "(not commas) will not parse -- paste the table as CSV, or upload a "
            ".csv / .xlsx instead."
        )
    return _ingest_mappings(rows, bounds, max_rows)


def _extract_pdf_text(data: bytes) -> str:
    """Extract the text layer. Prefers `pypdf`; falls back to a stdlib reader."""
    try:  # optional accelerator -- robust for arbitrary producers
        import pypdf  # type: ignore

        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # noqa: BLE001 - fall back to the stdlib path
        return _extract_pdf_text_stdlib(data)


def _extract_pdf_text_stdlib(data: bytes) -> str:
    """Minimal pure-stdlib PDF text recovery (FlateDecode + text operators).

    Sufficient for simple, digitally-generated table PDFs. Each content stream
    is inflated when compressed, then text-show operators are decoded into
    newline-separated lines.
    """
    lines: List[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", data, re.DOTALL):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass  # not compressed -- use as-is
        lines.append(_text_from_content_stream(chunk))
    return "\n".join(t for t in lines if t)


# Content-stream tokens: literal / hex strings, array delimiters (for TJ), numeric
# operands, and the text operators we track for positioning.
_CONTENT_TOKEN = re.compile(
    rb"\((?:\\.|[^\\()])*\)"          # ( literal string )
    rb"|<[0-9A-Fa-f\s]*>"             # <hex string>
    rb"|\[|\]"                        # TJ array delimiters
    rb"|[-+]?\d*\.\d+|[-+]?\d+"       # number
    rb"|T[dmDLf*]|Tj|TJ|BT|ET|'|\""   # text operators (incl. Tf)
)

# Row/column reconstruction thresholds, in multiples of the font size (so they
# scale with headings vs body text). Fragment widths are estimated from the glyph
# count, so the gap logic distinguishes an ordinary word space inside a cell from
# a real column gap -- a multi-word header like "Bituminous Layer" stays ONE cell.
_Y_TOL = 3.0        # PDF text units: fragments within this dy are the same row
_CHAR_W = 0.5       # approx glyph width as a fraction of the font size (em)
_SPACE_GAP = 0.10   # gap > this (x font size) after a fragment -> a word space
_COL_GAP = 0.85     # gap > this (x font size) -> a new column (tab), not a space


def _decode_hex_string(tok: bytes) -> str:
    body = re.sub(rb"\s+", b"", tok[1:-1])
    if len(body) % 2:
        body += b"0"
    try:
        return bytes.fromhex(body.decode("ascii")).decode("latin-1")
    except ValueError:
        return ""


def _text_from_content_stream(chunk: bytes) -> str:
    """Recover text from a content stream, reconstructing visual rows.

    Each shown string is recorded with the (x, y) of the text line it sits on
    (tracked through Tm / Td / TD / T* / TL) and the current font size (Tf).
    Fragments are grouped by y into rows and, within a row, joined by the gap to
    the previous fragment: a small gap is a word space (same cell), a large gap is
    a column boundary (tab). So a report table whose cells are individually placed
    on the page is recovered as tab-separated rows, while ordinary prose stays a
    readable line and a multi-word header cell is not split into columns. A
    "print-to-PDF of a CSV" still comes out one row per line. Best-effort:
    translations + font size only (no per-glyph widths, kerning matrices, rotation).
    """
    records: List[Tuple[float, float, str, float]] = []   # (y, x, text, size)
    tlm_x = tlm_y = 0.0
    leading = 0.0
    font_size = 10.0
    operands: List[float] = []
    last_string = ""
    array_buf: List[str] = []
    in_array = False

    for tok in _CONTENT_TOKEN.finditer(chunk):
        t = tok.group(0)
        c0 = t[:1]
        if c0 == b"(":
            s = _decode_pdf_literal(t[1:-1])
            if in_array:
                array_buf.append(s)
            else:
                last_string = s
        elif c0 == b"<":
            s = _decode_hex_string(t)
            if in_array:
                array_buf.append(s)
            else:
                last_string = s
        elif t == b"[":
            in_array = True
            array_buf = []
        elif t == b"]":
            in_array = False
            last_string = "".join(array_buf)
        elif c0 in b"-+0123456789.":
            try:
                operands.append(float(t))
            except ValueError:
                pass
        elif t == b"Tf":
            if operands:
                font_size = abs(operands[-1]) or 10.0
            operands = []
        elif t == b"Tm":
            if len(operands) >= 6:
                tlm_x, tlm_y = operands[-2], operands[-1]
            operands = []
        elif t in (b"Td", b"TD"):
            if len(operands) >= 2:
                tlm_x += operands[-2]
                tlm_y += operands[-1]
                if t == b"TD":
                    leading = -operands[-1]
            operands = []
        elif t == b"TL":
            if operands:
                leading = operands[-1]
            operands = []
        elif t == b"T*":
            tlm_y -= leading
            operands = []
        elif t in (b"Tj", b"'", b'"'):
            if t in (b"'", b'"'):
                tlm_y -= leading          # these show on the next line first
            if last_string:
                records.append((tlm_y, tlm_x, last_string, font_size))
            last_string = ""
            operands = []
        elif t == b"TJ":
            if last_string:
                records.append((tlm_y, tlm_x, last_string, font_size))
            last_string = ""
            operands = []
        elif t == b"BT":
            tlm_x = tlm_y = leading = 0.0
            operands = []
        else:
            operands = []
    return _reconstruct_rows(records)


def _reconstruct_rows(records: List[Tuple[float, float, str, float]]) -> str:
    """Group positioned fragments into rows (by y) and cells (by x-gap)."""
    if not records:
        return ""
    records.sort(key=lambda r: (-r[0], r[1]))     # top-to-bottom, then left-to-right
    rows: List[List[Tuple[float, str, float]]] = []
    ref_y = None
    for y, x, txt, size in records:
        if ref_y is None or abs(y - ref_y) > _Y_TOL:
            rows.append([])
            ref_y = y
        rows[-1].append((x, txt, size))
    lines: List[str] = []
    for cells in rows:
        cells.sort(key=lambda c: c[0])
        parts: List[str] = []
        prev_end = None
        for x, txt, size in cells:
            if prev_end is None:
                parts.append(txt)
            else:
                gap = x - prev_end
                if gap > size * _COL_GAP:
                    parts.append(txt)                 # -> joined with a tab: new column
                elif gap > size * _SPACE_GAP:
                    parts[-1] += " " + txt            # word space within the same cell
                else:
                    parts[-1] += txt                  # abutting / kerned continuation
            prev_end = x + len(txt) * size * _CHAR_W
        line = "\t".join(p.strip() for p in parts).strip("\t")
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


_PDF_ESCAPES = {
    b"n": "\n", b"r": "\r", b"t": "\t", b"b": "\b", b"f": "\f",
    b"(": "(", b")": ")", b"\\": "\\",
}


def _decode_pdf_literal(s: bytes) -> str:
    """Decode a PDF literal string body, handling \\-escapes and octal codes."""
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i : i + 1]
        if ch == b"\\" and i + 1 < n:
            nxt = s[i + 1 : i + 2]
            if nxt in _PDF_ESCAPES:
                out.append(_PDF_ESCAPES[nxt])
                i += 2
                continue
            if b"0" <= nxt <= b"7":  # up to 3 OCTAL digits (0-7 only)
                j = i + 1
                octal = b""
                while j < n and len(octal) < 3 and b"0" <= s[j : j + 1] <= b"7":
                    octal += s[j : j + 1]
                    j += 1
                out.append(chr(int(octal, 8) & 0xFF))
                i = j
                continue
            # Any other char after a backslash: per the PDF spec the backslash is
            # ignored and the character is taken literally (this covers \8 and \9,
            # which are NOT octal escapes).
            out.append(nxt.decode("latin-1"))
            i += 2
            continue
        out.append(ch.decode("latin-1"))
        i += 1
    return "".join(out)


def _looks_like_fwd_report(text: str) -> bool:
    """Heuristic: does the recovered PDF text read like an FWD deflection/moduli
    report (so we can point the user at the FWD overlay tool)?"""
    low = text.lower()
    hits = sum(kw in low for kw in (
        "falling weight", "fwd", "deflection", "back-calcul", "backcalcul",
        "modulus", "moduli", "benkelman", "d0 ", "central deflection",
    ))
    return hits >= 2


def _csv_rows_from_text(text: str) -> Optional[List[Dict[str, str]]]:
    """Locate the table header in recovered text and DictReader the rows.

    Returns None if no line containing the required columns can be found.
    """
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for idx, ln in enumerate(raw_lines):
        cells = [c.strip() for c in ln.split(",")]
        if all(col in cells for col in REQUIRED_COLUMNS):
            header_idx = idx
            break
    if header_idx is None:
        return None
    table_text = "\n".join(raw_lines[header_idx:])
    return list(csv.DictReader(io.StringIO(table_text)))


# --- multi-file ingest (merge 4-distress surveys across files & sheets) -----

def _csv_parts(text: str, bounds: InputBounds, max_rows: int):
    """Classify a CSV: (std_segments, std_errors, survey_rowsets, status)."""
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    norm = {c: _norm_key(c) for c in fields}
    rows = [{norm[k]: v for k, v in row.items() if k in norm} for row in reader]
    hset = set(norm.values())
    if all(c in hset for c in REQUIRED_COLUMNS):
        res = _ingest_mappings(rows, bounds, max_rows)
        return res.segments, res.errors, [], f"standard ({len(res.segments)} rows)"
    if is_survey(hset):
        return [], [], [rows], f"survey ({len(rows)} rows)"
    return [], [], [], "skipped (unrecognised columns)"


def ingest_multi_files(files, bounds: InputBounds = DEFAULT_BOUNDS,
                       max_rows: int = MAX_ROWS):
    """Ingest several files at once and merge their surveys.

    `files` is [(filename, raw_bytes)]. Every survey sheet across **all** files
    (e.g. separate rutting / roughness / cracking / pothole exports) is merged by
    (chainage, lane) into the fully-populated condition; standard-schema sheets/
    files are appended. Returns (IngestResult, [(filename, status)]).
    """
    std: List[SegmentInput] = []
    errors: List[Tuple[int, str]] = []
    rowsets: List[List[Dict[str, str]]] = []
    infos: List[Tuple[str, str]] = []
    for name, raw in files:
        ext = os.path.splitext(name)[1].lower()
        try:
            if ext == ".xlsx":
                s, e, rs, sheet_infos = ingest_workbook_parts(bytes(raw), bounds, max_rows)
                std.extend(s); errors.extend(e); rowsets.extend(rs)
                infos.append((name, "; ".join(f"{n} [{st}]" for n, st in sheet_infos)))
            elif ext == ".csv":
                s, e, rs, status = _csv_parts(
                    raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw,
                    bounds, max_rows)
                std.extend(s); errors.extend(e); rowsets.extend(rs)
                infos.append((name, status))
            elif ext == ".pdf":
                res = ingest_segments_pdf_bytes(bytes(raw), bounds, max_rows)
                std.extend(res.segments); errors.extend(res.errors)
                infos.append((name, f"pdf ({len(res.segments)} segments)"))
            else:
                infos.append((name, "skipped (unsupported type)"))
        except ValueError as exc:
            infos.append((name, f"error: {exc}"))
    segments = list(std)
    if rowsets:
        merged = merge_surveys(rowsets, bounds=bounds)
        segments.extend(merged.segments)
        errors.extend(merged.errors)
    return IngestResult(segments=segments, errors=errors), infos


# --- format dispatcher ------------------------------------------------------

def ingest_segments(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Ingest a network file, dispatching on extension (.csv / .xlsx / .pdf)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return ingest_segments_csv(path, bounds, max_rows)
    if ext == ".xlsx":
        return ingest_segments_xlsx(path, bounds, max_rows)
    if ext == ".pdf":
        return ingest_segments_pdf(path, bounds, max_rows)
    raise ValueError(f"unsupported input format {ext!r} (expected .csv, .xlsx or .pdf).")
