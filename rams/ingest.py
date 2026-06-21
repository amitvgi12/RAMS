"""
Multi-format network ingestion: CSV, XML and PDF -> validated SegmentInput.

Why this module exists (paper link):
    Taniguchi & Yoshida describe the MLIT-PMS *pavement databank*, which holds
    road-surface-attribute records -- cracking %, rut depth (mm), longitudinal
    roughness, traffic and pavement composition -- and exchanges them as report
    artefacts. A RAMS portal must ingest those records however they arrive: a
    PMS export (XML), a condition-survey report (PDF) or a flat CSV. This module
    adds XML and PDF loaders alongside the existing CSV path, all funnelled
    through the same `SegmentInput.validate()` trust boundary.

Security posture (consistent with the rest of RAMS):
    * XML is parsed with the stdlib `xml.etree.ElementTree`, and any document
      carrying a DOCTYPE/DTD is rejected up front. That blocks the classic XML
      attacks -- external-entity (XXE) file disclosure and "billion laughs"
      entity-expansion DoS -- which both require a DTD, without needing a
      third-party hardened parser in a zero-dependency government deployment.
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

__all__ = [
    "ingest_segments",
    "ingest_segments_csv",
    "ingest_segments_csv_text",
    "ingest_segments_xml",
    "ingest_segments_xml_text",
    "ingest_segments_pdf",
    "ingest_segments_pdf_bytes",
    "IngestResult",
]

# Optional roughness column (the MLIT-PMS "sigma"); not required by the engine
# but carried through so a faithful MCI can be computed downstream.
_OPTIONAL_FIELDS = ("length_km", "roughness_mm")

# Optional HDM-4 structural / FWD columns, consumed only by the HDM-4 rut model.
# Mapped onto SegmentInput kwargs when present; absent ones fall back to defaults.
# Hard cap on bytes we will read from an XML/PDF blob (DoS guard). CSV is read
# row-streaming (no blob cap, just MAX_ROWS), so large NSV CSVs are fine; XML/PDF
# are fully buffered, hence this ceiling.
MAX_BLOB_BYTES = 64 * 1024 * 1024


# --- shared row -> validated SegmentInput (defined in batch, reused here) ---
# `_segment_from_mapping` is imported from rams.batch so every importer (CSV on
# disk, XML, PDF, pasted text) shares one row->segment contract, including the
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
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")
    return _ingest_mappings(reader, bounds, max_rows)


# --- XML --------------------------------------------------------------------

def ingest_segments_xml(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Load segments from an XML pavement-databank export (file path)."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_BLOB_BYTES + 1)
    return ingest_segments_xml_text(data, bounds, max_rows)


def ingest_segments_xml_text(
    data, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Parse an XML document (str or bytes) into validated segments.

    Accepted shape (fields may be attributes or child elements, mixed freely)::

        <network>
          <segment id="NH66-KL-012" length_km="12.0">
            <base_iri>1.5</base_iri>
            <base_rut>2.0</base_rut>
            <base_crack>0.0</base_crack>
            <annual_msa>4.5</annual_msa>
            <traffic_growth_rate>0.06</traffic_growth_rate>
            <monsoon_zone>HIGH</monsoon_zone>
            <roughness_mm>3.0</roughness_mm>   <!-- optional sigma for MCI -->
          </segment>
        </network>
    """
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > MAX_BLOB_BYTES:
        raise ValueError("XML document too large.")
    # Reject any DTD: defends against XXE and billion-laughs without a 3rd-party
    # parser. Legitimate pavement-databank exports never need a DOCTYPE.
    if re.search(rb"<!DOCTYPE", raw, re.IGNORECASE):
        raise ValueError("XML DOCTYPE/DTD is not permitted (XXE/entity-expansion guard).")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"malformed XML: {exc}") from None

    seg_nodes = root.findall(".//segment") or (
        [root] if root.tag == "segment" else []
    )
    if not seg_nodes:
        raise ValueError("no <segment> elements found in XML.")

    def to_mapping(node: ET.Element) -> Dict[str, str]:
        row: Dict[str, str] = {}
        # Attributes first; child elements override (more specific).
        for k, v in node.attrib.items():
            row[_norm_key(k)] = v
        for child in node:
            text = (child.text or "").strip()
            if text:
                row[_norm_key(child.tag)] = text
        return row

    return _ingest_mappings((to_mapping(n) for n in seg_nodes), bounds, max_rows)


def _norm_key(key: str) -> str:
    """Map common attribute/element aliases onto the canonical column names."""
    k = key.strip().lower()
    return {
        "id": "segment_id",
        "sigma": "roughness_mm",
        "roughness": "roughness_mm",
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
        raise ValueError(
            "could not locate a segment table header in the PDF text "
            f"(expected columns: {', '.join(REQUIRED_COLUMNS)})."
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


# Tokens we care about: PDF literal strings and the operators that show text or
# advance to a new text line.
_PDF_TOKEN = re.compile(rb"\((?:\\.|[^\\()])*\)|\bTd\b|\bTD\b|\bT\*\b|\bTm\b|\bTj\b|\bTJ\b|'|\"")


def _text_from_content_stream(chunk: bytes) -> str:
    """Decode text-show operators in a content stream into text lines."""
    out_lines: List[str] = []
    current: List[str] = []
    for tok in _PDF_TOKEN.finditer(chunk):
        t = tok.group(0)
        if t.startswith(b"("):
            current.append(_decode_pdf_literal(t[1:-1]))
        elif t in (b"Td", b"TD", b"T*", b"Tm", b"'", b'"'):
            # A positioning / next-line operator => flush the current line.
            if current:
                out_lines.append("".join(current))
                current = []
    if current:
        out_lines.append("".join(current))
    return "\n".join(out_lines)


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
            if nxt.isdigit():  # up to 3 octal digits
                j = i + 1
                octal = b""
                while j < n and len(octal) < 3 and s[j : j + 1].isdigit():
                    octal += s[j : j + 1]
                    j += 1
                out.append(chr(int(octal, 8) & 0xFF))
                i = j
                continue
            out.append(nxt.decode("latin-1"))
            i += 2
            continue
        out.append(ch.decode("latin-1"))
        i += 1
    return "".join(out)


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


# --- format dispatcher ------------------------------------------------------

def ingest_segments(
    path: str, bounds: InputBounds = DEFAULT_BOUNDS, max_rows: int = MAX_ROWS
) -> IngestResult:
    """Ingest a network file, dispatching on extension (.csv / .xml / .pdf)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return ingest_segments_csv(path, bounds, max_rows)
    if ext == ".xml":
        return ingest_segments_xml(path, bounds, max_rows)
    if ext == ".pdf":
        return ingest_segments_pdf(path, bounds, max_rows)
    raise ValueError(f"unsupported input format {ext!r} (expected .csv, .xml or .pdf).")
