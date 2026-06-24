#!/usr/bin/env python3
"""Generate the RAMS client-presentation deck (docs/RAMS_Client_Presentation.pptx).

Pure stdlib via rams.ppt. Run: python scripts/make_ppt.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rams.ppt import pptx_bytes  # noqa: E402

SLIDES = [
    {"title": "RAMS",
     "subtitle": "Road Asset Management System — Indian pavement deterioration, "
                 "remaining-life & investment-planning engine",
     "footer": "Standards: IRC:37-2018 / IRC:115 / IRC:82 / IRC:81   ·   HDM-4   ·   "
               "MoRTH Standard Data Book"},

    {"title": "The problem",
     "bullets": [
         "Highway agencies must decide WHAT to fix, WHEN, and at WHAT cost — across thousands of km.",
         "Field data (NSV condition, FWD deflection, traffic) arrives in many vendor formats.",
         "Decisions must follow Indian codes (IRC) and be defensible to auditors and concession terms.",
         "Generic foreign tools don't speak IRC:37 / IRC:115 / IRC:82 or MoRTH rates.",
     ]},

    {"title": "What RAMS does",
     "bullets": [
         "Designs new pavements (IRC:37-2018) and checks them mechanistically (IITPAVE-style).",
         "Ingests real field surveys (CSV / XLSX / PDF) and builds homogeneous sections.",
         "Forecasts deterioration (rut, cracking, roughness, PCI) and remaining structural life.",
         "Plans investment: life-cycle analysis, network budget optimisation, and PBMC pricing.",
         "Every cost is grounded in the MoRTH Standard Data Book; every rule cites its IRC clause.",
     ]},

    {"title": "Standards & methods built in",
     "bullets": [
         "IRC:37-2018 — flexible pavement design (CBR → layer thicknesses; fatigue & rutting).",
         "IRC:115-2014 — FWD back-calculated moduli → remaining life → overlay need.",
         "IRC:82 / IRC:81 — pavement condition index (PCI) and deflection-based residual life.",
         "HDM-4 — calibrated rut / roughness / cracking / skid / pothole progression.",
         "MoRTH Standard Data Book — treatment unit rates for all costing.",
     ]},

    {"title": "The workflow",
     "bullets": [
         "1. DESIGN — size the pavement for the design traffic before any field data.",
         "2. FIELD DATA — load NSV / FWD surveys; auto-section the network.",
         "3. FORECAST — project condition, triggers and remaining life per section.",
         "4. INVEST — life-cycle analysis, budget optimisation, and 5–7 yr PBMC pricing.",
     ]},

    {"title": "Module 1 — IRC:37 design + IITPAVE check",
     "bullets": [
         "CBR + design traffic (MSA, or CVPD × VDF) → BC / DBM / WMM / GSB thicknesses.",
         "Catalogue method, or a least-cost mechanistic section sized by Odemark–Boussinesq.",
         "To-scale cross-section diagram plus a fatigue/rutting strain check.",
         "Calibrated to IRC:37 Annex II worked examples and published FWD reports (~±5–10%).",
     ]},

    {"title": "Module 2 — field-data ingestion",
     "bullets": [
         "Reads CSV, XLSX (multi-sheet, merged headers) and text PDFs — any vendor template.",
         "Column names auto-matched; missing fields fall back to documented defaults.",
         "Multi-file merge of separate rut / crack / roughness / pothole surveys by chainage.",
         "One upload is shared across every tab — no re-uploading.",
     ]},

    {"title": "Module 3 — forecasting & homogeneous sections",
     "bullets": [
         "Cumulative-difference sectioning (IRC:115) turns 100 m points into uniform sections.",
         "Per-section condition, treatment band and preventive window, in a paginated table.",
         "Untreated-vs-managed PCI trajectory with IRC intervention triggers.",
         "Download forecast reports as XLSX or PDF.",
     ]},

    {"title": "Module 4 — FWD remaining life & overlay",
     "bullets": [
         "15th-percentile back-calculated moduli + crust → remaining fatigue/rutting life.",
         "Flags sections needing an overlay for the design traffic.",
         "Screening-grade; borderline sections marked to confirm with full IITPAVE.",
         "Upload the FWD report directly (CSV / XLSX / PDF); optional columns default.",
     ]},

    {"title": "Module 5 — life-cycle analysis & MoRTH costing",
     "bullets": [
         "Year-by-year decision matrix: ROUTINE → PREVENTIVE → OVERLAY → RECONSTRUCTION.",
         "Triggers from PCI / rut / cracking / roughness thresholds, with condition reset.",
         "Costs from MoRTH SDB rates × carriageway area; reports total, NPV and EUAC.",
         "All rates and thresholds are editable data, not hard-coded logic.",
     ]},

    {"title": "Module 6 — network budget optimisation",
     "bullets": [
         "Greedy multi-year allocation of a fixed annual budget across the network.",
         "Prioritises busiest corridors and tightest preventive windows.",
         "Reports funded vs unfunded, avoided structural cost, and spend-by-year.",
         "Recommends the budget that clears the backlog — turns a warning into a decision.",
     ]},

    {"title": "Module 7 — PBMC contract pricing",
     "bullets": [
         "Prices a 5–7 year Performance-Based Maintenance Contract to hold a service level.",
         "Routine + periodic + initial rectification, with escalation, contingency, overhead.",
         "Per-year cash flow, contract value, cost per km and NPV.",
         "Performance check against the minimum PCI service level.",
     ]},

    {"title": "Outputs & reporting",
     "bullets": [
         "Interactive dashboard with charts, colour-coded decision tables and pagination.",
         "Download sections, LCA matrices and forecasts as XLSX and PDF.",
         "Self-contained, offline HTML reports — open on an air-gapped workstation, email as one file.",
     ]},

    {"title": "Why RAMS",
     "bullets": [
         "India-first: speaks IRC and MoRTH out of the box — credible to agencies and auditors.",
         "Zero third-party dependencies — installs and runs anywhere, fully offline.",
         "Transparent & auditable: every number traces to a code clause or an editable rate.",
         "Validated against IRC worked examples and real field reports.",
     ]},

    {"title": "Caveats & roadmap",
     "bullets": [
         "Mechanistic strains are screening-grade (Odemark–Boussinesq) — confirm borderline with IITPAVE.",
         "MoRTH rates are indicative — load the current SDB / State SoR before tendering.",
         "Calibration needs repeat surveys; a single snapshot feeds forecasting, not rate-fitting.",
         "Roadmap: full IITPAVE coupling, GIS map view, multi-year survey calibration.",
     ]},

    {"title": "Thank you",
     "subtitle": "RAMS — defensible, India-specific pavement asset management. Questions welcome."},
]


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(here, "docs", "RAMS_Client_Presentation.pptx")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(pptx_bytes(SLIDES))
    print(f"wrote {out} ({len(SLIDES)} slides)")


if __name__ == "__main__":
    main()
