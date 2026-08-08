"""Daily valuation-snapshot rebuild for every portfolio.

A thin client over the existing FastAPI service, following the same pattern as
`mcp_server.py`: it never touches the database directly, only `PORTFOLIO_API_BASE_URL`. Runs the
rebuild once at startup, then once a day at a configured UTC hour, forever. Each call is bounded
to a short rolling window rather than all-time, and `rebuild_snapshots` skips dates that already
have a snapshot, so a missed run or a late price correction is picked up by the next run without
any "last run" state to track.
"""

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8001"

logger = logging.getLogger("portfolio_manager.snapshot_cron")


def _run_once(client: httpx.Client, lookback_days: int) -> None:
    portfolios = client.get("/api/v1/portfolios").raise_for_status().json()
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)
    for portfolio in portfolios:
        portfolio_id = portfolio["id"]
        try:
            response = client.post(
                f"/api/v1/portfolios/{portfolio_id}/valuation-snapshots/rebuild",
                json={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            )
            response.raise_for_status()
            report = response.json()
            logger.info(
                "rebuilt portfolio=%s created=%d skipped_existing=%d partial=%d failed=%s "
                "warnings=%s",
                portfolio_id,
                report["created"],
                report["skipped_existing"],
                report["partial"],
                report["failed"],
                report["warnings"],
            )
        except httpx.HTTPError:
            logger.exception("rebuild failed for portfolio=%s", portfolio_id)


def _seconds_until_next_run(hour_utc: int) -> float:
    now = datetime.now(UTC)
    next_run = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    base_url = os.getenv("PORTFOLIO_API_BASE_URL", DEFAULT_BASE_URL)
    hour_utc = int(os.getenv("PORTFOLIO_SNAPSHOT_CRON_HOUR_UTC", "0"))
    lookback_days = int(os.getenv("PORTFOLIO_SNAPSHOT_LOOKBACK_DAYS", "7"))

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        while True:
            logger.info("starting snapshot rebuild for all portfolios")
            _run_once(client, lookback_days)
            sleep_seconds = _seconds_until_next_run(hour_utc)
            logger.info("next rebuild in %.0f seconds", sleep_seconds)
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
