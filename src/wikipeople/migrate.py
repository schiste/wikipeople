"""Copy every stored row from one deployment's database into another's.

Written for the WikiFame → WikiPeople rename, where a Toolforge tool cannot be
renamed and the data has to move to a new tool's database.

There is no ``mysqldump`` on the Toolforge bastion, and dumping through ``SELECT``
into TSV is not an option: block and lock reasons are free text and legitimately
contain tabs and newlines. So this reuses what the project already has --
``models.py`` describes the schema, ``create_schema()`` builds it in the target,
and the copy runs through the same SQLAlchemy metadata that wrote the rows.

Both databases are named by environment variable rather than by argument, because
a DSN carries a password and arguments are visible to every user in ``ps``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import Connection, Table, create_engine, delete, func, insert, select

from wikipeople.models import Base
from wikipeople.runtime import configure_logging

LOGGER = logging.getLogger(__name__)

SOURCE_URL_VARIABLE = "MIGRATE_SOURCE_URL"
TARGET_URL_VARIABLE = "MIGRATE_TARGET_URL"

# Large enough that 34,000 rows is a handful of round trips, small enough that a
# row holding a long block reason cannot make the batch unreasonable.
BATCH_ROWS = 500


@dataclass(frozen=True)
class TableOutcome:
    """What one table did, kept so the caller can compare rather than trust."""

    name: str
    source_rows: int
    copied_rows: int
    target_rows: int

    @property
    def complete(self) -> bool:
        return self.source_rows == self.copied_rows == self.target_rows


def _count(connection: Connection, table: Table) -> int:
    return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def _batches(connection: Connection, table: Table) -> Iterator[list[dict[str, object]]]:
    """Stream the table in fixed-size batches rather than loading it whole.

    ``yield_per`` streams server-side, so the tables that are small today stay
    cheap and the one that is not does not have to be special-cased.
    """
    result = connection.execution_options(yield_per=BATCH_ROWS).execute(select(table))
    for partition in result.partitions():
        yield [dict(row._mapping) for row in partition]


def copy_table(source: Connection, target: Connection, table: Table) -> TableOutcome:
    source_rows = _count(source, table)
    copied = 0
    for batch in _batches(source, table):
        target.execute(insert(table), batch)
        copied += len(batch)
        LOGGER.info("%s: %s/%s", table.name, copied, source_rows)
    return TableOutcome(
        name=table.name,
        source_rows=source_rows,
        copied_rows=copied,
        target_rows=_count(target, table),
    )


def occupied_tables(target: Connection) -> list[str]:
    return [table.name for table in Base.metadata.sorted_tables if _count(target, table) > 0]


def migrate(source_url: str, target_url: str, *, force_empty: bool = False) -> list[TableOutcome]:
    """Copy every table, refusing a target that already holds rows.

    Refusing is the whole safety story here. A second run against a populated
    target would duplicate every row with no primary key to stop it, and the
    result would look like a successful migration until someone counted. Recovery
    from a half-finished run is ``--force-empty``, which empties the target first
    and is deliberately a decision the operator has to type.
    """
    source_engine = create_engine(source_url, pool_pre_ping=True)
    target_engine = create_engine(target_url, pool_pre_ping=True)

    Base.metadata.create_all(target_engine)

    with source_engine.connect() as source, target_engine.begin() as target:
        occupied = occupied_tables(target)
        if occupied and not force_empty:
            raise SystemExit(
                "target is not empty: "
                + ", ".join(occupied)
                + " -- pass --force-empty to replace its contents"
            )
        if occupied:
            for table in reversed(Base.metadata.sorted_tables):
                target.execute(delete(table))
            LOGGER.warning("emptied target tables: %s", ", ".join(occupied))

        return [copy_table(source, target, table) for table in Base.metadata.sorted_tables]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Copy every WikiPeople table from the database named by "
            f"${SOURCE_URL_VARIABLE} into the one named by ${TARGET_URL_VARIABLE}."
        )
    )
    parser.add_argument(
        "--force-empty",
        action="store_true",
        help="empty the target's tables first instead of refusing to run",
    )
    args = parser.parse_args()
    configure_logging()

    source_url = os.getenv(SOURCE_URL_VARIABLE)
    target_url = os.getenv(TARGET_URL_VARIABLE)
    if not source_url or not target_url:
        raise SystemExit(f"set both {SOURCE_URL_VARIABLE} and {TARGET_URL_VARIABLE}")

    outcomes = migrate(source_url, target_url, force_empty=args.force_empty)

    for outcome in outcomes:
        LOGGER.info(
            "%-22s source=%-7s copied=%-7s target=%-7s %s",
            outcome.name,
            outcome.source_rows,
            outcome.copied_rows,
            outcome.target_rows,
            "ok" if outcome.complete else "MISMATCH",
        )

    incomplete = [outcome.name for outcome in outcomes if not outcome.complete]
    if incomplete:
        LOGGER.error("counts do not match for: %s", ", ".join(incomplete))
        sys.exit(1)
    LOGGER.info("migrated %s rows", sum(outcome.copied_rows for outcome in outcomes))


if __name__ == "__main__":
    main()
