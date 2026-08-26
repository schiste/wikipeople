"""Read the wiki replicas to decide what the backfill works on next.

``allpages`` walks titles in binary order, which is adversarial for a corpus with a
bot-generated segment: ``(`` sorts before every letter, so three days of backfill on
frwiki produced 86% asteroid designations, the newest of them carrying three
attributable tokens. The generator can filter by size (``gapminsize``) but cannot
order by it, so filtering would mean guessing a threshold per wiki instead of
working down from the top.

The replica orders by size exactly, and the index MediaWiki already maintains --
``page_redirect_namespace_len`` over ``(page_is_redirect, page_namespace, page_len)``
-- is the one this query needs. It also carries ``page_latest``, so a batch arrives
with its revision ids attached and the backfill stops calling the Action API.

Read-only, and never from the request path: the hourly backfill job is the only
caller. Analytics replicas rather than web ones, because this is exactly the long
analytical scan they exist for.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote_plus

from sqlalchemy import Engine, bindparam, create_engine, text

LOGGER = logging.getLogger(__name__)

DEFAULT_HOST_TEMPLATE = "{wiki}.analytics.db.svc.wikimedia.cloud"

_PAGE_COLUMNS = "page_id, page_latest, page_len"
_PAGE_FILTER = "page_namespace = 0 AND page_is_redirect = 0"
_PAGE_ORDER = "ORDER BY page_len DESC, page_id DESC LIMIT :limit"

FIRST_BATCH_SQL = text(f"SELECT {_PAGE_COLUMNS} FROM page WHERE {_PAGE_FILTER} {_PAGE_ORDER}")
BY_ID_SQL = text(
    f"SELECT {_PAGE_COLUMNS} FROM page WHERE {_PAGE_FILTER} AND page_id IN :page_ids"
).bindparams(bindparam("page_ids", expanding=True))
NEXT_BATCH_SQL = text(
    f"SELECT {_PAGE_COLUMNS} FROM page WHERE {_PAGE_FILTER}"
    f" AND (page_len, page_id) < (:length, :page_id) {_PAGE_ORDER}"
)


@dataclass(frozen=True)
class ReplicaPage:
    page_id: int
    revision_id: int
    length: int


@dataclass(frozen=True)
class LengthCursor:
    """Where the descending-size walk stopped.

    A keyset rather than an offset: ``(page_len, page_id)`` is the sort key, so
    resuming stays an index seek however deep the walk has gone. ``page_id`` is
    there only to break ties -- frwiki alone has tens of thousands of pages sharing
    a length, and without it a batch boundary inside a tie would skip or repeat them.
    """

    length: int
    page_id: int

    def encode(self) -> str:
        return f"{self.length}:{self.page_id}"

    @classmethod
    def decode(cls, raw: str | None) -> LengthCursor | None:
        if not raw:
            return None
        length, separator, page_id = raw.partition(":")
        if not separator or not length.isdigit() or not page_id.isdigit():
            LOGGER.warning("ignoring unreadable backfill cursor %r", raw)
            return None
        return cls(length=int(length), page_id=int(page_id))


class ReplicaClient:
    """Per-wiki read-only connections to the Wikimedia replicas."""

    def __init__(
        self,
        user: str,
        password: str,
        host_template: str = DEFAULT_HOST_TEMPLATE,
    ) -> None:
        self.user = user
        self.password = password
        self.host_template = host_template
        self._engines: dict[str, Engine] = {}

    def available(self) -> bool:
        return bool(self.user and self.password)

    def engine(self, wiki: str) -> Engine:
        if wiki not in self._engines:
            host = self.host_template.format(wiki=wiki)
            self._engines[wiki] = create_engine(
                f"mysql+pymysql://{quote_plus(self.user)}:{quote_plus(self.password)}"
                f"@{host}/{quote_plus(wiki)}_p?charset=utf8mb4",
                pool_pre_ping=True,
                pool_recycle=300,
            )
        return self._engines[wiki]

    def pages_by_descending_length(
        self, wiki: str, cursor: LengthCursor | None, limit: int
    ) -> tuple[list[ReplicaPage], LengthCursor | None]:
        """One batch of articles, heaviest first, and where to resume.

        A second return value of ``None`` means the wiki is exhausted, which is only
        true when the batch came back short. A full batch always yields a cursor,
        even if the next call turns out to be empty: guessing "done" from a full
        batch would silently drop the tail.
        """
        statement = FIRST_BATCH_SQL if cursor is None else NEXT_BATCH_SQL
        parameters: dict[str, int] = {"limit": limit}
        if cursor is not None:
            parameters |= {"length": cursor.length, "page_id": cursor.page_id}

        with self.engine(wiki).connect() as connection:
            rows = connection.execute(statement, parameters).all()

        pages = [
            ReplicaPage(page_id=int(row[0]), revision_id=int(row[1]), length=int(row[2]))
            for row in rows
        ]
        if len(pages) < limit:
            return pages, None
        last = pages[-1]
        return pages, LengthCursor(length=last.length, page_id=last.page_id)

    def latest_revisions(self, wiki: str, page_ids: Sequence[int]) -> dict[int, ReplicaPage]:
        """Current revision and size for a set of page ids, keyed by id.

        The demand ranking arrives as page ids and nothing else — the pageview dumps
        carry no revision — so this is the one lookup that turns it into work. Batched
        by primary key, which is the cheapest thing the replica can be asked, and
        filtered the same way the size walk is: a page deleted or turned into a
        redirect since the dump was published simply does not come back, and the caller
        treats its absence as "nothing to compute" rather than as an error.
        """
        if not page_ids:
            return {}
        with self.engine(wiki).connect() as connection:
            rows = connection.execute(BY_ID_SQL, {"page_ids": list(page_ids)}).all()
        return {
            int(row[0]): ReplicaPage(
                page_id=int(row[0]), revision_id=int(row[1]), length=int(row[2])
            )
            for row in rows
        }

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
