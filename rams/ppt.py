"""
Minimal but **designed** PowerPoint (.pptx) writer -- pure standard library.

A .pptx is an Office Open XML package (a ZIP of XML parts), the same family as
the .xlsx exporter in `rams.export`. This builds a spec-valid presentation with a
branded theme and lays out slides as explicit shapes (filled rectangles for the
cover/accent bars and footers, styled text boxes for titles and coloured
bullets) -- so the deck looks like a real client presentation, not pasted text.
"""
from __future__ import annotations

import io
import zipfile
from typing import List

# 16:9 slide, in English Metric Units (914400 EMU = 1 inch).
_CX, _CY = 12192000, 6858000
_MX = 685800                       # left/right margin
_RX = _CX - _MX                    # right content edge
_BW = _RX - _MX                    # content width

# Brand palette (hex, no #), matching the dashboard / PDF guide.
INK = "1F3B57"      # deep navy
ACCENT = "F0A000"   # amber
BODY = "2A3640"     # body text
SUBTLE = "D7E2EC"   # light text on navy
FOOT = "8493A0"     # footer grey
HAIR = "D8DEE5"     # hairline
WHITE = "FFFFFF"

_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
    '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
    '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
    '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
    "{slide_overrides}"
    "</Types>"
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
    "</Relationships>"
)

_THEME = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="RAMS">'
    "<a:themeElements>"
    '<a:clrScheme name="RAMS">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="1F3B57"/></a:dk2><a:lt2><a:srgbClr val="EEF2F6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="1F3B57"/></a:accent1><a:accent2><a:srgbClr val="F0A000"/></a:accent2>'
    '<a:accent3><a:srgbClr val="1A9850"/></a:accent3><a:accent4><a:srgbClr val="C0504D"/></a:accent4>'
    '<a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6>'
    '<a:hlink><a:srgbClr val="1F3B57"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="RAMS">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>'
    "</a:fontScheme>"
    '<a:fmtScheme name="RAMS">'
    '<a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
    '<a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    '<a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
    "<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>"
    '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
    "</a:fmtScheme></a:themeElements></a:theme>"
)

_MASTER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
    '<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>'
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"'
    ' accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"'
    ' hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    '<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="4000"/></a:lvl1pPr></p:titleStyle>'
    '<p:bodyStyle><a:lvl1pPr><a:defRPr sz="2000"/></a:lvl1pPr></p:bodyStyle>'
    '<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles>'
    "</p:sldMaster>"
)

_MASTER_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>'
    "</Relationships>"
)

_LAYOUT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">'
    '<p:cSld name="Blank"><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>'
    '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
)

_LAYOUT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
    "</Relationships>"
)

_SLIDE_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
    "</Relationships>"
)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _rect(sp_id: int, x: int, y: int, cx: int, cy: int, fill: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_id}" name="r{sp_id}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
    )


def _box(sp_id: int, x: int, y: int, cx: int, cy: int, paras: str, anchor: str = "t") -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_id}" name="t{sp_id}"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square" anchor="{anchor}"><a:normAutofit/></a:bodyPr>'
        f"<a:lstStyle/>{paras}</p:txBody></p:sp>"
    )


def _run(text: str, sz: int, *, bold: bool, colour: str, font: str = "+mn-lt") -> str:
    b = ' b="1"' if bold else ""
    return (f'<a:r><a:rPr lang="en-US" sz="{sz}"{b} dirty="0">'
            f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
            f'<a:latin typeface="{font}"/></a:rPr><a:t>{_esc(text)}</a:t></a:r>')


def _line_para(text: str, sz: int, *, bold: bool, colour: str, font: str = "+mn-lt",
               align: str = "l", spc_before: int = 0) -> str:
    spc = f'<a:spcBef><a:spcPts val="{spc_before}"/></a:spcBef>' if spc_before else ""
    return (f'<a:p><a:pPr algn="{align}">{spc}</a:pPr>'
            + _run(text, sz, bold=bold, colour=colour, font=font) + "</a:p>")


def _bullet_para(text: str, indent: int) -> str:
    sz = 1800 if indent == 0 else 1500
    colour = BODY if indent == 0 else "4A5A66"
    bch, bclr = ("▪", ACCENT) if indent == 0 else ("–", FOOT)
    ppr = (f'<a:pPr marL="{285750 + indent * 342900}" lvl="{indent}" indent="-285750">'
           '<a:spcBef><a:spcPts val="900"/></a:spcBef>'
           f'<a:buClr><a:srgbClr val="{bclr}"/></a:buClr>'
           f'<a:buSzPct val="90000"/><a:buFont typeface="Arial"/><a:buChar char="{bch}"/></a:pPr>')
    return f"<a:p>{ppr}" + _run(text, sz, bold=False, colour=colour) + "</a:p>"


def _slide(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        f"{inner}"
        "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _title_slide(title: str, subtitle: str, footer: str = "") -> str:
    inner = _rect(2, 0, 0, _CX, _CY, INK)                                  # full-bleed navy
    inner += _rect(3, _MX, 2520000, 1700000, 60000, ACCENT)               # amber accent bar
    inner += _box(4, _MX, 1500000, _BW, 1000000,
                  _line_para(title, 4400, bold=True, colour=WHITE, font="+mj-lt"), anchor="b")
    inner += _box(5, _MX, 2700000, _BW, 1700000,
                  _line_para(subtitle, 2000, bold=False, colour=SUBTLE))
    if footer:
        inner += _box(6, _MX, 6180000, _BW, 400000,
                      _line_para(footer, 1100, bold=False, colour="9FB2C2"))
    return _slide(inner)


def _content_slide(title: str, bullets, idx: int, total: int, footer: str) -> str:
    inner = _rect(2, _MX, 520000, 470000, 66000, ACCENT)                  # eyebrow accent bar
    inner += _box(3, _MX, 560000, _BW, 760000,
                  _line_para(title, 3000, bold=True, colour=INK, font="+mj-lt"))
    inner += _rect(4, _MX, 1380000, _BW, 12700, HAIR)                     # title underline
    paras = "".join(_bullet_para(b[0], b[1]) if isinstance(b, tuple) else _bullet_para(b, 0)
                    for b in bullets)
    inner += _box(5, _MX, 1560000, _BW, 4700000, paras)
    inner += _rect(6, _MX, 6430000, _BW, 9525, HAIR)                      # footer hairline
    inner += _box(7, _MX, 6470000, _BW // 2, 320000,
                  _line_para(footer, 1000, bold=False, colour=FOOT))
    inner += _box(8, _MX + _BW // 2, 6470000, _BW // 2, 320000,
                  _line_para(f"{idx:02d} / {total:02d}", 1000, bold=False, colour=FOOT, align="r"))
    return _slide(inner)


def pptx_bytes(slides: List[dict], footer: str = "RAMS — Road Asset Management System") -> bytes:
    """Build a designed .pptx. Each slide is {'title','subtitle'} (cover/section)
    or {'title','bullets'} (content)."""
    n = len(slides)
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, n + 1)
    )
    sld_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, n + 1))
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{sld_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{_CX}" cy="{_CY}" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>']
    for i in range(1, n + 1):
        rels.append(f'<Relationship Id="rId{i + 1}" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
                    f'Target="slides/slide{i}.xml"/>')
    rels.append(f'<Relationship Id="rId{n + 2}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
                'Target="theme/theme1.xml"/></Relationships>')
    pres_rels = "".join(rels)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(slide_overrides=slide_overrides))
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        z.writestr("ppt/theme/theme1.xml", _THEME)
        z.writestr("ppt/slideMasters/slideMaster1.xml", _MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _MASTER_RELS)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _LAYOUT_RELS)
        for i, s in enumerate(slides, start=1):
            if "subtitle" in s:
                xml = _title_slide(s["title"], s["subtitle"], s.get("footer", ""))
            else:
                xml = _content_slide(s["title"], s.get("bullets", []), i, n, footer)
            z.writestr(f"ppt/slides/slide{i}.xml", xml)
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _SLIDE_RELS)
    return buf.getvalue()
