#!/usr/bin/env python3
"""Render a Markdown file to a polished, professional PDF (pure stdlib).

Usage: python scripts/md_to_pdf.py docs/USER_GUIDE.md [docs/USER_GUIDE.pdf]

Uses rams.pdfdoc (a small layout engine: proportional Helvetica type, styled
headings, shaded/wrapped tables, bullets, callouts, code blocks, a cover page and
running headers/footers). Markdown supported: #/##/### headings, **bold**,
`code`, bullet lists, > callouts, fenced code blocks, pipe tables, --- rules.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rams.pdfdoc import PdfDoc, _md_runs  # noqa: E402


def _split_table_row(line: str):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def build(md: str) -> PdfDoc:
    lines = md.splitlines()
    # Cover: first H1 = title; following paragraph = subtitle/intro.
    title, idx = "Document", 0
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            title = ln[2:].strip()
            idx = i + 1
            break
    intro_parts = []
    while idx < len(lines) and not re.match(r"^#{1,6}\s", lines[idx]) and not lines[idx].startswith("---"):
        if lines[idx].strip():
            intro_parts.append(lines[idx].strip())
        elif intro_parts:
            break
        idx += 1
    intro_raw = " ".join(intro_parts)
    intro_plain = re.sub(r"\*\*(.+?)\*\*", r"\1", intro_raw).replace("`", "")

    # Table of contents = the top-level (##) section headings.
    contents = [re.sub(r"\*\*(.+?)\*\*", r"\1", ln[3:].strip()).replace("`", "")
                for ln in lines if ln.startswith("## ")]

    doc = PdfDoc(doc_title=title)
    first_sentence = re.split(r"(?<=[.])\s", intro_plain)[0]
    subtitle = first_sentence if len(first_sentence) < 160 else intro_plain[:157] + "..."
    doc.cover(title, subtitle, intro=intro_plain, contents=contents,
              meta="Standards: IRC:37-2018 / IRC:115 / IRC:82 / IRC:81  -  HDM-4  -  "
                   "MoRTH Standard Data Book   |   RAMS documentation")

    i = idx
    n = len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            doc.spacer(4); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = len(m.group(1))
            txt = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(2)).replace("`", "")
            doc.heading(txt, min(3, max(1, lvl - 1)))  # ## -> 1, ### -> 2, #### -> 3
            i += 1; continue
        if re.match(r"^---+$", s):
            doc.rule_sep(); i += 1; continue
        if s.startswith("```"):
            i += 1; block = []
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i]); i += 1
            i += 1
            doc.code_block(block); continue
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip()); i += 1
            doc.callout(" ".join(buf)); continue
        if s.startswith("|") and i + 1 < n and re.match(r"^\|?[\s:|-]+\|", lines[i + 1].strip()):
            headers = _split_table_row(lines[i]); i += 2  # skip header + separator
            body = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(_split_table_row(lines[i])); i += 1
            doc.table(headers, body); continue
        if re.match(r"^[-*]\s+", s):
            indent = (len(ln) - len(ln.lstrip())) // 2
            doc.bullet(re.sub(r"^[-*]\s+", "", s), indent)
            i += 1; continue
        # paragraph: gather until blank / block start
        para = [s]; i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|>|\||```|---+$|[-*]\s)", lines[i].strip()):
            para.append(lines[i].strip()); i += 1
        doc.rich_paragraph(_md_runs(" ".join(para)))
        doc.spacer(2)
    return doc


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: md_to_pdf.py <input.md> [output.pdf]")
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".pdf"
    with open(src, encoding="utf-8") as fh:
        md = fh.read()
    with open(dst, "wb") as fh:
        fh.write(build(md).render())
    print(f"wrote {dst}")


if __name__ == "__main__":
    main()
