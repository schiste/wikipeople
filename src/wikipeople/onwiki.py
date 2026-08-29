"""Materialise the on-wiki configuration page into rows the API can read.

One page per wiki holds everything a wiki-side decision can change, because there is one
decision-maker: while WikiPeople is a personal script, the person who installs it, the
person who maintains its wording and the person who answers an opt-out request are the
same person, and splitting that across three pages with three formats was three places
for the answer to be wrong.

The gadget fetches the page itself for the options it draws with. The API cannot: it is
not allowed to call MediaWiki while building a response, and a rule the API does not
apply is not a rule — the names would be one direct request away. So this job is the
bridge. It reads the page on a schedule and leaves behind two things the serve path can
read with a primary-key lookup: the page IDs covered by the opt-out list, and one row of
display policy.

Everything here fails towards leaving the stored answer alone. A page that cannot be
fetched, and a page whose JSON does not parse, both mean "we did not learn anything this
run", which is not the same as "the wiki changed its mind" — an empty list is an
instruction and an error is not.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field

from wikipeople.clients import MediaWikiClient
from wikipeople.errors import InvalidConfigPageError, WikiPeopleError
from wikipeople.policy import DISPLAY_POLICY_VALUES, DisplayPolicy, normalize_display_policy
from wikipeople.repository import OptOutEntry
from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)

ARTICLE_NAMESPACE = 0
CATEGORY_NAMESPACE = 14

OPTOUT_KEY = "optOut"


@dataclass(frozen=True)
class WikiConfig:
    """The half of the configuration page the server acts on.

    The gadget's own options — wording, help links, whether the history box appears —
    are on the same page and are none of this job's business. They are read in the
    browser, by the reader, and nothing here would improve by knowing about them.
    """

    display: DisplayPolicy = field(default_factory=DisplayPolicy)
    opt_out: tuple[str, ...] = ()


def parse_config_page(raw: str) -> WikiConfig:
    """Read one configuration page. Raises `InvalidConfigPageError` if it cannot be.

    Strict about the shape of the file and forgiving about its contents, which is the
    opposite way round from the wikitext list this replaces, and deliberately so. JSON
    has one syntax: a file that does not parse is a file being edited, not a file saying
    something, and guessing at it would be guessing at whose name to publish. Within a
    file that does parse, an unknown key and an unusable value both cost nothing.
    """
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise InvalidConfigPageError(f"page is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise InvalidConfigPageError(f"page is a JSON {type(parsed).__name__}, not an object")

    listed = parsed.get(OPTOUT_KEY, [])
    if not isinstance(listed, list):
        # The type is the contract. With it wrong there is no telling an empty list from
        # a broken one, and the two have opposite consequences for whoever is on it.
        raise InvalidConfigPageError(f"{OPTOUT_KEY!r} is a {type(listed).__name__}, not a list")

    targets: list[str] = []
    for entry in listed:
        if not isinstance(entry, str):
            LOGGER.warning("ignored %s entry %r: not a string", OPTOUT_KEY, entry)
            continue
        # A leading colon is how a category is linked rather than joined, and underscores
        # are what a title copied out of a URL carries. Both reach here from habit.
        target = entry.strip().lstrip(":").strip().replace("_", " ")
        if target and target not in targets:
            targets.append(target)

    for key, allowed in DISPLAY_POLICY_VALUES.items():
        stated = parsed.get(key)
        if key in parsed and (not isinstance(stated, str) or stated.strip().lower() not in allowed):
            # Rejecting a value silently is the right behaviour and a miserable thing to
            # debug, so the log says which key did nothing and what it would have taken.
            LOGGER.warning("ignored %r = %r: not one of %s", key, stated, ", ".join(allowed))

    return WikiConfig(display=normalize_display_policy(parsed), opt_out=tuple(targets))


def collect_entries(
    mediawiki: MediaWikiClient,
    wiki: str,
    targets: tuple[str, ...],
    category_limit: int,
) -> tuple[list[OptOutEntry], list[str]]:
    """Turn listed titles into the article IDs they cover, and a list of what was dropped.

    Articles are resolved before categories so that a page listed in its own right keeps
    that as its recorded reason even when a category also covers it.
    """
    entries: dict[int, OptOutEntry] = {}
    skipped: list[str] = []
    infos = mediawiki.classify_titles(wiki, list(targets)) if targets else []

    for info in infos:
        if info.namespace != ARTICLE_NAMESPACE:
            continue
        if info.page_id is None:
            skipped.append(f"{info.title} (page inexistante)")
            continue
        entries[info.page_id] = OptOutEntry(page_id=info.page_id, title=info.title, source="page")

    for info in infos:
        if info.namespace == ARTICLE_NAMESPACE:
            continue
        if info.namespace != CATEGORY_NAMESPACE:
            skipped.append(f"{info.title} (espace de noms {info.namespace}, ignoré)")
            continue
        members, truncated = mediawiki.category_members(wiki, info.title, category_limit)
        if truncated:
            skipped.append(f"{info.title} (plus de {category_limit} articles, liste tronquée)")
        for member in members:
            entries.setdefault(
                member.page_id,
                OptOutEntry(
                    page_id=member.page_id,
                    title=member.title,
                    source=f"category:{info.title}",
                ),
            )

    return list(entries.values()), skipped


@dataclass(frozen=True)
class SyncReport:
    """What one wiki's sync did, for the log line and for the tests."""

    policy: DisplayPolicy
    policy_changed: bool
    covered: int
    added: int
    removed: int


def sync_wiki(
    runtime: Runtime,
    mediawiki: MediaWikiClient,
    wiki: str,
    dry_run: bool = False,
) -> SyncReport:
    """Bring one wiki's stored configuration in line with its on-wiki page.

    Raises if MediaWiki cannot be reached or the page cannot be read, which leaves both
    the stored list and the stored policy exactly as they were.
    """
    settings = runtime.settings
    raw = mediawiki.get_wikitext(wiki, settings.config_page)
    if raw is None:
        # No page at all is a real state, and the only one that means "this wiki has
        # decided nothing": the defaults apply and nobody is opted out. Reachable only
        # because the Action API answered; a failure raised above.
        LOGGER.info("%s: no %s page, defaults apply", wiki, settings.config_page)
        raw = "{}"

    config = parse_config_page(raw)
    entries, skipped = collect_entries(
        mediawiki, wiki, config.opt_out, settings.optout_category_limit
    )
    for note in skipped:
        LOGGER.warning("%s: %s", wiki, note)

    if dry_run:
        LOGGER.info(
            "%s: page states %s and would cover %s articles",
            wiki,
            config.display,
            len(entries),
        )
        return SyncReport(config.display, False, len(entries), 0, 0)

    policy_changed = runtime.repository.save_display_policy(wiki, config.display)
    added, removed = runtime.repository.replace_optout(wiki, entries)
    LOGGER.info(
        "%s: %s listed titles cover %s articles (+%s, -%s), display policy %s",
        wiki,
        len(config.opt_out),
        len(entries),
        added,
        removed,
        f"changed to {config.display}" if policy_changed else "unchanged",
    )
    return SyncReport(config.display, policy_changed, len(entries), added, removed)


def resolve_target_wikis(runtime: Runtime, explicit: str | None) -> list[str]:
    """Wikis worth reading a configuration for: the ones that have produced a result.

    A wiki nobody has been served an attribution for has nothing to configure, and
    reading a page on all seventy WikiWho wikis every quarter of an hour to discover
    that would be traffic spent on nothing.
    """
    if explicit:
        return [explicit]
    return [wiki for wiki in runtime.repository.active_wikis() if runtime.resolver.is_capable(wiki)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the on-wiki WikiPeople configuration")
    parser.add_argument("--wiki", default=None, help="Sync one wiki instead of every active one")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what the page states without changing what is served",
    )
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = resolve_target_wikis(runtime, args.wiki)
    if not wikis:
        LOGGER.info("no active wiki to read a configuration for yet")
        return

    mediawiki = MediaWikiClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    try:
        covered = 0
        for wiki in wikis:
            try:
                covered += sync_wiki(runtime, mediawiki, wiki, args.dry_run).covered
            except WikiPeopleError as error:
                # One unreadable page keeps its wiki's previous answer rather than
                # losing it. Warned per wiki, so a page broken by an edit is visible in
                # the job log while every other wiki carries on.
                LOGGER.warning("%s: config sync skipped, nothing changed (%s)", wiki, error)
        LOGGER.info("%s articles opted out across %s wikis", covered, len(wikis))
    finally:
        mediawiki.close()


if __name__ == "__main__":
    main()
