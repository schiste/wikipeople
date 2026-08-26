# Architecture and scaling rules

## Which wikis exist, and which are switched on

Two questions are answered separately.

**Can this wiki be analysed?** `wikipeople/sites.py` strips the `wiki` suffix from the database
name and checks the remainder against the set of WikiWho language codes. Every such code is
dash-free, so `frwiki` resolves to `fr` and therefore to `fr.wikipedia.org` exactly. The same
rule excludes everything else without a denylist: `frwikisource` fails the suffix test, and
`commonswiki` and `wikidatawiki` yield language codes WikiWho does not publish. Because the rule
is purely syntactic it needs no sitematrix call, which is what keeps the FastAPI process free of
upstream dependencies. `WIKIWHO_LANGUAGES` overrides the built-in set if coverage changes.

**Is this wiki switched on?** `SUPPORTED_WIKIS` decides, and defaults to `*`. Capability always
wins over configuration, so listing a wiki WikiWho cannot analyse does not enable it.

Serving is universal; work is not. `PREWARM_WIKIS` pins wikis to keep warm, and `BACKFILL_WIKIS`
defaults to empty. A wiki nobody reads therefore costs nothing. See
[ADR-0003](decisions/0003-universal-wiki-support.md).

The gadget is one wiki-agnostic file. It reports `wgDBname` and lets the API decide; a wiki that
is off answers `404` and the gadget renders nothing. Wording, help links, and a local opt-out live
in an on-wiki JSON page, so they change without a deployment. While WikiPeople is a personal script
that page is `User:<name>/wikipeople-config.json`, next to the script itself, which keeps setup free
of any rights requirement; a site-wide gadget would move it to `MediaWiki:Wikipeople-config.json`. A
missing page yields the built-in defaults, and per-wiki defaults are published in `config/`.

Customisation beyond settings is deliberately not expressed in that JSON. `historyIntroPage` names
a wikitext page whose parsed HTML replaces the history-box wording, so images, Commons video, and
templates work through MediaWiki's own renderer and sanitiser rather than through gadget code; and
two `mw.hook` events carry arbitrary JavaScript into the reader's `common.js`. Nothing in a
configuration page is executed or treated as markup, which is what keeps it reviewable by reading
it. See [ADR-0004](decisions/0004-on-wiki-extensibility.md).

That page is parsed once and reused for every article, so per-article values cannot live in it. It
declares a slot — an element classed `wikipeople-count` or `wikipeople-number` — whose text the gadget
replaces with this article's contributor count, keeping the page's own wording as the fallback. A
page declaring no slot issues no request, so history views stay free unless someone opts in, and
the count shares the article view's page-keyed session cache rather than adding a second call.

## Request path

1. The gadget requests `GET /v2/{wiki}/pages/{page_id}?revision_id={revision_id}`.
2. The API selects the newest result for the page and current algorithm.
3. A result younger than 90 days is returned with a bounded one-day browser cache.
4. An older result is returned immediately and a P100 refresh is enqueued in the background.
5. A page with no result creates one durable queue row and returns `202 pending` immediately.
6. A continuous worker validates that the requested revision is still current.
7. The worker fetches token provenance from WikiWho, resolves user IDs through the
   MediaWiki Action API, obtains the historical editor count, and stores a compact result.
8. On the first stored result for a wiki, the worker records it in `active_wikis`, which enrols
   that wiki into daily prewarming from then on.

Step 8 is deliberately triggered by a completed calculation rather than by an incoming request,
so a scripted client cannot enrol every Wikipedia into scheduled work by poking every URL.

No WikiWho request runs in the web process. A unique database constraint collapses a
thundering herd for the same page and revision into one job.

## Storage identity, freshness, and updates

The immutable identity is:

```text
wiki + page_id + revision_id + algorithm_version
```

Titles are metadata rather than identity because pages can be renamed. V1 reads this exact
identity. V2 selects the newest stored result by `wiki + page_id + algorithm_version`, then exposes
its revision as `source_revision_id` for provenance.

The default freshness window is 90 days. Ordinary edits do not invalidate a fresh page-level
result because the product accepts attribution calculated within the last three months. Once the
window expires, stale-while-revalidate keeps the old data visible while exactly one current-
revision job is queued. Pending work for older revisions is marked `superseded`. The cleanup job
removes old non-current result versions but always retains the newest computed version for a page.

This policy separates three clocks:

- ToolsDB calculation freshness: 90 days;
- browser and intermediary freshness: one day;
- stale browser reuse during transient failure: seven days.

All three are configurable. A future editor-triggered invalidation can enqueue a refresh without
discarding the last known result; it does not require changing the storage identity.

Changing any attribution rule that can affect output requires a new `ALGORITHM_VERSION`.
This prevents an old response from being confused with a new interpretation of contribution.

## Attribution policy (`attribution-ladder-v3`)

Three rungs are tried in order, each claiming strictly less than the one above. The `metric` field
of every result says which one answered, and the gadget words its sentence from that field.

### Rung 1 — surviving tokens (`wikiwho-surviving-alphanumeric-tokens`)

- Count WikiWho tokens containing at least one Unicode letter or number.
- Rank origin editors by count of tokens surviving in the requested revision.
- Resolve numeric WikiWho editor IDs to current Wikimedia usernames.
- Permanently exclude bots, temporary accounts, missing users, IPs, and anonymous actors from
  the top three, by the rule below.
- Require at least 20 surviving tokens and 1% of the countable tokens.

This metric recognizes originators of currently visible wikitext. It does not measure research,
review, maintenance, media work, reverted contributions, or the quality of an edit.

### Rung 2 — edit counts (`mediawiki-revision-count`)

Reached when rung 1 names nobody: either WikiWho refuses the page with a permanent `400`, or it
answers about a page too short for anyone to clear the thresholds. Both are the same nothing to a
reader.

- Count revisions per account by walking the page history through the Action API.
- Exclude exactly the same accounts as rung 1, through the same shared rule — only the ordering
  differs, which is the one thing the two metrics disagree about.
- Apply no minimum share: a share of the edits is not a share of the text, so the 1% figure would
  be imported from a different measurement.
- Leave a history longer than `TOP_EDITOR_MAX_REVISIONS` unranked. “Most edits” computed over the
  newest slice of a history is a guess.

Edit counts are weaker evidence than surviving text, so the interface says “most edited by”, never
“written by”. See [ADR-0005](decisions/0005-attribution-ladder.md).

### Rung 3 — the count alone

No names. The aggregate is still served, and the sentence is about it.

### Who may be named

One rule, `should_highlight_contributor`, shared by both rungs. An account is excluded when it is
missing, when its name starts with `~`, when a local group contains `bot`, when a CentralAuth
global group contains `bot`, or when its username ends in `bot`.

The last two exist because the local flag misses real bots in both directions: `Addbot` is a bot
by global group only, and `Gallicbot` carries no flag anywhere. The name rule is a guess about
identity, and it excludes people called Talbot or Abbot as the accepted price of catching the
unflagged ones. Checks run cheapest-first, and the global lookup — one CentralAuth request per
account, cached per process — is reached only for accounts nothing else has excluded.
See [ADR-0006](decisions/0006-bot-exclusion.md).

### The aggregate count

- Keep anonymous and temporary actors in the historical distinct-contributor count.
- Subtract accounts that currently hold the MediaWiki `bot` right from that total.

The count uses the local bot right alone, not the wider rule above: it is a server-side filter,
and widening it would mean enumerating every contributor of every page. So the count can include
an unflagged bot the names exclude.

“Exclude temporary accounts” refers to public highlighting, not the aggregate count. The current
MediaWiki history-count endpoint does not provide a reliable temporary-account subtotal. The UI
therefore never names temporary accounts, while the linked aggregate can still include them.
See [ADR-0001](decisions/0001-attribution-policy.md).

## Priority and load control

| Priority | Source | Purpose |
| --- | --- | --- |
| 100 | Live gadget cache miss | Serve demonstrated reader demand first |
| 50 | Union of seven available daily top-1000 lists, plus articles readers opened in the last month, per active wiki | Keep likely requests warm |
| 10 | Demand-ordered backfill, opt-in wikis only | Grow long-tail coverage without starving demand |
| 5 | Retry of answers that fell back to edit counts | Replace a weak answer if WikiWho has since indexed the revision |

Workers retry transient failures with exponential backoff capped at six hours. A lease makes a
job recoverable after a worker crash. Two worker replicas are the conservative starting point;
increase concurrency only after agreeing on a safe rate with WikiWho operators.

The Analytics dataset can be published several days late. Prewarm scans backwards, skips `404`
days, and stops after finding seven available daily lists. Each run only queues pages whose newest
result has passed the 90-day window, so recurring popularity does not cause repeated WikiWho work.
It repeats that scan for every active wiki, isolating failures so one unavailable wiki does not
cancel the rest of the run.

Prewarm cost therefore grows with the number of wikis readers actually use, bounded at 1000 pages
per wiki per day and further reduced by the freshness check. Backfill cost does not grow at all
unless an operator opts a wiki in.

Backfill order is demand, not size. A daily job reads one published `pageview_complete` dump and
keeps the most-read page ids per opted-in wiki; the API request path marks the ids a reader
actually opened; the backfill ranks the second above the first and falls back to descending page
length once the ranking is spent. That ranking is also what the P5 sweep walks over, so the
answers retried first are the ones somebody is likely to see. See
[ADR-0010](decisions/0010-demand-and-usage-counters.md) for the bounds those two counters carry.

## Millions of pages

The schema stores only a compact top-three result, not WikiWho token payloads. At roughly one
kilobyte per ready row, several million current results fit within the nominal ToolsDB 25 GB
boundary, but indexes, historical revisions, queue rows, and backups reduce that headroom.

For sustained multi-million coverage:

1. Measure average row and index size after the first 100,000 pages.
2. Keep only the newest result per page plus a short revision grace period.
3. Rate-limit backfill to the capacity explicitly accepted by WikiWho.
4. Move to a dedicated Trove database before ToolsDB reaches operational limits. Universal serving
   makes this arrive sooner: all Wikipedias together hold on the order of 65 million articles.
5. If the gadget becomes default for readers, migrate the API behind Wikimedia production
   caching; Toolforge is appropriate for an opt-in prototype, not Wikipedia-wide request volume.

## Abuse and privacy

The public API accepts only analysable, enabled wikis and positive numeric IDs. Workers reject
missing, non-main-namespace, and stale revisions before contacting WikiWho. CORS limits browser
use to Wikipedia origins, although CORS is not authentication and cannot prevent scripted traffic.
Widening the origin regex to every Wikipedia does not change what an unauthenticated scripted
client could already do.

Revision-specific responses contain no reader identifier and are safe for public caching. The
service still receives an IP address and article ID on a cache miss, so access logs should use
short retention and must never be repurposed as reader profiles.

Attribution is public page-history data, but a credit under an article title is not the same act
as a page history, and it is not always welcome. Each wiki therefore maintains a list of articles
WikiPeople counts but does not name, at `Project:WikiPeople/opt-out` in its own project namespace. The
`optout-sync` job materialises that list into `page_optout`; the API applies it while building
every ready response, on both endpoints, so it cannot be sidestepped by the gadget or by a direct
request. An opted-out article still reports its full `distinct_contributors`. Nothing is deleted:
the list governs presentation, which is why an entry takes effect — and reverses — in minutes
without recomputation. See [ADR-0008](decisions/0008-article-opt-out.md).

The same reasoning covers a different question: whether to name an account the wiki has since
excluded. A credit under an article title speaks in the project's voice, and it should not
contradict the project — but a block is also an ordinary editorial event, so the line is drawn by
duration. `standing-sync` records each named account's local block and CentralAuth lock status in
`contributor_standing`, and the API drops accounts locked globally as a sanction, or under a
non-partial block longer than `MAX_VISIBLE_BLOCK_SECONDS`, ninety days by default. Both the lock
and the block are read together with their reasons, because each mechanism serves opposite
purposes — stewards lock the deceased as well as abusers, and administrators block people at
their own request — and a flag alone cannot tell a memorial from a ban. The rule is applied when the
response is built rather than when the result is computed, because a sanction changes without
anyone touching the article: baked into a stored row it would stay wrong for as long as that row
stays fresh. `distinct_contributors` is unchanged and the dropped share moves into
`other_contributors`. See [ADR-0009](decisions/0009-sanctioned-contributor-visibility.md).

## Known boundaries

- WikiWho attributes surviving source-wikitext tokens, not rendered prose or editorial quality.
- The bot subtraction reflects accounts that currently hold the `bot` right, not their status at
  the time of every historical edit.
- `Base.metadata.create_all()` creates a fresh schema but is not a migration framework. Introduce
  versioned migrations before changing a database that already contains production data.
- The gadget has no page-specific fixture; every article uses traceable Toolforge data.
- The opt-out list is materialised on a schedule, so an entry is live within fifteen minutes
  rather than instantly, and a wiki whose Action API is unreachable keeps the list it last read.
  A blanked or vandalised list page names everyone again until it is reverted; `/v1/stats` reports
  the per-wiki count so the collapse is observable.
- WikiWho covers Wikipedia only. Commons, Wikidata, Wiktionary, and Wikisource have no surviving-
  token provenance at all, so the dividing line is the project, not the language.
- Database-name resolution assumes every WikiWho language code is dash-free. A dashed code such
  as `be-tarask`, or coverage beyond Wikipedia, would require a real sitematrix lookup in
  `wikipeople/sites.py`.
- Toolforge is the prototype host. A default gadget for all readers requires a Wikimedia-scale
  request path and privacy review.
