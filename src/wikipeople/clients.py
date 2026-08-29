from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import islice
from typing import Any
from urllib.parse import quote, unquote

import httpx

from wikipeople.errors import PermanentDataError, ResponseTooLargeError, RetryableUpstreamError
from wikipeople.policy import AccountStanding, ResolvedUser
from wikipeople.sites import SiteResolver

GLOBAL_GROUP_CACHE_SIZE = 4096

# CentralAuth's log of lock and unlock actions lives on Meta and nowhere else. Every
# wiki can be asked whether an account is locked, but only this one can say why.
GLOBAL_ACCOUNT_LOG_HOST = "meta.wikimedia.org"

# Everything MediaWiki says instead of a date when a block does not end.
INDEFINITE_EXPIRIES = frozenset({"infinite", "infinity", "indefinite", "never"})


def _parse_timestamp(value: Any) -> datetime | None:
    """Turn a MediaWiki ISO-8601 timestamp into the naive UTC value the schema stores."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # MediaWiki always sends a zone. If one ever arrives without, reading it as
        # local time would silently shift every block by the server's offset.
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class GlobalUserInfo:
    """The CentralAuth facts about an account, from one request.

    Groups and lock status arrive together, so they are cached together. Asking twice
    for the same account would double the cost of the most expensive lookup the tool
    makes — CentralAuth answers about one account per request.
    """

    groups: frozenset[str]
    locked: bool


@dataclass(frozen=True)
class PageMetadata:
    page_id: int
    revision_id: int
    title: str
    namespace: int
    is_redirect: bool = False


@dataclass(frozen=True)
class TitleInfo:
    """A title as MediaWiki understands it, whether or not the page exists.

    The namespace number is the point of asking. It is what separates an article from a
    category without this code having to know that a category is "Catégorie" here,
    "Kategorie" there and "Категория" elsewhere. Guessing prefixes would work on one
    wiki; asking works on all of them.

    `page_id` is None for a title that does not exist. A redlinked category is still a
    usable instruction, because a category can have members without having a page.
    """

    title: str
    namespace: int
    page_id: int | None


@dataclass(frozen=True)
class CategoryMember:
    page_id: int
    title: str


@dataclass(frozen=True)
class EditorCount:
    count: int
    limited: bool


@dataclass(frozen=True)
class EditorHistory:
    """Per-account edit counts for one page, and whether the whole history was read.

    `complete` is False when the walk stopped at its revision budget. The counts are
    then those of the most recent revisions only, which is not what "most edits" means,
    so the caller is expected to decline to name anyone rather than rank a window.
    """

    counts: Counter[int]
    revisions: int
    complete: bool


USER_NAMESPACE = 2


def _batched(values: Iterable[int], size: int) -> Iterable[list[int]]:
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


class MediaWikiClient:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float,
        resolver: SiteResolver | None = None,
    ) -> None:
        self.resolver = resolver or SiteResolver()
        self._global_users: dict[str, GlobalUserInfo] = {}
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def host(self, wiki: str) -> str:
        return self.resolver.host(wiki)

    def _action(self, wiki: str, params: dict[str, Any], *, method: str = "GET") -> dict[str, Any]:
        return self._request(self.host(wiki), params, method=method)

    def _request(self, host: str, params: dict[str, Any], *, method: str = "GET") -> dict[str, Any]:
        payload = {"format": "json", "formatversion": 2, **params}
        try:
            if method == "POST":
                response = self.client.post(f"https://{host}/w/api.php", data=payload)
            else:
                response = self.client.get(f"https://{host}/w/api.php", params=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RetryableUpstreamError(f"Action API indisponible : {error}") from error
        if "error" in data:
            raise RetryableUpstreamError(f"Erreur Action API : {data['error']}")
        return data

    def get_page(self, wiki: str, page_id: int) -> PageMetadata:
        data = self._action(
            wiki,
            {
                "action": "query",
                "pageids": page_id,
                # `info` costs nothing here and is what carries `redirect`. The worker
                # refuses redirects on the strength of it, which is the only place the
                # refusal can live: the request path may not call MediaWiki.
                "prop": "revisions|info",
                "rvprop": "ids",
            },
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise PermanentDataError(f"Page inexistante : {wiki}/{page_id}")
        page = pages[0]
        revisions = page.get("revisions", [])
        if not revisions:
            raise PermanentDataError(f"Page sans révision : {wiki}/{page_id}")
        return PageMetadata(
            page_id=int(page["pageid"]),
            revision_id=int(revisions[0]["revid"]),
            title=str(page["title"]),
            namespace=int(page["ns"]),
            is_redirect=bool(page.get("redirect")),
        )

    def get_editor_count(self, wiki: str, title: str) -> EditorCount:
        encoded_title = quote(title.replace(" ", "_"), safe="")
        url = f"https://{self.host(wiki)}/w/rest.php/v1/page/{encoded_title}/history/counts/editors"
        try:
            response = self.client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RetryableUpstreamError(f"Comptage REST indisponible : {error}") from error
        if not isinstance(data.get("count"), int):
            raise RetryableUpstreamError("Réponse de comptage REST invalide")
        return EditorCount(count=data["count"], limited=bool(data.get("limit")))

    def get_bot_contributor_count(self, wiki: str, page_id: int) -> int:
        continuation: dict[str, Any] = {}
        count = 0
        for _page in range(100):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "pageids": page_id,
                    "prop": "contributors",
                    "pcrights": "bot",
                    "pclimit": "max",
                    **continuation,
                },
            )
            pages = data.get("query", {}).get("pages", [])
            if pages:
                count += len(pages[0].get("contributors", []))
            continuation = data.get("continue", {})
            if not continuation:
                return count
        raise RetryableUpstreamError("Pagination des bots anormalement longue")

    def get_editor_history(self, wiki: str, page_id: int, max_revisions: int) -> EditorHistory:
        """Count edits per account by walking the page history.

        The Action API is the same one already used everywhere here, so this adds no
        external dependency: XTools computes the same ranking, but reaching for it would
        put a second service in the job path for a number MediaWiki already holds.

        Revisions with no `userid` are IPs, or authorship that has been suppressed.
        Neither can be named, so they are counted in the total and in nobody's tally.
        """
        counts: Counter[int] = Counter()
        revisions = 0
        continuation: dict[str, Any] = {}

        while revisions < max_revisions:
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "pageids": page_id,
                    "prop": "revisions",
                    "rvprop": "ids|user|userid",
                    "rvlimit": "max",
                    **continuation,
                },
            )
            pages = data.get("query", {}).get("pages", [])
            if not pages:
                break
            for revision in pages[0].get("revisions", []):
                revisions += 1
                user_id = int(revision.get("userid") or 0)
                if user_id > 0:
                    counts[user_id] += 1
            continuation = data.get("continue", {})
            if not continuation:
                return EditorHistory(counts=counts, revisions=revisions, complete=True)

        return EditorHistory(counts=counts, revisions=revisions, complete=False)

    def resolve_users(self, wiki: str, user_ids: Iterable[int]) -> dict[int, ResolvedUser]:
        users: dict[int, ResolvedUser] = {}
        for batch in _batched(user_ids, 50):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "list": "users",
                    "ususerids": "|".join(str(user_id) for user_id in batch),
                    "usprop": "groups",
                },
            )
            for item in data.get("query", {}).get("users", []):
                user_id = int(item.get("userid", 0))
                if user_id <= 0:
                    continue
                users[user_id] = ResolvedUser(
                    user_id=user_id,
                    username=str(item.get("name", user_id)),
                    groups=frozenset(item.get("groups", [])),
                    missing=bool(item.get("missing") or item.get("invalid")),
                )
        return users

    def fetch_standing(self, wiki: str, user_ids: Iterable[int]) -> dict[int, AccountStanding]:
        """Return each account's local block state, fifty accounts per request.

        `blockinfo` rides along on the same `list=users` call that already answers about
        groups, so knowing whether an account is blocked — and why — costs no extra
        request. Only
        active blocks appear: MediaWiki keeps no trace of one that has expired, which is
        why the caller replaces the whole set rather than merging into it.

        The global lock is not here. It is a CentralAuth fact and `list=users` answers
        about one wiki, exactly as with global bot groups (ADR-0006); `global_user_info`
        fetches it one account at a time.
        """
        standings: dict[int, AccountStanding] = {}
        for batch in _batched(user_ids, 50):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "list": "users",
                    "ususerids": "|".join(str(user_id) for user_id in batch),
                    "usprop": "blockinfo",
                },
            )
            for item in data.get("query", {}).get("users", []):
                user_id = int(item.get("userid", 0))
                if user_id <= 0:
                    continue
                expiry = str(item.get("blockexpiry") or "")
                standings[user_id] = AccountStanding(
                    user_id=user_id,
                    username=str(item.get("name", user_id)),
                    blocked_at=_parse_timestamp(item.get("blockedtimestamp")),
                    # None means "no end", not "unknown": the caller only reads it once
                    # blocked_at says a block exists.
                    block_expires_at=(
                        None if expiry.lower() in INDEFINITE_EXPIRIES else _parse_timestamp(expiry)
                    ),
                    block_partial=bool(item.get("blockpartial")),
                    # Rides along on this same call, so knowing why an account was
                    # blocked is as free as knowing that it was.
                    block_reason=(str(item.get("blockreason") or "").strip() or None),
                )
        return standings

    def global_lock_reason(self, username: str) -> str | None:
        """Return the comment on the most recent global-account log entry for an account.

        CentralAuth exposes that an account is locked but not why, and the why is what
        separates a banned abuser from a Wikipedian who has died. The reason lives in the
        `globalauth` log, which is one extra request — but only for accounts already
        found locked, which is well under one percent of those tracked.

        Asked of Meta rather than of the wiki being served: any wiki will say whether an
        account is locked, but the log of who locked it and why exists only there.

        Returns None when the log has nothing to say, which leaves the account nameable.
        """
        data = self._request(
            GLOBAL_ACCOUNT_LOG_HOST,
            {
                "action": "query",
                "list": "logevents",
                "letype": "globalauth",
                "letitle": f"User:{username}@global",
                "leprop": "comment|timestamp",
                "lelimit": 1,
            },
        )
        events = data.get("query", {}).get("logevents", [])
        if not events:
            return None
        return str(events[0].get("comment") or "").strip() or None

    def global_user_info(self, wiki: str, username: str) -> GlobalUserInfo:
        """Return the CentralAuth groups and lock status of an account.

        `list=users` answers about one wiki only, so a bot flagged globally and never
        locally comes back with no groups at all (ADR-0006), and a locked account comes
        back looking unblocked. CentralAuth answers about one account per request, which
        is why callers ask only about accounts already in contention rather than about
        the whole candidate pool.

        Results are cached for the life of the process: a worker meets the same handful
        of prolific bots across thousands of pages.
        """
        cached = self._global_users.get(username)
        if cached is not None:
            return cached

        data = self._action(
            wiki,
            {
                "action": "query",
                "meta": "globaluserinfo",
                "guiuser": username,
                "guiprop": "groups",
            },
        )
        info = data.get("query", {}).get("globaluserinfo", {})
        # "locked" is present and true when set, and absent otherwise; there is no
        # false. An account with no global record at all is simply not locked.
        resolved = GlobalUserInfo(
            groups=frozenset(info.get("groups") or ()),
            locked=bool(info.get("locked")),
        )

        if len(self._global_users) >= GLOBAL_GROUP_CACHE_SIZE:
            self._global_users.clear()
        self._global_users[username] = resolved
        return resolved

    def global_groups(self, wiki: str, username: str) -> frozenset[str]:
        return self.global_user_info(wiki, username).groups

    def get_wikitext(self, wiki: str, title: str) -> str | None:
        """Return a page's source, or None if there is no such page.

        None means "MediaWiki answered, and the page is not there", which is different
        from "MediaWiki did not answer" — that raises. Callers act on the difference:
        one is an empty list, the other is a reason to change nothing.
        """
        data = self._action(
            wiki,
            {
                "action": "query",
                "titles": title,
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "rvlimit": 1,
            },
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or pages[0].get("invalid"):
            return None
        revisions = pages[0].get("revisions", [])
        if not revisions:
            return None
        return str(revisions[0].get("slots", {}).get("main", {}).get("content", ""))

    def classify_titles(self, wiki: str, titles: list[str]) -> list[TitleInfo]:
        """Resolve titles to their namespace and page ID, following redirects.

        Redirects are followed on purpose: someone listing a redirect means the article
        it points at, and the alternative is an entry that silently does nothing.
        """
        infos: list[TitleInfo] = []
        for start in range(0, len(titles), 50):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "titles": "|".join(titles[start : start + 50]),
                    "redirects": 1,
                },
            )
            for page in data.get("query", {}).get("pages", []):
                if page.get("invalid"):
                    continue
                infos.append(
                    TitleInfo(
                        title=str(page.get("title", "")),
                        namespace=int(page.get("ns", 0)),
                        page_id=None if page.get("missing") else int(page["pageid"]),
                    )
                )
        return infos

    def category_members(
        self, wiki: str, category: str, limit: int
    ) -> tuple[list[CategoryMember], bool]:
        """Return the articles directly in a category, and whether the cap cut the list short.

        Direct members only. Recursing would make one entry on a list page reach an
        unbounded and unreviewable part of the wiki, and category graphs on Wikipedia
        are wide enough that "Personnalité vivante" would swallow most of the project.
        A category tree that genuinely needs covering is several entries on the list,
        which is the point at which someone has to look at what they are covering.
        """
        members: list[CategoryMember] = []
        continuation: dict[str, Any] = {}
        while len(members) < limit:
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmnamespace": 0,
                    "cmlimit": "max",
                    "cmprop": "ids|title",
                    **continuation,
                },
            )
            for member in data.get("query", {}).get("categorymembers", []):
                members.append(
                    CategoryMember(page_id=int(member["pageid"]), title=str(member["title"]))
                )
            continuation = data.get("continue", {})
            if not continuation:
                # The list ended on its own, which is not the same as fitting. One batch
                # answers with as many members as the wiki will hand over at once, so it
                # can overshoot the cap and still be the last one. Compare against the cap
                # rather than trusting the absence of a continuation token.
                return members[:limit], len(members) > limit
        return members[:limit], True

    def existing_user_pages(self, wiki: str, usernames: list[str]) -> set[str]:
        """Which of these accounts have a user page, 50 at a time.

        A credit line whose names are blue links promises a page behind each one, and for
        a great many contributors there is none: the link is red, and the reader who
        follows it lands on an empty create form. The gadget cannot answer this itself —
        one extra local API call per article view is exactly the cost the wiki asked it
        not to impose — so it is answered here, in a job, in batches, once an hour.

        Redirects are deliberately not followed. A user page that redirects elsewhere is
        still a user page, and following the redirect would only change which title comes
        back, not whether one exists.

        Sent over POST for the same reason as `resolve_titles`: fifty names fit the API
        limit but not always a URL once a non-Latin alphabet is percent-encoded.
        """
        found: set[str] = set()
        for start in range(0, len(usernames), 50):
            batch = usernames[start : start + 50]
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "titles": "|".join(f"User:{name}" for name in batch),
                },
                method="POST",
            )
            for page in data.get("query", {}).get("pages", []):
                if page.get("invalid") or page.get("missing"):
                    continue
                if int(page.get("ns", 0)) != USER_NAMESPACE:
                    continue
                # MediaWiki answers with the local namespace name — "Utilisateur:Alice" —
                # so the prefix is dropped rather than matched. A username cannot itself
                # contain a colon, which is what makes the first one the separator.
                _, _, name = str(page.get("title", "")).partition(":")
                if name:
                    found.add(name.replace("_", " "))
        return found

    def resolve_titles(self, wiki: str, titles: list[str]) -> list[PageMetadata]:
        """Resolve titles to page metadata, 50 at a time (the Action API limit).

        Sent over POST: fifty titles fit the API limit but not always a URL. A
        Latin title costs about its own length once percent-encoded; a Cyrillic,
        Greek, or CJK one costs roughly six bytes per character, and a batch of
        those overran the server's URL limit and returned 414, which skipped the
        whole wiki. POST carries the titles in the body, so the request size stops
        depending on the alphabet instead of being tuned for it.
        """
        pages: list[PageMetadata] = []
        for start in range(0, len(titles), 50):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "titles": "|".join(titles[start : start + 50]),
                    "redirects": 1,
                    "prop": "revisions",
                    "rvprop": "ids",
                },
                method="POST",
            )
            for page in data.get("query", {}).get("pages", []):
                revisions = page.get("revisions", [])
                if page.get("missing") or not revisions:
                    continue
                pages.append(
                    PageMetadata(
                        page_id=int(page["pageid"]),
                        revision_id=int(revisions[0]["revid"]),
                        title=str(page["title"]),
                        namespace=int(page["ns"]),
                    )
                )
        return pages

    def resolve_page_ids(self, wiki: str, page_ids: list[int]) -> list[PageMetadata]:
        """Current revision and title for a set of page ids, 50 at a time.

        The demand table stores page ids because that is what survives a rename, and a
        page id is exactly what the queue needs; what it does not carry is the revision
        the page is on today. This is the lookup that supplies it, so a page a reader
        asked about last week is re-checked against the article as it stands now rather
        than as it stood when they read it.

        A deleted page comes back as ``missing`` and is dropped, as with titles.
        """
        pages: list[PageMetadata] = []
        for start in range(0, len(page_ids), 50):
            data = self._action(
                wiki,
                {
                    "action": "query",
                    "pageids": "|".join(str(page_id) for page_id in page_ids[start : start + 50]),
                    "prop": "revisions",
                    "rvprop": "ids",
                },
                method="POST",
            )
            for page in data.get("query", {}).get("pages", []):
                revisions = page.get("revisions", [])
                if page.get("missing") or not revisions:
                    continue
                pages.append(
                    PageMetadata(
                        page_id=int(page["pageid"]),
                        revision_id=int(revisions[0]["revid"]),
                        title=str(page["title"]),
                        namespace=int(page["ns"]),
                    )
                )
        return pages

    def all_pages_batch(
        self, wiki: str, cursor: str | None
    ) -> tuple[list[PageMetadata], str | None]:
        params: dict[str, Any] = {
            "action": "query",
            "generator": "allpages",
            "gapnamespace": 0,
            # 57% of frwiki's main namespace is redirects, and they sort in among the
            # articles, so an unfiltered walk spent more than half its WikiWho budget
            # attributing pages that are one line of wikitext pointing elsewhere.
            "gapfilterredir": "nonredirects",
            "gaplimit": "max",
            "prop": "revisions",
            "rvprop": "ids",
        }
        if cursor:
            params["gapcontinue"] = cursor
        data = self._action(wiki, params)
        pages = []
        for page in data.get("query", {}).get("pages", []):
            revisions = page.get("revisions", [])
            if not revisions:
                continue
            pages.append(
                PageMetadata(
                    page_id=int(page["pageid"]),
                    revision_id=int(revisions[0]["revid"]),
                    title=str(page["title"]),
                    namespace=int(page["ns"]),
                )
            )
        return pages, data.get("continue", {}).get("gapcontinue")

    def close(self) -> None:
        self.client.close()


class WikiWhoClient:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        timeout_seconds: float,
        max_response_bytes: int,
        resolver: SiteResolver | None = None,
    ) -> None:
        self.base_url = base_url
        self.max_response_bytes = max_response_bytes
        self.resolver = resolver or SiteResolver()
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def fetch_revision(self, wiki: str, revision_id: int) -> list[dict[str, Any]]:
        language = self.resolver.require_language(wiki)
        url = f"{self.base_url}/{language}/api/v1.0.0-beta/rev_content/rev_id/{revision_id}/"
        params = {
            "o_rev_id": "false",
            "editor": "true",
            "token_id": "false",
            "out": "false",
            "in": "false",
        }
        try:
            with self.client.stream("GET", url, params=params) as response:
                # 400 is WikiWho stating a fact about the page, not a hiccup: the two
                # forms observed are a rejected namespace and a revision it has no
                # article for. Neither heals, so retrying costs thirteen seconds and two
                # more upstream calls to reach the same answer. Nor is it a lag signal —
                # measured against fr.wikipedia edits seconds old, WikiWho answered 200
                # with the exact revision every time, so a fresh revision is not the
                # reason for a 400.
                if response.status_code == 400:
                    raise PermanentDataError(
                        f"WikiWho ne sert pas la révision {revision_id} (HTTP 400)"
                    )
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    raise RetryableUpstreamError(
                        f"WikiWho HTTP {response.status_code} pour la révision {revision_id}"
                    )
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise ResponseTooLargeError(
                            f"Réponse WikiWho supérieure à {self.max_response_bytes} octets"
                        )
                    chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise RetryableUpstreamError(f"WikiWho indisponible : {error}") from error

        if not data.get("success"):
            raise RetryableUpstreamError(f"WikiWho a refusé la révision : {data.get('message')}")
        for revision_wrapper in data.get("revisions", []):
            revision = revision_wrapper.get(str(revision_id))
            if isinstance(revision, dict) and isinstance(revision.get("tokens"), list):
                return revision["tokens"]
        raise RetryableUpstreamError(
            f"WikiWho n’a pas encore renvoyé la révision exacte {revision_id}"
        )

    def close(self) -> None:
        self.client.close()


class AnalyticsClient:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float,
        resolver: SiteResolver | None = None,
    ) -> None:
        self.resolver = resolver or SiteResolver()
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def top_pages(self, wiki: str, day: date) -> list[str] | None:
        host = self.resolver.host(wiki)
        url = (
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
            f"{host}/all-access/{day:%Y/%m/%d}"
        )
        try:
            response = self.client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RetryableUpstreamError(f"Pageviews indisponible : {error}") from error
        items = data.get("items", [])
        articles = items[0].get("articles", []) if items else []
        return [unquote(str(article["article"])).replace("_", " ") for article in articles]

    def close(self) -> None:
        self.client.close()
