import bz2
from dataclasses import replace
from datetime import date
from pathlib import Path

from wikipeople.config import Settings
from wikipeople.pageviews import (
    LAST_DAY_KEY,
    collect,
    domain_codes,
    dump_path,
    find_dump,
    read_daily_views,
    resolve_target_wikis,
    top_pages,
)
from wikipeople.runtime import Runtime, build_runtime
from wikipeople.sites import SiteResolver

# One line per page and access method, exactly as published: domain, title, page id,
# access method, daily total, hourly breakdown. Taken from a real file rather than
# invented, because the whole module depends on the field order.
SAMPLE = b"""fr.wikipedia !!! 351979 mobile-web 2 U1V1
fr.wikipedia France 30 desktop 900 A100
fr.wikipedia France 30 mobile-web 600 A100
fr.wikipedia Paris 40 desktop 700 B100
fr.wikipedia Obscurite 50 desktop 1 C1
fr.wikipedia Sans_identifiant null desktop 500 D100
de.wikipedia Frankreich 90 desktop 300 E100
en.wikipedia France 12 desktop 5000 F100
"""


def _write_dump(root: Path, day: date, content: bytes = SAMPLE) -> Path:
    path = dump_path(str(root), day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bz2.compress(content))
    return path


def _runtime(tmp_path: Path, root: Path, **overrides: object) -> Runtime:
    defaults: dict[str, object] = {
        "database_url": f"sqlite:///{tmp_path / 'demand.db'}",
        "backfill_wikis": ("frwiki", "dewiki"),
        "pageview_dump_root": str(root),
        "pageview_minimum_views": 2,
    }
    settings = replace(Settings.from_env(), **(defaults | overrides))  # type: ignore[arg-type]
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def test_the_published_path_is_the_one_the_analytics_team_uses() -> None:
    assert dump_path("/public/dumps", date(2026, 8, 25)) == Path(
        "/public/dumps/2026/2026-08/pageviews-20260825-user.bz2"
    )


def test_a_late_publication_falls_back_to_the_newest_day_that_exists(tmp_path: Path) -> None:
    """Yesterday's file lands during the morning, so a miss is normal rather than fatal."""
    _write_dump(tmp_path, date(2026, 8, 23))

    found = find_dump(str(tmp_path), lookback_days=5, today=date(2026, 8, 26))

    assert found is not None
    assert found[1] == date(2026, 8, 23)


def test_no_recent_dump_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    assert find_dump(str(tmp_path), lookback_days=3, today=date(2026, 8, 26)) is None


def test_domain_codes_come_from_the_same_host_rule_the_api_calls_use() -> None:
    """No table to keep in step: a wiki reaching the API reaches the dump the same way."""
    assert domain_codes(SiteResolver(), ["frwiki", "simplewiki"]) == {
        "fr.wikipedia": "frwiki",
        "simple.wikipedia": "simplewiki",
    }


def test_views_are_summed_across_access_methods_for_the_requested_wikis_only() -> None:
    totals = read_daily_views(
        Path("unused"),
        {"fr.wikipedia": "frwiki"},
        minimum_views=2,
        lines=iter(SAMPLE.splitlines(keepends=True)),
    )

    # Desktop and mobile are separate lines for the same article.
    assert totals["frwiki"][30] == 1500
    assert totals["frwiki"][40] == 700
    # Below the floor, so never held in memory.
    assert 50 not in totals["frwiki"]
    # 4% of lines carry no page id, and a title is not what the queue takes.
    assert set(totals) == {"frwiki"}


def test_the_ranking_is_capped_by_views_not_by_page_order() -> None:
    assert top_pages({1: 5, 2: 900, 3: 40}, limit=2) == {2: 900, 3: 40}
    assert top_pages({1: 5}, limit=0) == {1: 5}


def test_a_day_of_views_becomes_a_ranking_the_backfill_can_walk(tmp_path: Path) -> None:
    root = tmp_path / "dumps"
    _write_dump(root, date(2026, 8, 25))
    runtime = _runtime(tmp_path, root)

    recorded = collect(runtime, ["frwiki", "dewiki"], today=date(2026, 8, 26))

    assert recorded == 4  # three frwiki pages above the floor, one dewiki
    assert runtime.repository.pending_demand("frwiki", limit=10) == [30, 40, 351979]
    assert runtime.repository.pending_demand("dewiki", limit=10) == [90]
    assert runtime.repository.get_state(LAST_DAY_KEY) == "2026-08-25"


def test_views_accumulate_across_days_instead_of_replacing_each_other(tmp_path: Path) -> None:
    """A steady readership has to outrank a single day's spike, so days add up."""
    root = tmp_path / "dumps"
    _write_dump(root, date(2026, 8, 24))
    runtime = _runtime(tmp_path, root)
    collect(runtime, ["frwiki"], today=date(2026, 8, 25))

    _write_dump(root, date(2026, 8, 25))
    collect(runtime, ["frwiki"], today=date(2026, 8, 26))

    assert runtime.repository.demand_counts()["frwiki"]["pages"] == 3
    with runtime.database.session() as session:
        from wikipeople.models import PageDemand

        row = session.get(PageDemand, ("frwiki", 30))
        assert row is not None and row.views == 3000


def test_the_same_day_is_not_counted_twice(tmp_path: Path) -> None:
    root = tmp_path / "dumps"
    _write_dump(root, date(2026, 8, 25))
    runtime = _runtime(tmp_path, root)

    assert collect(runtime, ["frwiki"], today=date(2026, 8, 26)) == 3
    assert collect(runtime, ["frwiki"], today=date(2026, 8, 26)) == 0


def test_ranking_what_to_crawl_is_crawling_so_it_needs_the_same_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "dumps"
    runtime = _runtime(tmp_path, root, backfill_wikis=())

    assert resolve_target_wikis(runtime, None) == []
    # Serving every wiki on demand never enrols one into bulk work.
    assert runtime.settings.supported_wikis == ("*",)


def test_an_uncovered_wiki_is_dropped_rather_than_asked_for(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, tmp_path / "dumps", backfill_wikis=("frwiki", "commonswiki"))

    assert resolve_target_wikis(runtime, None) == ["frwiki"]
