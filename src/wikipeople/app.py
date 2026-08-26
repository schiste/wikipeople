from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from wikipeople.models import AttributionResult, utcnow
from wikipeople.policy import is_nameable_account
from wikipeople.runtime import Runtime, build_runtime


def _isoformat(value: Any) -> str:
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _etag(payload: dict[str, Any]) -> str:
    """
    A validator derived from the response itself.

    Hashing the payload rather than assembling a tag out of chosen fields means the tag
    cannot drift from what it claims to describe: anything that changes the body changes
    the tag, including the algorithm version, which is what a policy change moves and
    what nothing in the URL records.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32] + '"'


def _if_none_match(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


#: How much of the usage history `/v1/stats` shows. A week is enough to see whether a
#: day was quiet, and short enough that the endpoint stays a fixed size as the counters
#: accumulate behind it.
STATS_USAGE_DAYS = 7


def _counted_wiki(runtime: Runtime, wiki: str) -> str:
    """The name a refused request is counted under.

    A request for a wiki this deployment does not serve carries whatever the caller put
    in the path, and counters are keyed by wiki, so counting it verbatim would let
    anyone create rows by inventing names. A real Wikipedia that WikiWho covers is
    counted under its own name -- that is the useful signal, an edition turned off that
    people are asking for -- and everything else under a single bucket.
    """
    return wiki if runtime.resolver.is_capable(wiki) else "-"


def _nameable_contributors(
    runtime: Runtime, wiki: str, contributors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop the accounts this wiki has put lastingly out of the community.

    Applied here rather than in the worker for the same reason the opt-out is: a
    sanction is a fact about a person that changes without anyone touching the article.
    Filtering when the answer is built means a block imposed this morning takes effect
    within the hour, and lifting one restores the name just as fast, with nothing
    recomputed and `algorithm_version` unmoved. ADR-0009.

    The list shrinks; it is not backfilled from a fourth contributor, because only three
    were ever stored. Naming two people instead of three is a smaller loss than making
    every response depend on a recomputation, and the missing share is not lost — it
    moves into `other_contributors`.
    """
    settings = runtime.settings
    if not settings.hide_sanctioned_contributors or not contributors:
        return contributors

    user_ids = [
        contributor["user_id"]
        for contributor in contributors
        if isinstance(contributor.get("user_id"), int)
    ]
    standings = runtime.repository.standing_for(wiki, user_ids)
    threshold = settings.max_visible_block_seconds_for(wiki)
    now = utcnow()
    return [
        contributor
        for contributor in contributors
        if is_nameable_account(standings.get(contributor.get("user_id")), threshold, now)
    ]


def _attribution_fields(
    runtime: Runtime, wiki: str, page_id: int, result: AttributionResult
) -> dict[str, Any]:
    """The part of a ready answer that presentation policy changes.

    Two rules meet here, and they are not the same rule. An opt-out is about an article
    and drops every name; a sanction is about a person and drops one. Both leave
    `distinct_contributors` alone, so the sentence becomes "written by 47 people" or
    "by Alice and 46 others" rather than disappearing: the count is not a name, and
    losing it would hide that the article has a history at all.

    Only `opted_out` is reported. There is no matching flag for a withheld name, on
    purpose — it would be a machine-readable announcement that one of this article's
    main authors is banned, which is a worse disclosure than the credit it replaces.

    Nothing is deleted. The stored row keeps its contributors — they are public page
    history — and both rules govern what is presented, which is what makes them take
    effect, and be undone, without recomputing anything.
    """
    opted_out = runtime.repository.is_opted_out(wiki, page_id)
    contributors: list[dict[str, Any]] = (
        [] if opted_out else _nameable_contributors(runtime, wiki, list(result.contributors))
    )
    return {
        "contributors": contributors,
        "distinct_contributors": result.distinct_contributors,
        "other_contributors": max(0, result.distinct_contributors - len(contributors)),
        "opted_out": opted_out,
    }


def create_app(runtime: Runtime | None = None) -> FastAPI:
    app_runtime = runtime or build_runtime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        app_runtime.database.create_schema()
        yield

    app = FastAPI(
        title="WikiPeople API",
        version="0.1.0",
        description="Cached WikiWho attribution for the WikiPeople gadget",
        lifespan=lifespan,
    )
    app.state.runtime = app_runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_runtime.settings.cors_origins),
        allow_origin_regex=app_runtime.settings.cors_origin_regex or None,
        allow_methods=["GET"],
        allow_headers=["Accept"],
        max_age=86400,
    )

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, Any]:
        """Name the service at its root.

        The root was a 404, which reads as a broken deployment to anyone who trims
        a URL back to the host. It points at the docs rather than serving them, so
        the landing page costs no database access.
        """
        return {
            "name": "WikiPeople",
            "description": "Cached WikiWho attribution for the WikiPeople gadget",
            "docs": "/docs",
            "health": "/healthz",
            "stats": "/v1/stats",
            "source": "https://github.com/schiste/wikifame",
        }

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        app_runtime.database.ping()
        return {"status": "ok"}

    @app.get("/v1/stats")
    def stats() -> dict[str, Any]:
        return {
            "status": "ok",
            "algorithm_version": app_runtime.settings.algorithm_version,
            "supported_wikis": list(app_runtime.settings.supported_wikis),
            "active_wikis": app_runtime.repository.active_wikis(),
            "cache": app_runtime.repository.stats(),
            "opted_out": app_runtime.repository.optout_counts(),
            "standing": app_runtime.repository.standing_counts(),
            "metrics": app_runtime.repository.metric_counts(),
            "demand": app_runtime.repository.demand_counts(),
            "usage": app_runtime.repository.usage_since(STATS_USAGE_DAYS),
        }

    @app.get("/v1/{wiki}/pages/{page_id}", response_model=None)
    def page_attribution(
        request: Request,
        response: Response,
        wiki: str,
        page_id: int,
        revision_id: int = Query(gt=0),
    ) -> Any:
        settings = app_runtime.settings
        if not app_runtime.resolver.is_enabled(wiki, settings.supported_wikis):
            app_runtime.repository.count_request(_counted_wiki(app_runtime, wiki), "unsupported")
            raise HTTPException(status_code=404, detail="Wiki non pris en charge")
        if page_id <= 0:
            raise HTTPException(status_code=422, detail="page_id doit être positif")

        # Two counter rows per request, and nothing else: which article was asked
        # about, and how the answer turned out. Both are aggregates that existed before
        # this request did, so neither can be read back as a visit.
        app_runtime.repository.record_page_request(wiki, page_id)

        result = app_runtime.repository.get_result(
            wiki, page_id, revision_id, settings.algorithm_version
        )
        if result is not None:
            payload = {
                "status": "ready",
                "wiki": result.wiki,
                "page_id": result.page_id,
                "revision_id": result.revision_id,
                "title": result.title,
                "algorithm_version": result.algorithm_version,
                "metric": result.metric,
                **_attribution_fields(app_runtime, wiki, page_id, result),
                "count_limited": result.count_limited,
                "countable_tokens": result.countable_tokens,
                "computed_at": _isoformat(result.computed_at),
                "methodology_url": settings.methodology_url,
            }
            # Not "immutable", though the URL names an exact revision whose text will
            # never change again. What changes is the policy applied to that text, and
            # the URL says nothing about which policy produced the answer.
            headers = {
                "Cache-Control": f"public, max-age={settings.ready_cache_seconds}",
                "ETag": _etag(payload),
                "X-WikiPeople-Algorithm": settings.algorithm_version,
            }
            # Counted before the validator check, because a 304 is an answer served.
            app_runtime.repository.count_request(wiki, "ready")
            if _if_none_match(request.headers.get("if-none-match"), headers["ETag"]):
                return Response(status_code=304, headers=headers)
            response.headers.update(headers)
            return payload

        work = app_runtime.repository.get_work(
            wiki, page_id, revision_id, settings.algorithm_version
        )
        if work is not None and work.state == "dead":
            retry_at = work.updated_at + timedelta(seconds=settings.dead_retry_seconds)
            if not work.is_permanent and retry_at <= utcnow():
                app_runtime.repository.revive(work.id, priority=100)
            else:
                app_runtime.repository.count_request(wiki, "unavailable")
                return JSONResponse(
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "3600"},
                    content={
                        "status": "unavailable",
                        "wiki": wiki,
                        "page_id": page_id,
                        "revision_id": revision_id,
                        "error_code": work.error_code,
                    },
                )

        app_runtime.repository.enqueue(
            wiki=wiki,
            page_id=page_id,
            revision_id=revision_id,
            algorithm_version=settings.algorithm_version,
            priority=100,
        )
        app_runtime.repository.count_request(wiki, "pending")
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store", "Retry-After": "30"},
            content={
                "status": "pending",
                "wiki": wiki,
                "page_id": page_id,
                "revision_id": revision_id,
                "retry_after": 30,
            },
        )

    @app.get("/v2/{wiki}/pages/{page_id}", response_model=None)
    def page_attribution_by_freshness(
        request: Request,
        response: Response,
        wiki: str,
        page_id: int,
        revision_id: int = Query(gt=0),
    ) -> Any:
        settings = app_runtime.settings
        if not app_runtime.resolver.is_enabled(wiki, settings.supported_wikis):
            app_runtime.repository.count_request(_counted_wiki(app_runtime, wiki), "unsupported")
            raise HTTPException(status_code=404, detail="Wiki non pris en charge")
        if page_id <= 0:
            raise HTTPException(status_code=422, detail="page_id doit être positif")

        app_runtime.repository.record_page_request(wiki, page_id)

        result = app_runtime.repository.get_latest_result(wiki, page_id, settings.algorithm_version)
        if result is not None:
            now = utcnow()
            fresh_until = result.computed_at + timedelta(seconds=settings.page_freshness_seconds)
            is_fresh = fresh_until > now
            refreshing = False

            if not is_fresh:
                work = app_runtime.repository.get_work(
                    wiki, page_id, revision_id, settings.algorithm_version
                )
                if work is not None and work.state == "dead":
                    retry_at = work.updated_at + timedelta(seconds=settings.dead_retry_seconds)
                    if not work.is_permanent and retry_at <= now:
                        app_runtime.repository.revive(work.id, priority=100)

                app_runtime.repository.enqueue_if_stale(
                    wiki=wiki,
                    page_id=page_id,
                    revision_id=revision_id,
                    algorithm_version=settings.algorithm_version,
                    priority=100,
                    freshness_seconds=settings.page_freshness_seconds,
                )
                work = app_runtime.repository.get_work(
                    wiki, page_id, revision_id, settings.algorithm_version
                )
                refreshing = work is not None and work.state in {"pending", "leased"}

            payload = {
                "status": "ready",
                "wiki": result.wiki,
                "page_id": result.page_id,
                "requested_revision_id": revision_id,
                "source_revision_id": result.revision_id,
                "title": result.title,
                "algorithm_version": result.algorithm_version,
                "metric": result.metric,
                **_attribution_fields(app_runtime, wiki, page_id, result),
                "count_limited": result.count_limited,
                "countable_tokens": result.countable_tokens,
                "computed_at": _isoformat(result.computed_at),
                "fresh_until": _isoformat(fresh_until),
                "is_fresh": is_fresh,
                "refreshing": refreshing,
                "methodology_url": settings.methodology_url,
            }
            headers = {
                "Cache-Control": (
                    f"public, max-age={settings.page_cache_seconds}, "
                    f"stale-while-revalidate={settings.page_stale_while_revalidate_seconds}"
                ),
                "ETag": _etag(payload),
                "X-WikiPeople-Algorithm": settings.algorithm_version,
                "X-WikiPeople-Source-Revision": str(result.revision_id),
            }
            # The enqueue above has already happened, so a 304 still keeps a stale page
            # moving towards being recomputed. Only the body is spared.
            # Counted before the validator check, because a 304 is an answer served.
            app_runtime.repository.count_request(wiki, "ready")
            if _if_none_match(request.headers.get("if-none-match"), headers["ETag"]):
                return Response(status_code=304, headers=headers)
            response.headers.update(headers)
            return payload

        work = app_runtime.repository.get_work(
            wiki, page_id, revision_id, settings.algorithm_version
        )
        if work is not None and work.state == "dead":
            retry_at = work.updated_at + timedelta(seconds=settings.dead_retry_seconds)
            if not work.is_permanent and retry_at <= utcnow():
                app_runtime.repository.revive(work.id, priority=100)
            else:
                app_runtime.repository.count_request(wiki, "unavailable")
                return JSONResponse(
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "3600"},
                    content={
                        "status": "unavailable",
                        "wiki": wiki,
                        "page_id": page_id,
                        "requested_revision_id": revision_id,
                        "error_code": work.error_code,
                    },
                )

        app_runtime.repository.enqueue_if_stale(
            wiki=wiki,
            page_id=page_id,
            revision_id=revision_id,
            algorithm_version=settings.algorithm_version,
            priority=100,
            freshness_seconds=settings.page_freshness_seconds,
        )
        app_runtime.repository.count_request(wiki, "pending")
        return JSONResponse(
            status_code=202,
            headers={"Cache-Control": "no-store", "Retry-After": "30"},
            content={
                "status": "pending",
                "wiki": wiki,
                "page_id": page_id,
                "requested_revision_id": revision_id,
                "retry_after": 30,
            },
        )

    return app


app = create_app()
