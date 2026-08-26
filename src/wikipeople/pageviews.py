"""Rank articles by what people actually read, from the published pageview dumps.

The backfill works down from the longest article because that is what the replica can
sort on exactly. It is a decent proxy and a poor answer: three weeks into a
size-ordered walk, frwiki's cache is full of long articles nobody opens, and the
question "who wrote this" is only ever asked about an article someone is reading.

Readership is published, once a day, as ``pageview_complete``: one line per page and
access method, carrying the page id, so a ranking arrives already keyed the way the
queue wants it and no title has to be resolved. The file is bulky — roughly 640 MB
compressed, 55 million lines, two and a half minutes to stream — which is why this is
a daily job that writes a table rather than anything the backfill does inline.

Only the wikis that opted into backfill are read out of it. Serving a wiki on demand
has never implied crawling it, and a ranking of what to crawl is crawling.

The dumps are a public aggregate: they say an article was opened, never by whom, and
this job narrows them further to a page id and a number. Nothing per-reader exists in
the input, so nothing per-reader can come out.
"""

from __future__ import annotations

import argparse
import bz2
import heapq
import logging
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta
from operator import itemgetter
from pathlib import Path

from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)

LAST_DAY_KEY = "demand:pageviews:last-day"
DOMAIN_SUFFIX = ".org"


def dump_path(root: str, day: date) -> Path:
    """Where the Analytics team publishes one day of complete pageviews."""
    return Path(root) / f"{day:%Y}" / f"{day:%Y-%m}" / f"pageviews-{day:%Y%m%d}-user.bz2"


def previous_days(count: int, today: date | None = None) -> list[date]:
    reference = today or datetime.now(UTC).date()
    return [reference - timedelta(days=offset) for offset in range(1, count + 1)]


def find_dump(root: str, lookback_days: int, today: date | None = None) -> tuple[Path, date] | None:
    """The most recent published day, or None when none of the recent ones is there.

    Yesterday's file appears some hours into the day and occasionally later than that,
    so the job looks back rather than failing on the first miss. It takes the newest
    available day and only that one: a run that fell behind catches up one day per run,
    which keeps a single run's cost predictable.
    """
    for day in previous_days(max(1, lookback_days), today):
        path = dump_path(root, day)
        if path.exists():
            return path, day
    return None


def domain_codes(resolver, wikis: list[str]) -> dict[str, str]:
    """Map each wiki's dump domain code back to its database name.

    Derived from the same host rule the API calls use rather than tabulated, so a wiki
    reaching one of them reaches the other: ``frwiki`` -> ``fr.wikipedia.org`` ->
    ``fr.wikipedia``, which is what the dump's first column says.
    """
    codes: dict[str, str] = {}
    for wiki in wikis:
        host = resolver.host(wiki)
        codes[host.removesuffix(DOMAIN_SUFFIX)] = wiki
    return codes


def _lines(path: Path) -> Iterator[bytes]:
    with bz2.open(path, "rb") as stream:
        yield from stream


def read_daily_views(
    path: Path,
    domains: Mapping[str, str],
    minimum_views: int,
    lines: Iterator[bytes] | None = None,
) -> dict[str, dict[int, int]]:
    """Sum one day's views per page, for the requested wikis only.

    ``minimum_views`` is a memory bound, not a filter on merit. A large Wikipedia has
    over a million distinct pages opened in a day, most of them once, and holding that
    dictionary for several wikis at once is more memory than a Toolforge job is given.
    The floor is applied per line, so a page that reaches it only by adding desktop to
    mobile is missed — that page had fewer than ten views in a day, which is not what a
    popularity ranking is for.
    """
    totals: dict[str, dict[int, int]] = {wiki: {} for wiki in domains.values()}
    for raw in lines if lines is not None else _lines(path):
        fields = raw.split(b" ")
        if len(fields) < 5:
            continue
        wiki = domains.get(fields[0].decode("utf-8", "replace"))
        if wiki is None:
            continue
        page_id, views = fields[2], fields[4]
        if not page_id.isdigit() or not views.isdigit():
            continue
        count = int(views)
        if count < minimum_views:
            continue
        bucket = totals[wiki]
        key = int(page_id)
        bucket[key] = bucket.get(key, 0) + count
    return totals


def top_pages(counts: Mapping[int, int], limit: int) -> dict[int, int]:
    if limit <= 0 or len(counts) <= limit:
        return dict(counts)
    return dict(heapq.nlargest(limit, counts.items(), key=itemgetter(1)))


def resolve_target_wikis(runtime: Runtime, explicit: str | None) -> list[str]:
    """Only wikis that opted into backfill: ranking what to crawl is crawling."""
    wikis = [explicit] if explicit else list(runtime.settings.backfill_wikis)
    return sorted({wiki for wiki in wikis if runtime.resolver.is_capable(wiki)})


def collect(runtime: Runtime, wikis: list[str], today: date | None = None) -> int:
    settings = runtime.settings
    found = find_dump(settings.pageview_dump_root, settings.pageview_lookback_days, today)
    if found is None:
        LOGGER.info(
            "no pageview dump published under %s in the last %s days",
            settings.pageview_dump_root,
            settings.pageview_lookback_days,
        )
        return 0

    path, day = found
    last_day = runtime.repository.get_state(LAST_DAY_KEY)
    if last_day and last_day >= day.isoformat():
        LOGGER.info("pageviews for %s already recorded", day)
        return 0

    domains = domain_codes(runtime.resolver, wikis)
    LOGGER.info("reading %s for %s", path, ", ".join(sorted(domains)))
    totals = read_daily_views(path, domains, settings.pageview_minimum_views)

    recorded = 0
    for wiki, counts in sorted(totals.items()):
        ranked = top_pages(counts, settings.demand_top_pages)
        created, updated = runtime.repository.record_page_views(wiki, ranked)
        revived = runtime.repository.requeue_stale_demand(wiki, settings.page_freshness_seconds)
        recorded += len(ranked)
        LOGGER.info(
            "%s: %s pages above %s views, kept %s (new=%s updated=%s), %s back in line",
            wiki,
            len(counts),
            settings.pageview_minimum_views,
            len(ranked),
            created,
            updated,
            revived,
        )
    runtime.repository.set_state(LAST_DAY_KEY, day.isoformat())
    return recorded


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank articles by published pageviews")
    parser.add_argument("--wiki", default=None, help="Override BACKFILL_WIKIS for this run")
    parser.add_argument(
        "--again",
        action="store_true",
        help="Re-read the newest dump even if this day was already recorded",
    )
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = resolve_target_wikis(runtime, args.wiki)
    if not wikis:
        LOGGER.info("no wiki opted into backfill; set BACKFILL_WIKIS to enable it")
        return
    if args.again:
        runtime.repository.set_state(LAST_DAY_KEY, "")
    recorded = collect(runtime, wikis)
    LOGGER.info("recorded demand for %s pages across %s wikis", recorded, len(wikis))


if __name__ == "__main__":
    main()
