from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select

from wikipeople.migrate import migrate
from wikipeople.models import (
    ActiveWiki,
    AppState,
    AttributionResult,
    Base,
    ContributorStanding,
    PageOptOut,
    WorkItem,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC).replace(tzinfo=None)

# A block reason exactly like the ones that broke the TSV route: written by an
# administrator, wrapped across lines, and indented with a tab.
AWKWARD_REASON = "Bloqué à sa demande\n\tvoir la discussion\ndurée : indéfinie"


def url(path: Path) -> str:
    return f"sqlite:///{path}"


def populate(path: Path) -> None:
    engine = create_engine(url(path))
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(AttributionResult),
            [
                {
                    "wiki": "frwiki",
                    "page_id": 100 + index,
                    "revision_id": 200 + index,
                    "algorithm_version": "attribution-ladder-v3",
                    "title": f"Exemple {index}",
                    "metric": "surviving-tokens",
                    "contributors": [{"user_id": 1, "username": "Alice", "share": 0.3}],
                    "distinct_contributors": 47,
                    "count_limited": False,
                    "countable_tokens": 900,
                    "wikiwho_revision_id": 200 + index,
                    "computed_at": NOW,
                }
                for index in range(3)
            ],
        )
        connection.execute(
            insert(WorkItem),
            [
                {
                    "wiki": "frwiki",
                    "page_id": 100,
                    "revision_id": 200,
                    "algorithm_version": "attribution-ladder-v3",
                    "state": "done",
                    "available_at": NOW,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            ],
        )
        connection.execute(
            insert(ActiveWiki),
            [{"wiki": "frwiki", "first_seen_at": NOW, "last_result_at": NOW}],
        )
        connection.execute(
            insert(AppState),
            [{"key": "backfill_cursor", "value": "frwiki:100", "updated_at": NOW}],
        )
        connection.execute(
            insert(PageOptOut),
            [
                {
                    "wiki": "frwiki",
                    "page_id": 100,
                    "title": "Exemple",
                    "source": "Projet:WikiPeople/opt-out",
                    "synced_at": NOW,
                }
            ],
        )
        connection.execute(
            insert(ContributorStanding),
            [
                {
                    "wiki": "frwiki",
                    "user_id": 2,
                    "username": "Banni",
                    "blocked_at": NOW - timedelta(days=1),
                    "block_expires_at": None,
                    "block_partial": False,
                    "block_reason": AWKWARD_REASON,
                    "globally_locked": False,
                    "lock_reason": None,
                    "lock_checked_at": NOW,
                    "synced_at": NOW,
                }
            ],
        )
    engine.dispose()


def rows(path: Path, model) -> list:
    engine = create_engine(url(path))
    with engine.connect() as connection:
        result = connection.execute(select(model.__table__)).all()
    engine.dispose()
    return result


def test_every_table_arrives_with_the_same_number_of_rows(tmp_path: Path) -> None:
    """The migration's only claim is that nothing was left behind.

    So the test is the same check the operator makes: for each of the six tables,
    the source count, the number copied and the target count agree. A migration
    that silently skipped a table would still exit zero without this.
    """
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    populate(source)

    outcomes = migrate(url(source), url(target))

    assert {outcome.name for outcome in outcomes} == {
        table.name for table in Base.metadata.sorted_tables
    }
    assert all(outcome.complete for outcome in outcomes)
    assert sum(outcome.copied_rows for outcome in outcomes) == 8


def test_a_block_reason_with_tabs_and_newlines_survives_intact(tmp_path: Path) -> None:
    """The reason this is a script and not a dump.

    Reasons became free text when they were widened to TEXT, and an administrator
    writing one across several lines is ordinary. Through a TSV export that reason
    would come back truncated at the first tab -- and a courtesy phrased late would
    read as a sanction, which is the exact defect the column exists to prevent.
    """
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    populate(source)

    migrate(url(source), url(target))

    assert rows(target, ContributorStanding)[0].block_reason == AWKWARD_REASON


def test_a_target_that_already_holds_rows_is_refused_untouched(tmp_path: Path) -> None:
    """Re-running is the likely mistake, and it is the one that hides itself.

    Four of the six tables have composite primary keys, but `attribution_results`
    and `work_queue` autoincrement, so a second pass would append rather than
    conflict. The counts would then read double and look like success.
    """
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    populate(source)
    migrate(url(source), url(target))

    with pytest.raises(SystemExit) as refusal:
        migrate(url(source), url(target))

    assert "target is not empty" in str(refusal.value)
    assert len(rows(target, AttributionResult)) == 3


def test_force_empty_replaces_the_target_rather_than_appending(tmp_path: Path) -> None:
    """The recovery path from a half-finished run, and it must not accumulate."""
    source, target = tmp_path / "source.db", tmp_path / "target.db"
    populate(source)
    migrate(url(source), url(target))

    outcomes = migrate(url(source), url(target), force_empty=True)

    assert all(outcome.complete for outcome in outcomes)
    assert len(rows(target, AttributionResult)) == 3
