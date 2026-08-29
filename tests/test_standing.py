from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wikipeople.app import create_app
from wikipeople.clients import GlobalUserInfo
from wikipeople.config import Settings, _int_by_wiki
from wikipeople.errors import RetryableUpstreamError
from wikipeople.models import utcnow
from wikipeople.policy import AccountStanding, is_nameable_account
from wikipeople.repository import StandingRecord
from wikipeople.runtime import build_runtime
from wikipeople.standing import sync_wiki

NOW = utcnow()
DAY = timedelta(days=1)
NINETY_DAYS = int(timedelta(days=90).total_seconds())


def standing(**overrides: object) -> AccountStanding:
    return replace(AccountStanding(user_id=1, username="Alice"), **overrides)  # type: ignore[arg-type]


class FakeMediaWiki:
    """Stands in for the Action API. Records what was asked, so the job's rationing shows."""

    def __init__(
        self,
        blocks: dict[int, AccountStanding] | None = None,
        locked: set[str] | None = None,
        lock_reasons: dict[str, str] | None = None,
        fail_blocks: bool = False,
        fail_locks: set[str] | None = None,
        user_pages: set[str] | None = None,
        fail_user_pages: bool = False,
    ) -> None:
        self.blocks = blocks or {}
        self.locked = locked or set()
        self.lock_reasons = lock_reasons or {}
        self.fail_blocks = fail_blocks
        self.fail_locks = fail_locks or set()
        # None means "every account has one", which is the pre-existing behaviour and
        # keeps the tests below about blocks and locks rather than about links.
        self.user_pages = user_pages
        self.fail_user_pages = fail_user_pages
        self.lock_lookups: list[str] = []
        self.reason_lookups: list[str] = []
        self.user_page_lookups: list[str] = []

    def fetch_standing(self, _wiki: str, user_ids: list[int]) -> dict[int, AccountStanding]:
        if self.fail_blocks:
            raise RetryableUpstreamError("Action API indisponible")
        return {
            user_id: self.blocks.get(
                user_id, AccountStanding(user_id=user_id, username=f"U{user_id}")
            )
            for user_id in user_ids
        }

    def existing_user_pages(self, _wiki: str, usernames: list[str]) -> set[str]:
        if self.fail_user_pages:
            raise RetryableUpstreamError("Action API indisponible")
        self.user_page_lookups.extend(usernames)
        return set(usernames) if self.user_pages is None else set(usernames) & self.user_pages

    def global_user_info(self, _wiki: str, username: str) -> GlobalUserInfo:
        self.lock_lookups.append(username)
        if username in self.fail_locks:
            raise RetryableUpstreamError("CentralAuth indisponible")
        return GlobalUserInfo(groups=frozenset(), locked=username in self.locked)

    def global_lock_reason(self, username: str) -> str | None:
        self.reason_lookups.append(username)
        return self.lock_reasons.get(username, "Long-term abuse")


# --- the rule itself -------------------------------------------------------------


def test_an_account_nobody_has_looked_at_is_nameable() -> None:
    """Not knowing is not the same as knowing there is nothing.

    A sync that has never run, or a wiki added this morning, leaves every account
    absent from the table. Reading absence as "sanctioned" would blank the names off a
    whole wiki the first time the job was late.
    """
    assert is_nameable_account(None, NINETY_DAYS, NOW) is True


def test_an_unblocked_account_is_nameable() -> None:
    assert is_nameable_account(standing(), NINETY_DAYS, NOW) is True


@pytest.mark.parametrize("days", [1, 7, 30, 89, 90])
def test_a_block_no_longer_than_the_threshold_leaves_the_name(days: int) -> None:
    """The rule is about lasting exclusion, not about sanction.

    A week off is a normal editorial event and says nothing about the text the account
    wrote. The user asked for "≤ x is shown", so ninety days exactly is still shown.
    """
    account = standing(blocked_at=NOW, block_expires_at=NOW + days * DAY)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is True


@pytest.mark.parametrize("days", [91, 180, 365])
def test_a_block_longer_than_the_threshold_withholds_the_name(days: int) -> None:
    account = standing(blocked_at=NOW, block_expires_at=NOW + days * DAY)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is False


def test_an_indefinite_block_withholds_the_name() -> None:
    """No expiry is the opposite of no block, and both are spelled with a None.

    `blocked_at` is what tells them apart. Getting this backwards would either name
    every banned account or hide every clean one.
    """
    assert is_nameable_account(standing(blocked_at=NOW), NINETY_DAYS, NOW) is False


def test_a_partial_block_leaves_the_name() -> None:
    """A block scoped to one page is a targeted remedy, not a ban.

    Someone barred from a single article can be a respected contributor everywhere
    else, and duration says nothing about which of the two it is.
    """
    account = standing(blocked_at=NOW, block_expires_at=None, block_partial=True)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is True


def test_a_block_that_has_since_expired_leaves_the_name() -> None:
    """The row can outlive the block. MediaWiki keeps no trace of an expired one, so
    the next sync will drop it; until then the serve path must not apply a sanction the
    wiki has stopped applying."""
    account = standing(blocked_at=NOW - 400 * DAY, block_expires_at=NOW - DAY)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is True


@pytest.mark.parametrize(
    "reason",
    [
        "Long-term abuse, per [[:m:Special:Permalink/30919000]]",
        "long-term abuse",
        "Spam-only account",
        "Lock evasion: Malu Paul",
        "Cross-wiki abuse",
        "Globally or WMF banned user",
        "sockpuppetry",
    ],
)
def test_a_lock_imposed_as_a_sanction_withholds_the_name(reason: str) -> None:
    """A sanctioning lock has no duration to compare against, and it is not local.

    It is the case a local block check cannot see: an account locked for cross-wiki
    abuse may have no block at all on the wiki where it wrote the article.
    """
    account = standing(globally_locked=True, lock_reason=reason)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is False
    assert is_nameable_account(account, 10**9, NOW) is False


@pytest.mark.parametrize(
    "reason",
    [
        "Deceased user",
        "Deceased Wikipedian",
        "per [[m:Special:Permalink/28773743|request]]: Deceased user",
        "reported as deceased; see [[Special:Permalink/17723815]]",
        "This user vanish request has been automatically processed",
        "per [[m:Special:GlobalRenameQueue/request/18100]]",
        "Compromised account",
        "Requested by the user",
    ],
)
def test_a_lock_imposed_as_a_courtesy_leaves_the_name(reason: str) -> None:
    """Stewards use one lock mechanism for opposite purposes.

    They lock abusers, and they lock the accounts of editors who have died, who have
    vanished, or whose account was compromised. The first production run of this rule
    read the flag alone and withheld the credit of five deceased Wikipedians — the exact
    opposite of what it is for. These are their real lock reasons.
    """
    account = standing(globally_locked=True, lock_reason=reason)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is True
    assert is_nameable_account(account, 0, NOW) is True


@pytest.mark.parametrize(
    "reason",
    [
        "Bloqué à sa demande (blocked at own request)",
        "Blocage à la demande du contributeur",
        "À sa demande, cf. ma Pdd",
        "à sa demande hier sur [[WP:RA]]",
        "requested on my talk page, user has retired; no prejudice against unblocking",
        "Compromised account",
        "Compte compromis - l'utilisateur indique avoir perdu son mot de passe",
        "[[WP:Vandalism|Vandalism]]: compromised?",
        "[[WP:RTV|RTV]] enforcement",
        "по просьбе участника",
    ],
)
def test_a_block_imposed_as_a_courtesy_leaves_the_name(reason: str) -> None:
    """The block-side mirror of the memorial lock, and the same mistake.

    Every string here is a real block reason carried by an account WikiPeople names.
    Someone blocked at their own request asked to be stopped from editing, which is the
    opposite of a wiki withdrawing its trust; someone whose password was stolen did
    nothing at all. Neither has forfeited the credit for what they wrote.
    """
    account = standing(blocked_at=NOW - DAY, block_reason=reason)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is True
    assert is_nameable_account(account, 0, NOW) is True


@pytest.mark.parametrize(
    "reason",
    [
        "[[WP:Sockpuppetry|Abusing multiple accounts]]",
        "Long-term abuse",
        "contributeur multi-problématique : [[Wikipédia:Bulletin des administrateurs]]",
        "Vandalism-only account",
        "determined to not be compromised. restore TPA per request",
        "",
    ],
)
def test_a_block_imposed_as_a_sanction_still_withholds_the_name(reason: str) -> None:
    """A block withholds unless its reason proves otherwise, which is the opposite
    default to the lock rule and matches how the two mechanisms are actually used: of
    the accounts withheld on the first live run, 536 of 556 were sanctions.

    The fifth string is why the negation guard exists. Reading "not compromised" as a
    compromise would name someone their wiki deliberately blocked.
    """
    account = standing(blocked_at=NOW - 400 * DAY, block_reason=reason)
    assert is_nameable_account(account, NINETY_DAYS, NOW) is False


def test_a_courtesy_block_does_not_override_a_sanctioning_lock() -> None:
    """The lock is the movement speaking; one wiki's kindness does not answer it."""
    account = standing(
        blocked_at=NOW - DAY,
        block_reason="Bloqué à sa demande",
        globally_locked=True,
        lock_reason="Long-term abuse",
    )
    assert is_nameable_account(account, NINETY_DAYS, NOW) is False


def test_a_lock_whose_reason_is_unknown_leaves_the_name() -> None:
    """Absence of data is not a finding, here as everywhere else in this module.

    An unreadable log, a steward's unfamiliar phrasing, or a row written before reasons
    were stored all arrive as no reason at all. Failing towards naming costs a sanction
    missed; failing the other way costs someone erased for no reason anyone can state.
    """
    assert is_nameable_account(standing(globally_locked=True), NINETY_DAYS, NOW) is True
    assert is_nameable_account(standing(globally_locked=True, lock_reason=""), NINETY_DAYS, NOW)


def test_a_courtesy_lock_does_not_rescue_an_account_its_wiki_has_banned() -> None:
    """The two rules are independent. A deceased editor who was also indefinitely
    blocked locally stays withheld on the strength of the block alone."""
    account = standing(blocked_at=NOW - DAY, globally_locked=True, lock_reason="Deceased user")
    assert is_nameable_account(account, NINETY_DAYS, NOW) is False


def test_a_threshold_of_zero_withholds_every_blocked_name() -> None:
    account = standing(blocked_at=NOW, block_expires_at=NOW + timedelta(hours=1))
    assert is_nameable_account(account, 0, NOW) is False
    assert is_nameable_account(standing(), 0, NOW) is True


# --- configuration ---------------------------------------------------------------


def test_the_rule_is_on_by_default_at_ninety_days() -> None:
    settings = Settings.from_env()
    assert settings.hide_sanctioned_contributors is True
    assert settings.max_visible_block_seconds == NINETY_DAYS


def test_a_wiki_may_be_given_its_own_threshold() -> None:
    """What counts as a serious sanction is a community's question, not an operator's."""
    settings = replace(
        Settings.from_env(),
        max_visible_block_seconds=NINETY_DAYS,
        max_visible_block_seconds_by_wiki=(("frwiki", 0), ("enwiki", 3600)),
    )
    assert settings.max_visible_block_seconds_for("frwiki") == 0
    assert settings.max_visible_block_seconds_for("enwiki") == 3600
    assert settings.max_visible_block_seconds_for("dewiki") == NINETY_DAYS


def test_a_malformed_override_is_dropped_rather_than_fatal() -> None:
    """Settings are read at import time in the web process. A typo in one wiki's
    override must cost that wiki its override, not the service its startup."""
    assert _int_by_wiki("frwiki:600,broken,enwiki:,dewiki:abc,ruwiki:0") == (
        ("frwiki", 600),
        ("ruwiki", 0),
    )


# --- the serve path --------------------------------------------------------------


def save(runtime, page_id: int, contributors: list[dict]) -> None:
    runtime.repository.save_result(
        {
            "wiki": "frwiki",
            "page_id": page_id,
            "revision_id": 200,
            "algorithm_version": "test-standing",
            "title": "Exemple",
            "metric": "test-metric",
            "contributors": contributors,
            "distinct_contributors": 47,
            "count_limited": False,
            "countable_tokens": 900,
            "wikiwho_revision_id": 200,
            "computed_at": utcnow(),
        }
    )


def make_runtime(tmp_path: Path, name: str, **overrides):
    settings = replace(
        Settings.from_env(),
        database_url=f"sqlite:///{tmp_path / name}",
        algorithm_version="test-standing",
        **overrides,
    )
    runtime = build_runtime(settings)
    runtime.database.create_schema()
    return runtime


CONTRIBUTORS = [
    {"user_id": 1, "username": "Alice", "token_count": 300, "share": 0.30},
    {"user_id": 2, "username": "Banni", "token_count": 200, "share": 0.20},
    {"user_id": 3, "username": "Claire", "token_count": 100, "share": 0.10},
]


def test_a_banned_contributor_is_dropped_from_both_endpoints(tmp_path: Path) -> None:
    """v1 names an exact revision, so it is the obvious way round a serve-time rule.

    Both versions build their answer through the same function precisely so that asking
    for the revision instead of the page is not a way to get the name back.
    """
    runtime = make_runtime(tmp_path, "serve.db")
    app = create_app(runtime)
    save(runtime, 100, CONTRIBUTORS)
    runtime.repository.replace_standing(
        "frwiki",
        [
            StandingRecord(
                standing=AccountStanding(user_id=2, username="Banni", blocked_at=utcnow()),
                lock_checked_at=utcnow(),
            )
        ],
    )

    with TestClient(app) as client:
        for url in (
            "/v1/frwiki/pages/100?revision_id=200",
            "/v2/frwiki/pages/100?revision_id=200",
        ):
            payload = client.get(url).json()
            assert [c["username"] for c in payload["contributors"]] == ["Alice", "Claire"]
            # The person still contributed. Only the credit line changes, so the count
            # is untouched and the missing name moves into the remainder.
            assert payload["distinct_contributors"] == 47
            assert payload["other_contributors"] == 45
            # No flag. A machine-readable "one of this article's main authors is
            # banned" would be a worse disclosure than the credit it replaced.
            assert payload["opted_out"] is False
            assert "sanctioned" not in payload


def test_switching_the_rule_off_restores_the_name(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, "off.db", hide_sanctioned_contributors=False)
    save(runtime, 100, CONTRIBUTORS)
    runtime.repository.replace_standing(
        "frwiki",
        [
            StandingRecord(
                standing=AccountStanding(user_id=2, username="Banni", blocked_at=utcnow()),
                lock_checked_at=utcnow(),
            )
        ],
    )
    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()
    assert [c["username"] for c in payload["contributors"]] == ["Alice", "Banni", "Claire"]


def test_the_stored_row_is_never_touched(tmp_path: Path) -> None:
    """Nothing is recomputed and nothing is deleted, which is what makes the rule
    reversible. The names are public page history; only the answer changes."""
    runtime = make_runtime(tmp_path, "intact.db")
    save(runtime, 100, CONTRIBUTORS)
    runtime.repository.replace_standing(
        "frwiki",
        [
            StandingRecord(
                standing=AccountStanding(user_id=2, username="Banni", blocked_at=utcnow()),
                lock_checked_at=utcnow(),
            )
        ],
    )
    with TestClient(create_app(runtime)) as client:
        client.get("/v2/frwiki/pages/100?revision_id=200")

    stored = runtime.repository.get_latest_result("frwiki", 100, "test-standing")
    assert [c["username"] for c in stored.contributors] == ["Alice", "Banni", "Claire"]


def test_withholding_a_name_changes_the_etag(tmp_path: Path) -> None:
    """ADR-0007 hashes the body, which is what makes this rule enforceable at all: a
    reader holding the old answer cannot revalidate it once a name is withheld."""
    runtime = make_runtime(tmp_path, "etag.db")
    save(runtime, 100, CONTRIBUTORS)
    with TestClient(create_app(runtime)) as client:
        before = client.get("/v2/frwiki/pages/100?revision_id=200").headers["etag"]
        runtime.repository.replace_standing(
            "frwiki",
            [
                StandingRecord(
                    standing=AccountStanding(user_id=2, username="Banni", blocked_at=utcnow()),
                    lock_checked_at=utcnow(),
                )
            ],
        )
        after = client.get("/v2/frwiki/pages/100?revision_id=200").headers["etag"]
        assert before != after
        stale = client.get(
            "/v2/frwiki/pages/100?revision_id=200", headers={"If-None-Match": before}
        )
        assert stale.status_code == 200

        # And lifting it returns the answer to exactly what it was.
        runtime.repository.replace_standing("frwiki", [])
        assert client.get("/v2/frwiki/pages/100?revision_id=200").headers["etag"] == before


def test_an_opted_out_page_costs_no_standing_lookup(tmp_path: Path) -> None:
    """The names are already gone, so there is nothing left to filter."""
    runtime = make_runtime(tmp_path, "optout.db")
    save(runtime, 100, CONTRIBUTORS)
    from wikipeople.repository import OptOutEntry

    runtime.repository.replace_optout(
        "frwiki", [OptOutEntry(page_id=100, title="Exemple", source="page")]
    )
    calls: list[tuple] = []
    original = runtime.repository.contributor_facts
    runtime.repository.contributor_facts = lambda *a: calls.append(a) or original(*a)  # type: ignore[method-assign]

    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()
    assert payload["contributors"] == []
    assert payload["opted_out"] is True
    assert calls == []


# --- the sync job ----------------------------------------------------------------


def test_the_job_tracks_only_the_accounts_results_actually_name(tmp_path: Path) -> None:
    """Not the wiki's whole block log. The top three of popular articles are the same
    prolific editors over and over, so this set is thousands, not millions."""
    runtime = make_runtime(tmp_path, "sync.db")
    save(runtime, 100, CONTRIBUTORS)
    save(runtime, 101, [{"user_id": 3, "username": "Claire", "token_count": 5, "share": 0.1}])

    mediawiki = FakeMediaWiki(locked={"U2"})
    tracked, added, removed, checked = sync_wiki(runtime, mediawiki, "frwiki")

    assert (tracked, added, removed, checked) == (3, 3, 0, 3)
    assert sorted(mediawiki.lock_lookups) == ["U1", "U2", "U3"]
    stored = runtime.repository.standing_records("frwiki")
    assert stored[2].standing.globally_locked is True
    assert stored[1].standing.globally_locked is False
    # Only the locked account costs the second request. The reason is what separates a
    # ban from a memorial, and asking about it for every account would double the price
    # of the most expensive lookup the tool makes.
    assert mediawiki.reason_lookups == ["U2"]
    assert stored[2].standing.lock_reason == "Long-term abuse"
    assert stored[1].standing.lock_reason is None


def test_a_courtesy_locked_contributor_keeps_their_name_end_to_end(tmp_path: Path) -> None:
    """The production defect, from the job through to the response.

    The first live run of this rule withheld five deceased Wikipedians on enwiki, none
    of whom carried any block at all. The flag was true and the reason went unread.
    """
    runtime = make_runtime(tmp_path, "memorial.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(
        runtime,
        FakeMediaWiki(locked={"U2"}, lock_reasons={"U2": "Deceased user"}),
        "frwiki",
    )

    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()

    assert [c["username"] for c in payload["contributors"]] == ["Alice", "Banni", "Claire"]
    assert payload["other_contributors"] == 44


def test_a_courtesy_blocked_contributor_keeps_their_name_end_to_end(tmp_path: Path) -> None:
    """The block-side twin, and the reason it is tested through the job.

    The rule itself was right the first time; the sync built its record without the
    reason, so every stored row said None and every courtesy read as a sanction. A test
    of the policy alone cannot see that. This one runs the whole path.
    """
    runtime = make_runtime(tmp_path, "courtesy-block.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(
        runtime,
        FakeMediaWiki(
            blocks={
                2: AccountStanding(
                    user_id=2,
                    username="Banni",
                    blocked_at=utcnow(),
                    block_reason="Bloqué à sa demande (blocked at own request)",
                )
            }
        ),
        "frwiki",
    )

    assert runtime.repository.standing_records("frwiki")[2].standing.block_reason is not None

    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()

    assert [c["username"] for c in payload["contributors"]] == ["Alice", "Banni", "Claire"]


def test_an_account_no_result_names_any_more_is_dropped(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, "drop.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(runtime, FakeMediaWiki(), "frwiki")

    save(runtime, 100, [CONTRIBUTORS[0]])
    tracked, _, removed, _ = sync_wiki(runtime, FakeMediaWiki(), "frwiki")
    assert (tracked, removed) == (1, 2)
    assert list(runtime.repository.standing_records("frwiki")) == [1]


def test_an_unreachable_wiki_keeps_its_previous_standings(tmp_path: Path) -> None:
    """A network failure is not a general amnesty, just as it is not a mass opt-out."""
    runtime = make_runtime(tmp_path, "fail.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(runtime, FakeMediaWiki(locked={"U2"}), "frwiki")

    with pytest.raises(RetryableUpstreamError):
        sync_wiki(runtime, FakeMediaWiki(fail_blocks=True), "frwiki")

    assert runtime.repository.standing_records("frwiki")[2].standing.globally_locked is True


def test_lock_checks_are_rationed_and_rotate(tmp_path: Path) -> None:
    """CentralAuth answers about one account per request, so the lock pass is capped.

    Blocks are still refreshed for everyone on every run; only locks queue, and an
    account already checked waits behind one that never was.
    """
    runtime = make_runtime(tmp_path, "ration.db", standing_lock_checks_per_run=2)
    save(runtime, 100, CONTRIBUTORS)

    first = FakeMediaWiki()
    assert sync_wiki(runtime, first, "frwiki")[3] == 2
    second = FakeMediaWiki()
    sync_wiki(runtime, second, "frwiki")
    # The account left out of the first run is the one asked about in the second.
    assert set(first.lock_lookups).isdisjoint(second.lock_lookups)
    assert len(second.lock_lookups) == 1


def test_a_checked_account_is_not_asked_about_again_until_it_goes_stale(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, "recheck.db", standing_lock_recheck_seconds=86400)
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(runtime, FakeMediaWiki(), "frwiki")

    again = FakeMediaWiki()
    assert sync_wiki(runtime, again, "frwiki")[3] == 0
    assert again.lock_lookups == []


def test_a_failed_lock_check_keeps_the_previous_answer_and_retries(tmp_path: Path) -> None:
    """One account's CentralAuth call failing must not cost the run its block pass, and
    leaving the timestamp alone puts that account back at the front of the queue."""
    runtime = make_runtime(tmp_path, "lockfail.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(runtime, FakeMediaWiki(locked={"U2"}), "frwiki")

    sync_wiki(runtime, FakeMediaWiki(fail_locks={"U2"}), "frwiki")  # nothing due yet
    runtime.repository.replace_standing(
        "frwiki",
        [
            replace(record, lock_checked_at=None)
            for record in runtime.repository.standing_records("frwiki").values()
        ],
    )
    failing = FakeMediaWiki(fail_locks={"U2"})
    sync_wiki(runtime, failing, "frwiki")

    stored = runtime.repository.standing_records("frwiki")
    assert stored[2].standing.globally_locked is True
    assert stored[2].lock_checked_at is None
    assert stored[1].lock_checked_at is not None


def test_a_lifted_block_disappears_because_the_table_is_replaced(tmp_path: Path) -> None:
    """MediaWiki reports only active blocks: a lapsed one comes back as nothing said.

    Merging would keep the stale row for ever, which is why the sync replaces rather
    than updates.
    """
    runtime = make_runtime(tmp_path, "lifted.db")
    save(runtime, 100, CONTRIBUTORS)
    blocked = AccountStanding(user_id=2, username="U2", blocked_at=utcnow())
    sync_wiki(runtime, FakeMediaWiki(blocks={2: blocked}), "frwiki")
    assert runtime.repository.standing_records("frwiki")[2].standing.blocked_at is not None

    sync_wiki(runtime, FakeMediaWiki(), "frwiki")
    assert runtime.repository.standing_records("frwiki")[2].standing.blocked_at is None


def test_stats_report_what_is_tracked_not_what_is_hidden(tmp_path: Path) -> None:
    """How many names are withheld depends on the threshold, which is applied per
    response. Reporting it here would be a second copy of the rule, free to drift."""
    runtime = make_runtime(tmp_path, "stats.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(
        runtime,
        FakeMediaWiki(blocks={2: AccountStanding(2, "U2", utcnow())}, locked={"U3"}),
        "frwiki",
    )

    with TestClient(create_app(runtime)) as client:
        stats = client.get("/v1/stats").json()
    assert stats["standing"] == {"frwiki": {"tracked": 3, "blocked": 1, "locked": 1}}


def test_a_missing_user_page_is_recorded_so_the_credit_can_stop_promising_one(
    tmp_path: Path,
) -> None:
    """The answer belongs on the server: the gadget asking would be a request per view.

    That extra local API call on every article is exactly the cost the wiki objected
    to, and the same fifty-titles-per-request pass that reads the blocks answers this
    for free.
    """
    runtime = make_runtime(tmp_path, "userpage.db")
    save(runtime, 100, CONTRIBUTORS)
    mediawiki = FakeMediaWiki(user_pages={"U1", "U3"})
    sync_wiki(runtime, mediawiki, "frwiki")

    assert sorted(mediawiki.user_page_lookups) == ["U1", "U2", "U3"]
    stored = runtime.repository.standing_records("frwiki")
    assert stored[2].has_user_page is False
    assert stored[1].has_user_page is True

    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()
    assert [(c["username"], c["display"]) for c in payload["contributors"]] == [
        ("Alice", "link"),
        ("Banni", "unlink"),
        ("Claire", "link"),
    ]


def test_a_failed_user_page_pass_leaves_the_links_exactly_as_they_were(tmp_path: Path) -> None:
    """Not knowing is not knowing there is nothing.

    Reading a failed request as "no user page" would unlink every name on the wiki the
    first time the Action API had a bad minute, and the block pass would still have
    succeeded, so the run must not be lost either.
    """
    runtime = make_runtime(tmp_path, "userpage-fail.db")
    save(runtime, 100, CONTRIBUTORS)
    sync_wiki(runtime, FakeMediaWiki(user_pages={"U1"}), "frwiki")

    sync_wiki(runtime, FakeMediaWiki(user_pages={"U1"}, fail_user_pages=True), "frwiki")

    stored = runtime.repository.standing_records("frwiki")
    assert stored[1].has_user_page is True
    assert stored[2].has_user_page is False


def test_an_account_nobody_has_looked_at_keeps_its_link(tmp_path: Path) -> None:
    """The first response of a wiki added this morning must not be a wall of plain text."""
    runtime = make_runtime(tmp_path, "userpage-unknown.db")
    save(runtime, 100, CONTRIBUTORS)
    with TestClient(create_app(runtime)) as client:
        payload = client.get("/v2/frwiki/pages/100?revision_id=200").json()
    assert {c["display"] for c in payload["contributors"]} == {"link"}
