from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from wikipeople.config import Settings
from wikipeople.prewarm import collect_recent_top_titles, resolve_target_wikis
from wikipeople.runtime import Runtime, build_runtime


class FakeAnalytics:
    def __init__(self, results: dict[date, list[str] | None]) -> None:
        self.results = results

    def top_pages(self, _wiki: str, day: date) -> list[str] | None:
        return self.results.get(day)


def test_collects_last_available_days_instead_of_failing_on_publication_lag() -> None:
    analytics = FakeAnalytics(
        {
            date(2026, 8, 15): None,
            date(2026, 8, 14): None,
            date(2026, 8, 13): ["France", "Paris"],
            date(2026, 8, 12): ["France", "Europe"],
        }
    )

    titles, loaded_days = collect_recent_top_titles(
        analytics,  # type: ignore[arg-type]
        "frwiki",
        days=2,
        today=date(2026, 8, 16),
    )

    assert titles == {"France", "Paris", "Europe"}
    assert loaded_days == [date(2026, 8, 13), date(2026, 8, 12)]


def _runtime(tmp_path: Path) -> Runtime:
    settings = replace(
        Settings.from_env(),
        database_url=f"sqlite:///{tmp_path / 'prewarm.db'}",
    )
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def test_a_wiki_discovered_by_a_worker_is_prewarmed_from_then_on(tmp_path: Path) -> None:
    """The first real result for a wiki enrols it into daily popular-page warming."""
    runtime = _runtime(tmp_path)

    assert resolve_target_wikis(runtime, None) == []

    assert runtime.repository.register_active_wiki("dewiki") is True
    assert resolve_target_wikis(runtime, None) == ["dewiki"]

    # Re-registering is idempotent and must not duplicate the wiki.
    assert runtime.repository.register_active_wiki("dewiki") is False
    assert resolve_target_wikis(runtime, None) == ["dewiki"]


def test_pinned_and_discovered_wikis_are_merged(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime = replace(runtime, settings=replace(runtime.settings, prewarm_wikis=("frwiki",)))
    runtime.repository.register_active_wiki("dewiki")

    assert resolve_target_wikis(runtime, None) == ["dewiki", "frwiki"]


def test_an_uncovered_wiki_is_never_prewarmed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime = replace(runtime, settings=replace(runtime.settings, prewarm_wikis=("commonswiki",)))

    assert resolve_target_wikis(runtime, None) == []


def test_explicit_wiki_argument_overrides_discovery(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.repository.register_active_wiki("dewiki")

    assert resolve_target_wikis(runtime, "frwiki") == ["frwiki"]


class FakeMediaWiki:
    def __init__(self, pages: dict[int, tuple[int, int]]) -> None:
        self.pages = pages
        self.calls: list[list[int]] = []

    def resolve_page_ids(self, _wiki: str, page_ids: list[int]):
        from wikipeople.clients import PageMetadata

        self.calls.append(list(page_ids))
        return [
            PageMetadata(page_id, revision_id, f"Page {page_id}", namespace)
            for page_id, (revision_id, namespace) in self.pages.items()
            if page_id in page_ids
        ]


def test_an_article_a_reader_opened_is_rechecked_against_its_current_text(
    tmp_path: Path,
) -> None:
    """Nothing brings a once-computed page back today, and its reader is the likeliest to return."""
    from wikipeople.models import utcnow
    from wikipeople.prewarm import prewarm_requested

    runtime = _runtime(tmp_path)
    runtime.repository.record_page_request("frwiki", 100)
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": 100,
            "revision_id": 200,
            "algorithm_version": runtime.settings.algorithm_version,
            "title": "France",
            "metric": "test-metric",
            "contributors": [],
            "distinct_contributors": 0,
            "count_limited": False,
            "countable_tokens": 10,
            "wikiwho_revision_id": 200,
            "computed_at": utcnow() - timedelta(days=30),
        }
    )
    mediawiki = FakeMediaWiki({100: (250, 0)})

    assert prewarm_requested(runtime, mediawiki, "frwiki") == 1

    assert mediawiki.calls == [[100]]
    work = runtime.repository.get_work("frwiki", 100, 250, runtime.settings.algorithm_version)
    assert work is not None and work.priority == 50


def test_a_page_still_on_the_revision_its_answer_describes_costs_nothing(tmp_path: Path) -> None:
    from wikipeople.models import utcnow
    from wikipeople.prewarm import prewarm_requested

    runtime = _runtime(tmp_path)
    runtime.repository.record_page_request("frwiki", 100)
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": 100,
            "revision_id": 200,
            "algorithm_version": runtime.settings.algorithm_version,
            "title": "France",
            "metric": "test-metric",
            "contributors": [],
            "distinct_contributors": 0,
            "count_limited": False,
            "countable_tokens": 10,
            "wikiwho_revision_id": 200,
            "computed_at": utcnow() - timedelta(days=30),
        }
    )

    assert prewarm_requested(runtime, FakeMediaWiki({100: (200, 0)}), "frwiki") == 0


def test_a_page_recomputed_this_week_is_left_to_settle(tmp_path: Path) -> None:
    """A heavily edited article would otherwise take the whole budget by itself."""
    from wikipeople.models import utcnow
    from wikipeople.prewarm import prewarm_requested

    runtime = _runtime(tmp_path)
    runtime.repository.record_page_request("frwiki", 100)
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": 100,
            "revision_id": 200,
            "algorithm_version": runtime.settings.algorithm_version,
            "title": "France",
            "metric": "test-metric",
            "contributors": [],
            "distinct_contributors": 0,
            "count_limited": False,
            "countable_tokens": 10,
            "wikiwho_revision_id": 200,
            "computed_at": utcnow() - timedelta(hours=6),
        }
    )

    assert prewarm_requested(runtime, FakeMediaWiki({100: (250, 0)}), "frwiki") == 0


def test_a_page_that_left_the_main_namespace_is_skipped(tmp_path: Path) -> None:
    from wikipeople.prewarm import prewarm_requested

    runtime = _runtime(tmp_path)
    runtime.repository.record_page_request("frwiki", 100)

    assert prewarm_requested(runtime, FakeMediaWiki({100: (250, 3)}), "frwiki") == 0
