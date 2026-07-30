"""Maintenance commands that operate on the database directly.

These are deliberately not HTTP endpoints: they rewrite history in bulk and should be run
deliberately by an operator, not reachable from an agent loop.
"""

import argparse
import json
import sys
from datetime import date

from .backfill import backfill_all, verify_projection_consistency
from .db import SessionLocal
from .flows import set_flow_override, suggest_reclassifications
from .market import YahooMarketProvider
from .valuation import rebuild_snapshots


def _backfill(_args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        report = backfill_all(session)
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def _verify(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        result = verify_projection_consistency(session, args.portfolio_id)
    printable = {
        key: (format(value, "f") if hasattr(value, "quantize") else value)
        for key, value in result.items()
    }
    print(json.dumps(printable, indent=2))
    return 0 if result["consistent"] else 1


def _rebuild(args: argparse.Namespace) -> int:
    provider = YahooMarketProvider()
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


def _review_flows(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        suggestions = suggest_reclassifications(session, args.portfolio_id)
    if not suggestions:
        print("No unruled legacy cash events. Nothing to review.")
        return 0

    print(
        f"{len(suggestions)} legacy cash events have no ruling on whether they crossed the\n"
        "portfolio boundary. Suggestions below are advisory: confirm each one before applying.\n"
    )
    for item in suggestions:
        print(f"  event    {item.event_id}")
        print(f"  when     {item.occurred_at:%Y-%m-%d}   was: {item.source_reference or '-'}")
        print(f"  cash     {item.cash_delta:>14,.2f}")
        print(f"  now      {item.current.value}  ->  suggested: {item.suggested.value} "
              f"({item.confidence} confidence)")
        for line in item.evidence:
            print(f"           - {line}")
        print(
            f"  apply    portfolio-admin set-flow {item.event_id} "
            f"{item.suggested.value} --reason '...'\n"
        )
    return 0


def _set_flow(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        row = set_flow_override(
            session,
            args.event_id,
            classification=args.classification,
            reason=args.reason,
            retract=args.retract,
        )
        session.commit()
        state = "retracted" if row.is_retracted else "active"
        print(
            json.dumps(
                {
                    "event_id": row.event_id,
                    "classification": row.classification,
                    "provenance": row.provenance,
                    "state": state,
                    "reason": row.reason,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="portfolio-admin", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    backfill = subcommands.add_parser(
        "backfill-journal",
        help="Convert legacy trades and cash transactions into journal events",
        description=(
            "Migrates pre-journal rows into the journal. Safe to re-run: already-migrated rows "
            "are skipped. Positions and cash are left untouched, since they already reflect "
            "these rows. Migrated events are marked unlinked_legacy because the original model "
            "never recorded which cash transaction settled which trade."
        ),
    )
    backfill.set_defaults(handler=_backfill)

    verify = subcommands.add_parser(
        "verify-journal",
        help="Compare a portfolio's stored cash against its journal",
    )
    verify.add_argument("portfolio_id")
    verify.set_defaults(handler=_verify)

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

    review = subcommands.add_parser(
        "review-flows",
        help="List legacy cash events awaiting a ruling, with suggested classifications",
        description=(
            "Shows every migrated cash event whose external/internal classification has not been "
            "confirmed, alongside the evidence for a suggestion. Suggestions are advisory and are "
            "never applied automatically: whether a cash movement was investor capital or a trade "
            "settlement is a fact about your records that only you can confirm."
        ),
    )
    review.add_argument("portfolio_id")
    review.set_defaults(handler=_review_flows)

    set_flow = subcommands.add_parser(
        "set-flow",
        help="Record a ruling on one event's flow classification",
        description=(
            "Records that an event was investor capital (external) or portfolio activity "
            "(internal). The posted event is never modified; this is a higher-ranked opinion that "
            "replay reads instead of the type-derived value, and --retract restores the original."
        ),
    )
    set_flow.add_argument("event_id")
    set_flow.add_argument("classification", choices=["external", "internal", "unknown"])
    set_flow.add_argument("--reason", required=True, help="Why, for the audit trail")
    set_flow.add_argument(
        "--retract", action="store_true", help="Withdraw this ruling and restore the derived value"
    )
    set_flow.set_defaults(handler=_set_flow)

    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
