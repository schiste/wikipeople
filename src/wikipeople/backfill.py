"""Enqueue main-namespace articles for attribution, heaviest first.

The order is the whole point. `allpages` walks titles in binary order, and three
days of that on frwiki cached 36,046 asteroid designations out of 41,783 rows --
86% of the work, the newest entries carrying three attributable tokens each. At
that rate frwiki's 2.8 million articles need seven months, so the question is not
how fast the backfill runs but which 46,000 pages it has cached by then.

Descending page size answers it: the 220,000 frwiki articles above 20 KB are three
weeks of work, and they are the ones where "who wrote this" has an answer worth
caching. Size is a proxy, not a merit ranking -- but it is a proxy the replica can
sort on exactly, which "notability" is not.

Readership is the better proxy, and `pageviews.py` makes it sortable too. So the
order is now demand first -- the pages readers asked this API about, then the pages
the published dumps say the world opens -- and size for whatever budget is left over
once that ranking runs dry. Size never stopped being useful; it stopped being first.

The Action API keeps its place as a fallback for a wiki whose replica is
unreachable, filtered to non-redirects but still in title order.
"""

from __future__ import annotations

import argparse
import logging

from wikipeople.clients import MediaWikiClient
from wikipeople.replica import LengthCursor, ReplicaClient
from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)
COMPLETE = "__COMPLETE__"


def title_cursor_key(wiki: str) -> str:
    return f"backfill:{wiki}:cursor"


def length_cursor_key(wiki: str) -> str:
    """A separate key from the title cursor, because the two are not interchangeable.

    One holds a page title, the other a ``length:page_id`` pair. Reusing the key would
    have made a fallback run read a cursor it cannot parse, and a switch back read a
    title as a length.
    """
    return f"backfill:{wiki}:length-cursor"


def enqueue_pages(runtime: Runtime, wiki: str, pages: list[tuple[int, int]]) -> int:
    queued = 0
    for page_id, revision_id in pages:
        if runtime.repository.enqueue_if_stale(
            wiki=wiki,
            page_id=page_id,
            revision_id=revision_id,
            algorithm_version=runtime.settings.algorithm_version,
            priority=10,
            freshness_seconds=runtime.settings.page_freshness_seconds,
        ):
            queued += 1
    return queued


def demand_batch(
    runtime: Runtime, replica: ReplicaClient, wiki: str, limit: int
) -> tuple[int, int]:
    """Hand one batch of the demand ranking to the queue. Returns (queued, selected).

    The two numbers differ and both matter. ``selected`` is how much ranking there was
    left, which is what decides whether this strategy still has work; ``queued`` is how
    much of it was not already cached and fresh, which is usually far smaller and says
    nothing about whether to keep going.

    The ranking is page ids, so the replica is asked for their current revisions in one
    round trip. Every page selected is marked as handed over, including the ones the
    replica did not return: a deleted article or one turned into a redirect would
    otherwise sit at the top of the ranking for ever, re-selected every hour.

    Marked after the enqueue rather than before, so a run that dies against the replica
    retries the same pages next hour instead of silently dropping them for a quarter.
    """
    page_ids = runtime.repository.pending_demand(wiki, limit)
    if not page_ids:
        return 0, 0
    pages = replica.latest_revisions(wiki, page_ids)
    queued = enqueue_pages(
        runtime, wiki, [(page.page_id, page.revision_id) for page in pages.values()]
    )
    runtime.repository.mark_demand_queued(wiki, page_ids)
    LOGGER.info(
        "%s: %s of %s most-wanted pages queued (%s no longer articles)",
        wiki,
        queued,
        len(page_ids),
        len(page_ids) - len(pages),
    )
    return queued, len(page_ids)


def backfill_by_demand(
    runtime: Runtime, replica: ReplicaClient, wiki: str, batches: int
) -> tuple[int, int]:
    """Work down the demand ranking. Returns (pages queued, batches spent).

    Spending fewer batches than offered is how the size walk gets its turn: an empty
    ranking costs nothing and spends nothing, so a deployment with no pageview data yet
    behaves exactly as it did before this strategy existed.
    """
    queued = 0
    spent = 0
    for _batch in range(max(1, batches)):
        batch_queued, selected = demand_batch(
            runtime, replica, wiki, runtime.settings.backfill_batch_size
        )
        queued += batch_queued
        if not selected:
            break
        spent += 1
    return queued, spent


def backfill_by_length(runtime: Runtime, replica: ReplicaClient, wiki: str, batches: int) -> int:
    state_key = length_cursor_key(wiki)
    raw_cursor = runtime.repository.get_state(state_key)
    if raw_cursor == COMPLETE:
        LOGGER.info("%s: backfill already complete", wiki)
        return 0

    cursor = LengthCursor.decode(raw_cursor)
    queued = 0
    for _batch in range(max(1, batches)):
        pages, cursor = replica.pages_by_descending_length(
            wiki, cursor, runtime.settings.backfill_batch_size
        )
        if pages:
            LOGGER.info(
                "%s: batch of %s pages, %s to %s bytes",
                wiki,
                len(pages),
                pages[0].length,
                pages[-1].length,
            )
        queued += enqueue_pages(runtime, wiki, [(p.page_id, p.revision_id) for p in pages])
        runtime.repository.set_state(state_key, cursor.encode() if cursor else COMPLETE)
        if cursor is None:
            break
    LOGGER.info(
        "%s: queued %s backfill pages; next cursor=%s",
        wiki,
        queued,
        cursor.encode() if cursor else COMPLETE,
    )
    return queued


def backfill_by_title(runtime: Runtime, mediawiki: MediaWikiClient, wiki: str, batches: int) -> int:
    state_key = title_cursor_key(wiki)
    cursor = runtime.repository.get_state(state_key)
    if cursor == COMPLETE:
        LOGGER.info("%s: backfill already complete", wiki)
        return 0

    queued = 0
    for _batch in range(max(1, batches)):
        pages, next_cursor = mediawiki.all_pages_batch(wiki, cursor or None)
        queued += enqueue_pages(runtime, wiki, [(p.page_id, p.revision_id) for p in pages])
        cursor = next_cursor
        runtime.repository.set_state(state_key, cursor or COMPLETE)
        if cursor is None:
            break
    LOGGER.info("%s: queued %s backfill pages; next cursor=%s", wiki, queued, cursor or COMPLETE)
    return queued


def backfill_wiki(
    runtime: Runtime,
    mediawiki: MediaWikiClient,
    replica: ReplicaClient,
    wiki: str,
    batches: int,
) -> int:
    if not replica.available():
        LOGGER.info("%s: no replica credentials, falling back to title order", wiki)
        return backfill_by_title(runtime, mediawiki, wiki, batches)

    queued, spent = backfill_by_demand(runtime, replica, wiki, batches)
    remaining = max(0, batches - spent)
    if remaining:
        queued += backfill_by_length(runtime, replica, wiki, remaining)
    return queued


def main() -> None:
    parser = argparse.ArgumentParser(description="Gradually enqueue all main-namespace pages")
    parser.add_argument("--wiki", default=None, help="Override BACKFILL_WIKIS for this run")
    parser.add_argument("--batches", type=int, default=1)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = [args.wiki] if args.wiki else list(runtime.settings.backfill_wikis)
    wikis = [wiki for wiki in wikis if runtime.resolver.is_capable(wiki)]
    if not wikis:
        LOGGER.info("no wiki opted into backfill; set BACKFILL_WIKIS to enable it")
        return

    if args.restart:
        for wiki in wikis:
            runtime.repository.set_state(title_cursor_key(wiki), "")
            runtime.repository.set_state(length_cursor_key(wiki), "")

    mediawiki = MediaWikiClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    replica = ReplicaClient(
        runtime.settings.replica_user,
        runtime.settings.replica_password,
        runtime.settings.replica_host_template,
    )
    try:
        total = sum(
            backfill_wiki(runtime, mediawiki, replica, wiki, args.batches) for wiki in wikis
        )
    finally:
        mediawiki.close()
        replica.close()
    LOGGER.info("queued %s backfill pages across %s wikis", total, len(wikis))


if __name__ == "__main__":
    main()
