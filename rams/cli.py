"""
Command-line interface for the RAMS deterioration engine.

Usage examples:
    # Single segment (defaults reproduce the spec example):
    python -m rams.cli --iri 1.5 --rut 2.0 --crack 0.0 \\
        --msa 4.5 --growth 0.06 --zone HIGH --years 10

    # Emit an HTML report:
    python -m rams.cli --zone HIGH --html forecast.html

    # Forecast a whole network from CSV / XLSX / PDF and triage it:
    python -m rams.cli --csv examples/sample_network.csv --summary
    python -m rams.cli --xlsx survey.xlsx --summary
    python -m rams.cli --pdf condition_survey.pdf --summary

UI/UX note: terminal output is colour-coded by maintenance band (with a
NO_COLOR / non-TTY fallback) so a planner can eyeball the inflection year.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .batch import forecast_network, network_summary
from .calibrate import (
    calibrate_hdm4_potholes,
    calibrate_hdm4_roughness,
    calibrate_hdm4_rut,
    calibrate_hdm4_skid,
    calibrate_mlit_cracking,
    load_observations_csv,
)
from .config import (
    CrackModelType,
    MonsoonZone,
    PotholeModelType,
    RoughnessModelType,
    RutModelType,
    SkidModelType,
)
from .distress import mlit_crack_preset
from .fwd import snp_from_deflection
from .hdm4 import preset as hdm4_preset
from .ingest import (
    ingest_segments_csv,
    ingest_segments_pdf,
    ingest_segments_xlsx,
)
from .engine import IndianPavementDeteriorationEngine
from .design import design_pavement
from .iitpave import (
    FWDSection,
    LayerModel,
    design_pavement_mechanistic,
    evaluate_fwd_sections,
    evaluate_section,
)
from .pbmc import PBMCParams, estimate_pbmc, estimate_pbmc_network
from .residual import handback_assessment, remaining_fatigue_life
from .traffic import default_vdf, design_msa, lane_distribution_factor
from .triggers import TriggerSeverity, evaluate_triggers
from .maintenance import (
    MaintenanceFlag,
    MaintenancePolicy,
    annotate_timeline,
    build_maintenance_plan,
)
from .models import SegmentInput, YearResult
from .report import to_csv, to_html, to_json

_ANSI = {
    MaintenanceFlag.ROUTINE: "\033[32m",     # green
    MaintenanceFlag.PREVENTIVE: "\033[33m",  # amber
    MaintenanceFlag.STRUCTURAL: "\033[31m",  # red
}
_RESET = "\033[0m"


def _use_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _print_table(timeline: List[YearResult], policy: MaintenancePolicy) -> None:
    colour = _use_colour()
    header = f"{'Year':>4} {'Cum_MSA':>8} {'IRI':>5} {'Rut':>6} {'Crack':>7} {'PCI':>6}  Flag"
    print(header)
    print("-" * len(header))
    for yr in timeline:
        r = yr.as_row()
        flag = policy.classify(yr.irc82_pci)
        line = (
            f"{r['Year']:>4} {r['Cumulative_MSA']:>8.2f} {r['IRI']:>5.2f} "
            f"{r['Rutting_mm']:>6.1f} {r['Cracking_Pct']:>7.1f} "
            f"{r['IRC82_PCI']:>6.2f}  {flag.value}"
        )
        if colour:
            line = f"{_ANSI[flag]}{line}{_RESET}"
        print(line)


def _print_hdm4_breakdown(breakdown: List[dict]) -> None:
    """Show the HDM-4 per-year rut increment split into its three components."""
    if not breakdown:
        return
    print("\nHDM-4 rut increment breakdown (mm/yr):")
    print(f"{'Year':>4} {'Densif.':>8} {'Struct.':>8} {'Plastic':>8} {'Total':>8}")
    print("-" * 40)
    for b in breakdown:
        print(
            f"{b['year']:>4} {b['densification']:>8.3f} {b['structural']:>8.3f} "
            f"{b['plastic']:>8.3f} {b['total']:>8.3f}"
        )


def _print_residual(args: argparse.Namespace) -> None:
    """Print IRC:81/IRC:37 remaining structural life (+ handback verdict)."""
    res = remaining_fatigue_life(
        deflection_mm=args.deflection,
        annual_msa=args.msa,
        traffic_growth_rate=args.growth,
        cumulative_msa=args.cumulative_msa,
        design_msa=args.design_msa,
    )
    print("\nRemaining structural (fatigue) life:")
    print(f"  IRC:81 deflection capacity : {res.allowable_msa_deflection:.1f} MSA "
          f"(at {args.deflection:.2f} mm rebound)")
    if args.design_msa is not None:
        print(f"  IRC:37 traffic budget left  : {res.remaining_msa_traffic:.1f} MSA "
              f"(of {args.design_msa:.0f} MSA design)")
    yrs = "never (no traffic)" if res.residual_years is None else f"{res.residual_years:.1f} yr"
    print(f"  >> governing: {res.governing_remaining_msa:.1f} MSA via {res.governing_basis} "
          f"-> ~{yrs}")
    if args.required_residual_msa is not None:
        h = handback_assessment(res, required_residual_msa=args.required_residual_msa)
        colour = ""
        if _use_colour():
            colour = "\033[32m" if h.verdict.value == "PASS" else (
                "\033[33m" if h.verdict.value == "MARGINAL" else "\033[31m")
        reset = _RESET if _use_colour() else ""
        print(f"  handback ({args.required_residual_msa:.0f} MSA reqd): "
              f"{colour}{h.verdict.value}{reset} -- {h.rationale}")


def _print_triggers(timeline, deflection_mm, design_msa) -> None:
    """Print the first year each Indian intervention trigger fires."""
    seen = set()
    rows = []
    for yr in timeline:
        for t in evaluate_triggers(
            yr, cumulative_msa=yr.cumulative_msa, design_msa=design_msa,
            deflection_mm=deflection_mm,
        ):
            key = (t.name, t.severity)
            if key in seen:
                continue
            seen.add(key)
            rows.append((yr.year, t))
    if not rows:
        return
    print("\nIntervention triggers (first crossing):")
    print(f"{'Year':>4} {'Severity':<11} {'Trigger':<12} {'IRC ref':<16} Reason")
    print("-" * 78)
    for year, t in sorted(rows, key=lambda r: r[0]):
        mark = "\033[31m" if t.severity is TriggerSeverity.STRUCTURAL else "\033[33m"
        reset = _RESET if _use_colour() else ""
        mark = mark if _use_colour() else ""
        print(
            f"{year:>4} {mark}{t.severity.value:<11}{reset} {t.name:<12} "
            f"{t.irc_reference:<16} {t.reason}"
        )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rams",
        description="Indian Pavement Deterioration Engine (IRC:82 forecasting).",
    )
    p.add_argument("--iri", type=float, default=1.5, help="Initial IRI (mm/m)")
    p.add_argument("--rut", type=float, default=2.0, help="Initial rut depth (mm)")
    p.add_argument("--crack", type=float, default=0.0, help="Initial cracking (%%)")
    p.add_argument("--msa", type=float, default=4.5, help="Annual MSA (ignored if --cvpd given)")
    p.add_argument("--growth", type=float, default=0.06, help="Traffic growth rate")
    # --- IRC:37 traffic from CVPD/VDF (Indian overloading) -----------------
    p.add_argument("--cvpd", type=float, default=None, help="Commercial vehicles/day; derives MSA via IRC:37")
    p.add_argument("--vdf", type=float, default=None, help="Vehicle Damage Factor (default: IRC:37 indicative for --terrain)")
    p.add_argument("--terrain", default="plain", choices=["plain", "rolling", "hilly"], help="Terrain (for default VDF)")
    p.add_argument("--design-life", type=int, default=15, help="IRC:37 design life (years) for design MSA")
    p.add_argument(
        "--carriageway", default="two_lane",
        choices=["single", "intermediate", "two_lane", "four_lane", "six_lane"],
        help="Carriageway type (IRC:37 lane-distribution factor)",
    )
    p.add_argument(
        "--zone", default="HIGH", choices=[z.value for z in MonsoonZone],
        help="Monsoon zone",
    )
    p.add_argument("--years", type=int, default=10, help="Forecast horizon (years)")
    p.add_argument("--id", default="SEGMENT", help="Segment identifier")
    # --- rut model selection (default IRC:82 law vs mechanistic HDM-4) ------
    p.add_argument(
        "--model", default="default", choices=[m.value.lower() for m in RutModelType],
        help="Rut model: 'default' (IRC:82 power law) or 'hdm4' (mechanistic)",
    )
    p.add_argument(
        "--pavement", default="dense", choices=["dense", "porous"],
        help="HDM-4 / MLIT calibration preset (dense-graded vs porous AC, Japan NH)",
    )
    p.add_argument(
        "--crack-model", default="default", choices=[m.value.lower() for m in CrackModelType],
        help="Cracking model: 'default' (IRC:82 S-curve) or 'mlit' (paper recursion)",
    )
    p.add_argument(
        "--roughness-model", default="default", choices=[m.value.lower() for m in RoughnessModelType],
        help="Roughness model: 'default' (IRI law) or 'hdm4' (coupled to rut/crack)",
    )
    p.add_argument(
        "--skid-model", default="none", choices=[m.value.lower() for m in SkidModelType],
        help="Skid model: 'none' or 'hdm4' (SFC aggregate-polishing decay)",
    )
    p.add_argument("--base-skid", type=float, default=0.55, help="Initial skid resistance (SFC, fraction)")
    p.add_argument(
        "--pothole-model", default="none", choices=[m.value.lower() for m in PotholeModelType],
        help="Pothole model: 'none' or 'hdm4' (crack-initiated potholing)",
    )
    p.add_argument("--base-potholes", type=float, default=0.0, help="Initial potholing area (%%)")
    p.add_argument("--deflection", type=float, default=0.5, help="HDM-4: FWD/Benkelman rebound deflection (mm)")
    p.add_argument("--snp", type=float, default=4.0, help="HDM-4: adjusted structural number")
    p.add_argument("--comp", type=float, default=98.0, help="HDM-4: relative compaction (%%)")
    p.add_argument("--hs", type=float, default=100.0, help="HDM-4: bituminous surfacing thickness (mm)")
    p.add_argument("--cds", type=float, default=1.0, help="HDM-4: construction-defects indicator (0.5-1.5)")
    p.add_argument("--speed", type=float, default=50.0, help="HDM-4: heavy-vehicle speed (km/h)")
    p.add_argument("--derive-snp", action="store_true", help="Back-calculate SNP from --deflection (FWD survey)")
    p.add_argument("--design-msa", type=float, default=None, help="IRC:37 design traffic (MSA) for the fatigue-life trigger")
    # --- residual structural (fatigue) life --------------------------------
    p.add_argument("--residual", action="store_true", help="Print IRC:81/IRC:37 remaining structural life")
    p.add_argument("--cumulative-msa", type=float, default=0.0, help="MSA carried since last overlay (residual life)")
    p.add_argument("--required-residual-msa", type=float, default=None, help="HAM/BOT handback requirement (MSA); prints PASS/FAIL")

    # IRC:37 new-pavement structural design (CBR + design MSA -> layer thicknesses)
    p.add_argument("--design", action="store_true", help="Run an IRC:37 pavement design (needs --cbr; design MSA from --design-msa or --cvpd)")
    p.add_argument("--cbr", type=float, default=8.0, help="Subgrade CBR (%%) for the IRC:37 design")
    p.add_argument("--reliability", type=int, default=None, choices=[80, 90], help="IRC:37 reliability (default: 80 if <20 MSA else 90)")
    p.add_argument("--design-method", default="catalogue", choices=["catalogue", "iitpave"], help="Design method: IRC:37-2018 catalogue, or IITPAVE mechanistic (layered-elastic)")

    # IITPAVE-style mechanistic check of an existing section (FWD moduli -> life)
    p.add_argument("--iitpave", action="store_true", help="Mechanistic check of a section from layer moduli + thicknesses")
    p.add_argument("--e-bt", type=float, default=3000.0, help="IITPAVE: bituminous modulus (MPa)")
    p.add_argument("--e-gran", type=float, default=250.0, help="IITPAVE: granular modulus (MPa)")
    p.add_argument("--e-sub", type=float, default=70.0, help="IITPAVE: subgrade modulus (MPa)")
    p.add_argument("--h-bt", type=float, default=150.0, help="IITPAVE: bituminous thickness (mm)")
    p.add_argument("--h-gran", type=float, default=450.0, help="IITPAVE: granular thickness (mm)")
    p.add_argument("--standard", default="irc37", choices=["irc37", "irc115"], help="Mechanistic fatigue model: IRC:37-2018 (design) or IRC:115-2014 (FWD remaining-life)")
    p.add_argument("--fwd", metavar="PATH", help="FWD overlay: CSV of homogeneous sub-sections (15th-pct moduli) -> remaining life + overlay (needs --design-msa)")

    # Performance-Based Maintenance Contract (PBMC) cost estimate (5-7 years)
    p.add_argument("--pbmc", action="store_true", help="Price a 5-7y PBMC for the segment/network (financial-forecast layer)")
    p.add_argument("--pbmc-years", type=int, default=5, help="PBMC term in years (typically 5-7)")
    p.add_argument("--pbmc-pci", type=float, default=3.0, help="Contractual minimum IRC:82 PCI (service level)")
    p.add_argument("--routine-rate", type=float, default=1.5, help="Routine maintenance rate (cost units/km/yr, year-1 prices)")
    p.add_argument("--escalation", type=float, default=0.05, help="PBMC annual price escalation")
    p.add_argument("--contingency", type=float, default=0.10, help="PBMC contingency fraction")
    p.add_argument("--overhead", type=float, default=0.10, help="PBMC contractor overhead+profit fraction")
    p.add_argument("--discount", type=float, default=0.08, help="PBMC discount rate for NPV")
    # --- calibration harness -----------------------------------------------
    p.add_argument("--calibrate-csv", metavar="PATH", help="Fit a deterioration model from a field-observations CSV")
    p.add_argument("--calibrate-kind", default="rut", choices=["rut", "cracking", "roughness", "skid", "potholes"], help="Which model to calibrate")
    p.add_argument("--calibrate-out", metavar="PATH", help="Write the fitted calibration to JSON")
    p.add_argument("--csv", metavar="PATH", help="Forecast a network from a CSV file")
    p.add_argument("--xlsx", metavar="PATH", help="Forecast a network from an XLSX survey (RAMS or NSV chainage schema)")
    p.add_argument("--pdf", metavar="PATH", help="Forecast a network from a (text-based) PDF condition report")
    p.add_argument("--summary", action="store_true", help="Print network triage summary")
    p.add_argument("--html", metavar="PATH", help="Write a self-contained HTML report")
    p.add_argument("--json", metavar="PATH", help="Write a JSON export")
    p.add_argument("--out-csv", metavar="PATH", help="Write the timeline as CSV")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    policy = MaintenancePolicy()

    try:
        if args.calibrate_csv:
            return _run_calibrate(args)
        if args.fwd:
            return _run_fwd_overlay(args)
        if args.iitpave:
            return _run_iitpave(args)
        if args.design:
            return _run_design(args)
        if args.csv or args.xlsx or args.pdf:
            return _run_network(args, policy)
        return _run_single(args, policy)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run_calibrate(args: argparse.Namespace) -> int:
    import csv as _csv
    import json

    kind = args.calibrate_kind
    print(f"\n{kind.capitalize()} model calibration")
    print("-" * 56)

    if kind == "rut":
        result = calibrate_hdm4_rut(load_observations_csv(args.calibrate_csv))
        print(result.summary())
        if result.fixed_to_zero:
            print("note: " + ", ".join(result.fixed_to_zero) +
                  " regressed <= 0 and were forced to 0 (physically inadmissible).")
        payload = {
            "kind": "rut", "k_rid": result.k_rid, "k_rst": result.k_rst,
            "k_rpd": result.k_rpd, "r_squared": result.r_squared,
            "rmse_before": result.rmse_before, "rmse_after": result.rmse_after,
            "n": result.n, "fixed_to_zero": list(result.fixed_to_zero),
        }
    elif kind == "cracking":
        with open(args.calibrate_csv, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        pairs = [(float(r["crack_prev"]), float(r["crack_next"])) for r in rows]
        result = calibrate_mlit_cracking(pairs)
        print(result.summary())
        payload = {"kind": "cracking", "a": result.a, "b": result.b,
                   "r_squared": result.r_squared, "n": result.n}
    elif kind == "roughness":
        from .calibrate import RoughnessObservation
        with open(args.calibrate_csv, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        obs = [RoughnessObservation(
            measured_iri_increment=float(r["measured_iri_increment"]), iri=float(r["iri"]),
            structural_number=float(r["structural_number"]), age=int(float(r["age"])),
            d_msa=float(r["d_msa"]), d_crack_pct=float(r["d_crack_pct"]), d_rut_mm=float(r["d_rut_mm"]),
        ) for r in rows]
        result = calibrate_hdm4_roughness(obs)
        print(result.summary())
        payload = {"kind": "roughness", "env_coeff": result.env_coeff,
                   "struct_a0": result.struct_a0, "crack_coeff": result.crack_coeff,
                   "rut_coeff": result.rut_coeff, "r_squared": result.r_squared, "n": result.n}
    elif kind == "skid":
        from .calibrate import SkidObservation
        with open(args.calibrate_csv, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        obs = [SkidObservation(
            measured_sfc_decrement=float(r["measured_sfc_decrement"]),
            sfc=float(r["sfc"]), d_msa=float(r["d_msa"]),
        ) for r in rows]
        result = calibrate_hdm4_skid(obs)
        print(result.summary())
        payload = {"kind": "skid", "decay_k": result.decay_k, "sfc_min": result.sfc_min,
                   "r_squared": result.r_squared, "n": result.n}
    else:  # potholes
        from .calibrate import PotholeObservation
        with open(args.calibrate_csv, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        obs = [PotholeObservation(
            measured_pothole_increment=float(r["measured_pothole_increment"]),
            cracking_pct=float(r["cracking_pct"]), d_msa=float(r["d_msa"]),
        ) for r in rows]
        result = calibrate_hdm4_potholes(obs)
        print(result.summary())
        payload = {"kind": "potholes", "rate": result.rate,
                   "crack_threshold_pct": result.crack_threshold_pct,
                   "r_squared": result.r_squared, "n": result.n}

    if args.calibrate_out:
        _write(args.calibrate_out, json.dumps(payload, indent=2))
    return 0


def _run_design(args: argparse.Namespace) -> int:
    """IRC:37 flexible-pavement structural design from CBR + design traffic."""
    d_msa = args.design_msa
    if d_msa is None and args.cvpd is not None:
        vdf = args.vdf if args.vdf is not None else default_vdf(args.cvpd, args.terrain)
        t = design_msa(
            args.cvpd, vdf=vdf, growth_rate=args.growth, design_life_years=args.design_life,
            lane_distribution=lane_distribution_factor(args.carriageway),
        )
        d_msa = t.design_msa
        print(f"(IRC:37 traffic: CVPD={args.cvpd:.0f} x VDF={vdf:.2f} -> "
              f"{args.design_life}y design {t.design_msa:.1f} MSA)")
    if d_msa is None:
        raise ValueError("provide --design-msa, or --cvpd to derive it via IRC:37.")

    if args.design_method == "iitpave":
        m = design_pavement_mechanistic(
            cbr=args.cbr, design_msa=d_msa, design_life_years=args.design_life,
        )
        s = m.strains
        print(f"\nIITPAVE mechanistic design | CBR={m.cbr:.1f}% | "
              f"design {m.design_msa:.0f} MSA / {m.design_life_years}y")
        print(f"subgrade M_R {m.subgrade_modulus_mpa:.0f} MPa | bituminous E {m.e_bituminous_mpa:.0f} "
              f"| granular E {m.e_granular_mpa:.0f} MPa")
        print("-" * 52)
        print(f"  {'-> bituminous (BC+DBM)':<24}{m.bituminous_mm:>6.0f} mm")
        print(f"  {'-> granular (WMM+GSB)':<24}{m.granular_mm:>6.0f} mm")
        print(f"  {'TOTAL above subgrade':<24}{m.total_mm:>6.0f} mm")
        print("-" * 52)
        print(f"  eps_t {s.tensile_microstrain:.0f} microstrain -> fatigue  {s.fatigue_life_msa:.0f} MSA")
        print(f"  eps_v {s.vertical_microstrain:.0f} microstrain -> rutting  {s.rutting_life_msa:.0f} MSA")
        print(f"  governing: {s.governing_mode} ({s.governing_life_msa:.0f} MSA)")
        print(f"\n>> {m.rationale}\n")
        if args.json:
            import json
            _write(args.json, json.dumps(m.as_dict(), indent=2))
        return 0

    design = design_pavement(
        cbr=args.cbr, design_msa=d_msa, design_life_years=args.design_life,
        reliability=args.reliability,
    )
    L = design  # noqa: E741  (short alias for the table below)
    print(f"\nIRC:37 flexible-pavement design | CBR={L.cbr:.1f}% | "
          f"design {L.design_msa:.0f} MSA / {L.design_life_years}y | {L.reliability}% reliability")
    print(f"subgrade resilient modulus: {L.subgrade_modulus_mpa:.0f} MPa")
    print("-" * 52)
    print(f"  {'BC (wearing)':<22}{L.bc_mm:>6.0f} mm")
    print(f"  {'DBM (binder)':<22}{L.dbm_mm:>6.0f} mm")
    print(f"  {'-> bituminous total':<22}{L.bituminous_mm:>6.0f} mm")
    print(f"  {'WMM (base)':<22}{L.wmm_mm:>6.0f} mm")
    print(f"  {'GSB (sub-base)':<22}{L.gsb_mm:>6.0f} mm")
    print(f"  {'-> granular total':<22}{L.granular_mm:>6.0f} mm")
    print("-" * 52)
    print(f"  {'TOTAL above subgrade':<22}{L.total_mm:>6.0f} mm")
    print(f"\n>> {L.rationale}\n")
    if args.json:
        import json
        _write(args.json, json.dumps(L.as_dict(), indent=2))
    return 0


def _run_iitpave(args: argparse.Namespace) -> int:
    """Mechanistic (IITPAVE-style) check of a section from its layer moduli."""
    layer = LayerModel(
        e_bituminous_mpa=args.e_bt, e_granular_mpa=args.e_gran, e_subgrade_mpa=args.e_sub,
        h_bituminous_mm=args.h_bt, h_granular_mm=args.h_gran,
    )
    a = evaluate_section(
        layer, annual_msa=args.msa, traffic_growth_rate=args.growth,
        cumulative_msa=args.cumulative_msa, design_msa=args.design_msa,
        standard=args.standard,
    )
    s = a.strains
    print(f"\nIITPAVE layered-elastic section check ({args.standard.upper()})")
    print(f"section: BT {args.h_bt:.0f}mm @ {args.e_bt:.0f} MPa | granular {args.h_gran:.0f}mm @ "
          f"{args.e_gran:.0f} MPa | subgrade {args.e_sub:.0f} MPa")
    print("-" * 52)
    print(f"  eps_t {s.tensile_microstrain:.0f} microstrain -> fatigue  {s.fatigue_life_msa:.0f} MSA")
    print(f"  eps_v {s.vertical_microstrain:.0f} microstrain -> rutting  {s.rutting_life_msa:.0f} MSA")
    print(f"  governing capacity: {s.governing_life_msa:.0f} MSA ({s.governing_mode})")
    print(f"\n>> {a.rationale}\n")
    return 0


def _run_fwd_overlay(args: argparse.Namespace) -> int:
    """FWD remaining-life + overlay across homogeneous sub-sections (IRC:115)."""
    import csv as _csv
    if args.design_msa is None:
        raise ValueError("--fwd needs --design-msa (the pavement's design traffic).")
    with open(args.fwd, newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    if not rows:
        raise ValueError("no rows in the FWD sections CSV.")
    sections = [
        FWDSection(
            section_id=r.get("section_id", f"Sec-{i}"),
            e_bituminous_mpa=float(r["e_bituminous"]), e_granular_mpa=float(r["e_granular"]),
            e_subgrade_mpa=float(r["e_subgrade"]), h_bituminous_mm=float(r["h_bituminous"]),
            h_granular_mm=float(r["h_granular"]),
            chainage_from=float(r["chainage_from"]) if r.get("chainage_from") else None,
            chainage_to=float(r["chainage_to"]) if r.get("chainage_to") else None,
        )
        for i, r in enumerate(rows, start=1)
    ]
    res = evaluate_fwd_sections(sections, args.design_msa)
    print(f"\nFWD remaining-life & overlay (IRC:115-2014) | design {args.design_msa:.0f} MSA | "
          f"{len(res.rows)} sub-section(s)")
    print("-" * 78)
    print(f"  {'Section':<14}{'eps_t':>7}{'eps_v':>7}{'Fatigue':>9}{'Rutting':>9}{'Remain':>9}  Overlay")
    for r in res.rows:
        flag = "YES" if r.overlay_required else "no"
        if r.confirm_with_iitpave:
            flag += "*"
        print(f"  {r.section_id[:14]:<14}{r.tensile_microstrain:>7.0f}{r.vertical_microstrain:>7.0f}"
              f"{r.remaining_fatigue_msa:>9.0f}{r.remaining_rutting_msa:>9.0f}"
              f"{r.remaining_life_msa:>9.0f}  {flag}")
    print("-" * 78)
    print(f">> {res.as_dict()['verdict']}")
    if res.borderline_sections:
        print("   (* borderline: within 15% of design life -- confirm with IITPAVE)")
    print()
    return 0


def _print_pbmc(est) -> None:
    """Render a single-segment PBMC estimate as a year-by-year cash flow."""
    print(f"\nPBMC estimate | {est.segment_id} | {est.term_years}y term | "
          f"service level PCI >= {est.performance_pci:.2f} | {est.length_km:.1f} km")
    compliant = "yes" if est.compliant else f"NO (min PCI {est.min_pci:.2f})"
    print(f"performance-compliant: {compliant}")
    print("-" * 64)
    print(f"  {'Yr':>2} {'Routine':>9} {'Periodic':>9} {'Initial':>9} {'Total':>10}  Treatments")
    for y in est.years:
        treat = ", ".join(y.treatments) if y.treatments else ""
        print(f"  {y.year:>2} {y.routine:>9.1f} {y.periodic:>9.1f} {y.initial:>9.1f} "
              f"{y.total:>10.1f}  {treat}")
    print("-" * 64)
    print(f"  routine {est.total_routine:.1f} | periodic {est.total_periodic:.1f} | "
          f"initial {est.initial_rectification:.1f}")
    print(f"  CONTRACT VALUE {est.contract_value:.1f}  (NPV {est.npv:.1f}, "
          f"{est.cost_per_km:.1f}/km)")
    print(f"\n>> {est.rationale}\n")


def _pbmc_params(args: argparse.Namespace) -> PBMCParams:
    return PBMCParams(
        term_years=args.pbmc_years, performance_pci=args.pbmc_pci,
        routine_rate_per_km_year=args.routine_rate, base_unit_cost=30.0,
        escalation_rate=args.escalation, contingency_pct=args.contingency,
        overhead_pct=args.overhead, discount_rate=args.discount,
    )


def _run_single(args: argparse.Namespace, policy: MaintenancePolicy) -> int:
    rut_model = RutModelType.from_str(args.model)
    hdm4_cal = hdm4_preset(args.pavement)
    snp = snp_from_deflection(args.deflection) if args.derive_snp else args.snp

    # IRC:37: derive annual + design MSA from CVPD/VDF when supplied.
    annual_msa = args.msa
    if args.cvpd is not None:
        vdf = args.vdf if args.vdf is not None else default_vdf(args.cvpd, args.terrain)
        t = design_msa(
            args.cvpd, vdf=vdf, growth_rate=args.growth, design_life_years=args.design_life,
            lane_distribution=lane_distribution_factor(args.carriageway),
        )
        annual_msa = t.annual_msa
        args.msa = annual_msa  # so residual-life / triggers use the IRC:37 value
        if args.design_msa is None:
            args.design_msa = t.design_msa
        print(f"(IRC:37 traffic: CVPD={args.cvpd:.0f} x VDF={vdf:.2f} -> "
              f"annual {t.annual_msa:.2f} MSA, {args.design_life}y design {t.design_msa:.1f} MSA)")
    if args.derive_snp:
        print(f"(derived SNP={snp} from FWD deflection {args.deflection} mm)")
    engine = IndianPavementDeteriorationEngine(
        base_iri=args.iri,
        base_rut=args.rut,
        base_crack=args.crack,
        annual_msa=annual_msa,
        traffic_growth_rate=args.growth,
        monsoon_zone=args.zone,
        rut_model=rut_model,
        hdm4_calibration=hdm4_cal,
        crack_model=CrackModelType.from_str(args.crack_model),
        mlit_crack=mlit_crack_preset(args.pavement),
        roughness_model=RoughnessModelType.from_str(args.roughness_model),
        skid_model=SkidModelType.from_str(args.skid_model),
        base_skid=args.base_skid,
        pothole_model=PotholeModelType.from_str(args.pothole_model),
        base_potholes=args.base_potholes,
        deflection_mm=args.deflection,
        structural_number=snp,
        compaction_pct=args.comp,
        surfacing_thickness_mm=args.hs,
        cds=args.cds,
        heavy_speed_kmh=args.speed,
    )
    timeline = engine.run_lifecycle_forecast(args.years)
    plan = build_maintenance_plan(timeline, policy)
    annotate_timeline(timeline, policy)

    model_note = (
        hdm4_cal.label if rut_model is RutModelType.HDM4 else "default IRC:82 power law"
    )
    print(f"\nSegment {args.id} | zone={args.zone} | horizon={args.years}y")
    print(f"rut model: {model_note}")
    if (args.crack_model != "default" or args.roughness_model != "default"
            or args.skid_model != "none" or args.pothole_model != "none"):
        print(f"crack: {args.crack_model} | roughness: {args.roughness_model} | "
              f"skid: {args.skid_model} | potholes: {args.pothole_model}")
    print()
    _print_table(timeline, policy)
    if args.skid_model != "none":
        sfc = [f"y{yr.year}:{yr.skid}" for yr in timeline]
        print("\nSkid resistance (SFC): " + "  ".join(sfc))
    if args.pothole_model != "none":
        pot = [f"y{yr.year}:{yr.potholes}" for yr in timeline]
        print("\nPotholing (area %): " + "  ".join(pot))
    if rut_model is RutModelType.HDM4:
        _print_hdm4_breakdown(engine.rut_breakdown)
    _print_triggers(timeline, args.deflection, args.design_msa)
    if args.residual or args.design_msa is not None or args.required_residual_msa is not None:
        _print_residual(args)
    print(f"\n>> {plan.rationale}\n")

    if args.pbmc:
        seg = SegmentInput(
            base_iri=args.iri, base_rut=args.rut, base_crack=args.crack,
            annual_msa=annual_msa, traffic_growth_rate=args.growth,
            monsoon_zone=MonsoonZone.from_str(args.zone), segment_id=args.id,
            length_km=1.0, deflection_mm=args.deflection, structural_number=snp,
        )
        est = estimate_pbmc(
            seg, _pbmc_params(args), rut_model=rut_model, hdm4_calibration=hdm4_cal,
            crack_model=CrackModelType.from_str(args.crack_model),
            roughness_model=RoughnessModelType.from_str(args.roughness_model),
        )
        _print_pbmc(est)

    if args.out_csv:
        _write(args.out_csv, to_csv(timeline))
    if args.json:
        _write(args.json, to_json(timeline, plan))
    if args.html:
        _write(args.html, to_html(args.id, timeline, plan, policy))
    return 0


def _run_network(args: argparse.Namespace, policy: MaintenancePolicy) -> int:
    if args.csv:
        path, ingest = args.csv, ingest_segments_csv(args.csv)
    elif args.xlsx:
        path, ingest = args.xlsx, ingest_segments_xlsx(args.xlsx)
    else:
        path, ingest = args.pdf, ingest_segments_pdf(args.pdf)
    print(f"loaded {len(ingest.segments)} segment(s) from {path}")
    if ingest.errors:
        print(f"warning: {len(ingest.errors)} row(s) skipped:", file=sys.stderr)
        for row_no, msg in ingest.errors[:10]:
            print(f"  row {row_no}: {msg}", file=sys.stderr)
    rut_model = RutModelType.from_str(args.model)
    if rut_model is RutModelType.HDM4:
        print(f"rut model: {hdm4_preset(args.pavement).label}")
    forecasts = list(
        forecast_network(
            ingest.segments, args.years, policy,
            rut_model=rut_model, hdm4_calibration=hdm4_preset(args.pavement),
            crack_model=CrackModelType.from_str(args.crack_model),
            mlit_crack=mlit_crack_preset(args.pavement),
            roughness_model=RoughnessModelType.from_str(args.roughness_model),
            skid_model=SkidModelType.from_str(args.skid_model),
        )
    )

    print(f"\n{len(forecasts)} segment(s) forecast over {args.years}y\n")
    print(f"{'Segment':<16} {'Prev.Yr':>8} {'Expiry':>7} {'Recommended':<26}")
    print("-" * 60)
    for fc in forecasts:
        rec = fc.plan.recommended_treatment.name if fc.plan.recommended_treatment else "-"
        print(
            f"{fc.segment_id[:16]:<16} "
            f"{str(fc.plan.preventive_window_year or '-'):>8} "
            f"{str(fc.plan.window_expired_year or '-'):>7} {rec:<26}"
        )

    if args.summary:
        s = network_summary(forecasts)
        print(
            f"\nNetwork triage: {s['total']} total | "
            f"{s['routine_only']} routine | {s['needs_preventive']} preventive | "
            f"{s['window_expired']} structural"
        )

    # Per-segment remaining structural life / handback (IRC:81 + IRC:37).
    if args.design_msa is not None or args.required_residual_msa is not None:
        print(f"\n{'Segment':<16} {'Resid.MSA':>9} {'Resid.yr':>8} {'Basis':<24} {'Handback':>9}")
        print("-" * 70)
        counts = {"PASS": 0, "MARGINAL": 0, "FAIL": 0}
        for seg in ingest.segments:
            res = remaining_fatigue_life(
                deflection_mm=seg.deflection_mm, annual_msa=seg.annual_msa,
                traffic_growth_rate=seg.traffic_growth_rate, cumulative_msa=0.0,
                design_msa=args.design_msa,
            )
            verdict = "-"
            if args.required_residual_msa is not None:
                v = handback_assessment(res, required_residual_msa=args.required_residual_msa).verdict.value
                counts[v] += 1
                verdict = v
            yrs = "inf" if res.residual_years is None else f"{res.residual_years:.1f}"
            print(
                f"{seg.segment_id[:16]:<16} {res.governing_remaining_msa:>9.1f} {yrs:>8} "
                f"{res.governing_basis:<24} {verdict:>9}"
            )
        if args.required_residual_msa is not None:
            print(
                f"\nHandback ({args.required_residual_msa:.0f} MSA reqd): "
                f"{counts['PASS']} pass | {counts['MARGINAL']} marginal | {counts['FAIL']} fail"
            )

    if args.pbmc:
        net = estimate_pbmc_network(
            ingest.segments, _pbmc_params(args), rut_model=rut_model,
            hdm4_calibration=hdm4_preset(args.pavement),
            crack_model=CrackModelType.from_str(args.crack_model),
            roughness_model=RoughnessModelType.from_str(args.roughness_model),
        )
        print(f"\n{net.term_years}y PBMC over {net.n_segments} segment(s) / "
              f"{net.total_length_km:.1f} km (service level PCI >= {net.performance_pci:.2f})")
        print("-" * 64)
        print(f"  {'Segment':<16} {'km':>6} {'Contract':>10} {'NPV':>10} {'/km':>8}  Compl.")
        for e in net.segments:
            print(f"  {e.segment_id[:16]:<16} {e.length_km:>6.1f} {e.contract_value:>10.1f} "
                  f"{e.npv:>10.1f} {e.cost_per_km:>8.1f}  {'y' if e.compliant else 'N'}")
        print("-" * 64)
        print(f"  routine {net.total_routine:.1f} | periodic {net.total_periodic:.1f} | "
              f"initial {net.total_initial:.1f}")
        print(f"  NETWORK CONTRACT VALUE {net.contract_value:.1f}  (NPV {net.npv:.1f})")
        if net.non_compliant:
            print(f"  performance-deficient (raise budget/shorten interval): "
                  f"{', '.join(net.non_compliant)}")
    return 0


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
