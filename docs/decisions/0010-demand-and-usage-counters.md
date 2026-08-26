# ADR-0010: What WikiPeople records about being used

- Status: Accepted
- Date: 2026-08-26
- Algorithm version: unchanged (this changes what is scheduled, not what is computed)

## Context

Two questions had no answer, for the same reason.

**Which pages should be computed next.** The backfill works down from the longest article
because that is what the replica can sort on exactly ([operations](../operations.md)). Three
weeks in, the cache holds long articles, and "who wrote this" is only ever asked about an
article somebody is reading. Size was always a stand-in for readership; readership itself was
not available in a form the queue could take.

**Whether the tool is used at all.** The only record of use is the webservice access log, and
behind the Toolforge front proxy every line carries the proxy's address, never a reader's. It
is also retained for days. So it cannot say how many people run the gadget, cannot say how a
wiki's traffic changed after an announcement, and must not be asked: reconstructing reading
histories from access logs is exactly what this project has committed not to do.

Both questions want counts. Neither wants a reader.

## Decision

**WikiPeople keeps two aggregate tables. Both are counters that exist before any particular
request does, and neither has a column that could hold a reader.**

`page_demand` — one row per article, never per visit:

| column | written by | meaning |
| --- | --- | --- |
| `views` | the daily `demand` job | published pageviews, accumulated day by day |
| `requests` | the API request path | how often this API was asked about the article |
| `queued_at` | the hourly backfill | when the page was last handed to the queue |

`usage_counters` — one row per (day, wiki, outcome), holding a single integer. Outcomes are
`ready`, `pending`, `unavailable` and `unsupported`.

The backfill ranks by `requests` first and `views` second, and hands each batch over by
marking `queued_at` rather than by walking a cursor: the ranking changes every day, and a
keyset cursor over a ranking that moves under it skips rows and repeats others.

### What is deliberately absent

- **No actor.** No address, no user agent, no account, no session, no cookie. Nothing that
  distinguishes two readers of the same article, or the same reader on two articles.
- **No sequence.** No row per request and no timestamp per request, so no ordering exists to
  be reconstructed. `last_requested_at` is one field that is overwritten, not a list.
- **No reader-supplied key.** A request for a wiki this deployment does not serve is counted
  under that wiki's name only when it is a real Wikipedia WikiWho covers — roughly seventy
  possible values — and under `-` otherwise, so nobody can create rows by inventing names in
  a URL path.
- **No indefinite retention.** `cache-cleanup` drops demand rows nothing has viewed or
  requested in 180 days and counters older than a year. Pruning is not housekeeping here; it
  is what stops a table of counters from accumulating into the history it is built not to be.

The pageview dumps that feed `views` are a published aggregate that says an article was
opened and never by whom, and this job narrows them further to a page id and a number.
Nothing per-reader exists in the input, so nothing per-reader can come out of it.

Only wikis in `BACKFILL_WIKIS` are read out of the dumps. Serving a wiki on demand has never
implied crawling it ([ADR-0003](0003-universal-wiki-support.md)), and ranking what to crawl
is crawling.

## Consequences

- The backfill order becomes readership, with size as the fallback for whatever budget is
  left once the ranking runs dry. Size never stopped being useful; it stopped being first.
- `/v1/stats` gains `usage`, `demand` and `metrics`, so "is this used, and how is it being
  answered" is answerable without touching a log.
- Every served request now costs two counter updates. At the volumes this tool sees that is
  invisible, and both are ordinary writes to the database the endpoint already depends on.
- A page a reader opened is re-checked weekly against its current text rather than quarterly
  ([ADR-0002](0002-page-freshness.md) still governs everything else), because a page somebody
  opened is the one likeliest to be opened again.

## Alternatives rejected

**Keep the access log longer and analyse it.** Answers the usage question badly — the proxy
address makes per-reader counting impossible anyway — and answers it by building precisely
the artefact this project promised not to build.

**Recover demand from `work_queue`.** Completed work items are deleted, so historical reader
demand is not there to recover; only `dead` and `superseded` rows survive, which is a record
of failures rather than of interest.

**Rank by pageviews without storing them.** The dump is 640 MB and takes two and a half
minutes to stream. Reading it inline would put a batch job's cost on an hourly one, and the
ranking would still have to be re-derived every hour to answer "what is left".

**A row per request, aggregated later.** The aggregate is the only thing anyone needs, and
the window between writing the rows and aggregating them is a reading history that exists on
disk. Not writing it is simpler than protecting it.
