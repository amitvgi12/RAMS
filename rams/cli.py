"""
Command-line interface for the RAMS deterioration engine.

Usage examples:
    # Single segment (defaults reproduce the spec example):
    python -m rams.cli --iri 1.5 --rut 2.0 --crack 0.0 \\
        --msa 4.5 --growth 0.06 --zone HIGH --years 10

    # Emit an HTML report:
    python -m rams.cli --zone HIGH --html forecast.html

    # Forecast a whole network from CSV and triage it:
    python -m rams.cli --csv examples/sample_network.csv --summary

UI/UX note: terminal output is colour-coded by maintenance band (with a
NO_COLOR / non-TTY fallback) so a planner can eyeball the inflection year.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .batch import forecast_network, ingest_segments_csv, network_summary
from .config import MonsoonZone
from .engine import IndianPavementDeteriorationEngine
from .maintenance import (
    MaintenanceFlag,
    MaintenancePolicy,
    annotate_timeline,
    build_maintenance_plan,
)
from .models import YearResult
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


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rams",
        description="Indian Pavement Deterioration Engine (IRC:82 forecasting).",
    )
    p.add_argument("--iri", type=float, default=1.5, help="Initial IRI (mm/m)")
    p.add_argument("--rut", type=float, default=2.0, help="Initial rut depth (mm)")
    p.add_argument("--crack", type=float, default=0.0, help="Initial cracking (%%)")
    p.add_argument("--msa", type=float, default=4.5, help="Annual MSA")
    p.add_argument("--growth", type=float, default=0.06, help="Traffic growth rate")
    p.add_argument(
        "--zone", default="HIGH", choices=[z.value for z in MonsoonZone],
        help="Monsoon zone",
    )
    p.add_argument("--years", type=int, default=10, help="Forecast horizon (years)")
    p.add_argument("--id", default="SEGMENT", help="Segment identifier")
    p.add_argument("--csv", metavar="PATH", help="Forecast a network from a CSV file")
    p.add_argument("--summary", action="store_true", help="Print network triage summary")
    p.add_argument("--html", metavar="PATH", help="Write a self-contained HTML report")
    p.add_argument("--json", metavar="PATH", help="Write a JSON export")
    p.add_argument("--out-csv", metavar="PATH", help="Write the timeline as CSV")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    policy = MaintenancePolicy()

    try:
        if args.csv:
            return _run_network(args, policy)
        return _run_single(args, policy)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run_single(args: argparse.Namespace, policy: MaintenancePolicy) -> int:
    engine = IndianPavementDeteriorationEngine(
        base_iri=args.iri,
        base_rut=args.rut,
        base_crack=args.crack,
        annual_msa=args.msa,
        traffic_growth_rate=args.growth,
        monsoon_zone=args.zone,
    )
    timeline = engine.run_lifecycle_forecast(args.years)
    plan = build_maintenance_plan(timeline, policy)
    annotate_timeline(timeline, policy)

    print(f"\nSegment {args.id} | zone={args.zone} | horizon={args.years}y\n")
    _print_table(timeline, policy)
    print(f"\n>> {plan.rationale}\n")

    if args.out_csv:
        _write(args.out_csv, to_csv(timeline))
    if args.json:
        _write(args.json, to_json(timeline, plan))
    if args.html:
        _write(args.html, to_html(args.id, timeline, plan, policy))
    return 0


def _run_network(args: argparse.Namespace, policy: MaintenancePolicy) -> int:
    ingest = ingest_segments_csv(args.csv)
    if ingest.errors:
        print(f"warning: {len(ingest.errors)} row(s) skipped:", file=sys.stderr)
        for row_no, msg in ingest.errors[:10]:
            print(f"  row {row_no}: {msg}", file=sys.stderr)
    forecasts = list(forecast_network(ingest.segments, args.years, policy))

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
    return 0


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
