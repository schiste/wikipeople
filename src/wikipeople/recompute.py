"""Try again on the answers that had to settle for counting edits.

The attribution ladder has three rungs (ADR-0005). The top one asks WikiWho who wrote
the words that are still there; when WikiWho has not indexed the revision, the answer
falls to counting edits instead, which is a real answer to a different question and is
labelled as such. On frwiki that is 14% of the cache.

Nothing ever revisits those. The fallback is recorded as a result, results are keyed by
revision, and a revision that WikiWho indexes next week produces no event anybody is
listening for -- so a page that missed by hours keeps its weaker answer for as long as
it stays cached. This job is the listener: a slow, bounded sweep that re-queues weak
rows against the same revision they describe, so a successful recomputation replaces
the row in place rather than adding a second answer beside it.

Self-limiting by construction. A recomputation that lands on the same rung refreshes
`computed_at`, which puts the row back under the minimum-age floor and out of the next
sweep; only a row that has waited again becomes a candidate again. Queued below the
backfill, because a weaker answer that already exists is the least urgent work there is.
"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from wikipeople.models import utcnow
from wikipeople.runtime import Runtime, build_runtime, configure_logging
from wikipeople.worker import METRIC_EDITS

LOGGER = logging.getLogger(__name__)

#: Below the backfill's 10 and far below a reader's 100. A page with a weaker answer is
#: already answerable; a page with none is not.
RECOMPUTE_PRIORITY = 5


def cursor_key(algorithm_version: str, metric: str) -> str:
    """One cursor per (version, metric).

    Keyed on the algorithm version because a version bump invalidates every row it
    names: resuming a new version's sweep at the old one's high-water mark would skip
    everything below it.
    """
    return f"recompute:{algorithm_version}:{metric}:cursor"


def sweep(runtime: Runtime, metric: str, limit: int) -> int:
    """One bounded pass. Returns how many pages were queued.

    The cursor is the primary key, not a date, so the sweep covers the table exactly
    once per lap however much of it changes underneath. Reaching the end resets it to
    zero rather than stopping, which is what turns this into a rotation: by the time a
    lap finishes, the rows at the start of it have aged past the floor again.
    """
    settings = runtime.settings
    key = cursor_key(settings.algorithm_version, metric)
    cursor = runtime.repository.get_state(key)
    after_id = int(cursor) if cursor and cursor.isdigit() else 0
    cutoff = utcnow() - timedelta(seconds=settings.recompute_min_age_seconds)

    rows = runtime.repository.results_with_metric(
        algorithm_version=settings.algorithm_version,
        metric=metric,
        computed_before=cutoff,
        limit=limit,
        after_id=after_id,
    )
    if not rows:
        if after_id:
            LOGGER.info("%s: sweep complete past id %s, restarting", metric, after_id)
            runtime.repository.set_state(key, "0")
        else:
            LOGGER.info("%s: nothing older than %s to retry", metric, cutoff)
        return 0

    queued = 0
    for row in rows:
        if runtime.repository.enqueue_if_stale(
            wiki=row.wiki,
            page_id=row.page_id,
            # The revision the weak answer describes, not the current one. Same cache
            # key, so a better answer replaces this row instead of sitting beside it.
            revision_id=row.revision_id,
            algorithm_version=settings.algorithm_version,
            priority=RECOMPUTE_PRIORITY,
            freshness_seconds=settings.recompute_min_age_seconds,
        ):
            queued += 1
    runtime.repository.set_state(key, str(rows[-1].id))
    LOGGER.info(
        "%s: queued %s of %s rows, cursor now %s",
        metric,
        queued,
        len(rows),
        rows[-1].id,
    )
    return queued


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retry cached answers that fell back to edit counts"
    )
    parser.add_argument("--limit", type=int, default=None, help="Override RECOMPUTE_BATCH_SIZE")
    parser.add_argument("--metric", default=METRIC_EDITS)
    parser.add_argument("--restart", action="store_true", help="Sweep from the first row again")
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    if args.restart:
        runtime.repository.set_state(
            cursor_key(runtime.settings.algorithm_version, args.metric), "0"
        )
    limit = args.limit or runtime.settings.recompute_batch_size
    queued = sweep(runtime, args.metric, max(1, limit))
    LOGGER.info("queued %s pages for recomputation", queued)


if __name__ == "__main__":
    main()
