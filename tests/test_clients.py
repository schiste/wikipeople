import json
from datetime import date, datetime

import httpx
import pytest

from wikipeople.clients import AnalyticsClient, MediaWikiClient, WikiWhoClient
from wikipeople.errors import PermanentDataError, ResponseTooLargeError, RetryableUpstreamError


def make_client(payload: dict, max_bytes: int = 10_000) -> WikiWhoClient:
    client = WikiWhoClient("https://example.test", "tests", 1, max_bytes)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def make_failing_client(status: int) -> WikiWhoClient:
    client = WikiWhoClient("https://example.test", "tests", 1, 10_000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b'{"Error":"nope"}')

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_wikiwho_extracts_tokens_from_real_nested_shape() -> None:
    client = make_client(
        {
            "success": True,
            "revisions": [
                {
                    "200": {
                        "tokens": [
                            {"str": "Bonjour", "editor": "1"},
                            {"str": "monde", "editor": "2"},
                        ]
                    }
                }
            ],
        }
    )

    assert client.fetch_revision("frwiki", 200)[0]["editor"] == "1"
    client.close()


def test_wikiwho_response_size_is_bounded() -> None:
    client = make_client(
        {"success": True, "revisions": [{"200": {"tokens": ["x" * 100]}}]},
        max_bytes=20,
    )

    with pytest.raises(ResponseTooLargeError):
        client.fetch_revision("frwiki", 200)
    client.close()


def test_a_refused_revision_is_permanent_rather_than_retried() -> None:
    """WikiWho answers 400 to state a fact about the page, and facts do not heal.

    The two forms it sends are a rejected namespace and a revision it has no article
    for. Retrying either burns the whole thirteen-second chain to arrive at the same
    answer, and leaves the job marked retryable so it is revived again later.
    """
    client = make_failing_client(400)

    with pytest.raises(PermanentDataError):
        client.fetch_revision("frwiki", 200)
    client.close()


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_a_busy_or_broken_wikiwho_is_still_worth_retrying(status: int) -> None:
    """Only 400 changed meaning: everything else remains a hiccup to wait out."""
    client = make_failing_client(status)

    with pytest.raises(RetryableUpstreamError):
        client.fetch_revision("frwiki", 200)
    client.close()


def test_global_groups_are_read_from_centralauth_and_asked_for_once() -> None:
    client = MediaWikiClient("tests", 1)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["guiuser"])
        return httpx.Response(
            200,
            json={"query": {"globaluserinfo": {"name": "Addbot", "groups": ["local-bot"]}}},
        )

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert client.global_groups("frwiki", "Addbot") == frozenset({"local-bot"})
    assert client.global_groups("frwiki", "Addbot") == frozenset({"local-bot"})
    assert asked == ["Addbot"]
    client.close()


def test_an_account_centralauth_does_not_know_has_no_global_groups() -> None:
    client = MediaWikiClient("tests", 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {"globaluserinfo": {"missing": True}}})

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert client.global_groups("frwiki", "Inconnu") == frozenset()
    client.close()


def test_unpublished_pageview_day_returns_none() -> None:
    client = AnalyticsClient("tests", 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"title": "Not Found"})

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert client.top_pages("frwiki", date(2026, 8, 15)) is None
    client.close()


def make_category_client(batches: list[dict]) -> tuple[MediaWikiClient, list[httpx.Request]]:
    client = MediaWikiClient("tests", 1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=batches[len(requests) - 1])

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, requests


def members_batch(start: int, count: int, continuation: str | None = None) -> dict:
    batch: dict = {
        "query": {
            "categorymembers": [
                {"pageid": page_id, "title": f"Article {page_id}"}
                for page_id in range(start, start + count)
            ]
        }
    }
    if continuation is not None:
        batch["continue"] = {"cmcontinue": continuation}
    return batch


def test_a_category_larger_than_the_cap_is_cut_and_reported() -> None:
    """One batch can overshoot the cap and still be the last one.

    `cmlimit=max` hands over as many members as the wiki will give at once, so a
    category of 253 arrives complete in a single answer with no continuation token.
    Reading "no continuation" as "it fitted" would return the whole category above a
    cap of 10 and report it as untruncated.
    """
    client, requests = make_category_client([members_batch(1, 253)])

    members, truncated = client.category_members("frwiki", "Catégorie:Exemple", 10)

    assert len(members) == 10
    assert truncated is True
    assert len(requests) == 1
    client.close()


def test_a_category_that_exactly_fills_the_cap_is_not_called_truncated() -> None:
    client, _requests = make_category_client([members_batch(1, 10)])

    members, truncated = client.category_members("frwiki", "Catégorie:Exemple", 10)

    assert len(members) == 10
    assert truncated is False
    client.close()


def test_a_paginated_category_is_followed_to_its_end() -> None:
    client, requests = make_category_client(
        [members_batch(1, 2, continuation="next"), members_batch(3, 2)]
    )

    members, truncated = client.category_members("frwiki", "Catégorie:Exemple", 100)

    assert [member.page_id for member in members] == [1, 2, 3, 4]
    assert truncated is False
    assert requests[1].url.params["cmcontinue"] == "next"
    client.close()


def test_block_and_lock_arrive_from_the_calls_already_being_made() -> None:
    """Neither fact costs a request of its own.

    `blockinfo` rides on the `list=users` call that already answers about groups, and
    the lock flag is in the CentralAuth response the bot check already fetches. The
    only new cost is that the lock has to be asked for one account at a time, which is
    why the sync rations it.
    """
    client = MediaWikiClient("tests", 1)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if params.get("list") == "users":
            asked.append(params["ususerids"])
            return httpx.Response(
                200,
                json={
                    "query": {
                        "users": [
                            {
                                "userid": 1,
                                "name": "Banni",
                                "blockid": 42,
                                "blockedtimestamp": "2026-08-18T08:41:59Z",
                                "blockexpiry": "infinite",
                                "blockpartial": False,
                            },
                            {
                                "userid": 2,
                                "name": "Puni",
                                "blockid": 43,
                                "blockedtimestamp": "2026-08-01T00:00:00Z",
                                "blockexpiry": "2026-08-08T00:00:00Z",
                                "blockpartial": True,
                            },
                            {"userid": 3, "name": "Propre"},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={"query": {"globaluserinfo": {"name": "Verrouille", "locked": True}}},
        )

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    standings = client.fetch_standing("frwiki", [1, 2, 3])
    assert asked == ["1|2|3"]
    # "infinite" is not a date and must not be read as one: None here means the block
    # never ends, which blocked_at is what distinguishes from "no block at all".
    # Naive UTC, matching what the schema stores (`models.utcnow`).
    assert standings[1].blocked_at == datetime.fromisoformat("2026-08-18T08:41:59")
    assert standings[1].block_expires_at is None
    assert standings[2].block_expires_at == datetime.fromisoformat("2026-08-08T00:00:00")
    assert standings[2].block_partial is True
    assert standings[3].blocked_at is None

    assert client.global_user_info("frwiki", "Verrouille").locked is True
    client.close()


def test_an_account_that_is_not_locked_says_nothing_about_it() -> None:
    """CentralAuth omits the field rather than sending false, so absence is the answer."""
    client = MediaWikiClient("tests", 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"query": {"globaluserinfo": {"name": "Schiste", "groups": []}}}
        )

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    assert client.global_user_info("frwiki", "Schiste").locked is False
    client.close()


def test_resolving_titles_sends_them_in_the_body_not_the_url() -> None:
    """Fifty titles fit the API's limit but not always a URL.

    A batch of Cyrillic titles is well under `titles`'s limit of 50 and still
    overran the server's URL limit once percent-encoded, returning 414 and
    skipping the whole wiki's prewarm. The form-encoded body carries the same
    percent-encoding expansion -- what changes is that the bytes land somewhere
    without the URL's length limit, so this asserts on where they go.
    """
    client, requests = make_category_client(
        [
            {
                "query": {
                    "pages": [
                        {
                            "pageid": 7,
                            "ns": 0,
                            "title": "Толстой",
                            "revisions": [{"revid": 9}],
                        }
                    ]
                }
            }
        ]
    )

    pages = client.resolve_titles("ruwiki", ["Толстой Лев Николаевич писатель" * 3] * 50)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert len(str(requests[0].url)) < 200
    assert "titles=" in requests[0].content.decode()
    assert "titles=" not in str(requests[0].url)
    assert pages[0].page_id == 7
    client.close()


def test_the_title_walk_skips_redirects() -> None:
    """The Action API fallback must exclude redirects too, not just the replica path.

    57% of frwiki's main namespace is redirects and they sort in among the articles,
    so an unfiltered walk spent more than half the WikiWho budget on pages that are
    one line of wikitext pointing elsewhere.
    """
    client, requests = make_category_client([{"query": {"pages": []}}])

    client.all_pages_batch("frwiki", None)

    assert "gapfilterredir=nonredirects" in str(requests[0].url)
    client.close()


def test_get_page_reports_a_redirect() -> None:
    client, _ = make_category_client(
        [
            {
                "query": {
                    "pages": [
                        {
                            "pageid": 17556072,
                            "ns": 0,
                            "title": '"Heroes"',
                            "redirect": True,
                            "revisions": [{"revid": 238456872}],
                        }
                    ]
                }
            }
        ]
    )

    assert client.get_page("frwiki", 17556072).is_redirect is True
    client.close()


def test_page_ids_resolve_to_their_current_revision_fifty_at_a_time() -> None:
    """The demand table stores page ids; what it cannot store is the revision of today."""
    client = MediaWikiClient("tests", 1)
    batches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(httpx.QueryParams(request.content.decode()))
        batches.append(body["pageids"])
        return httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {"pageid": 100, "ns": 0, "title": "France", "revisions": [{"revid": 250}]},
                        {"pageid": 101, "ns": 0, "title": "Supprimée", "missing": True},
                    ]
                }
            },
        )

    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))

    pages = client.resolve_page_ids("frwiki", list(range(100, 160)))

    # Fifty per request is the Action API's limit, not a tuning choice.
    assert [len(batch.split("|")) for batch in batches] == [50, 10]
    # A page deleted since it was read is dropped rather than queued.
    assert [(page.page_id, page.revision_id) for page in pages] == [(100, 250), (100, 250)]
    client.close()
