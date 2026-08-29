"""The one configuration page, read by the server.

The gadget reads the same page for what it draws; that half is covered by
tests/test_gadget_source.py and tests/test_onwiki_config.py. What is here is the half
the API applies itself, because a rule enforced only in the browser leaves the names one
direct request away.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wikipeople.app import create_app
from wikipeople.clients import CategoryMember, TitleInfo
from wikipeople.config import Settings
from wikipeople.errors import InvalidConfigPageError, RetryableUpstreamError
from wikipeople.models import utcnow
from wikipeople.onwiki import (
    WikiConfig,
    collect_entries,
    parse_config_page,
    resolve_target_wikis,
    sync_wiki,
)
from wikipeople.policy import AccountStanding, DisplayPolicy, is_anonymised_account
from wikipeople.repository import StandingRecord
from wikipeople.runtime import Runtime, build_runtime

PAGE = json.dumps(
    {
        "//": {"note": "documentation the readers ignore"},
        "enabled": True,
        "messages": {"wikipeople-people": "{{PLURAL:$1|$1 personne|$1 personnes}}"},
        "sanctionedAccounts": "unlink",
        "optOut": [
            "Jean Dupont",
            "Affaire Machin",
            "Jean Dupont",
            ":Catégorie:Personnalité vivante",
        ],
    }
)


class FakeMediaWiki:
    def __init__(
        self,
        raw: str | None,
        infos: list[TitleInfo] | None = None,
        members: dict[str, list[CategoryMember]] | None = None,
        fail: bool = False,
    ) -> None:
        self.raw = raw
        self.infos = infos or []
        self.members = members or {}
        self.fail = fail
        self.asked: list[str] = []

    def get_wikitext(self, _wiki: str, title: str) -> str | None:
        if self.fail:
            raise RetryableUpstreamError("Action API indisponible")
        self.asked.append(title)
        return self.raw

    def classify_titles(self, _wiki: str, titles: list[str]) -> list[TitleInfo]:
        return [info for info in self.infos if info.title in titles]

    def category_members(
        self, _wiki: str, category: str, limit: int
    ) -> tuple[list[CategoryMember], bool]:
        members = self.members.get(category, [])
        return members[:limit], len(members) > limit


# --- reading the page ------------------------------------------------------------


def test_the_two_halves_of_one_page_do_not_read_each_other() -> None:
    """The gadget's options and the API's options share a file and nothing else.

    Neither side has to know the other's keys exist, which is what lets one of them
    gain an option without the other being redeployed.
    """
    config = parse_config_page(PAGE)
    assert config.display == DisplayPolicy(sanctioned_accounts="unlink")
    # Deduplicated, in page order, with the category's linking colon dropped.
    assert config.opt_out == ("Jean Dupont", "Affaire Machin", "Catégorie:Personnalité vivante")


def test_an_empty_page_is_the_defaults_and_opts_nobody_out() -> None:
    assert parse_config_page("{}") == WikiConfig()


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("Page_avec_tiret_bas", ("Page avec tiret bas",)),
        ("  Page  ", ("Page",)),
        (":Catégorie:X", ("Catégorie:X",)),
        ("", ()),
        ("   ", ()),
    ],
)
def test_entry_shapes(entry: str, expected: tuple[str, ...]) -> None:
    assert parse_config_page(json.dumps({"optOut": [entry]})).opt_out == expected


def test_an_entry_of_the_wrong_type_is_dropped_and_the_rest_of_the_list_stands() -> None:
    """One bad line is one bad line. The list around it was still written by someone."""
    config = parse_config_page(json.dumps({"optOut": ["Machin", 42, None, "Truc"]}))
    assert config.opt_out == ("Machin", "Truc")


@pytest.mark.parametrize(
    "stated",
    [
        {"sanctionedAccounts": "unlnik"},
        {"sanctionedAccounts": True},
        {"contributorNames": "off"},
        {"anonymisedAccounts": ""},
        {"invented-key": "hide"},
    ],
)
def test_a_value_outside_the_list_is_not_obeyed_half_way(stated: dict) -> None:
    """A typo must not become a fourth behaviour nobody wrote.

    The gadget has applied this rule to its own half of the page since the start, for
    the same reason: a rejected value leaves the option at its default, where it is
    visible as "the page did not take effect" rather than as new behaviour.
    """
    assert parse_config_page(json.dumps(stated)).display == DisplayPolicy()


def test_a_value_is_read_however_it_was_typed() -> None:
    assert parse_config_page('{"sanctionedAccounts": "  LINK "}').display.sanctioned_accounts == (
        "link"
    )


@pytest.mark.parametrize(
    "raw",
    ['{"optOut": ["Machin",]}', "[]", '"hide"', "", "not json at all", '{"optOut": "Machin"}'],
)
def test_a_page_that_cannot_be_read_is_not_a_page_saying_nothing(raw: str) -> None:
    """A file being edited must not read as "the wiki changed its mind".

    A missing comma is the most ordinary thing that happens to a JSON page, and read as
    an empty configuration it would un-hide every opted-out article on the wiki at once.
    The single-quoted list is the same trap: with the type wrong there is no telling an
    empty list from a broken one, and the two have opposite consequences for whoever is
    on it.
    """
    with pytest.raises(InvalidConfigPageError):
        parse_config_page(raw)


@pytest.mark.parametrize(
    "username",
    ["Renamed user 4501e2a3c", "renamed user 123456", "Vanished user", "Vanished_user_ltjrm"],
)
def test_a_placeholder_left_by_a_rename_is_recognised(username: str) -> None:
    assert is_anonymised_account(username) is True


@pytest.mark.parametrize(
    "username",
    ["Vanished userland fan", "Renamed user 42 bot", "Renamer", "Alice", "Compte renommé"],
)
def test_an_ordinary_name_that_merely_looks_like_one_is_not(username: str) -> None:
    """The pattern is anchored because the two mistakes are not equally bad.

    A placeholder missed is odd — a reader sees a number. A real name mistaken for a
    placeholder erases a person's credit for text they wrote, and they never find out.
    """
    assert is_anonymised_account(username) is False


PUBLISHED = sorted((Path(__file__).resolve().parents[1] / "config").glob("*.json"))


def test_the_published_pages_change_nothing_on_arrival() -> None:
    """The copies a wiki pastes must opt nobody out and mask nothing new.

    They are dense with the syntax they document — a "//" block full of quoted example
    values, every option stated explicitly — and any of that read as an instruction
    would change what a wiki shows without anyone having decided it.
    """
    for path in PUBLISHED:
        assert parse_config_page(path.read_text(encoding="utf-8")) == WikiConfig(), path.name


# --- resolving what the list covers ----------------------------------------------


def test_a_category_covers_its_members_and_an_article_keeps_its_own_reason() -> None:
    mediawiki = FakeMediaWiki(
        raw=None,
        infos=[
            TitleInfo(title="Jean Dupont", namespace=0, page_id=11),
            TitleInfo(title="Catégorie:Personnalité vivante", namespace=14, page_id=99),
            TitleInfo(title="Discussion:Quelque chose", namespace=1, page_id=77),
            TitleInfo(title="Page supprimée", namespace=0, page_id=None),
        ],
        members={
            "Catégorie:Personnalité vivante": [
                CategoryMember(page_id=11, title="Jean Dupont"),
                CategoryMember(page_id=12, title="Marie Durand"),
            ]
        },
    )

    entries, skipped = collect_entries(
        mediawiki,  # type: ignore[arg-type]
        "frwiki",
        (
            "Jean Dupont",
            "Catégorie:Personnalité vivante",
            "Discussion:Quelque chose",
            "Page supprimée",
        ),
        category_limit=5000,
    )

    by_id = {entry.page_id: entry for entry in entries}
    assert set(by_id) == {11, 12}
    assert by_id[11].source == "page"
    assert by_id[12].source == "category:Catégorie:Personnalité vivante"
    assert any("Page supprimée" in note for note in skipped)
    assert any("espace de noms 1" in note for note in skipped)


def test_a_category_past_the_cap_is_reported_rather_than_silently_halved() -> None:
    mediawiki = FakeMediaWiki(
        raw=None,
        infos=[TitleInfo(title="Catégorie:Immense", namespace=14, page_id=99)],
        members={
            "Catégorie:Immense": [CategoryMember(page_id=i, title=f"A{i}") for i in range(1, 6)]
        },
    )

    entries, skipped = collect_entries(
        mediawiki,  # type: ignore[arg-type]
        "frwiki",
        ("Catégorie:Immense",),
        category_limit=3,
    )

    assert len(entries) == 3
    assert any("tronquée" in note for note in skipped)


# --- the sync job ----------------------------------------------------------------


def _runtime(tmp_path: Path, name: str = "onwiki.db", **overrides: object) -> Runtime:
    settings = replace(
        Settings.from_env(),
        database_url=f"sqlite:///{tmp_path / name}",
        algorithm_version="test-onwiki",
        config_page="User:Schiste/wikipeople-config.json",
        **overrides,  # type: ignore[arg-type]
    )
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def _listing(*titles: str, **rest: object) -> str:
    return json.dumps({"optOut": list(titles), **rest})


def test_one_page_answers_both_questions_in_one_fetch(tmp_path: Path) -> None:
    """Which is the point of merging them: one request, one edit, one history."""
    runtime = _runtime(tmp_path, "both.db")
    mediawiki = FakeMediaWiki(
        _listing("Jean Dupont", sanctionedAccounts="unlink"),
        infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=11)],
    )

    report = sync_wiki(runtime, mediawiki, "frwiki")  # type: ignore[arg-type]

    assert mediawiki.asked == ["User:Schiste/wikipeople-config.json"]
    assert (report.covered, report.added, report.policy_changed) == (1, 1, True)
    assert runtime.repository.is_opted_out("frwiki", 11) is True
    assert runtime.repository.display_policy("frwiki").sanctioned_accounts == "unlink"


def test_a_removed_entry_stops_hiding_the_names(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "removed.db")
    listed = FakeMediaWiki(
        _listing("Jean Dupont"), infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=11)]
    )
    sync_wiki(runtime, listed, "frwiki")  # type: ignore[arg-type]
    assert runtime.repository.is_opted_out("frwiki", 11) is True

    emptied = FakeMediaWiki(_listing(), infos=[])
    assert sync_wiki(runtime, emptied, "frwiki").removed == 1  # type: ignore[arg-type]
    assert runtime.repository.is_opted_out("frwiki", 11) is False


def test_reading_the_same_page_twice_is_not_a_change(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "quiet.db")
    page = _listing(contributorNames="hide")
    assert sync_wiki(runtime, FakeMediaWiki(page), "frwiki").policy_changed is True  # type: ignore[arg-type]
    assert sync_wiki(runtime, FakeMediaWiki(page), "frwiki").policy_changed is False  # type: ignore[arg-type]


def test_no_page_at_all_means_the_wiki_decided_nothing(tmp_path: Path) -> None:
    """A missing page is an answer. Unlike a failed read, it is one the API can act on."""
    runtime = _runtime(tmp_path, "missing.db")
    report = sync_wiki(runtime, FakeMediaWiki(None), "frwiki")  # type: ignore[arg-type]
    assert (report.policy, report.covered) == (DisplayPolicy(), 0)


@pytest.mark.parametrize(
    ("mediawiki", "error"),
    [
        (FakeMediaWiki(None, fail=True), RetryableUpstreamError),
        (FakeMediaWiki('{"optOut": ["Jean Dupont",]}'), InvalidConfigPageError),
    ],
)
def test_a_page_that_could_not_be_read_keeps_everything_it_had(
    tmp_path: Path, mediawiki: FakeMediaWiki, error: type[Exception]
) -> None:
    """An outage and a syntax error are the same event: we learned nothing this run.

    Either one read as an empty configuration would name every opted-out article on the
    wiki again and undo whatever masking the wiki had asked for, which is the exact
    failure the page exists to prevent.
    """
    runtime = _runtime(tmp_path, f"outage-{error.__name__}.db")
    sync_wiki(  # type: ignore[arg-type]
        runtime,
        FakeMediaWiki(
            _listing("Jean Dupont", contributorNames="hide"),
            infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=11)],
        ),
        "frwiki",
    )

    with pytest.raises(error):
        sync_wiki(runtime, mediawiki, "frwiki")  # type: ignore[arg-type]

    assert runtime.repository.is_opted_out("frwiki", 11) is True
    assert runtime.repository.display_policy("frwiki").show_contributor_names is False


def test_a_dry_run_reports_without_changing_what_is_served(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "dry.db")
    report = sync_wiki(  # type: ignore[arg-type]
        runtime,
        FakeMediaWiki(
            _listing("Jean Dupont", sanctionedAccounts="link"),
            infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=11)],
        ),
        "frwiki",
        dry_run=True,
    )

    assert (report.covered, report.added, report.policy.sanctioned_accounts) == (1, 0, "link")
    assert runtime.repository.is_opted_out("frwiki", 11) is False
    assert runtime.repository.display_policy("frwiki") == DisplayPolicy()


def test_only_wikis_that_have_served_something_are_read(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "wikis.db")
    assert resolve_target_wikis(runtime, None) == []
    runtime.repository.register_active_wiki("frwiki")
    assert resolve_target_wikis(runtime, None) == ["frwiki"]
    assert resolve_target_wikis(runtime, "dewiki") == ["dewiki"]


# --- the serve path --------------------------------------------------------------

CONTRIBUTORS = [
    {"user_id": 1, "username": "Alice", "token_count": 300, "share": 0.30},
    {"user_id": 2, "username": "Renamed user 4501e2a3c", "token_count": 200, "share": 0.20},
    {"user_id": 3, "username": "Banni", "token_count": 100, "share": 0.10},
]


def _save(runtime: Runtime, page_id: int = 100) -> None:
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": page_id,
            "revision_id": 200,
            "algorithm_version": "test-onwiki",
            "title": "Jean Dupont",
            "metric": "surviving-tokens",
            "contributors": CONTRIBUTORS,
            "distinct_contributors": 47,
            "count_limited": False,
            "countable_tokens": 900,
            "wikiwho_revision_id": 200,
            "computed_at": utcnow(),
        }
    )
    runtime.repository.replace_standing(
        "frwiki",
        [
            StandingRecord(
                standing=AccountStanding(user_id=1, username="Alice"),
                lock_checked_at=utcnow(),
                has_user_page=False,
            ),
            StandingRecord(
                standing=AccountStanding(user_id=2, username="Renamed user 4501e2a3c"),
                lock_checked_at=utcnow(),
                has_user_page=True,
            ),
            StandingRecord(
                standing=AccountStanding(user_id=3, username="Banni", blocked_at=utcnow()),
                lock_checked_at=utcnow(),
                has_user_page=True,
            ),
        ],
    )


def _served(runtime: Runtime, path: str = "/v2/frwiki/pages/100?revision_id=200") -> dict:
    with TestClient(create_app(runtime)) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.json()


def test_by_default_a_missing_user_page_costs_the_link_and_nothing_else(tmp_path: Path) -> None:
    """The blue link that led to an empty creation form, fixed where the answer is built.

    Alice is named exactly as before; only the promise that there is something to read
    at the other end is withdrawn.
    """
    runtime = _runtime(tmp_path, "serve-default.db")
    _save(runtime)
    payload = _served(runtime)

    assert [(c["username"], c["display"]) for c in payload["contributors"]] == [
        ("Alice", "unlink"),
        ("Renamed user 4501e2a3c", "label"),
    ]
    # The sanctioned account is withheld, so 47 - 2 named remain in the remainder.
    assert (payload["distinct_contributors"], payload["other_contributors"]) == (47, 45)
    assert payload["opted_out"] is False


def test_an_anonymised_account_is_labelled_rather_than_dropped(tmp_path: Path) -> None:
    """Hiding it would move a fifth of the article into "and 45 others".

    The account did write the text. What a rename destroys is the name, not the
    authorship, so the credit stays and only the label changes.
    """
    runtime = _runtime(tmp_path, "serve-anon.db")
    _save(runtime)
    named = [c for c in _served(runtime)["contributors"] if c["display"] == "label"]
    assert [c["share"] for c in named] == [0.20]


def test_a_wiki_can_ask_for_the_count_without_the_names(tmp_path: Path) -> None:
    """Same shape as an opt-out, and deliberately not the same flag.

    `opted_out` means "this article was listed". A wiki that never names anybody has
    listed nothing, and a reader told otherwise would go looking for a list entry that
    does not exist.
    """
    runtime = _runtime(tmp_path, "serve-count.db")
    _save(runtime)
    sync_wiki(runtime, FakeMediaWiki(_listing(contributorNames="hide")), "frwiki")  # type: ignore[arg-type]

    payload = _served(runtime)
    assert payload["contributors"] == []
    assert payload["opted_out"] is False
    assert (payload["distinct_contributors"], payload["other_contributors"]) == (47, 47)
    # Nothing was recomputed and nothing was deleted, so the wiki can change its mind.
    assert runtime.repository.get_latest_result("frwiki", 100, "test-onwiki").contributors


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("hide", [("Alice", "unlink"), ("Renamed user 4501e2a3c", "label")]),
        (
            "unlink",
            [("Alice", "unlink"), ("Renamed user 4501e2a3c", "label"), ("Banni", "unlink")],
        ),
        ("link", [("Alice", "unlink"), ("Renamed user 4501e2a3c", "label"), ("Banni", "link")]),
    ],
)
def test_a_wiki_chooses_how_far_a_sanctioned_account_is_masked(
    tmp_path: Path, setting: str, expected: list[tuple[str, str]]
) -> None:
    runtime = _runtime(tmp_path, f"serve-{setting}.db")
    _save(runtime)
    sync_wiki(runtime, FakeMediaWiki(_listing(sanctionedAccounts=setting)), "frwiki")  # type: ignore[arg-type]

    assert [(c["username"], c["display"]) for c in _served(runtime)["contributors"]] == expected


def test_an_unlinked_name_says_nothing_about_why(tmp_path: Path) -> None:
    """One value, three causes, on purpose.

    ADR-0009 withholds a sanctioned name without ever saying that is what happened. A
    second value meaning "unlinked because sanctioned" would hand back the disclosure
    the withholding avoided, so a missing user page, a rename and a sanction all
    produce the same word and the gadget explains none of them.
    """
    runtime = _runtime(tmp_path, "serve-opaque.db")
    _save(runtime)
    sync_wiki(runtime, FakeMediaWiki(_listing(sanctionedAccounts="unlink")), "frwiki")  # type: ignore[arg-type]

    payload = _served(runtime)
    assert {c["display"] for c in payload["contributors"]} == {"unlink", "label"}
    assert "sanctioned" not in str(payload)
    assert all("reason" not in c for c in payload["contributors"])


def test_the_operator_switch_still_overrides_the_wiki_towards_showing(tmp_path: Path) -> None:
    """HIDE_SANCTIONED_CONTRIBUTORS is the tool-wide off switch and stays one.

    It can only ever restore a name, never withhold one the wiki asked to keep, so a
    wiki that has asked for less masking than the operator gets what it asked for.
    """
    runtime = _runtime(tmp_path, "serve-switch.db", hide_sanctioned_contributors=False)
    _save(runtime)
    sync_wiki(runtime, FakeMediaWiki(_listing(sanctionedAccounts="hide")), "frwiki")  # type: ignore[arg-type]

    assert [c["username"] for c in _served(runtime)["contributors"]] == [
        "Alice",
        "Renamed user 4501e2a3c",
        "Banni",
    ]


@pytest.mark.parametrize(
    "path", ["/v1/frwiki/pages/100?revision_id=200", "/v2/frwiki/pages/100?revision_id=200"]
)
def test_an_opted_out_page_is_served_as_a_total_with_no_names(tmp_path: Path, path: str) -> None:
    """Both endpoints, because a list only one of them honours is a list with a way round it."""
    runtime = _runtime(tmp_path, f"serve-optout-{path.count('v2')}.db")
    _save(runtime)

    assert _served(runtime, path)["opted_out"] is False

    sync_wiki(  # type: ignore[arg-type]
        runtime,
        FakeMediaWiki(
            _listing("Jean Dupont"),
            infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=100)],
        ),
        "frwiki",
    )

    payload = _served(runtime, path)
    assert payload["contributors"] == []
    assert payload["opted_out"] is True
    # The count survives: the sentence becomes "written by 47 people" rather than
    # disappearing, and 47 is the whole total now that nobody is named separately.
    assert (payload["distinct_contributors"], payload["other_contributors"]) == (47, 47)
    assert runtime.repository.get_latest_result("frwiki", 100, "test-onwiki").contributors


def test_a_configuration_change_invalidates_a_readers_cached_copy(tmp_path: Path) -> None:
    """The names must not survive in a browser that already holds them.

    ADR-0007 made every answer carry a body-derived ETag precisely so a change of this
    kind cannot validate as current. Both halves of this page are such a change.
    """
    runtime = _runtime(tmp_path, "etag.db")
    _save(runtime)
    app = create_app(runtime)

    with TestClient(app) as client:
        first = client.get("/v2/frwiki/pages/100?revision_id=200")
        assert (
            client.get(
                "/v2/frwiki/pages/100?revision_id=200",
                headers={"If-None-Match": first.headers["etag"]},
            ).status_code
            == 304
        )

        sync_wiki(  # type: ignore[arg-type]
            runtime,
            FakeMediaWiki(
                _listing("Jean Dupont"),
                infos=[TitleInfo(title="Jean Dupont", namespace=0, page_id=100)],
            ),
            "frwiki",
        )

        after = client.get(
            "/v2/frwiki/pages/100?revision_id=200",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert after.status_code == 200, "a copy holding the names must not validate as current"
        assert after.json()["contributors"] == []
