"""Maintenance commands that operate on the database directly.

These are deliberately not HTTP endpoints: they rewrite history in bulk and should be run
deliberately by an operator, not reachable from an agent loop.
"""

import argparse
import json
import sys

from .backfill import backfill_all, verify_projection_consistency
from .db import SessionLocal


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

    args = parser.parse_args()
    sys.exit(args.handler(args))


if __name__ == "__main__":
    main()
