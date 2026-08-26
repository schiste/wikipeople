from __future__ import annotations

import argparse
import logging

from wikipeople.runtime import build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune obsolete WikiPeople cache rows")
    parser.add_argument("--queue-days", type=int, default=30)
    parser.add_argument("--old-revision-days", type=int, default=30)
    # Half a year of not being viewed and not being asked for. The demand table is a
    # ranking, and a page nothing has touched in that long has dropped out of it; the
    # pruning is also what keeps a table of page counters from accumulating into
    # something a reading history could be read out of.
    parser.add_argument("--demand-days", type=int, default=180)
    parser.add_argument("--usage-days", type=int, default=365)
    args = parser.parse_args()
    configure_logging()
    runtime = build_runtime()
    runtime.database.create_schema()
    removed = runtime.repository.cleanup(
        args.queue_days, args.old_revision_days, args.demand_days, args.usage_days
    )
    LOGGER.info(
        "removed queue=%s results=%s demand=%s usage=%s",
        removed["queue"],
        removed["results"],
        removed["demand"],
        removed["usage"],
    )


if __name__ == "__main__":
    main()
