from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from wikipeople.db import Database
from wikipeople.models import (
    ActiveWiki,
    AppState,
    AttributionResult,
    ContributorStanding,
    PageDemand,
    PageOptOut,
    UsageCounter,
    WikiDisplayPolicy,
    WorkItem,
    utcnow,
    utctoday,
)
from wikipeople.policy import AccountStanding, DisplayPolicy


@dataclass(frozen=True)
class OptOutEntry:
    """One article the on-wiki list resolves to, with the entry that named it."""

    page_id: int
    title: str
    source: str


@dataclass(frozen=True)
class StandingRecord:
    """One account's standing, plus when its global lock was last confirmed.

    The timestamp is bookkeeping for the refresh rotation rather than part of the fact,
    so it travels beside `AccountStanding` instead of inside it — nothing on the serve
    path has any use for it.
    """

    standing: AccountStanding
    lock_checked_at: datetime | None
    has_user_page: bool | None = None


@dataclass(frozen=True)
class ContributorFacts:
    """Everything the serve path knows about one account, from one query.

    Two unrelated facts travel together because one lookup answers both and the response
    needs both: what the wiki has decided about this account, and whether the user page a
    credit would link to exists. They are kept as separate fields rather than merged into
    `AccountStanding`, which describes decisions; a user page existing is not one.
    """

    standing: AccountStanding
    has_user_page: bool | None = None


@dataclass(frozen=True)
class WorkLease:
    id: int
    wiki: str
    page_id: int
    revision_id: int
    algorithm_version: str
    priority: int
    attempts: int


class Repository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_result(
        self, wiki: str, page_id: int, revision_id: int, algorithm_version: str
    ) -> AttributionResult | None:
        with self.database.session() as session:
            return session.scalar(
                select(AttributionResult).where(
                    AttributionResult.wiki == wiki,
                    AttributionResult.page_id == page_id,
                    AttributionResult.revision_id == revision_id,
                    AttributionResult.algorithm_version == algorithm_version,
                )
            )

    def get_latest_result(
        self, wiki: str, page_id: int, algorithm_version: str
    ) -> AttributionResult | None:
        with self.database.session() as session:
            return session.scalar(
                select(AttributionResult)
                .where(
                    AttributionResult.wiki == wiki,
                    AttributionResult.page_id == page_id,
                    AttributionResult.algorithm_version == algorithm_version,
                )
                .order_by(AttributionResult.computed_at.desc(), AttributionResult.id.desc())
                .limit(1)
            )

    def get_work(
        self, wiki: str, page_id: int, revision_id: int, algorithm_version: str
    ) -> WorkItem | None:
        with self.database.session() as session:
            return session.scalar(
                select(WorkItem).where(
                    WorkItem.wiki == wiki,
                    WorkItem.page_id == page_id,
                    WorkItem.revision_id == revision_id,
                    WorkItem.algorithm_version == algorithm_version,
                )
            )

    def enqueue(
        self,
        wiki: str,
        page_id: int,
        revision_id: int,
        algorithm_version: str,
        priority: int,
        allow_cached_result: bool = False,
    ) -> None:
        now = utcnow()
        if (
            not allow_cached_result
            and self.get_result(wiki, page_id, revision_id, algorithm_version) is not None
        ):
            return
        try:
            with self.database.session() as session, session.begin():
                existing = session.scalar(
                    select(WorkItem).where(
                        WorkItem.wiki == wiki,
                        WorkItem.page_id == page_id,
                        WorkItem.revision_id == revision_id,
                        WorkItem.algorithm_version == algorithm_version,
                    )
                )
                newer_work_exists = session.scalar(
                    select(WorkItem.id)
                    .where(
                        WorkItem.wiki == wiki,
                        WorkItem.page_id == page_id,
                        WorkItem.algorithm_version == algorithm_version,
                        WorkItem.revision_id > revision_id,
                        WorkItem.state.in_(("pending", "leased")),
                    )
                    .limit(1)
                )
                if existing is None and newer_work_exists is not None:
                    return
                if existing is None:
                    session.add(
                        WorkItem(
                            wiki=wiki,
                            page_id=page_id,
                            revision_id=revision_id,
                            algorithm_version=algorithm_version,
                            priority=priority,
                            available_at=now,
                        )
                    )
                elif existing.state not in {"dead", "superseded"}:
                    existing.priority = max(existing.priority, priority)
                    existing.updated_at = now

                session.execute(
                    update(WorkItem)
                    .where(
                        WorkItem.wiki == wiki,
                        WorkItem.page_id == page_id,
                        WorkItem.algorithm_version == algorithm_version,
                        WorkItem.revision_id < revision_id,
                        WorkItem.state == "pending",
                    )
                    .values(state="superseded", updated_at=now)
                )
        except IntegrityError:
            # Two simultaneous cache misses may race to insert the same unique job.
            with self.database.session() as session, session.begin():
                session.execute(
                    update(WorkItem)
                    .where(
                        WorkItem.wiki == wiki,
                        WorkItem.page_id == page_id,
                        WorkItem.revision_id == revision_id,
                        WorkItem.algorithm_version == algorithm_version,
                    )
                    .values(priority=priority, updated_at=now)
                )

    def enqueue_if_stale(
        self,
        wiki: str,
        page_id: int,
        revision_id: int,
        algorithm_version: str,
        priority: int,
        freshness_seconds: int,
    ) -> bool:
        latest = self.get_latest_result(wiki, page_id, algorithm_version)
        cutoff = utcnow() - timedelta(seconds=freshness_seconds)
        if latest is not None and latest.computed_at >= cutoff:
            return False

        self.enqueue(
            wiki=wiki,
            page_id=page_id,
            revision_id=revision_id,
            algorithm_version=algorithm_version,
            priority=priority,
            allow_cached_result=True,
        )
        work = self.get_work(wiki, page_id, revision_id, algorithm_version)
        return work is not None and work.state in {"pending", "leased"}

    def enqueue_if_revision_changed(
        self,
        wiki: str,
        page_id: int,
        revision_id: int,
        algorithm_version: str,
        priority: int,
        minimum_age_seconds: int,
    ) -> bool:
        """Queue a page whose text has moved on since the cached answer was computed.

        Different from ``enqueue_if_stale`` in what it treats as the reason to redo the
        work. Staleness is a clock: past the freshness window every page is redone,
        whether or not anyone edited it. This is an observation: the article is not on
        the revision the answer describes, so the answer is about text that is no longer
        there. Cheap enough to apply to the handful of pages readers actually opened,
        and worth far more on those than on any page picked by a timer.

        ``minimum_age_seconds`` is the floor under it. A heavily edited article changes
        revision several times a day, and recomputing it each time would spend the whole
        WikiWho budget on one page; past that age it gets one recomputation, not one per
        edit.
        """
        latest = self.get_latest_result(wiki, page_id, algorithm_version)
        if latest is not None:
            if latest.revision_id == revision_id:
                return False
            if latest.computed_at >= utcnow() - timedelta(seconds=minimum_age_seconds):
                return False

        self.enqueue(
            wiki=wiki,
            page_id=page_id,
            revision_id=revision_id,
            algorithm_version=algorithm_version,
            priority=priority,
            allow_cached_result=True,
        )
        work = self.get_work(wiki, page_id, revision_id, algorithm_version)
        return work is not None and work.state in {"pending", "leased"}

    def claim(self, worker_id: str, lease_seconds: int) -> WorkLease | None:
        now = utcnow()
        with self.database.session() as session, session.begin():
            statement = (
                select(WorkItem)
                .where(
                    or_(
                        and_(WorkItem.state == "pending", WorkItem.available_at <= now),
                        and_(WorkItem.state == "leased", WorkItem.lease_until < now),
                    )
                )
                .order_by(WorkItem.priority.desc(), WorkItem.created_at.asc())
                .limit(1)
            )
            if self.database.engine.dialect.name != "sqlite":
                statement = statement.with_for_update(skip_locked=True)
            item = session.scalar(statement)
            if item is None:
                return None
            item.state = "leased"
            item.worker_id = worker_id
            item.lease_until = now + timedelta(seconds=lease_seconds)
            item.attempts += 1
            item.updated_at = now
            return WorkLease(
                id=item.id,
                wiki=item.wiki,
                page_id=item.page_id,
                revision_id=item.revision_id,
                algorithm_version=item.algorithm_version,
                priority=item.priority,
                attempts=item.attempts,
            )

    def save_result(self, values: dict[str, Any]) -> None:
        with self.database.session() as session, session.begin():
            existing = session.scalar(
                select(AttributionResult).where(
                    AttributionResult.wiki == values["wiki"],
                    AttributionResult.page_id == values["page_id"],
                    AttributionResult.revision_id == values["revision_id"],
                    AttributionResult.algorithm_version == values["algorithm_version"],
                )
            )
            if existing is None:
                session.add(AttributionResult(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)

    def complete(self, work_id: int) -> None:
        with self.database.session() as session, session.begin():
            session.execute(delete(WorkItem).where(WorkItem.id == work_id))

    def supersede(self, work_id: int, reason: str) -> None:
        with self.database.session() as session, session.begin():
            session.execute(
                update(WorkItem)
                .where(WorkItem.id == work_id)
                .values(
                    state="superseded",
                    lease_until=None,
                    worker_id=None,
                    error_code="revision_superseded",
                    last_error=reason[:2000],
                    updated_at=utcnow(),
                )
            )

    def fail(
        self,
        lease: WorkLease,
        code: str,
        message: str,
        max_attempts: int,
        permanent: bool = False,
    ) -> None:
        is_dead = permanent or lease.attempts >= max_attempts
        delay_seconds = min(6 * 60 * 60, 30 * (2 ** max(0, lease.attempts - 1)))
        with self.database.session() as session, session.begin():
            session.execute(
                update(WorkItem)
                .where(WorkItem.id == lease.id)
                .values(
                    state="dead" if is_dead else "pending",
                    available_at=utcnow() + timedelta(seconds=delay_seconds),
                    lease_until=None,
                    worker_id=None,
                    error_code=code[:64],
                    last_error=message[:2000],
                    is_permanent=permanent,
                    updated_at=utcnow(),
                )
            )

    def revive(self, work_id: int, priority: int) -> None:
        with self.database.session() as session, session.begin():
            session.execute(
                update(WorkItem)
                .where(WorkItem.id == work_id, WorkItem.state == "dead")
                .values(
                    state="pending",
                    attempts=0,
                    priority=priority,
                    available_at=utcnow(),
                    error_code=None,
                    last_error=None,
                    updated_at=utcnow(),
                )
            )

    def register_active_wiki(self, wiki: str) -> bool:
        """Record that a wiki has produced a result. Returns True on first sighting.

        A unique primary key makes concurrent workers safe: the loser of the race
        simply refreshes the timestamp.
        """
        now = utcnow()
        try:
            with self.database.session() as session, session.begin():
                existing = session.get(ActiveWiki, wiki)
                if existing is not None:
                    existing.last_result_at = now
                    return False
                session.add(ActiveWiki(wiki=wiki, first_seen_at=now, last_result_at=now))
                return True
        except IntegrityError:
            with self.database.session() as session, session.begin():
                session.execute(
                    update(ActiveWiki).where(ActiveWiki.wiki == wiki).values(last_result_at=now)
                )
            return False

    def active_wikis(self) -> list[str]:
        with self.database.session() as session:
            return sorted(session.scalars(select(ActiveWiki.wiki)).all())

    def is_opted_out(self, wiki: str, page_id: int) -> bool:
        """Whether this page's contributors may be counted but not named.

        On the serve path for every ready response, so it is a primary-key lookup and
        nothing more. A page absent from the table is the overwhelmingly common case and
        costs one index probe.
        """
        with self.database.session() as session:
            return session.get(PageOptOut, (wiki, page_id)) is not None

    def replace_optout(self, wiki: str, entries: Sequence[OptOutEntry]) -> tuple[int, int]:
        """Make the stored list for one wiki match `entries` exactly. Returns (added, removed).

        Only ever called after the on-wiki page has actually been read. An empty list is
        a legitimate instruction — it means nobody is opted out any more — so a failed
        read must raise long before it reaches here rather than arrive as an empty list
        and quietly un-opt-out a whole wiki.
        """
        now = utcnow()
        wanted = {entry.page_id: entry for entry in entries}
        with self.database.session() as session, session.begin():
            existing = {
                row.page_id: row
                for row in session.scalars(select(PageOptOut).where(PageOptOut.wiki == wiki))
            }
            removed = sorted(set(existing) - set(wanted))
            if removed:
                session.execute(
                    delete(PageOptOut).where(
                        PageOptOut.wiki == wiki, PageOptOut.page_id.in_(removed)
                    )
                )
            added = 0
            for page_id, entry in wanted.items():
                row = existing.get(page_id)
                if row is None:
                    session.add(
                        PageOptOut(
                            wiki=wiki,
                            page_id=page_id,
                            title=entry.title,
                            source=entry.source,
                            synced_at=now,
                        )
                    )
                    added += 1
                else:
                    row.title = entry.title
                    row.source = entry.source
                    row.synced_at = now
        return added, len(removed)

    def contributor_facts(self, wiki: str, user_ids: Sequence[int]) -> dict[int, ContributorFacts]:
        """Return what is known about these accounts. On the serve path, so it stays one query.

        Called with at most three IDs — the accounts a ready response would name — and
        skipped entirely when there are none. An ID missing from the answer means nobody
        has looked at that account yet, which `contributor_display` treats as an ordinary
        linked name rather than as a finding.
        """
        if not user_ids:
            return {}
        with self.database.session() as session:
            rows = session.scalars(
                select(ContributorStanding).where(
                    ContributorStanding.wiki == wiki,
                    ContributorStanding.user_id.in_(tuple(user_ids)),
                )
            ).all()
        return {
            row.user_id: ContributorFacts(
                standing=AccountStanding(
                    user_id=row.user_id,
                    username=row.username,
                    blocked_at=row.blocked_at,
                    block_expires_at=row.block_expires_at,
                    block_partial=row.block_partial,
                    block_reason=row.block_reason,
                    globally_locked=row.globally_locked,
                    lock_reason=row.lock_reason,
                ),
                has_user_page=row.has_user_page,
            )
            for row in rows
        }

    def display_policy(self, wiki: str) -> DisplayPolicy:
        """What this wiki has asked to be shown, or the defaults if it has asked nothing.

        On the serve path for every ready response, so it is a primary-key lookup and
        nothing more — the same shape as `is_opted_out`, and absent for the same reason
        in the overwhelmingly common case.
        """
        with self.database.session() as session:
            row = session.get(WikiDisplayPolicy, wiki)
        if row is None:
            return DisplayPolicy()
        return DisplayPolicy(
            show_contributor_names=row.show_contributor_names,
            sanctioned_accounts=row.sanctioned_accounts,
            anonymised_accounts=row.anonymised_accounts,
        )

    def save_display_policy(self, wiki: str, policy: DisplayPolicy) -> bool:
        """Store one wiki's policy. Returns whether it differs from what was stored.

        Only ever called after the page has actually been read. A failed read must raise
        long before it reaches here rather than arrive as a defaults object and quietly
        undo whatever the wiki had decided — the same rule the opt-out list runs on.
        """
        with self.database.session() as session, session.begin():
            row = session.get(WikiDisplayPolicy, wiki)
            changed = row is None or (
                row.show_contributor_names,
                row.sanctioned_accounts,
                row.anonymised_accounts,
            ) != (
                policy.show_contributor_names,
                policy.sanctioned_accounts,
                policy.anonymised_accounts,
            )
            if row is None:
                row = WikiDisplayPolicy(wiki=wiki)
                session.add(row)
            row.show_contributor_names = policy.show_contributor_names
            row.sanctioned_accounts = policy.sanctioned_accounts
            row.anonymised_accounts = policy.anonymised_accounts
            row.synced_at = utcnow()
        return changed

    def standing_records(self, wiki: str) -> dict[int, StandingRecord]:
        """Everything stored about one wiki's tracked accounts, for the sync job.

        Separate from `contributor_facts` because the two callers want different things. The
        serve path wants three accounts and no bookkeeping; the job wants the whole wiki
        including `lock_checked_at`, which is how it knows whose turn it is.
        """
        with self.database.session() as session:
            rows = session.scalars(
                select(ContributorStanding).where(ContributorStanding.wiki == wiki)
            ).all()
        return {
            row.user_id: StandingRecord(
                standing=AccountStanding(
                    user_id=row.user_id,
                    username=row.username,
                    blocked_at=row.blocked_at,
                    block_expires_at=row.block_expires_at,
                    block_partial=row.block_partial,
                    block_reason=row.block_reason,
                    globally_locked=row.globally_locked,
                    lock_reason=row.lock_reason,
                ),
                lock_checked_at=row.lock_checked_at,
                has_user_page=row.has_user_page,
            )
            for row in rows
        }

    def lock_check_candidates(self, wiki: str, cutoff: datetime, limit: int) -> list[int]:
        """User IDs whose global lock status is stale or was never read, oldest first.

        Never-checked accounts sort first. That matters more than it looks: an account
        WikiPeople has only just started naming is the one whose status nobody here has
        ever confirmed, so it goes ahead of one that was confirmed yesterday.
        """
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ContributorStanding.user_id)
                    .where(
                        ContributorStanding.wiki == wiki,
                        or_(
                            ContributorStanding.lock_checked_at.is_(None),
                            ContributorStanding.lock_checked_at < cutoff,
                        ),
                    )
                    .order_by(
                        ContributorStanding.lock_checked_at.is_(None).desc(),
                        ContributorStanding.lock_checked_at.asc(),
                    )
                    .limit(limit)
                ).all()
            )

    def named_contributor_ids(self, wiki: str) -> list[int]:
        """Every account this wiki's stored results would name, deduplicated.

        Read in Python rather than with a JSON query because the two supported engines
        spell that differently, and this runs in a job, once an hour, over one column.
        The set is small — a few thousand accounts for tens of thousands of articles,
        because the top contributors of popular pages are the same people repeatedly.
        """
        seen: set[int] = set()
        with self.database.session() as session:
            rows = session.scalars(
                select(AttributionResult.contributors).where(AttributionResult.wiki == wiki)
            )
            for contributors in rows:
                for contributor in contributors or ():
                    user_id = contributor.get("user_id")
                    if isinstance(user_id, int) and user_id > 0:
                        seen.add(user_id)
        return sorted(seen)

    def replace_standing(self, wiki: str, records: Sequence[StandingRecord]) -> tuple[int, int]:
        """Make one wiki's tracked accounts match `records` exactly. Returns (added, removed).

        A full replacement, because MediaWiki reports only active blocks: an account
        whose block lapsed comes back with nothing said about it, and merging would keep
        the stale row for ever. Removal also covers an account no stored result names any
        more, so the table tracks what is displayed rather than growing without bound.
        """
        now = utcnow()
        wanted = {record.standing.user_id: record for record in records}
        with self.database.session() as session, session.begin():
            existing = {
                row.user_id: row
                for row in session.scalars(
                    select(ContributorStanding).where(ContributorStanding.wiki == wiki)
                )
            }
            removed = sorted(set(existing) - set(wanted))
            if removed:
                session.execute(
                    delete(ContributorStanding).where(
                        ContributorStanding.wiki == wiki,
                        ContributorStanding.user_id.in_(removed),
                    )
                )
            added = 0
            for user_id, record in wanted.items():
                row = existing.get(user_id)
                if row is None:
                    row = ContributorStanding(wiki=wiki, user_id=user_id)
                    session.add(row)
                    added += 1
                standing = record.standing
                row.username = standing.username
                row.blocked_at = standing.blocked_at
                row.block_expires_at = standing.block_expires_at
                row.block_partial = standing.block_partial
                row.block_reason = standing.block_reason
                row.globally_locked = standing.globally_locked
                row.lock_reason = standing.lock_reason
                row.lock_checked_at = record.lock_checked_at
                row.has_user_page = record.has_user_page
                row.synced_at = now
        return added, len(removed)

    def standing_counts(self) -> dict[str, dict[str, int]]:
        """Per wiki: how many accounts are tracked, and how many carry a block or a lock.

        Deliberately not "how many are hidden". That depends on the threshold, which is
        applied per response; reporting it here would put a second copy of the rule
        somewhere it could drift from the first.
        """
        with self.database.session() as session:
            rows = session.execute(
                select(
                    ContributorStanding.wiki,
                    func.count(),
                    func.sum(case((ContributorStanding.blocked_at.is_not(None), 1), else_=0)),
                    func.sum(case((ContributorStanding.globally_locked, 1), else_=0)),
                ).group_by(ContributorStanding.wiki)
            ).all()
        return {
            str(wiki): {
                "tracked": int(tracked),
                "blocked": int(blocked or 0),
                "locked": int(locked or 0),
            }
            for wiki, tracked, blocked, locked in rows
        }

    def optout_counts(self) -> dict[str, int]:
        with self.database.session() as session:
            rows = session.execute(
                select(PageOptOut.wiki, func.count()).group_by(PageOptOut.wiki)
            ).all()
            return {str(wiki): int(count) for wiki, count in rows}

    def record_page_request(self, wiki: str, page_id: int) -> None:
        """Count that a reader asked about this page, without recording that it was a reader.

        An increment rather than an insert, so the table holds one row per article
        however many times it is read and no row can be read as an event. The update
        runs first and the insert only when nothing was there, which is also what keeps
        two web workers from losing each other's count: the arithmetic happens in the
        database rather than in a value one of them read a moment earlier.
        """
        now = utcnow()
        with self.database.session() as session, session.begin():
            updated = session.execute(
                update(PageDemand)
                .where(PageDemand.wiki == wiki, PageDemand.page_id == page_id)
                .values(requests=PageDemand.requests + 1, last_requested_at=now)
            ).rowcount
            if updated:
                return
        try:
            with self.database.session() as session, session.begin():
                session.add(
                    PageDemand(
                        wiki=wiki,
                        page_id=page_id,
                        views=0,
                        requests=1,
                        last_requested_at=now,
                        created_at=now,
                    )
                )
        except IntegrityError:
            with self.database.session() as session, session.begin():
                session.execute(
                    update(PageDemand)
                    .where(PageDemand.wiki == wiki, PageDemand.page_id == page_id)
                    .values(requests=PageDemand.requests + 1, last_requested_at=now)
                )

    def record_page_views(self, wiki: str, counts: Mapping[int, int]) -> tuple[int, int]:
        """Add a day of published pageviews to the demand table. Returns (new, updated).

        Read-then-write in batches rather than one statement per page, because a day of
        one large Wikipedia is tens of thousands of pages and a round trip each would
        cost more than reading the dump did. Safe as a read-modify-write only because
        the daily job is the sole writer of this column; the request path increments a
        different one, in the database, and never collides with it.
        """
        now = utcnow()
        page_ids = sorted(counts)
        created = 0
        updated = 0
        for start in range(0, len(page_ids), 500):
            chunk = page_ids[start : start + 500]
            with self.database.session() as session, session.begin():
                known = {
                    int(row[0]): int(row[1])
                    for row in session.execute(
                        select(PageDemand.page_id, PageDemand.views).where(
                            PageDemand.wiki == wiki, PageDemand.page_id.in_(chunk)
                        )
                    ).all()
                }
                changes = [
                    {
                        "wiki": wiki,
                        "page_id": page_id,
                        "views": known[page_id] + counts[page_id],
                        "last_viewed_at": now,
                    }
                    for page_id in chunk
                    if page_id in known
                ]
                additions = [
                    {
                        "wiki": wiki,
                        "page_id": page_id,
                        "views": counts[page_id],
                        "requests": 0,
                        "last_viewed_at": now,
                        "created_at": now,
                    }
                    for page_id in chunk
                    if page_id not in known
                ]
                if changes:
                    session.execute(update(PageDemand), changes)
                    updated += len(changes)
                if additions:
                    session.execute(insert(PageDemand), additions)
                    created += len(additions)
        return created, updated

    def pending_demand(self, wiki: str, limit: int) -> list[int]:
        """The most-wanted articles this wiki has not handed to the queue yet.

        Requests outrank views because they are a different claim: a view says the
        world reads this article, a request says someone reading it right now had the
        gadget installed and waited. There are far fewer of the second, and they are
        the ones a warm cache is actually for.
        """
        with self.database.session() as session:
            return [
                int(page_id)
                for page_id in session.scalars(
                    select(PageDemand.page_id)
                    .where(PageDemand.wiki == wiki, PageDemand.queued_at.is_(None))
                    .order_by(
                        PageDemand.requests.desc(),
                        PageDemand.views.desc(),
                        PageDemand.page_id.desc(),
                    )
                    .limit(limit)
                ).all()
            ]

    def mark_demand_queued(self, wiki: str, page_ids: Sequence[int]) -> int:
        """Take these pages out of the ranking, whether or not they reached the queue.

        Marked after the enqueue rather than before, so a run that dies against the
        replica retries the same pages next hour instead of dropping them. Marked even
        for a page the replica did not return — a deleted article or one turned into a
        redirect would otherwise come back at the top of the ranking for ever.
        """
        if not page_ids:
            return 0
        with self.database.session() as session, session.begin():
            return int(
                session.execute(
                    update(PageDemand)
                    .where(PageDemand.wiki == wiki, PageDemand.page_id.in_(list(page_ids)))
                    .values(queued_at=utcnow())
                ).rowcount
                or 0
            )

    def requeue_stale_demand(self, wiki: str, older_than_seconds: int) -> int:
        """Put pages queued long enough ago back in line, newest demand first.

        The queue itself refuses to redo fresh work, so this is not what decides
        whether a page is recomputed — ``enqueue_if_stale`` is. It only decides when a
        page becomes eligible to be considered again, which is why the same window is
        the right one: past it, the page is worth a look, and its accumulated views and
        requests decide whether it is worth more than the pages ahead of it.
        """
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        with self.database.session() as session, session.begin():
            return int(
                session.execute(
                    update(PageDemand)
                    .where(
                        PageDemand.wiki == wiki,
                        PageDemand.queued_at.is_not(None),
                        PageDemand.queued_at < cutoff,
                    )
                    .values(queued_at=None)
                ).rowcount
                or 0
            )

    def recently_requested_pages(self, wiki: str, since_days: int, limit: int) -> list[int]:
        """Articles readers actually asked about lately, most recent first."""
        cutoff = utcnow() - timedelta(days=since_days)
        with self.database.session() as session:
            return [
                int(page_id)
                for page_id in session.scalars(
                    select(PageDemand.page_id)
                    .where(
                        PageDemand.wiki == wiki,
                        PageDemand.requests > 0,
                        PageDemand.last_requested_at.is_not(None),
                        PageDemand.last_requested_at >= cutoff,
                    )
                    .order_by(
                        PageDemand.last_requested_at.desc(),
                        PageDemand.requests.desc(),
                    )
                    .limit(limit)
                ).all()
            ]

    def demand_counts(self) -> dict[str, dict[str, int]]:
        with self.database.session() as session:
            rows = session.execute(
                select(
                    PageDemand.wiki,
                    func.count(),
                    func.sum(case((PageDemand.queued_at.is_(None), 1), else_=0)),
                    func.sum(case((PageDemand.requests > 0, 1), else_=0)),
                ).group_by(PageDemand.wiki)
            ).all()
        return {
            str(wiki): {
                "pages": int(total or 0),
                "waiting": int(waiting or 0),
                "requested": int(requested or 0),
            }
            for wiki, total, waiting, requested in rows
        }

    def count_request(self, wiki: str, outcome: str, day: date | None = None) -> None:
        """Add one to today's counter for this wiki and outcome.

        Incremented in the database for the same reason the page counter is: several
        web workers share the row, and a value read a moment ago is not a count.
        """
        today = day or utctoday()
        with self.database.session() as session, session.begin():
            updated = session.execute(
                update(UsageCounter)
                .where(
                    UsageCounter.day == today,
                    UsageCounter.wiki == wiki,
                    UsageCounter.outcome == outcome,
                )
                .values(requests=UsageCounter.requests + 1, updated_at=utcnow())
            ).rowcount
            if updated:
                return
        try:
            with self.database.session() as session, session.begin():
                session.add(UsageCounter(day=today, wiki=wiki, outcome=outcome, requests=1))
        except IntegrityError:
            with self.database.session() as session, session.begin():
                session.execute(
                    update(UsageCounter)
                    .where(
                        UsageCounter.day == today,
                        UsageCounter.wiki == wiki,
                        UsageCounter.outcome == outcome,
                    )
                    .values(requests=UsageCounter.requests + 1, updated_at=utcnow())
                )

    def usage_since(self, days: int) -> dict[str, dict[str, dict[str, int]]]:
        """Daily counters for the last ``days`` days, as day -> wiki -> outcome -> count."""
        cutoff = utctoday() - timedelta(days=max(0, days - 1))
        with self.database.session() as session:
            rows = session.execute(
                select(
                    UsageCounter.day,
                    UsageCounter.wiki,
                    UsageCounter.outcome,
                    UsageCounter.requests,
                )
                .where(UsageCounter.day >= cutoff)
                .order_by(UsageCounter.day.desc())
            ).all()
        usage: dict[str, dict[str, dict[str, int]]] = {}
        for day, wiki, outcome, requests in rows:
            usage.setdefault(day.isoformat(), {}).setdefault(str(wiki), {})[str(outcome)] = int(
                requests
            )
        return usage

    def results_with_metric(
        self,
        algorithm_version: str,
        metric: str,
        computed_before: datetime,
        limit: int,
        after_id: int = 0,
    ) -> list[AttributionResult]:
        """Cached answers that settled for a weaker metric, oldest row first.

        Ordered by primary key rather than by date so a job can sweep the whole table
        with a cursor that cannot repeat or skip. The date filter is a floor, not the
        order: it is what stops a page recomputed this morning from being tried again
        this afternoon, and what makes the sweep self-limiting — a recompute that fails
        the same way refreshes ``computed_at`` and drops out until the window passes.
        """
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(AttributionResult)
                    .where(
                        AttributionResult.algorithm_version == algorithm_version,
                        AttributionResult.metric == metric,
                        AttributionResult.computed_at < computed_before,
                        AttributionResult.id > after_id,
                    )
                    .order_by(AttributionResult.id)
                    .limit(limit)
                ).all()
            )

    def metric_counts(self) -> dict[str, dict[str, int]]:
        with self.database.session() as session:
            rows = session.execute(
                select(AttributionResult.wiki, AttributionResult.metric, func.count()).group_by(
                    AttributionResult.wiki, AttributionResult.metric
                )
            ).all()
        counts: dict[str, dict[str, int]] = {}
        for wiki, metric, total in rows:
            counts.setdefault(str(wiki), {})[str(metric)] = int(total)
        return counts

    def get_state(self, key: str) -> str | None:
        with self.database.session() as session:
            state = session.get(AppState, key)
            return state.value if state else None

    def set_state(self, key: str, value: str) -> None:
        with self.database.session() as session, session.begin():
            state = session.get(AppState, key)
            if state is None:
                session.add(AppState(key=key, value=value))
            else:
                state.value = value
                state.updated_at = utcnow()

    def stats(self) -> dict[str, int]:
        with self.database.session() as session:
            ready = session.scalar(select(func.count()).select_from(AttributionResult)) or 0
            rows = session.execute(
                select(WorkItem.state, func.count()).group_by(WorkItem.state)
            ).all()
            return {"ready": int(ready), **{str(state): int(count) for state, count in rows}}

    def cleanup(
        self,
        queue_days: int = 30,
        superseded_result_days: int = 30,
        demand_days: int = 180,
        usage_days: int = 365,
    ) -> dict[str, int]:
        queue_cutoff = utcnow() - timedelta(days=queue_days)
        result_cutoff = utcnow() - timedelta(days=superseded_result_days)
        # A page nobody has viewed or asked for in half a year is not a ranking any
        # more, and dropping it is also what keeps the table from turning into the
        # reading history it is built not to be.
        demand_cutoff = utcnow() - timedelta(days=demand_days)
        usage_cutoff = utctoday() - timedelta(days=usage_days)
        with self.database.session() as session, session.begin():
            removed_queue = session.execute(
                delete(WorkItem).where(
                    WorkItem.state.in_(("dead", "superseded")),
                    WorkItem.updated_at < queue_cutoff,
                )
            ).rowcount

            newer = aliased(AttributionResult)
            obsolete_ids = session.scalars(
                select(AttributionResult.id)
                .join(
                    newer,
                    and_(
                        newer.wiki == AttributionResult.wiki,
                        newer.page_id == AttributionResult.page_id,
                        newer.algorithm_version == AttributionResult.algorithm_version,
                        newer.computed_at > AttributionResult.computed_at,
                    ),
                )
                .where(AttributionResult.computed_at < result_cutoff)
                .distinct()
                .limit(10_000)
            ).all()
            removed_results = 0
            if obsolete_ids:
                removed_results = session.execute(
                    delete(AttributionResult).where(AttributionResult.id.in_(obsolete_ids))
                ).rowcount
            removed_demand = session.execute(
                delete(PageDemand).where(
                    or_(
                        PageDemand.last_viewed_at.is_(None),
                        PageDemand.last_viewed_at < demand_cutoff,
                    ),
                    or_(
                        PageDemand.last_requested_at.is_(None),
                        PageDemand.last_requested_at < demand_cutoff,
                    ),
                    PageDemand.created_at < demand_cutoff,
                )
            ).rowcount

            removed_usage = session.execute(
                delete(UsageCounter).where(UsageCounter.day < usage_cutoff)
            ).rowcount
        return {
            "queue": int(removed_queue or 0),
            "results": int(removed_results or 0),
            "demand": int(removed_demand or 0),
            "usage": int(removed_usage or 0),
        }
