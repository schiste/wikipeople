# API contract

The gadget consumes the page-freshness resource under `/v2`. The exact-revision `/v1` contract
remains available for backwards compatibility. Keep each version backwards compatible; add a new
API version for breaking changes.

## Get current page attribution (`v2`)

```http
GET /v2/{wiki}/pages/{page_id}?revision_id={revision_id}
```

`{wiki}` is a Wikipedia database name such as `frwiki`, `enwiki`, or `simplewiki`. Any Wikipedia
WikiWho covers is accepted by default; see [wiki scope](#wiki-scope) below.

The gadget sends the current revision so a refresh job knows what to calculate. A ready result is
selected by `(wiki, page_id, algorithm_version)`, not by exact requested revision. This lets one
calculation remain usable across ordinary edits for `PAGE_FRESHNESS_SECONDS`, 90 days by default.

### Ready or refreshing — `200 OK`

```json
{
  "status": "ready",
  "wiki": "frwiki",
  "page_id": 123,
  "requested_revision_id": 789,
  "source_revision_id": 456,
  "title": "Exemple",
  "algorithm_version": "attribution-ladder-v3",
  "metric": "wikiwho-surviving-alphanumeric-tokens",
  "contributors": [],
  "distinct_contributors": 47,
  "other_contributors": 47,
  "opted_out": false,
  "count_limited": false,
  "countable_tokens": 987,
  "computed_at": "2026-08-16T10:00:00Z",
  "fresh_until": "2026-11-14T10:00:00Z",
  "is_fresh": true,
  "refreshing": false,
  "methodology_url": "https://github.com/schiste/wikifame/blob/main/docs/architecture.md"
}
```

- `metric` says which rung of the attribution ladder produced `contributors`, and clients must
  word their sentence from it. `wikiwho-surviving-alphanumeric-tokens` means the names wrote the
  text; `mediawiki-revision-count` means only that they edited the page most, which is a weaker
  claim. Treat an unrecognized value as the weaker claim, never the stronger one. Contributor
  entries carry `token_count` under the first metric and `edit_count` under the second; `share`
  is present under both and is a share of that metric.
- `opted_out` says the wiki has asked for this article to be counted but not named. It is then
  always accompanied by an empty `contributors` list and an unchanged `distinct_contributors`, so
  the sentence becomes "written by 47 people" rather than disappearing. Clients must not treat it
  as an error or fall back to another source of names. See
  [ADR-0008](decisions/0008-article-opt-out.md).
- `contributors` may be shorter than the ranking that produced it. An account the wiki has
  lastingly excluded — locked globally as a sanction, or under a non-partial, non-courtesy block
  longer than the wiki's threshold, ninety days by default — is dropped when the response is
  built. There is no flag for
  it, deliberately: a machine-readable "one of this article's main authors is banned" would be a
  worse disclosure than the credit it replaces. `distinct_contributors` is unchanged and the
  missing share appears in `other_contributors`, so a two-name answer with a large remainder is
  normal and is not an error. See
  [ADR-0009](decisions/0009-sanctioned-contributor-visibility.md).
- `source_revision_id` is the exact revision analyzed by WikiWho.
- `requested_revision_id` is the revision displayed when the request was made.
- `is_fresh` is true until `computed_at + PAGE_FRESHNESS_SECONDS`.
- When the result is expired, the same payload is returned with `is_fresh: false` and normally
  `refreshing: true`; a P100 refresh has been queued for the requested revision.

Ready responses are cacheable but always checkable:

```http
Cache-Control: public, max-age=300, stale-while-revalidate=604800
ETag: "6f1c…"
X-WikiPeople-Algorithm: attribution-ladder-v3
X-WikiPeople-Source-Revision: 456
```

A browser may reuse a response for five minutes without asking. After that it revalidates, and a
request carrying a matching `If-None-Match` gets `304 Not Modified` with the headers and no body;
`stale-while-revalidate` means the reader still sees the cached answer immediately while that
happens. The window is short because the answer is not a property of the revision alone — a policy
change alters it while the URL stays the same, and a long window is a long period of serving an
answer the service has already retired. See [ADR-0007](decisions/0007-cache-validation.md).

The `ETag` is a hash of the response body, so it changes whenever anything in the answer does,
including `algorithm_version`. Do not parse it. Operators can tune the durations with
`PAGE_CACHE_SECONDS` and `PAGE_STALE_WHILE_REVALIDATE_SECONDS`.

A `304` is returned only after the usual freshness check has run, so a stale page is still queued
for recomputation by a request that receives no body.

### No result yet — `202 Accepted`

The response shape is the same pending shape documented for v1 below. The request creates or
reuses one durable P100 job for the requested revision. The gadget retries briefly during the
current visit and otherwise waits for a later page view.

## Get exact revision attribution (`v1`, legacy)

```http
GET /v1/{wiki}/pages/{page_id}?revision_id={revision_id}
```

Scope:

- `wiki`: database name of a WikiWho-covered Wikipedia
- `page_id`: positive MediaWiki page ID
- `revision_id`: positive current revision ID

The API deliberately does not accept a title as identity. Page titles can change; page and
revision IDs are stable.

### Ready — `200 OK`

```json
{
  "status": "ready",
  "wiki": "frwiki",
  "page_id": 123,
  "revision_id": 456,
  "title": "Exemple",
  "algorithm_version": "attribution-ladder-v3",
  "metric": "wikiwho-surviving-alphanumeric-tokens",
  "contributors": [
    {
      "user_id": 10,
      "username": "Alice",
      "token_count": 310,
      "share": 0.314
    }
  ],
  "distinct_contributors": 47,
  "other_contributors": 46,
  "opted_out": false,
  "count_limited": false,
  "countable_tokens": 987,
  "computed_at": "2026-08-16T10:00:00Z",
  "methodology_url": "https://github.com/schiste/wikifame/blob/main/docs/architecture.md"
}
```

`share` is a fraction between zero and one. `other_contributors` is always
`max(0, distinct_contributors - contributors.length)`.

`opted_out` behaves exactly as it does on v2. The opt-out is applied when the response is built,
so it cannot be sidestepped by asking v1 for an exact revision.

Ready responses use:

```http
Cache-Control: public, max-age=300
ETag: "9a04…"
X-WikiPeople-Algorithm: attribution-ladder-v3
```

v1 was previously `max-age=31536000, immutable`, on the reasoning that a revision's attribution
never changes. The revision does not, but the policy applied to it does, and the algorithm version
is part of the *stored row's* identity while being absent from the URL — so the reader's copy
outlived the answer it held. v1 now revalidates on the same terms as v2 and honours
`If-None-Match`. New gadget clients should use v2.

### Pending — `202 Accepted`

```json
{
  "status": "pending",
  "wiki": "frwiki",
  "page_id": 123,
  "revision_id": 456,
  "retry_after": 30
}
```

The request created or reused one durable queue item. The gadget intentionally renders nothing
and does not poll continuously. A later page visit can consume the ready result.

### Unavailable — `503 Service Unavailable`

```json
{
  "status": "unavailable",
  "wiki": "frwiki",
  "page_id": 123,
  "revision_id": 456,
  "error_code": "upstream_unavailable"
}
```

Transient dead jobs become eligible again after `DEAD_RETRY_SECONDS`. Permanent data errors stay
unavailable for that algorithm version.

## Wiki scope

Both versions accept the same wikis. A wiki qualifies when it is a Wikipedia WikiWho analyses —
`{lang}wiki` where `{lang}` is a covered language code — and when `SUPPORTED_WIKIS` enables it.
`SUPPORTED_WIKIS` defaults to `*`, so no configuration is needed to serve a new Wikipedia.

Anything else, including `commonswiki`, `wikidatawiki`, and every Wiktionary or Wikisource,
returns:

```http
HTTP/1.1 404 Not Found
{ "detail": "Wiki non pris en charge" }
```

`404` is a normal, expected answer rather than an error condition: the gadget ships unchanged on
every wiki and simply renders nothing where the API declines. This lets operators enable wikis
progressively without any on-wiki script edit.

Being served is not a promise of prewarming. A wiki keeps its popular pages warm once it has
produced at least one real result, or once an operator lists it in `PREWARM_WIKIS`.

## Operational endpoints

- `GET /healthz`: database connectivity probe; returns `{"status":"ok"}`.
- `GET /v1/stats`: aggregate ready and queue counts, plus `supported_wikis` (the configured
  enablement, `["*"]` by default), `active_wikis` (wikis that have produced a result and are
  therefore prewarmed), and `opted_out` (articles covered by each wiki's opt-out list), and `standing`
  (per wiki, how many named accounts are tracked and how many carry a block or a lock — not how
  many names are withheld, which depends on a threshold applied per response), `metrics` (how
  many cached results carry each attribution metric, so the share still on the edit-count
  fallback is visible), `demand` (per wiki, how many pages are ranked for the backfill, how many
  are still waiting, and how many were asked for by a reader), and `usage` (the last seven days
  of answers per day, per wiki and per outcome). Do not scrape at high frequency because an exact result count can become
  expensive on a large InnoDB table.
- `GET /docs`: generated OpenAPI interface. This documents HTTP structure, while this file
  documents behavioral guarantees.

## CORS and privacy

CORS permits Wikipedia origins, desktop and mobile, through `CORS_ORIGIN_REGEX`; `CORS_ORIGINS`
adds exact origins on top. CORS is not authentication; scripted clients can still call the public
API, and that was equally true when a single origin was allowed. Responses contain no reader data. Operational access logs
must not be used to reconstruct reading histories.

The `demand` and `usage` figures in `/v1/stats` are counters, not a log. They record how many
answers a wiki was given on a day and how many times an article was asked for; they hold no
actor, no address, no session, no ordering and no timestamp finer than a day, so no reading
history can be reconstructed from them. They expire on the same schedule as the rest of the
cache. [ADR-0010](decisions/0010-demand-and-usage-counters.md) records what they may not
become.
