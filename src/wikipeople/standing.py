"""Materialise what each wiki has decided about the accounts WikiPeople names.

A credit line saying an article was written by someone the community has since banned
is the tool speaking for the wiki and getting it wrong. The fix cannot live where the
result is computed: a block is imposed and lifted without anyone touching the article,
so a filtered stored row would go stale for as long as the result stays fresh — up to
ninety days. So the rule is applied when the response is built, and this job is the
bridge, because the serve path is not allowed to call MediaWiki.

Three facts, two costs. A local block and the existence of a user page both arrive
fifty accounts per request, so both are refreshed for every tracked account on every
run. A CentralAuth lock costs one request per account, so it is refreshed on a rotation
and each row remembers when its turn came.

The user page is here rather than in the gadget for the reason everything else is: the
gadget would have to ask the wiki once per article view, which is the cost the wiki
objected to. One batched question per fifty accounts per hour answers it for everybody.

Only accounts the stored results actually name are tracked — a few thousand, not the
wiki's whole block log, because the top contributors of popular articles are the same
prolific editors over and over. See ADR-0009.
"""

from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from wikipeople.clients import MediaWikiClient
from wikipeople.errors import WikiPeopleError
from wikipeople.models import utcnow
from wikipeople.policy import AccountStanding
from wikipeople.repository import StandingRecord
from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)


def sync_wiki(
    runtime: Runtime,
    mediawiki: MediaWikiClient,
    wiki: str,
    dry_run: bool = False,
) -> tuple[int, int, int, int]:
    """Refresh one wiki's tracked accounts. Returns (tracked, added, removed, locks_checked).

    Raises if the block pass cannot reach MediaWiki, which leaves the stored table
    exactly as it was. Losing contact with a wiki must not read as "every sanction was
    lifted" any more than it reads as "everybody withdrew their opt-out".
    """
    settings = runtime.settings
    now = utcnow()
    user_ids = runtime.repository.named_contributor_ids(wiki)
    known = runtime.repository.standing_records(wiki)

    # Raises on failure, before anything is written.
    blocks = mediawiki.fetch_standing(wiki, user_ids)

    usernames = sorted({block.username for block in blocks.values()})
    try:
        with_user_page: set[str] | None = mediawiki.existing_user_pages(wiki, usernames)
    except WikiPeopleError as error:
        # Losing this pass costs a link, not a name. Every account keeps whatever was
        # last known about its user page rather than the whole run being thrown away
        # over the least consequential of the three facts.
        LOGGER.warning("%s: user page check failed, links unchanged (%s)", wiki, error)
        with_user_page = None

    cutoff = now - timedelta(seconds=settings.standing_lock_recheck_seconds)
    due = set(
        runtime.repository.lock_check_candidates(
            wiki, cutoff, settings.standing_lock_checks_per_run
        )
    )
    # An account nobody has ever tracked has no row yet, so it cannot be in the query
    # above. It is the one most worth asking about, so it jumps the queue.
    unseen = [user_id for user_id in user_ids if user_id not in known]
    due.update(unseen[: max(0, settings.standing_lock_checks_per_run - len(due))])

    records: list[StandingRecord] = []
    locks_checked = 0
    for user_id in user_ids:
        previous = known.get(user_id)
        block = blocks.get(user_id)
        if block is None:
            # MediaWiki did not answer for this ID — a deleted or renamed account. Keep
            # what was known rather than inventing a clean slate for it.
            if previous is not None:
                records.append(previous)
            continue

        locked = previous.standing.globally_locked if previous else False
        lock_reason = previous.standing.lock_reason if previous else None
        lock_checked_at = previous.lock_checked_at if previous else None
        if user_id in due:
            try:
                locked = mediawiki.global_user_info(wiki, block.username).locked
                # Only a locked account costs the second request, and only its reason can
                # tell a ban from a memorial. See is_sanction_lock.
                lock_reason = mediawiki.global_lock_reason(block.username) if locked else None
                lock_checked_at = now
                locks_checked += 1
            except WikiPeopleError as error:
                # One account's lock check failing must not cost the whole run its block
                # pass. Leaving lock_checked_at alone puts it back at the front of the
                # queue next time.
                LOGGER.warning("%s: lock check failed for %s (%s)", wiki, block.username, error)

        if with_user_page is None:
            has_user_page = previous.has_user_page if previous else None
        else:
            has_user_page = block.username.replace("_", " ") in with_user_page

        records.append(
            StandingRecord(
                standing=AccountStanding(
                    user_id=user_id,
                    username=block.username,
                    blocked_at=block.blocked_at,
                    block_expires_at=block.block_expires_at,
                    block_partial=block.block_partial,
                    block_reason=block.block_reason,
                    globally_locked=locked,
                    lock_reason=lock_reason,
                ),
                lock_checked_at=lock_checked_at,
                has_user_page=has_user_page,
            )
        )

    if len(due) >= settings.standing_lock_checks_per_run:
        LOGGER.info(
            "%s: lock checks capped at %s this run; the rest keep their previous status",
            wiki,
            settings.standing_lock_checks_per_run,
        )

    if dry_run:
        LOGGER.info(
            "%s: %s named accounts, %s would have their lock status read",
            wiki,
            len(records),
            len(due),
        )
        return len(records), 0, 0, 0

    added, removed = runtime.repository.replace_standing(wiki, records)
    LOGGER.info(
        "%s: %s named accounts tracked (+%s, -%s), %s lock checks, %s with a user page",
        wiki,
        len(records),
        added,
        removed,
        locks_checked,
        "?" if with_user_page is None else len(with_user_page),
    )
    return len(records), added, removed, locks_checked


def resolve_target_wikis(runtime: Runtime, explicit: str | None) -> list[str]:
    """Wikis worth reading standings for: the ones that have produced a result."""
    if explicit:
        return [explicit]
    return [wiki for wiki in runtime.repository.active_wikis() if runtime.resolver.is_capable(wiki)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh block and lock status for the accounts WikiPeople names"
    )
    parser.add_argument("--wiki", default=None, help="Sync one wiki instead of every active one")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be read without changing what is served",
    )
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = resolve_target_wikis(runtime, args.wiki)
    if not wikis:
        LOGGER.info("no active wiki to read account standings for yet")
        return

    if not runtime.settings.hide_sanctioned_contributors:
        # Still synced. The table is a record of facts, and keeping it current means
        # switching the rule back on takes effect immediately rather than after a run.
        LOGGER.info("HIDE_SANCTIONED_CONTRIBUTORS is off; standings are tracked but not applied")

    mediawiki = MediaWikiClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    try:
        tracked = 0
        for wiki in wikis:
            try:
                tracked += sync_wiki(runtime, mediawiki, wiki, args.dry_run)[0]
            except WikiPeopleError as error:
                # One unreachable wiki keeps its previous standings rather than losing them.
                LOGGER.warning("%s: standing sync skipped, table unchanged (%s)", wiki, error)
        LOGGER.info("%s accounts tracked across %s wikis", tracked, len(wikis))
    finally:
        mediawiki.close()


if __name__ == "__main__":
    main()
