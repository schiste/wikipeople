from datetime import date, datetime, timedelta
from pathlib import Path

from wikipeople.db import Database
from wikipeople.models import utcnow
from wikipeople.repository import Repository


def make_repository(tmp_path: Path) -> Repository:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_schema()
    return Repository(database)


def test_enqueue_deduplicates_and_boosts_priority(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    repository.enqueue("frwiki", 10, 20, "v1", priority=10)
    repository.enqueue("frwiki", 10, 20, "v1", priority=100)

    assert repository.stats()["pending"] == 1
    work = repository.get_work("frwiki", 10, 20, "v1")
    assert work is not None
    assert work.priority == 100


def test_new_revision_supersedes_old_pending_work(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    repository.enqueue("frwiki", 10, 20, "v1", priority=10)
    repository.enqueue("frwiki", 10, 21, "v1", priority=10)

    old = repository.get_work("frwiki", 10, 20, "v1")
    new = repository.get_work("frwiki", 10, 21, "v1")
    assert old is not None and old.state == "superseded"
    assert new is not None and new.state == "pending"


def test_stale_request_does_not_supersede_newer_pending_work(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    repository.enqueue("frwiki", 10, 21, "v1", priority=10)
    repository.enqueue("frwiki", 10, 20, "v1", priority=100)

    current = repository.get_work("frwiki", 10, 21, "v1")
    stale = repository.get_work("frwiki", 10, 20, "v1")
    assert current is not None and current.state == "pending"
    assert stale is None


def test_expired_lease_can_be_reclaimed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.enqueue("frwiki", 10, 20, "v1", priority=10)

    first = repository.claim("worker-1", lease_seconds=-1)
    second = repository.claim("worker-2", lease_seconds=60)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert second.attempts == 2


def test_cleanup_keeps_latest_result_even_when_it_is_old(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    def save(page_id: int, revision_id: int, computed_at: datetime) -> None:
        repository.save_result(
            {
                "wiki": "frwiki",
                "page_id": page_id,
                "revision_id": revision_id,
                "algorithm_version": "v1",
                "title": f"Page {page_id}",
                "metric": "test",
                "contributors": [],
                "distinct_contributors": 1,
                "count_limited": False,
                "countable_tokens": 1,
                "wikiwho_revision_id": revision_id,
                "computed_at": computed_at,
            }
        )

    old = utcnow() - timedelta(days=60)
    save(10, 20, old)
    save(10, 21, utcnow())
    save(11, 30, old)

    removed = repository.cleanup(superseded_result_days=30)

    assert removed["results"] == 1
    assert repository.get_result("frwiki", 10, 20, "v1") is None
    assert repository.get_result("frwiki", 10, 21, "v1") is not None
    assert repository.get_result("frwiki", 11, 30, "v1") is not None


def test_latest_result_is_selected_by_computation_time(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    now = utcnow()

    for revision_id, computed_at in ((20, now - timedelta(days=2)), (21, now)):
        repository.save_result(
            {
                "wiki": "frwiki",
                "page_id": 10,
                "revision_id": revision_id,
                "algorithm_version": "v1",
                "title": "Page",
                "metric": "test",
                "contributors": [],
                "distinct_contributors": 1,
                "count_limited": False,
                "countable_tokens": 1,
                "wikiwho_revision_id": revision_id,
                "computed_at": computed_at,
            }
        )

    latest = repository.get_latest_result("frwiki", 10, "v1")

    assert latest is not None and latest.revision_id == 21


def test_expired_exact_revision_can_be_enqueued_for_refresh(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": 10,
            "revision_id": 20,
            "algorithm_version": "v1",
            "title": "Page",
            "metric": "test",
            "contributors": [],
            "distinct_contributors": 1,
            "count_limited": False,
            "countable_tokens": 1,
            "wikiwho_revision_id": 20,
            "computed_at": utcnow() - timedelta(days=91),
        }
    )

    queued = repository.enqueue_if_stale(
        "frwiki", 10, 20, "v1", priority=50, freshness_seconds=90 * 24 * 60 * 60
    )

    assert queued is True
    assert repository.get_work("frwiki", 10, 20, "v1") is not None


def test_a_request_counts_the_page_and_never_the_reader(tmp_path: Path) -> None:
    """One row per article, however many times it is read: a counter, not a log."""
    repository = make_repository(tmp_path)

    repository.record_page_request("frwiki", 42)
    repository.record_page_request("frwiki", 42)
    repository.record_page_request("frwiki", 43)

    assert repository.demand_counts()["frwiki"] == {"pages": 2, "waiting": 2, "requested": 2}
    assert repository.recently_requested_pages("frwiki", since_days=7, limit=10) == [43, 42]


def test_pages_readers_asked_for_outrank_pages_the_world_reads(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.record_page_views("frwiki", {10: 500_000, 11: 400_000, 12: 3})
    repository.record_page_request("frwiki", 12)

    # 12 is read three times a day by the world and once by someone running the gadget,
    # which is the whole point of the ranking.
    assert repository.pending_demand("frwiki", limit=10) == [12, 10, 11]


def test_a_second_day_of_views_adds_to_the_first(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    created, updated = repository.record_page_views("frwiki", {10: 100, 11: 50})
    assert (created, updated) == (2, 0)
    created, updated = repository.record_page_views("frwiki", {10: 100, 12: 400})
    assert (created, updated) == (1, 1)

    assert repository.pending_demand("frwiki", limit=10) == [12, 10, 11]


def test_a_page_handed_to_the_queue_leaves_the_ranking(tmp_path: Path) -> None:
    """Marking beats a keyset cursor here: the ranking changes under it every day."""
    repository = make_repository(tmp_path)
    repository.record_page_views("frwiki", {10: 100, 11: 50})

    assert repository.mark_demand_queued("frwiki", [10]) == 1

    assert repository.pending_demand("frwiki", limit=10) == [11]
    assert repository.demand_counts()["frwiki"]["waiting"] == 1


def test_a_page_queued_long_enough_ago_comes_back_in_line(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.record_page_views("frwiki", {10: 100})
    repository.mark_demand_queued("frwiki", [10])

    assert repository.requeue_stale_demand("frwiki", older_than_seconds=3600) == 0

    with repository.database.session() as session, session.begin():
        from wikipeople.models import PageDemand

        row = session.get(PageDemand, ("frwiki", 10))
        assert row is not None
        row.queued_at = utcnow() - timedelta(days=120)

    assert repository.requeue_stale_demand("frwiki", older_than_seconds=90 * 86400) == 1
    assert repository.pending_demand("frwiki", limit=10) == [10]


def test_outcomes_are_counted_per_day_and_per_wiki(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    repository.count_request("frwiki", "ready")
    repository.count_request("frwiki", "ready")
    repository.count_request("frwiki", "pending")
    repository.count_request("dewiki", "ready")

    usage = repository.usage_since(7)
    assert len(usage) == 1
    (day,) = usage
    assert usage[day] == {"frwiki": {"ready": 2, "pending": 1}, "dewiki": {"ready": 1}}


def test_older_counters_fall_outside_the_reported_window(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.count_request("frwiki", "ready", day=date(2020, 1, 1))

    assert repository.usage_since(7) == {}


def test_weak_answers_are_swept_by_primary_key_oldest_first(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    for page_id, metric in ((1, "edits"), (2, "tokens"), (3, "edits")):
        repository.save_result(
            {
                "wiki": "frwiki",
                "page_id": page_id,
                "revision_id": page_id * 10,
                "algorithm_version": "v1",
                "title": f"Page {page_id}",
                "metric": metric,
                "contributors": [],
                "distinct_contributors": 0,
                "count_limited": False,
                "countable_tokens": 0,
                "wikiwho_revision_id": page_id * 10,
                "computed_at": utcnow() - timedelta(days=30),
            }
        )

    rows = repository.results_with_metric("v1", "edits", utcnow(), limit=10)
    assert [row.page_id for row in rows] == [1, 3]

    # Recomputed this morning: too fresh to be worth the shared WikiWho budget again.
    recent = repository.results_with_metric("v1", "edits", utcnow() - timedelta(days=90), limit=10)
    assert recent == []

    assert repository.metric_counts()["frwiki"] == {"edits": 2, "tokens": 1}


def test_a_page_nothing_has_touched_in_months_leaves_the_demand_table(tmp_path: Path) -> None:
    """The pruning is what keeps a table of counters from accumulating into a history."""
    repository = make_repository(tmp_path)
    repository.record_page_views("frwiki", {10: 100})
    repository.record_page_request("frwiki", 11)

    assert repository.cleanup(demand_days=180)["demand"] == 0

    with repository.database.session() as session, session.begin():
        from wikipeople.models import PageDemand

        stale = utcnow() - timedelta(days=400)
        for page_id in (10, 11):
            row = session.get(PageDemand, ("frwiki", page_id))
            assert row is not None
            row.created_at = stale
            row.last_viewed_at = stale if row.last_viewed_at else None
            row.last_requested_at = stale if row.last_requested_at else None

    assert repository.cleanup(demand_days=180)["demand"] == 2
