from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SQL_ID = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """Return a timezone-naive UTC value, suitable for MariaDB DATETIME."""
    return datetime.now(UTC).replace(tzinfo=None)


def utctoday() -> date:
    """The current UTC day, so counters do not roll over on a server's local midnight."""
    return datetime.now(UTC).date()


class Base(DeclarativeBase):
    pass


class AttributionResult(Base):
    __tablename__ = "attribution_results"
    __table_args__ = (
        UniqueConstraint(
            "wiki",
            "page_id",
            "revision_id",
            "algorithm_version",
            name="uq_result_revision_algorithm",
        ),
        Index("ix_result_page", "wiki", "page_id", "algorithm_version", "computed_at"),
    )

    id: Mapped[int] = mapped_column(SQL_ID, primary_key=True, autoincrement=True)
    wiki: Mapped[str] = mapped_column(String(32), nullable=False)
    page_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    contributors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    distinct_contributors: Mapped[int] = mapped_column(Integer, nullable=False)
    count_limited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    countable_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    wikiwho_revision_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class WorkItem(Base):
    __tablename__ = "work_queue"
    __table_args__ = (
        UniqueConstraint(
            "wiki",
            "page_id",
            "revision_id",
            "algorithm_version",
            name="uq_work_revision_algorithm",
        ),
        Index("ix_work_claim", "state", "available_at", "priority", "created_at"),
        Index("ix_work_page", "wiki", "page_id", "algorithm_version"),
    )

    id: Mapped[int] = mapped_column(SQL_ID, primary_key=True, autoincrement=True)
    wiki: Mapped[str] = mapped_column(String(32), nullable=False)
    page_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_permanent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class ActiveWiki(Base):
    """A wiki that has produced at least one real result.

    Workers register a wiki here the first time they store a result for it. The
    daily prewarm job reads this table, so a wiki discovered through genuine reader
    demand starts keeping its own top-1000 warm without any configuration change.
    Registration is deliberately driven by a completed calculation rather than by an
    API request, so scripted traffic cannot enrol wikis into scheduled work.
    """

    __tablename__ = "active_wikis"

    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_result_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(191), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class PageOptOut(Base):
    """A page whose contributors are counted but not named.

    Rows are materialised from an on-wiki list by the `optout` job and read on every
    ready response. Storing page IDs rather than titles keeps the serve path to one
    primary-key lookup: it never has to normalise a title, and it is not allowed to ask
    MediaWiki anything. A page also keeps its ID across a rename, so the opt-out follows
    the article rather than the name someone happened to list it under.

    `source` records which list entry put the row here — the page itself, or the
    category it belongs to — so "why is this article opted out?" is answerable without
    re-deriving the whole list.
    """

    __tablename__ = "page_optout"

    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    page_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(512), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ContributorStanding(Base):
    """What each named account's wiki has decided about it, materialised for the serve path.

    Only accounts WikiPeople actually names are tracked. That is a few thousand rows rather
    than the wiki's whole block log, because the top three contributors of popular
    articles are the same prolific editors over and over.

    A row is a fact with a timestamp, not a verdict: the threshold that turns a block
    into a withheld name is applied when the response is built (`is_nameable_account`),
    so an operator can move it, or switch the rule off, without this table changing and
    without a single result being recomputed. ADR-0009 explains why the verdict may not
    be stored here and may not be baked into `attribution_results`.

    `lock_checked_at` is separate from `synced_at` because the two facts cost very
    different amounts to obtain. Block status arrives fifty accounts per request; a
    global lock costs one request per account, so locks are refreshed on a rotation and
    each row remembers when its turn last came.

    `block_reason` and `lock_reason` are stored because neither flag says what the
    sanction means. Both mechanisms serve opposite purposes — a block can be a ban or a
    kindness, a lock can be a ban or a memorial — and only the administrator's wording
    tells them apart. `is_courtesy_block` and `is_sanction_lock` are what read them.

    Both reasons are stored because neither flag says what it means. Stewards use one
    lock mechanism for opposite purposes and a memorial must not read as a ban;
    administrators likewise block abusers and block people at their own request.
    `is_sanction_lock` and `is_courtesy_block` are what tell them apart, and both need
    the text.
    """

    __tablename__ = "contributor_standing"
    __table_args__ = (Index("ix_standing_lock_age", "wiki", "lock_checked_at"),)

    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    block_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    block_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Text rather than a bounded string: administrators write long block reasons, and
    # the whole point of storing them is that a courtesy must not be missed. A cut-off
    # reason would hide a courtesy phrased late and withhold the name — the exact defect
    # this column exists to fix, reintroduced by the storage.
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    globally_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lock_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    lock_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Whether the account has a user page, so a credit line does not offer a link to a
    # page that is not there. Nullable because "nobody has looked yet" and "there is no
    # page" are opposite answers, and only one of them may take a link away. Refreshed
    # for every tracked account on every run, like the block: MediaWiki answers about
    # fifty titles per request, so this fact costs what the block pass already costs.
    has_user_page: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class WikiDisplayPolicy(Base):
    """How much of an attribution one wiki wants shown.

    One row per wiki, materialised from an on-wiki page by the `display` job and read on
    every ready response. Stored as columns rather than as a blob so that the schema is
    the vocabulary: a value the service does not understand cannot be persisted here and
    then quietly mean something later.

    A wiki with no row is a wiki that has said nothing, which is not the same as a wiki
    that has asked for the defaults — but it is served the same way, because the defaults
    are what the service does when nobody has decided. The difference matters only to the
    sync job, which must never write this row from a page it failed to read.
    """

    __tablename__ = "wiki_display_policy"

    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    show_contributor_names: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sanctioned_accounts: Mapped[str] = mapped_column(String(32), nullable=False)
    anonymised_accounts: Mapped[str] = mapped_column(String(32), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PageDemand(Base):
    """Which articles are worth computing next, and nothing about who wanted them.

    Two very different signals share one row because they answer the same question.
    ``views`` comes from the published pageview dumps: how much the world reads this
    article, accumulated day by day so a steady readership outranks a one-day spike.
    ``requests`` comes from this API's own traffic: how often a reader with the gadget
    installed opened the article and waited for an answer. Ranking by requests first
    and views second puts the pages this tool's actual readers open ahead of the pages
    the world reads, which is the difference between a warm cache and a fast one.

    Deliberately a counter and not a log. There is no actor, no address, no timestamp
    per visit and no row per request, so this table can say "someone reads this
    article" and can never say who, from where, or in what order. A reading history is
    not reconstructible from data that was never a sequence, and ``cleanup`` drops rows
    nothing has touched in months so it does not become one by accumulation either.

    ``queued_at`` is what makes the walk exact. A keyset cursor over a ranking that
    changes under it — and this one changes every day — skips rows and repeats others;
    marking each page as it is handed to the queue cannot. It doubles as the re-warm
    clock: clearing it past ``PAGE_FRESHNESS_SECONDS`` puts the page back in line.
    """

    __tablename__ = "page_demand"
    __table_args__ = (Index("ix_demand_rank", "wiki", "queued_at", "requests", "views"),)

    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    page_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class UsageCounter(Base):
    """How many answers each wiki got, per day and per kind of answer.

    The webservice access log is the only record of use today, and it is the wrong
    one twice over: it is retained for days rather than months, and behind Toolforge's
    front proxy every line carries the proxy's address, so it can neither answer "is
    this tool used" over a season nor be asked to. Counting outcomes instead answers
    the operational question — how much of the traffic is served warm, how much waits,
    how much asks for a wiki this deployment does not serve — from four integers a day
    per wiki.

    The primary key is the aggregate itself, so there is nothing to anonymise: a row
    exists before any particular request does, and every request only increments one.
    The wiki of an unserved request is recorded only when it is a real Wikipedia this
    deployment has turned off; anything else counts under ``-``, so a stranger cannot
    make rows by inventing wiki names.
    """

    __tablename__ = "usage_counters"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    wiki: Mapped[str] = mapped_column(String(64), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(32), primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
