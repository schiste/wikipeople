from dataclasses import replace
from pathlib import Path

from wikipeople.backfill import COMPLETE, backfill_wiki, length_cursor_key, title_cursor_key
from wikipeople.clients import PageMetadata
from wikipeople.config import Settings
from wikipeople.replica import LengthCursor, ReplicaPage
from wikipeople.runtime import Runtime, build_runtime


class FakeReplica:
    def __init__(self, batches: list[tuple[list[ReplicaPage], LengthCursor | None]]) -> None:
        self.batches = batches
        self.calls: list[LengthCursor | None] = []

    def available(self) -> bool:
        return True

    def pages_by_descending_length(
        self, _wiki: str, cursor: LengthCursor | None, _limit: int
    ) -> tuple[list[ReplicaPage], LengthCursor | None]:
        self.calls.append(cursor)
        return self.batches[len(self.calls) - 1]


class AbsentReplica:
    def available(self) -> bool:
        return False


class FakeMediaWiki:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def all_pages_batch(
        self, _wiki: str, cursor: str | None
    ) -> tuple[list[PageMetadata], str | None]:
        self.calls.append(cursor)
        return [PageMetadata(7, 700, "Aardvark", 0)], None


def build(tmp_path: Path, name: str) -> Runtime:
    settings = replace(Settings.from_env(), database_url=f"sqlite:///{tmp_path / name}")
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def test_backfill_walks_down_from_the_heaviest_page(tmp_path: Path) -> None:
    runtime = build(tmp_path, "length.db")
    replica = FakeReplica(
        [
            ([ReplicaPage(1, 11, 90000), ReplicaPage(2, 12, 80000)], LengthCursor(80000, 2)),
            ([ReplicaPage(3, 13, 70000)], None),
        ]
    )

    queued = backfill_wiki(runtime, FakeMediaWiki(), replica, "frwiki", batches=2)  # type: ignore[arg-type]

    assert queued == 3
    assert replica.calls == [None, LengthCursor(80000, 2)]
    assert runtime.repository.get_state(length_cursor_key("frwiki")) == COMPLETE
    for page_id, revision_id in ((1, 11), (2, 12), (3, 13)):
        work = runtime.repository.get_work(
            "frwiki", page_id, revision_id, runtime.settings.algorithm_version
        )
        assert work is not None and work.state == "pending"


def test_the_length_walk_resumes_from_its_own_cursor(tmp_path: Path) -> None:
    runtime = build(tmp_path, "resume.db")
    runtime.repository.set_state(length_cursor_key("frwiki"), "80000:2")
    replica = FakeReplica([([ReplicaPage(3, 13, 70000)], None)])

    backfill_wiki(runtime, FakeMediaWiki(), replica, "frwiki", batches=1)  # type: ignore[arg-type]

    assert replica.calls == [LengthCursor(80000, 2)]


def test_without_a_replica_the_backfill_still_runs_on_the_action_api(tmp_path: Path) -> None:
    """The fallback keeps its own cursor rather than sharing the length one.

    A title and a `length:page_id` pair are not interchangeable, and a wiki can move
    between the two paths when a replica goes down. Sharing one key would have made
    each switch read the other's cursor as its own.
    """
    runtime = build(tmp_path, "fallback.db")
    runtime.repository.set_state(length_cursor_key("frwiki"), "80000:2")
    mediawiki = FakeMediaWiki()

    queued = backfill_wiki(runtime, mediawiki, AbsentReplica(), "frwiki", batches=1)  # type: ignore[arg-type]

    assert queued == 1
    assert mediawiki.calls == [None]
    assert runtime.repository.get_state(title_cursor_key("frwiki")) == COMPLETE
    assert runtime.repository.get_state(length_cursor_key("frwiki")) == "80000:2"


class DemandReplica(FakeReplica):
    """A replica that also answers the demand walk's page-id lookup."""

    def __init__(
        self,
        batches: list[tuple[list[ReplicaPage], LengthCursor | None]],
        by_id: dict[int, ReplicaPage],
    ) -> None:
        super().__init__(batches)
        self.by_id = by_id
        self.id_lookups: list[list[int]] = []

    def latest_revisions(self, _wiki: str, page_ids: list[int]) -> dict[int, ReplicaPage]:
        self.id_lookups.append(list(page_ids))
        return {page_id: self.by_id[page_id] for page_id in page_ids if page_id in self.by_id}


def test_the_most_wanted_pages_are_queued_before_the_heaviest_ones(tmp_path: Path) -> None:
    """Size was only ever a proxy; readership is the thing it was standing in for."""
    runtime = build(tmp_path, "demand.db")
    runtime.repository.record_page_views("frwiki", {50: 900, 51: 400})
    replica = DemandReplica(
        [([ReplicaPage(1, 11, 90000)], None)],
        {50: ReplicaPage(50, 500, 4000), 51: ReplicaPage(51, 510, 3000)},
    )

    queued = backfill_wiki(runtime, FakeMediaWiki(), replica, "frwiki", batches=2)  # type: ignore[arg-type]

    assert replica.id_lookups == [[50, 51]]
    assert queued == 3
    # The ranking was exhausted in one batch, so the size walk got the second.
    assert replica.calls == [None]


def test_an_empty_ranking_costs_nothing_and_leaves_the_size_walk_untouched(
    tmp_path: Path,
) -> None:
    runtime = build(tmp_path, "no-demand.db")
    replica = DemandReplica([([ReplicaPage(1, 11, 90000)], None)], {})

    backfill_wiki(runtime, FakeMediaWiki(), replica, "frwiki", batches=1)  # type: ignore[arg-type]

    assert replica.id_lookups == []
    assert replica.calls == [None]


def test_a_page_the_replica_no_longer_returns_still_leaves_the_ranking(tmp_path: Path) -> None:
    """A deleted article would otherwise sit at the top of the ranking for ever."""
    runtime = build(tmp_path, "gone.db")
    runtime.repository.record_page_views("frwiki", {60: 900})
    replica = DemandReplica([([], None)], {})

    backfill_wiki(runtime, FakeMediaWiki(), replica, "frwiki", batches=1)  # type: ignore[arg-type]

    assert runtime.repository.pending_demand("frwiki", limit=10) == []
    assert runtime.repository.demand_counts()["frwiki"]["waiting"] == 0
