from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from wikipeople.config import Settings
from wikipeople.models import utcnow
from wikipeople.recompute import cursor_key, sweep
from wikipeople.runtime import Runtime, build_runtime

METRIC = "mediawiki-revision-count"


def _runtime(tmp_path: Path, **overrides: object) -> Runtime:
    defaults: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'recompute.db'}",
        "algorithm_version": "v1",
        "recompute_min_age_seconds": 14 * 86400,
    }
    settings = replace(Settings.from_env(), **(defaults | overrides))  # type: ignore[arg-type]
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def _store(runtime: Runtime, page_id: int, metric: str, age_days: int) -> None:
    runtime.repository.save_result(
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
            "computed_at": utcnow() - timedelta(days=age_days),
        }
    )


def test_an_answer_that_fell_back_to_edit_counts_is_tried_again(tmp_path: Path) -> None:
    """WikiWho indexing a revision next week produces no event; this is the listener."""
    runtime = _runtime(tmp_path)
    _store(runtime, 1, METRIC, age_days=30)
    _store(runtime, 2, "wikiwho-surviving-alphanumeric-tokens", age_days=30)

    assert sweep(runtime, METRIC, limit=10) == 1

    work = runtime.repository.get_work("frwiki", 1, 10, "v1")
    assert work is not None and work.state == "pending"
    # Below the backfill, because a weaker answer that exists is the least urgent work.
    assert work.priority == 5
    assert runtime.repository.get_work("frwiki", 2, 20, "v1") is None


def test_the_same_revision_is_recomputed_so_the_row_is_replaced_not_doubled(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    _store(runtime, 7, METRIC, age_days=30)

    sweep(runtime, METRIC, limit=10)

    # 70, not the page's current revision: same cache key as the weak row it replaces.
    assert runtime.repository.get_work("frwiki", 7, 70, "v1") is not None


def test_a_recent_fallback_is_left_alone(tmp_path: Path) -> None:
    """A recomputation that lands on the same rung refreshes the date and drops out."""
    runtime = _runtime(tmp_path)
    _store(runtime, 1, METRIC, age_days=2)

    assert sweep(runtime, METRIC, limit=10) == 0
    assert runtime.repository.get_work("frwiki", 1, 10, "v1") is None


def test_the_sweep_advances_by_primary_key_and_laps(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    for page_id in (1, 2, 3):
        _store(runtime, page_id, METRIC, age_days=30)
    key = cursor_key("v1", METRIC)

    assert sweep(runtime, METRIC, limit=2) == 2
    first_cursor = runtime.repository.get_state(key)
    assert first_cursor is not None and int(first_cursor) > 0

    assert sweep(runtime, METRIC, limit=2) == 1
    # Nothing left past the cursor: the sweep restarts rather than stopping for ever.
    assert sweep(runtime, METRIC, limit=2) == 0
    assert runtime.repository.get_state(key) == "0"


def test_a_version_bump_does_not_resume_at_the_old_version_high_water_mark() -> None:
    assert cursor_key("v1", METRIC) != cursor_key("v2", METRIC)
