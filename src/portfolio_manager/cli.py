"""Maintenance commands that operate on the database directly.

These are deliberately not HTTP endpoints: they rewrite history in bulk and should be run
deliberately by an operator, not reachable from an agent loop.
"""

import argparse
import json
import sys
from datetime import date

from .cache import build_provider
from .db import SessionLocal
from .market import YahooMarketProvider
from .valuation import rebuild_snapshots


def _rebuild(args: argparse.Namespace) -> int:
    provider = build_provider(YahooMarketProvider())
    with SessionLocal() as session:
        report = rebuild_snapshots(
            session,
            args.portfolio_id,
            date.fromisoformat(args.start_date),
            date.fromisoformat(args.end_date),
            provider,
            force_revision=args.force,
        )
    print(
        json.dumps(
            {
                "portfolio_id": report.portfolio_id,
                "start_date": report.start_date.isoformat(),
                "end_date": report.end_date.isoformat(),
                "calculation_version": report.calculation_version,
                "created": report.created,
                "skipped_existing": report.skipped_existing,
                "partial": report.partial,
                "failed": report.failed,
                "warnings": report.warnings,
            },
            indent=2,
        )
    )
    return 1 if report.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="portfolio-admin", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    rebuild = subcommands.add_parser(
        "rebuild-snapshots",
        help="Build daily valuation snapshots across a date range",
        description=(
            "Values a portfolio on every date in the range, rebuilding holdings from the journal "
            "and pricing them with data available on each date. Safe to re-run: dates that "
            "already have a snapshot are skipped, so an interrupted run is resumed by repeating "
            "the command. This calls the market provider once per instrument for the range."
        ),
    )
    rebuild.add_argument("portfolio_id")
    rebuild.add_argument("start_date", help="First valuation date, YYYY-MM-DD")
    rebuild.add_argument("end_date", help="Last valuation date, YYYY-MM-DD")
    rebuild.add_argument(
        "--force",
        action="store_true",
        help="Replace snapshots that already exist instead of skipping those dates",
    )
    rebuild.set_defaults(handler=_rebuild)

    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
