from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime, timedelta

from wikipeople.clients import AnalyticsClient, MediaWikiClient
from wikipeople.errors import RetryableUpstreamError, WikiPeopleError
from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)


def previous_days(days: int, today: date | None = None) -> list[date]:
    reference_day = today or datetime.now(UTC).date()
    return [reference_day - timedelta(days=offset) for offset in range(1, days + 1)]


def collect_recent_top_titles(
    analytics: AnalyticsClient,
    wiki: str,
    days: int,
    today: date | None = None,
) -> tuple[set[str], list[date]]:
    target_days = max(1, days)
    lookback_days = max(14, target_days * 3)
    titles: set[str] = set()
    loaded_days: list[date] = []

    for day in previous_days(lookback_days, today):
        articles = analytics.top_pages(wiki, day)
        if articles is None:
            LOGGER.info("pageviews not published yet for %s", day)
            continue
        titles.update(articles)
        loaded_days.append(day)
        if len(loaded_days) == target_days:
            break

    if not loaded_days:
        raise RetryableUpstreamError("Aucun classement Pageviews récent disponible")
    return titles, loaded_days


def resolve_target_wikis(runtime: Runtime, explicit: str | None) -> list[str]:
    """Return the wikis this prewarm run should cover.

    Discovered wikis come first: any wiki a worker has already produced a result for
    keeps its own popular pages warm, so coverage follows real readership instead of
    a hand-maintained list. PREWARM_WIKIS pins additional wikis that should stay warm
    even before anyone has read them.
    """
    if explicit:
        return [explicit]
    wikis = set(runtime.repository.active_wikis()) | set(runtime.settings.prewarm_wikis)
    return sorted(wiki for wiki in wikis if runtime.resolver.is_capable(wiki))


def prewarm_wiki(runtime: Runtime, analytics, mediawiki, wiki: str, days: int) -> int:
    titles, loaded_days = collect_recent_top_titles(analytics, wiki, days)
    pages = mediawiki.resolve_titles(wiki, sorted(titles))
    queued = 0
    for page in pages:
        if page.namespace != 0:
            continue
        if runtime.repository.enqueue_if_stale(
            wiki=wiki,
            page_id=page.page_id,
            revision_id=page.revision_id,
            algorithm_version=runtime.settings.algorithm_version,
            priority=50,
            freshness_seconds=runtime.settings.page_freshness_seconds,
        ):
            queued += 1
    LOGGER.info(
        "%s: queued %s popular pages from %s unique titles across %s available days",
        wiki,
        queued,
        len(titles),
        len(loaded_days),
    )
    return queued


def prewarm_requested(runtime: Runtime, mediawiki, wiki: str) -> int:
    """Re-check the articles readers of this tool actually opened.

    The top-1000 ranking above is what the world reads; this is what the handful of
    people running the gadget read, and the two overlap less than one would hope. A
    reader who opens an article outside the ranking pays the wait themselves, and today
    nothing brings that page back afterwards: it is computed once and left to go stale
    for a quarter while the reader who asked for it is precisely the one likely to
    return to it.

    Cheap by construction. The demand table holds page ids, so this is one Action API
    call per fifty pages, and a page still sitting on the revision its cached answer
    describes is skipped without any work at all.
    """
    settings = runtime.settings
    page_ids = runtime.repository.recently_requested_pages(
        wiki, settings.requested_prewarm_days, settings.requested_prewarm_limit
    )
    if not page_ids:
        return 0

    pages = mediawiki.resolve_page_ids(wiki, page_ids)
    queued = 0
    for page in pages:
        if page.namespace != 0:
            continue
        if runtime.repository.enqueue_if_revision_changed(
            wiki=wiki,
            page_id=page.page_id,
            revision_id=page.revision_id,
            algorithm_version=settings.algorithm_version,
            priority=50,
            minimum_age_seconds=settings.requested_recompute_seconds,
        ):
            queued += 1
    LOGGER.info(
        "%s: queued %s of %s recently requested pages",
        wiki,
        queued,
        len(page_ids),
    )
    return queued


def main() -> None:
    parser = argparse.ArgumentParser(description="Prewarm popular Wikipedia articles")
    parser.add_argument("--wiki", default=None, help="Override the discovered wiki list")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = resolve_target_wikis(runtime, args.wiki)
    if not wikis:
        LOGGER.info("no active or configured wiki to prewarm yet")
        return

    analytics = AnalyticsClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    mediawiki = MediaWikiClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    try:
        total = 0
        for wiki in wikis:
            try:
                total += prewarm_wiki(runtime, analytics, mediawiki, wiki, max(1, args.days))
            except WikiPeopleError as error:
                # One unavailable wiki must not cancel prewarming for the others.
                LOGGER.warning("%s: prewarm skipped (%s)", wiki, error)
            try:
                total += prewarm_requested(runtime, mediawiki, wiki)
            except WikiPeopleError as error:
                # Independently of the ranking above: Pageviews being down is no reason
                # to stop refreshing the pages this tool's own readers opened.
                LOGGER.warning("%s: requested-page refresh skipped (%s)", wiki, error)
        LOGGER.info("queued %s pages across %s wikis", total, len(wikis))
    finally:
        analytics.close()
        mediawiki.close()


if __name__ == "__main__":
    main()
