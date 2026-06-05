"""
Reporting & visualisation layer (UI/UX).

Produces three outputs from a forecast timeline + maintenance plan:
  * to_csv / to_json -- machine-readable exports for the priority algorithms.
  * to_html          -- a single self-contained HTML report (no CDN, no JS
    frameworks) with an inline-SVG PCI lifecycle chart, decision-band
    shading and a colour-coded data table.

UI/UX principles applied:
  - One screen tells the whole story: headline verdict, chart, then detail.
  - Colour encodes the maintenance decision band consistently
    (green=routine, amber=preventive window, red=structural).
  - The "window of maximum return" is annotated directly on the chart, where
    the decision is actually made -- not buried in a footnote.

Security Lead note:
    Every dynamic string (segment_id, rationale, treatment names) is passed
    through html.escape() before templating. NSV/CSV-sourced identifiers are
    untrusted input; this prevents stored-XSS in a report a planner opens in
    a browser.
"""
from __future__ import annotations

import html
import json
from typing import List

from .maintenance import MaintenanceFlag, MaintenancePlan, MaintenancePolicy
from .models import YearResult

# Decision-band colours (consistent across chart + table).
_BAND_COLOURS = {
    MaintenanceFlag.ROUTINE: "#1a9850",      # green
    MaintenanceFlag.PREVENTIVE: "#f0a000",   # amber
    MaintenanceFlag.STRUCTURAL: "#d73027",   # red
}


def to_csv(timeline: List[YearResult]) -> str:
    """RFC-4180-ish CSV string of the timeline (no file I/O side effects)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(YearResult.COLUMNS)
    for yr in timeline:
        row = yr.as_row()
        writer.writerow([row[c] for c in YearResult.COLUMNS])
    return buf.getvalue()


def to_json(timeline: List[YearResult], plan: MaintenancePlan) -> str:
    payload = {
        "timeline": [yr.as_row() for yr in timeline],
        "maintenance_plan": {
            "preventive_window_year": plan.preventive_window_year,
            "window_expired_year": plan.window_expired_year,
            "recommended_year": plan.recommended_year,
            "recommended_treatment": (
                plan.recommended_treatment.name if plan.recommended_treatment else None
            ),
            "morth_reference": (
                plan.recommended_treatment.morth_reference
                if plan.recommended_treatment
                else None
            ),
            "rationale": plan.rationale,
            "flags_by_year": [f.value for f in plan.flags_by_year],
        },
    }
    return json.dumps(payload, indent=2)


def _svg_pci_chart(
    timeline: List[YearResult], policy: MaintenancePolicy,
    width: int = 720, height: int = 320,
) -> str:
    """Inline SVG line chart of PCI over the horizon with band shading."""
    pad_l, pad_r, pad_t, pad_b = 48, 16, 16, 32
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    years = [yr.year for yr in timeline]
    pcis = [yr.irc82_pci for yr in timeline]
    y_min, y_max = 1.0, 4.0
    x_min, x_max = min(years), max(years)
    x_span = max(1, x_max - x_min)

    def px(year: int) -> float:
        return pad_l + (year - x_min) / x_span * plot_w

    def py(pci: float) -> float:
        return pad_t + (y_max - pci) / (y_max - y_min) * plot_h

    # Horizontal band rectangles (structural / preventive / routine).
    bands = [
        (y_min, policy.structural_lower, _BAND_COLOURS[MaintenanceFlag.STRUCTURAL]),
        (policy.structural_lower, policy.preventive_upper, _BAND_COLOURS[MaintenanceFlag.PREVENTIVE]),
        (policy.preventive_upper, y_max, _BAND_COLOURS[MaintenanceFlag.ROUTINE]),
    ]
    band_svg = "".join(
        f'<rect x="{pad_l:.1f}" y="{py(hi):.1f}" width="{plot_w:.1f}" '
        f'height="{py(lo) - py(hi):.1f}" fill="{c}" opacity="0.12"/>'
        for lo, hi, c in bands
    )

    # PCI polyline.
    pts = " ".join(f"{px(y):.1f},{py(p):.1f}" for y, p in zip(years, pcis))
    dots = "".join(
        f'<circle cx="{px(y):.1f}" cy="{py(p):.1f}" r="3" fill="#1f3b57"/>'
        for y, p in zip(years, pcis)
    )

    # Y axis labels at band thresholds.
    y_ticks = sorted({y_min, policy.structural_lower, policy.preventive_upper, y_max})
    y_labels = "".join(
        f'<text x="{pad_l - 6:.1f}" y="{py(t) + 4:.1f}" font-size="10" '
        f'text-anchor="end" fill="#555">{t:.2f}</text>'
        f'<line x1="{pad_l:.1f}" y1="{py(t):.1f}" x2="{width - pad_r:.1f}" '
        f'y2="{py(t):.1f}" stroke="#ccc" stroke-width="0.5"/>'
        for t in y_ticks
    )
    x_labels = "".join(
        f'<text x="{px(y):.1f}" y="{height - pad_b + 18:.1f}" font-size="10" '
        f'text-anchor="middle" fill="#555">{y}</text>'
        for y in years
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="PCI lifecycle chart" '
        f'xmlns="http://www.w3.org/2000/svg" style="max-width:{width}px">'
        f"{band_svg}{y_labels}{x_labels}"
        f'<polyline points="{pts}" fill="none" stroke="#1f3b57" stroke-width="2"/>'
        f"{dots}"
        f'<text x="{pad_l:.1f}" y="{pad_t + 4:.1f}" font-size="10" '
        f'fill="#555">IRC:82 PCI vs Year</text>'
        f"</svg>"
    )


def to_html(
    segment_id: str,
    timeline: List[YearResult],
    plan: MaintenancePlan,
    policy: MaintenancePolicy,
) -> str:
    """Render a single self-contained HTML report. Returns an HTML string."""
    sid = html.escape(str(segment_id))
    rationale = html.escape(plan.rationale)
    treatment = html.escape(
        plan.recommended_treatment.name if plan.recommended_treatment else "None"
    )
    morth = html.escape(
        plan.recommended_treatment.morth_reference if plan.recommended_treatment else ""
    )

    # Verdict banner colour follows the worst decision band reached.
    if plan.window_expired_year is not None:
        banner = _BAND_COLOURS[MaintenanceFlag.STRUCTURAL]
        verdict = f"STRUCTURAL ACTION by year {plan.window_expired_year}"
    elif plan.preventive_window_year is not None:
        banner = _BAND_COLOURS[MaintenanceFlag.PREVENTIVE]
        verdict = f"PREVENTIVE WINDOW opens year {plan.preventive_window_year}"
    else:
        banner = _BAND_COLOURS[MaintenanceFlag.ROUTINE]
        verdict = "ROUTINE maintenance only"

    rows = []
    for yr, flag in zip(timeline, plan.flags_by_year):
        r = yr.as_row()
        colour = _BAND_COLOURS[flag]
        rows.append(
            f"<tr>"
            f"<td>{r['Year']}</td><td>{r['Cumulative_MSA']}</td>"
            f"<td>{r['IRI']}</td><td>{r['Rutting_mm']}</td>"
            f"<td>{r['Cracking_Pct']}</td>"
            f'<td style="font-weight:600;color:{colour}">{r["IRC82_PCI"]}</td>'
            f'<td><span style="color:{colour}">&#9679;</span> '
            f"{html.escape(flag.value)}</td>"
            f"</tr>"
        )
    table_rows = "".join(rows)
    chart = _svg_pci_chart(timeline, policy)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAMS Forecast &mdash; {sid}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
          color: #1f2a36; background: #f5f7fa; }}
  .wrap {{ max-width: 820px; margin: 0 auto; padding: 24px; }}
  .banner {{ background: {banner}; color: #fff; padding: 14px 18px;
             border-radius: 8px; font-weight: 600; font-size: 17px; }}
  .card {{ background: #fff; border-radius: 8px; padding: 18px; margin-top: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #667; font-size: 13px; margin-bottom: 16px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ padding: 6px 10px; text-align: right; border-bottom: 1px solid #eee; }}
  th:last-child, td:last-child, th:first-child, td:first-child {{ text-align: left; }}
  th {{ background: #f0f3f7; color: #445; font-weight: 600; }}
  .legend span {{ font-size: 12px; margin-right: 14px; }}
  .rationale {{ font-size: 14px; line-height: 1.5; }}
  .meta {{ font-size: 12px; color: #778; }}
</style></head>
<body><div class="wrap">
  <h1>Pavement Lifecycle Forecast</h1>
  <div class="sub">Segment <strong>{sid}</strong> &middot; IRC:82 deterministic forecast</div>
  <div class="banner">{html.escape(verdict)}</div>
  <div class="card">
    <div class="legend">
      <span style="color:{_BAND_COLOURS[MaintenanceFlag.ROUTINE]}">&#9679; Routine (PCI &ge; {policy.preventive_upper:.2f})</span>
      <span style="color:{_BAND_COLOURS[MaintenanceFlag.PREVENTIVE]}">&#9679; Preventive window ({policy.structural_lower:.2f}&ndash;{policy.preventive_upper:.2f})</span>
      <span style="color:{_BAND_COLOURS[MaintenanceFlag.STRUCTURAL]}">&#9679; Structural (PCI &lt; {policy.structural_lower:.2f})</span>
    </div>
    {chart}
  </div>
  <div class="card rationale">
    <strong>Recommended action:</strong> {treatment}
    <span class="meta">({morth})</span><br><br>
    {rationale}
  </div>
  <div class="card">
    <table>
      <thead><tr><th>Year</th><th>Cum. MSA</th><th>IRI</th><th>Rut (mm)</th>
        <th>Crack (%)</th><th>PCI</th><th>Flag</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
  <p class="meta">Generated by RAMS deterioration engine. Deterministic &mdash;
     identical inputs always reproduce this forecast.</p>
</div></body></html>"""
