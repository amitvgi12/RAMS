"""
A small, professional PDF layout engine -- pure standard library, no deps.

Unlike `rams.export.pdf_bytes` (a monospace text dump), this lays out a real
document: proportional Helvetica type, styled headings with rules, shaded and
wrapped tables, bulleted lists, callouts, code blocks, a cover page, and running
headers/footers with page numbers. It uses the PDF base-14 fonts (no embedding)
with WinAnsi encoding and the published Adobe Helvetica metrics for measurement.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

# Adobe AFM advance widths (per 1000 em) for ASCII 32..126.
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
_HELVB = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]
# A few common WinAnsi punctuation glyphs (unicode -> width), used for measurement.
_EXTRA = {0x2013: 556, 0x2014: 1000, 0x2022: 350, 0x2018: 222, 0x2019: 222,
          0x201C: 333, 0x201D: 333, 0x2026: 1000, 0x00B2: 333, 0x00D7: 584,
          0x00B1: 584, 0x00B0: 400, 0x00B5: 556}

# Transliterate the symbols WinAnsi cannot represent.
_TRANS = {"₹": "Rs ", "→": "->", "←": "<-", "ε": "eps",
          "σ": "sigma", "≈": "~", "≥": ">=", "≤": "<=",
          " ": " ", "‑": "-"}

# Style palette (RGB 0..1).
INK = (0.122, 0.231, 0.341)     # 1F3B57 deep navy
GREY = (0.42, 0.46, 0.50)
FAINT = (0.62, 0.66, 0.70)
RULE = (0.80, 0.84, 0.88)
LIGHT = (0.945, 0.960, 0.975)   # row / box fill
ACCENT = (0.95, 0.62, 0.0)      # amber accent
WHITE = (1, 1, 1)

# Font ids in the page resources.
_FID = {"N": 1, "B": 2, "O": 3, "C": 4}


def _prep(s: str) -> str:
    for k, v in _TRANS.items():
        s = s.replace(k, v)
    return s


def char_width(ch: str, font: str) -> int:
    if font == "C":
        return 600
    o = ord(ch)
    if o in _EXTRA:
        return _EXTRA[o]
    table = _HELVB if font == "B" else _HELV
    return table[o - 32] if 32 <= o <= 126 else table[0]


def text_width(s: str, font: str, size: float) -> float:
    return sum(char_width(c, font) for c in s) * size / 1000.0


def _esc(s: str) -> bytes:
    b = _prep(s).encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class PdfDoc:
    def __init__(self, doc_title: str = "", page: Tuple[int, int] = (595, 842), margin: int = 54):
        self.pw, self.ph = page
        self.m = margin
        self.x0 = margin
        self.x1 = self.pw - margin
        self.W = self.x1 - self.x0
        self.ctop = self.ph - margin
        self.cbot = margin + 6
        self.doc_title = _prep(doc_title)
        self._pages: List[List[bytes]] = []
        self._ops: List[bytes] = []
        self.y = self.ctop
        self._cover = False

    # ---- low-level drawing ------------------------------------------------
    def _txt(self, x: float, y: float, s: str, font: str, size: float, color) -> None:
        r, g, b = color
        self._ops.append(
            (f"BT /F{_FID[font]} {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg "
             f"{x:.2f} {y:.2f} Td (").encode("ascii") + _esc(s) + b") Tj ET")

    def _rect(self, x: float, y: float, w: float, h: float, color) -> None:
        r, g, b = color
        self._ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f".encode("ascii"))

    def _line(self, x1: float, y1: float, x2: float, y2: float, color, w: float = 0.6) -> None:
        r, g, b = color
        self._ops.append(
            f"{r:.3f} {g:.3f} {b:.3f} RG {w:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S".encode("ascii"))

    # ---- pagination -------------------------------------------------------
    def _start_page(self) -> None:
        self._ops = []
        self.y = self.ctop
        if not self._cover:
            self._txt(self.x1 - text_width(self.doc_title, "N", 8), self.ph - self.m + 16,
                      self.doc_title, "N", 8, FAINT)
            self._line(self.x0, self.ph - self.m + 11, self.x1, self.ph - self.m + 11, RULE, 0.5)

    def _end_page(self) -> None:
        self._pages.append(self._ops)

    def _newpage(self) -> None:
        self._end_page()
        self._start_page()

    def _need(self, h: float) -> None:
        if self.y - h < self.cbot:
            self._newpage()

    def spacer(self, h: float) -> None:
        self.y -= h

    # ---- elements ---------------------------------------------------------
    def cover(self, title: str, subtitle: str, intro: str = "", meta: str = "",
              contents: Optional[Sequence[str]] = None) -> None:
        self._cover = True
        self._start_page()
        band_h = 210
        self._rect(0, self.ph - band_h, self.pw, band_h, INK)
        self._rect(0, self.ph - band_h - 6, self.pw, 6, ACCENT)
        self._txt(self.m, self.ph - 104, title, "B", 30, WHITE)
        yy = self.ph - 138
        for ln in self._wrap(subtitle, "N", 13, self.pw - 2 * self.m):
            self._txt(self.m, yy, ln, "N", 13, (0.86, 0.90, 0.94))
            yy -= 18
        self.y = self.ph - band_h - 40
        if intro:
            self.paragraph(intro, color=(0.20, 0.24, 0.28))
            self.y -= 6
        if contents:
            self._txt(self.x0, self.y - 11, "WHAT'S INSIDE", "B", 11, INK)
            self.y -= 22
            col_w = (self.W - 24) / 2
            half = (len(contents) + 1) // 2
            cols = [contents[:half], contents[half:]]
            top = self.y
            for ci, items in enumerate(cols):
                cx = self.x0 + ci * (col_w + 24)
                cy = top
                for item in items:
                    for j, ln in enumerate(self._wrap(_prep(item), "N", 10, col_w - 16)):
                        if j == 0:
                            self._txt(cx, cy - 10, "•", "B", 10, ACCENT)
                        self._txt(cx + 14, cy - 10, ln, "N", 10, (0.20, 0.24, 0.28))
                        cy -= 15
                    cy -= 3
            self.y = min(self.y, top)  # keep cursor sane
        if meta:
            self.y = self.cbot + 34
            self._line(self.x0, self.y + 10, self.x1, self.y + 10, RULE, 0.5)
            self._txt(self.m, self.y - 6, meta, "O", 9, GREY)
        self._end_page()
        self._cover = False
        self._start_page()

    def heading(self, text: str, level: int = 1) -> None:
        text = _prep(text)
        if level == 1:
            self.spacer(16); self._need(40)
            self._txt(self.x0, self.y - 15, text, "B", 15, INK)
            self.y -= 21
            self._line(self.x0, self.y, self.x1, self.y, INK, 0.9)
            self.y -= 9
        elif level == 2:
            self.spacer(11); self._need(26)
            self._txt(self.x0, self.y - 12, text, "B", 11.5, INK)
            self.y -= 19
        else:
            self.spacer(7); self._need(20)
            self._txt(self.x0, self.y - 10, text, "B", 10, GREY)
            self.y -= 15

    def _wrap(self, text: str, font: str, size: float, width: float) -> List[str]:
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if text_width(trial, font, size) <= width or not cur:
                cur = trial
            else:
                lines.append(cur); cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def paragraph(self, text: str, size: float = 10, lead: float = 14.5,
                  color=(0.13, 0.16, 0.19), x: Optional[float] = None, width: Optional[float] = None) -> None:
        x = self.x0 if x is None else x
        width = (self.x1 - x) if width is None else width
        for ln in self._wrap(_prep(text), "N", size, width):
            self._need(lead)
            self._txt(x, self.y - size, ln, "N", size, color)
            self.y -= lead
        self.y -= 2

    def rich_paragraph(self, runs: List[Tuple[str, str]], size: float = 10, lead: float = 14.5,
                       color=(0.13, 0.16, 0.19), x: Optional[float] = None,
                       width: Optional[float] = None, bullet: bool = False) -> None:
        """Lay out styled runs (text, style in {N,B,C}) with wrapping across runs."""
        x = self.x0 if x is None else x
        width = (self.x1 - x) if width is None else width
        # explode into (word, style, trailing_space) tokens
        tokens: List[Tuple[str, str]] = []
        for txt, st in runs:
            parts = _prep(txt).split(" ")
            for i, p in enumerate(parts):
                if p:
                    tokens.append((p, st))
                if i < len(parts) - 1:
                    tokens.append((" ", st))
        # group into lines
        lines: List[List[Tuple[str, str]]] = [[]]
        cx = 0.0
        for tok, st in tokens:
            w = text_width(tok, "C" if st == "C" else st, size)
            if tok != " " and cx + w > width and lines[-1]:
                lines.append([]); cx = 0.0
                if tok == " ":
                    continue
            lines[-1].append((tok, st)); cx += w
        for i, line in enumerate(lines):
            self._need(lead)
            cxp = x
            if bullet and i == 0:
                self._txt(x - 12, self.y - size, "•", "B", size, INK)
            for tok, st in line:
                f = "C" if st == "C" else st
                self._txt(cxp, self.y - size, tok, f, size, INK if st == "B" else color)
                cxp += text_width(tok, f, size)
            self.y -= lead

    def bullet(self, text: str, indent: int = 0) -> None:
        x = self.x0 + 14 + indent * 16
        runs = _md_runs(text)
        self.rich_paragraph(runs, x=x, width=self.x1 - x, bullet=True)
        self.y -= 1

    def callout(self, text: str) -> None:
        self.spacer(4)
        runs = _md_runs(text)
        start_y = self.y
        x = self.x0 + 16
        # measure height by laying out into a temp? simpler: draw text, then bar.
        self._need(20)
        self.rich_paragraph(runs, size=10, lead=14.5, color=(0.20, 0.24, 0.28),
                            x=x, width=self.x1 - x - 8)
        bar_bottom = self.y + 2
        self._rect(self.x0, bar_bottom, 3, start_y - bar_bottom, INK)
        self.y -= 4

    def code_block(self, lines: Sequence[str]) -> None:
        self.spacer(4)
        pad, lead, size = 7, 13, 9
        h = pad * 2 + lead * max(1, len(lines))
        self._need(h)
        self._rect(self.x0, self.y - h, self.W, h, LIGHT)
        yy = self.y - pad - size
        for ln in lines:
            self._txt(self.x0 + pad, yy, _prep(ln), "C", size, (0.20, 0.24, 0.28))
            yy -= lead
        self.y -= h + 4

    def rule_sep(self) -> None:
        self.spacer(6); self._need(8)
        self._line(self.x0, self.y, self.x1, self.y, RULE, 0.5)
        self.y -= 8

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        self.spacer(6)
        n = len(headers)
        size, hsize, pad, lead = 9, 9, 5, 12
        # column widths proportional to natural content width, clamped.
        nat = []
        for c in range(n):
            cells = [str(headers[c])] + [str(r[c]) if c < len(r) else "" for r in rows]
            nat.append(max(text_width(_prep(x), "N", size) for x in cells) + 2 * pad)
        total = sum(nat) or 1
        colw = [max(46.0, w / total * self.W) for w in nat]
        scale = self.W / sum(colw)
        colw = [w * scale for w in colw]

        def wrap_cell(txt: str, w: float, font: str) -> List[str]:
            return self._wrap(_prep(str(txt)), font, size, w - 2 * pad)

        def draw_row(cells: Sequence[str], font: str, fill, txtcolor) -> None:
            wrapped = [wrap_cell(c if i < len(cells) else "", colw[i], font) for i, c in enumerate(cells)]
            rh = pad * 2 + lead * max(len(w) for w in wrapped)
            self._need(rh)
            top = self.y
            if fill is not None:
                self._rect(self.x0, top - rh, self.W, rh, fill)
            cx = self.x0
            for i in range(n):
                yy = top - pad - size
                for ln in wrapped[i]:
                    self._txt(cx + pad, yy, ln, font, size, txtcolor)
                    yy -= lead
                cx += colw[i]
            # borders
            self._line(self.x0, top - rh, self.x1, top - rh, RULE, 0.4)
            cx = self.x0
            for i in range(n + 1):
                self._line(cx, top, cx, top - rh, RULE, 0.4)
                if i < n:
                    cx += colw[i]
            self.y -= rh

        # header (repeats if the table breaks across pages)
        def header_row():
            draw_row(headers, "B", INK, WHITE)
        self._need(40)
        self._line(self.x0, self.y, self.x1, self.y, RULE, 0.4)
        header_row()
        for ri, r in enumerate(rows):
            if self.y - 24 < self.cbot:
                self._newpage()
                header_row()
            draw_row(r, "N", LIGHT if ri % 2 else None, (0.15, 0.18, 0.21))
        self.y -= 6

    # ---- output -----------------------------------------------------------
    def render(self) -> bytes:
        self._end_page()
        total = len(self._pages)
        objs: List[bytes] = []

        def add(b: bytes) -> int:
            objs.append(b); return len(objs)

        catalog_n = add(b"")
        pages_n = add(b"")
        fonts = {
            "N": "Helvetica", "B": "Helvetica-Bold",
            "O": "Helvetica-Oblique", "C": "Courier",
        }
        font_ns = {}
        for code, name in fonts.items():
            font_ns[code] = add(
                (f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
                 "/Encoding /WinAnsiEncoding >>").encode("ascii"))

        page_ns = []
        for i, ops in enumerate(self._pages, start=1):
            foot = []
            if i > 1 or total == 1:
                label = f"Page {i} of {total}"
                fx = (self.pw - text_width(label, "N", 8)) / 2
                foot.append(f"0.42 0.46 0.50 rg BT /F1 8 Tf {fx:.2f} {self.m - 24:.2f} Td (".encode("ascii")
                            + _esc(label) + b") Tj ET")
                foot.append(f"0.80 0.84 0.88 RG 0.5 w {self.x0:.2f} {self.m - 12:.2f} m "
                            f"{self.x1:.2f} {self.m - 12:.2f} l S".encode("ascii"))
            stream = b"\n".join(ops + foot)
            content_n = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
            fontres = " ".join(f"/F{_FID[c]} {font_ns[c]} 0 R" for c in fonts)
            page_ns.append(add(
                (f"<< /Type /Page /Parent {pages_n} 0 R /MediaBox [0 0 {self.pw} {self.ph}] "
                 f"/Resources << /Font << {fontres} >> >> /Contents {content_n} 0 R >>").encode("ascii")))

        kids = " ".join(f"{n} 0 R" for n in page_ns)
        objs[catalog_n - 1] = f"<< /Type /Catalog /Pages {pages_n} 0 R >>".encode("ascii")
        objs[pages_n - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ns)} >>".encode("ascii")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode("ascii")
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_n} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF").encode("ascii")
        return bytes(out)


def _md_runs(text: str) -> List[Tuple[str, str]]:
    """Split inline markdown (**bold**, `code`) into styled runs (text, style)."""
    import re
    runs: List[Tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`", text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], "N"))
        if m.group(1) is not None:
            runs.append((m.group(1), "B"))
        else:
            runs.append((m.group(2), "C"))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], "N"))
    return runs or [(text, "N")]
