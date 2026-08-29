from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wikipeople.app import create_app
from wikipeople.config import Settings
from wikipeople.displaypolicy import parse_display_policy_page, resolve_target_wikis, sync_wiki
from wikipeople.errors import RetryableUpstreamError
from wikipeople.models import utcnow
from wikipeople.policy import (
    AccountStanding,
    DisplayPolicy,
    is_anonymised_account,
    normalize_display_policy,
)
from wikipeople.repository import StandingRecord
from wikipeople.runtime import Runtime, build_runtime

PAGE = """
Cette page décide de ce que WikiPeople affiche sur ce wiki.

== Réglages ==
* contributor-names : show
* sanctioned-accounts : unlink
* anonymised-accounts : label

Voir la [[Discussion Wikipédia:WikiPeople/affichage|page de discussion]].
"""


class FakeMediaWiki:
    def __init__(self, wikitext: str | None, fail: bool = False) -> None:
        self.wikitext = wikitext
        self.fail = fail
        self.asked: list[str] = []

    def get_wikitext(self, _wiki: str, title: str) -> str | None:
        if self.fail:
            raise RetryableUpstreamError("Action API indisponible")
        self.asked.append(title)
        return self.wikitext


# --- reading the page ------------------------------------------------------------


def test_only_bulleted_settings_are_read() -> None:
    """The page has to stay writable by the people who maintain it.

    A heading, the sentence explaining why a value was chosen, and the link to the
    discussion that chose it all live on the same page as the settings, and none of
    them is one.
    """
    assert parse_display_policy_page(PAGE) == {
        "contributor-names": "show",
        "sanctioned-accounts": "unlink",
        "anonymised-accounts": "label",
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("* sanctioned-accounts: hide", {"sanctioned-accounts": "hide"}),
        ("* sanctioned-accounts = hide", {"sanctioned-accounts": "hide"}),
        ("*sanctioned_accounts:HIDE", {"sanctioned-accounts": "hide"}),
        ("** sanctioned accounts : hide", {"sanctioned-accounts": "hide"}),
        ("* <code>sanctioned-accounts</code>: ''hide''", {"sanctioned-accounts": "hide"}),
        ("* sanctioned-accounts: « hide »", {"sanctioned-accounts": "hide"}),
        ("sanctioned-accounts: hide", {}),
        ("* sanctioned-accounts", {}),
        ("* sanctioned-accounts:", {}),
        ("# sanctioned-accounts: hide", {}),
    ],
)
def test_setting_shapes(line: str, expected: dict[str, str]) -> None:
    assert parse_display_policy_page(line) == expected


def test_a_setting_hidden_in_a_comment_is_not_a_setting() -> None:
    """Commenting a line out is how a wiki editor undoes a setting."""
    assert parse_display_policy_page("<!--\n* contributor-names: hide\n-->\n* x: y") == {"x": "y"}


def test_the_first_line_wins_like_the_opt_out_list() -> None:
    """Two pages a community maintains should not have two rules for a repeat."""
    assert parse_display_policy_page("* a: un\n* a: deux") == {"a": "un"}


def test_a_value_outside_the_list_is_not_obeyed_half_way() -> None:
    """A typo must not become a fourth behaviour nobody wrote.

    The gadget's own configuration page has applied this rule since the start; the
    reason is the same on both sides. A typo leaves the setting at its default, where
    it is visible as "the page did not take effect" rather than as new behaviour.
    """
    stated = {"sanctioned-accounts": "unlnik", "anonymised-accounts": "", "invented-key": "hide"}
    assert normalize_display_policy(stated) == DisplayPolicy()


def test_an_empty_page_is_the_defaults() -> None:
    assert normalize_display_policy(parse_display_policy_page("")) == DisplayPolicy()


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


STARTER_PAGE = Path(__file__).resolve().parents[1] / "docs/onwiki/display.fr.wiki"


def test_the_starter_page_states_exactly_the_defaults() -> None:
    """The copy communities paste must change nothing on arrival.

    It is dense with the syntax it documents — bulleted rules, worked examples of the
    values nobody chose — and any of that read as a setting would change what a wiki
    shows without anyone having decided it.
    """
    stated = parse_display_policy_page(STARTER_PAGE.read_text(encoding="utf-8"))
    assert stated == {
        "contributor-names": "show",
        "sanctioned-accounts": "hide",
        "anonymised-accounts": "label",
    }
    assert normalize_display_policy(stated) == DisplayPolicy()


# --- the sync job ----------------------------------------------------------------


def _runtime(tmp_path: Path, name: str, **overrides: object) -> Runtime:
    settings = replace(
        Settings.from_env(),
        database_url=f"sqlite:///{tmp_path / name}",
        algorithm_version="test-display",
        display_policy_page="Project:WikiPeople/display",
        **overrides,  # type: ignore[arg-type]
    )
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


def test_a_wiki_that_changes_its_mind_is_served_differently_within_the_hour(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, "sync.db")
    mediawiki = FakeMediaWiki(PAGE)

    policy, changed = sync_wiki(runtime, mediawiki, "frwiki")  # type: ignore[arg-type]
    assert mediawiki.asked == ["Project:WikiPeople/display"]
    assert (policy.sanctioned_accounts, changed) == ("unlink", True)
    assert runtime.repository.display_policy("frwiki").sanctioned_accounts == "unlink"

    # Reading the same page again is not a change, so the job stays quiet.
    assert sync_wiki(runtime, FakeMediaWiki(PAGE), "frwiki")[1] is False  # type: ignore[arg-type]


def test_no_page_at_all_means_the_wiki_decided_nothing(tmp_path: Path) -> None:
    """A missing page is an answer. Unlike a failed read, it is one the API can act on."""
    runtime = _runtime(tmp_path, "missing.db")
    policy, _ = sync_wiki(runtime, FakeMediaWiki(None), "frwiki")  # type: ignore[arg-type]
    assert policy == DisplayPolicy()


def test_an_unreachable_wiki_keeps_the_policy_it_had(tmp_path: Path) -> None:
    """An empty page means "we changed our minds". A network error does not.

    Conflating the two would turn one bad minute at the Action API into every wiki on
    the tool silently reverting to the operator's defaults, which is the exact decision
    this page exists to take away from the operator.
    """
    runtime = _runtime(tmp_path, "outage.db")
    sync_wiki(runtime, FakeMediaWiki("* contributor-names: hide"), "frwiki")  # type: ignore[arg-type]

    with pytest.raises(RetryableUpstreamError):
        sync_wiki(runtime, FakeMediaWiki(None, fail=True), "frwiki")  # type: ignore[arg-type]

    assert runtime.repository.display_policy("frwiki").show_contributor_names is False


def test_a_dry_run_reports_without_changing_what_is_served(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "dry.db")
    policy, changed = sync_wiki(runtime, FakeMediaWiki(PAGE), "frwiki", dry_run=True)  # type: ignore[arg-type]
    assert (policy.sanctioned_accounts, changed) == ("unlink", False)
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


def _save(runtime: Runtime) -> None:
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": 100,
            "revision_id": 200,
            "algorithm_version": "test-display",
            "title": "Exemple",
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


def _served(runtime: Runtime) -> dict:
    with TestClient(create_app(runtime)) as client:
        response = client.get("/v2/frwiki/pages/100?revision_id=200")
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
    sync_wiki(runtime, FakeMediaWiki("* contributor-names: hide"), "frwiki")  # type: ignore[arg-type]

    payload = _served(runtime)
    assert payload["contributors"] == []
    assert payload["opted_out"] is False
    assert (payload["distinct_contributors"], payload["other_contributors"]) == (47, 47)
    # Nothing was recomputed and nothing was deleted, so the wiki can change its mind.
    assert runtime.repository.get_latest_result("frwiki", 100, "test-display").contributors


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("hide", [("Alice", "unlink"), ("Renamed user 4501e2a3c", "label")]),
        (
            "unlink",
            [
                ("Alice", "unlink"),
                ("Renamed user 4501e2a3c", "label"),
                ("Banni", "unlink"),
            ],
        ),
        (
            "link",
            [("Alice", "unlink"), ("Renamed user 4501e2a3c", "label"), ("Banni", "link")],
        ),
    ],
)
def test_a_wiki_chooses_how_far_a_sanctioned_account_is_masked(
    tmp_path: Path, setting: str, expected: list[tuple[str, str]]
) -> None:
    runtime = _runtime(tmp_path, f"serve-{setting}.db")
    _save(runtime)
    sync_wiki(runtime, FakeMediaWiki(f"* sanctioned-accounts: {setting}"), "frwiki")  # type: ignore[arg-type]

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
    sync_wiki(runtime, FakeMediaWiki("* sanctioned-accounts: unlink"), "frwiki")  # type: ignore[arg-type]

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
    sync_wiki(runtime, FakeMediaWiki("* sanctioned-accounts: hide"), "frwiki")  # type: ignore[arg-type]

    assert [c["username"] for c in _served(runtime)["contributors"]] == [
        "Alice",
        "Renamed user 4501e2a3c",
        "Banni",
    ]
