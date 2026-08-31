# Toolforge operations runbook

This runbook is the handoff reference for deploying and maintaining WikiPeople. Never commit
credentials, `.env`, database dumps, or Toolforge account files.

## Required ownership

Before production use, ensure at least two maintainers have:

- access to the GitHub repository;
- membership in the Toolforge `wikipeople` tool;
- permission to inspect webservice and job logs;
- access to the on-wiki gadget or personal-script pages;
- a documented contact path to WikiWho operators.

Update `WIKIPEOPLE_USER_AGENT` whenever the operational contact changes.

The personal-script prototype lives on the user-page subpages `wikipeople.js` and `wikipeople.css` of
each maintainer's account, on whichever wiki they use. Its `common.js` must not simultaneously
import the former `ContributeursHumains` filenames.

## External dependencies

| Dependency | Purpose | Failure behavior |
| --- | --- | --- |
| Wikipedia Action API, per wiki | Page/revision validation and user resolution | Job retries |
| MediaWiki REST history counts | Aggregate contributor count | Job retries |
| WikiWho API, per language | Surviving-token provenance | Job retries with backoff |
| Wikimedia Analytics API | Popular-page prewarming | Only the scheduled prewarm fails |
| ToolsDB MariaDB | Results, leases, durable queue, backfill cursor | API health fails; workers stop |

The API web process never calls these upstream services. Only workers and scheduled jobs do.

## First deployment

1. Create or join the Toolforge tool. If its name is not `wikipeople`, update the image names in
   `jobs.yaml` and `TOOLFORGE_API_BASE` in `wikipeople.js`.
2. Create the ToolsDB database named `${TOOL_TOOLSDB_USER}__wikipeople` using the credential-user
   prefix required by Toolforge. The application automatically consumes Toolforge's injected
   `TOOL_TOOLSDB_USER` and `TOOL_TOOLSDB_PASSWORD`; do not copy those secrets into the repository.
   Set `TOOLSDB_DATABASE` only if a different database suffix is intentionally used.
3. Configure `WIKIPEOPLE_USER_AGENT` with a monitored contact address or user page.
4. Build from the public repository:

   ```bash
   toolforge build start https://github.com/schiste/wikipeople
   ```

5. Start the webservice defined by `service.template`:

   ```bash
   toolforge webservice buildservice start --mount=none
   ```

6. Load the continuous and scheduled jobs:

   ```bash
   toolforge jobs load jobs.yaml
   ```

7. Check the service and jobs:

   ```bash
   curl -fsS https://wikipeople.toolforge.org/healthz
   toolforge jobs list --output long
   ```

8. Request one real page/revision, run or wait for a worker, and verify the transition from
   `202 pending` to `200 ready` before publishing the gadget URL.

No database migration is required for the page-freshness release: it reuses the existing
`computed_at` column and page/algorithm index. Its three environment controls are:

- `PAGE_FRESHNESS_SECONDS` (default `7776000`, 90 days) — how long a stored row stays usable;
- `PAGE_CACHE_SECONDS` (default `300`, five minutes) — how long a browser may reuse an answer
  without revalidating. Keep it short: it is the delay between deploying a policy change and
  readers seeing it. Every response carries an `ETag`, so the revalidation this buys is normally
  a `304` with no body;
- `READY_CACHE_SECONDS` (default `300`) — the same, for the legacy v1 endpoint;
- `PAGE_STALE_WHILE_REVALIDATE_SECONDS` (default `604800`, seven days) — beyond the window above
  the cached answer is still shown immediately while the refresh happens behind it, so a short
  window costs no waiting.

After deploying a change to `ALGORITHM_VERSION`, readers get the new answer on their next page
view once their five-minute window lapses. Before ETags existed this took up to a day on v2 and a
year on v1; if you are debugging a stale answer in a browser, check `sessionStorage` for
`wikipeople:*` as well, which the gadget holds for five minutes.

The opt-out release adds the `page_optout` table and the display-policy release adds
`wiki_display_policy`; `create_all()` creates both on first start. They are fed from one on-wiki
page by one job, under two environment controls:

- `CONFIG_PAGE` (default `User:Schiste/wikipeople-config.json`) — the single page WikiPeople is
  configured on, read on every active wiki. It carries the gadget's own options, which the browser
  reads and the server ignores, and four the server applies: `optOut`, `contributorNames`,
  `sanctionedAccounts` and `anonymisedAccounts`. `User:` is a canonical prefix MediaWiki resolves
  per wiki, so always set it in canonical form. A localized prefix only resolves on the wiki it
  came from: `Utilisateur:…` is userspace on frwiki but a *mainspace article title* on enwiki, and
  the sync reads every active wiki, so a stray article by that name would become that wiki's
  configuration. The same title must be set in `wikipeople.js`, where `CONFIG_OWNER` and
  `CONFIG_PAGE_SUFFIX` compose it; `tests/test_config.py` fails if the two drift apart;
- `OPTOUT_CATEGORY_LIMIT` (default `5000`) — how many articles one `optOut` category entry may
  cover. Categories are not walked recursively. A category past the cap is logged as truncated by
  `config-sync`, and that log line is the only signal, so read it.

A wiki with no configuration page is served the defaults. A page that cannot be fetched, or that
does not parse as JSON, leaves the stored configuration exactly as it was — an error is not an
instruction, while an empty `optOut` array is one.

See [ADR-0008](decisions/0008-article-opt-out.md) for what the list does and does not do, and
[ADR-0011](decisions/0011-on-wiki-display-policy.md) for why the two live on one page.

The display-policy release also adds `has_user_page` to `contributor_standing`, so a name is not drawn as a link to a user
page that does not exist. **On a database that predates it, `create_all()` will not add the
column**: run `ALTER TABLE contributor_standing ADD COLUMN has_user_page BOOLEAN NULL` before
deploying, or every read of the table fails. Nullable on purpose — "nobody has looked yet" and
"there is no page" are opposite answers, and only the second may take a link away, so the column
starts null everywhere and the next `standing-sync` fills it.

See [ADR-0011](decisions/0011-on-wiki-display-policy.md) for the vocabulary and why an unlinked
name says nothing about why it is unlinked, and the [on-wiki setup guide](onwiki-setup.md) for
what each option accepts.

The sanctioned-contributor release adds the `contributor_standing` table, which
`create_all()` creates on first start. Its controls are:

- `HIDE_SANCTIONED_CONTRIBUTORS` (default `true`) — whether an account the wiki has lastingly
  excluded is dropped from the names. Switching it off leaves `standing-sync` running, so
  switching it back on takes effect on the next response rather than on the next run;
- `MAX_VISIBLE_BLOCK_SECONDS` (default `7776000`, ninety days) — the longest block an account may
  carry and still be named. Indefinite blocks exceed every threshold, as do global locks whose
  steward reason reads as a sanction; a lock recorded as deceased, vanished or compromised does
  not withhold a name, and neither does one whose reason cannot be read. A block whose own
  reason reads as a courtesy — blocked at their own request, or because the account was
  compromised — does not withhold a name either, whatever its duration. The two tests take
  opposite defaults on purpose, and ADR-0009 says why. Equal by
  coincidence to `PAGE_FRESHNESS_SECONDS` and unrelated to it: one is about when an answer goes
  stale, the other about what a community has decided about a person. Do not tie them;
- `MAX_VISIBLE_BLOCK_SECONDS_BY_WIKI` (default empty) — per-wiki overrides as
  `frwiki:2592000,enwiki:0`, where `0` withholds the name of anyone under an active non-partial
  block. A malformed pair is dropped and that wiki keeps the global default, because these are
  read at import time in the web process;
- `STANDING_LOCK_CHECKS_PER_RUN` (default `500`) — CentralAuth answers about one account per
  request, so lock checks are rationed and rotate, never-checked accounts first. Blocks are
  refreshed for every tracked account on every run. At the defaults, and with the current few
  thousand named accounts, every lock is confirmed within a day; the job logs when it caps out,
  and that log line is the only signal that the rotation is falling behind;
- `STANDING_LOCK_RECHECK_SECONDS` (default `86400`) — how old a lock check may be before that
  account rejoins the queue.

Expect roughly an hour between a block being imposed and the name disappearing, and up to a day
for a global lock alone. The opt-out list stays the fast path when something must go now. See
[ADR-0009](decisions/0009-sanctioned-contributor-visibility.md) for the rule and its edge cases.

Both reasons are stored, in `block_reason` and `lock_reason`, because neither flag says what the
sanction means. **On a database that predates them, `create_all()` will not add the columns** —
it only creates missing tables. Run `ALTER TABLE contributor_standing ADD COLUMN block_reason
TEXT NULL` and the same for `lock_reason` before deploying, or every read of the table fails.
`TEXT` rather than a bounded column on purpose: administrators write long block reasons, and a
truncated one could hide a courtesy phrased late and withhold the name. If a courtesy wording is found being read as a sanction, the fix is to extend the patterns
in `policy.py` and deploy; nothing is recomputed and no row needs editing.

The universal-wiki release adds the `active_wikis` table, which `create_all()` creates on first
start; no migration is required either. Its environment controls are:

- `SUPPORTED_WIKIS` (default `*`): wikis served on demand. A list of database names narrows it.
  A wiki WikiWho cannot analyse is never served, whether or not it appears here.
- `PREWARM_WIKIS` (default empty): wikis pinned for daily top-1000 prewarming, in addition to the
  wikis discovered automatically.
- `BACKFILL_WIKIS` (default empty): wikis crawled article by article. Never inferred.
- `WIKIWHO_LANGUAGES` (default empty): overrides the built-in WikiWho coverage list.
- `BACKFILL_BATCH_SIZE` (default `500`): articles enqueued per hourly backfill batch.
- `REPLICA_HOST_TEMPLATE` (default `{wiki}.analytics.db.svc.wikimedia.cloud`): only worth
  changing to point the backfill at the web replicas, which is the wrong cluster for a scan.
- `TOOL_REPLICA_USER` / `TOOL_REPLICA_PASSWORD`: set by Toolforge, not by the maintainer. The
  backfill reads them to order its work; without them it falls back to title order.
- `CORS_ORIGIN_REGEX` (default matches every Wikipedia, desktop and mobile).

The demand release adds the `page_demand` and `usage_counters` tables, which `create_all()`
creates on first start; no migration is required. Its controls are:

- `PAGEVIEW_DUMP_ROOT` (default `/public/dumps/public/other/pageview_complete`): the NFS mount
  the `demand` job reads. The job logs and stops when the path is absent, which is what makes it
  a no-op off Toolforge without needing a flag to say so.
- `PAGEVIEW_LOOKBACK_DAYS` (default `5`): how far back to look for the newest published day.
- `PAGEVIEW_MINIMUM_VIEWS` (default `5`): a memory bound, not a threshold of merit. Raise it if
  the job is killed for memory on a large wiki; lower it only with more memory in `jobs.yaml`.
- `DEMAND_TOP_PAGES` (default `50000`): how much of one day's ranking is kept per wiki.
- `REQUESTED_PREWARM_DAYS` / `REQUESTED_PREWARM_LIMIT` (default `30` / `2000`): how far back and
  how wide the daily pass over articles readers actually opened reaches.
- `REQUESTED_RECOMPUTE_SECONDS` (default `604800`): how often such a page may be re-checked
  against its current revision. Below this it is left to settle whatever it does on the wiki.
- `RECOMPUTE_BATCH_SIZE` / `RECOMPUTE_MIN_AGE_SECONDS` (default `200` / `1209600`): the size of
  one weak-metric sweep and how long a fallback answer must have sat before it is retried.

Toolforge environment-variable configuration is deployment state, not source code. Record the
variable names—not their values—in the maintainer handoff.

## Enabling a wiki

Serving requires no action: `SUPPORTED_WIKIS=*` already answers for every WikiWho-covered
Wikipedia, and the first result a worker stores enrols that wiki into daily prewarming. What
remains is a community conversation before the script is advertised on that wiki.

Check current state with `GET /v1/stats`: `supported_wikis` is the configured enablement,
`active_wikis` is what is actually being prewarmed.

To pin a wiki before its first reader arrives, add it to `PREWARM_WIKIS`. To prime it by hand:

```bash
python -m wikipeople.prewarm --wiki dewiki --days 1
```

Enable `BACKFILL_WIKIS` only after measuring row size. English Wikipedia alone has millions of
articles against a nominal 25 GB ToolsDB boundary.

### Why the backfill works down from the heaviest article

No wiki gets backfilled to completion. frwiki has 2.8 million articles outside redirects and the
worker sustains roughly 13,000 attributions a day, so a full pass is seven months; enwiki is a
decade. The backfill is therefore not a job that finishes, and the only question it answers is
*which* pages are cached at any moment.

It used to walk `allpages`, which orders titles by byte value. `(` sorts before every letter, so
the walk entered frwiki's asteroid designations immediately and stayed there: after three days,
36,046 of 41,783 cached frwiki rows were `(NNNN) Name` articles, the newest with three
attributable tokens. Meanwhile 57% of the main namespace is redirects, which sort in among the
articles and were being attributed too.

So the source is now the analytics replica, ordered by `page_len` descending and filtered to
non-redirects — the index MediaWiki already keeps, `page_redirect_namespace_len`, is the one this
query needs. Size is a proxy for "an article where authorship has an answer worth caching", not a
merit ranking; it is used because it is a proxy the replica can sort on exactly. The 220,000
frwiki articles above 20 KB are about three weeks of work.

The cursor is a keyset, `page_len:page_id`, stored under `backfill:{wiki}:length-cursor`. The page
id is there to break ties: tens of thousands of pages share a length, and a batch boundary inside
a tie would otherwise skip or repeat them. A wiki whose replica is unreachable falls back to the
Action API in title order, under its own separate cursor key.

Redirects are refused twice, because filtering the source is not enough: a reader opening a
redirect makes the endpoint enqueue it, and the endpoint may not call MediaWiki to find out. The
worker has already fetched the page when it decides, so that is where the second refusal lives.

### Why demand comes before size

Size is a proxy for readership and a poor one. `pageview_complete`, published daily, is
readership itself, and it carries page ids — so a ranking arrives already keyed the way the
queue wants it, with no title to resolve. The `demand` job streams one day's file (640 MB, 55
million lines, about two and a half minutes), keeps the top `DEMAND_TOP_PAGES` per wiki above
`PAGEVIEW_MINIMUM_VIEWS`, and adds them to `page_demand`. Views accumulate across days, so a
steady readership outranks a one-day spike.

The API request path increments a second column on the same rows, and the backfill ranks by that
one first: a view says the world reads this article, a request says somebody running the gadget
opened it and waited. There are far fewer of the second and they are what a warm cache is for.
See [ADR-0010](decisions/0010-demand-and-usage-counters.md) for what these columns may and may
not hold.

Each hourly batch takes the top of the ranking, asks the replica for those pages' current
revisions in one round trip, enqueues them at P10, and marks them handed over. Marking rather
than a cursor, because the ranking changes every day and a keyset cursor over a moving ranking
skips rows and repeats others; marked after the enqueue rather than before, so a run that dies
against the replica retries next hour instead of dropping a batch for a quarter. A page the
replica no longer returns — deleted, or turned into a redirect since the dump — is marked too,
or it would sit at the top of the ranking for ever.

When the ranking runs dry the same run spends its remaining batches on the size walk above, so a
deployment with no pageview data yet behaves exactly as it did before. `page_demand` rows past
`PAGE_FRESHNESS_SECONDS` are put back in line by the daily job; the queue still refuses to redo
work that is fresh, so this decides eligibility, not recomputation.

### Retrying the answers that fell back to edit counts

14% of frwiki's cache carries `mediawiki-revision-count`: WikiWho had not indexed the revision,
so the ladder fell to counting edits ([ADR-0005](decisions/0005-attribution-ladder.md)). Nothing
ever revisited those — a revision WikiWho indexes next week raises no event — so a page that
missed by hours kept its weaker answer for as long as it stayed cached.

`weak-metric-recompute` is the listener. It sweeps `attribution_results` by primary key, a
cursor per (algorithm version, metric) in `app_state`, and re-queues weak rows at P5 against
**the same revision** they describe, so a better answer replaces the row rather than sitting
beside it. Rows recomputed within `RECOMPUTE_MIN_AGE_SECONDS` are skipped, which makes the job
self-limiting: a retry that lands on the same rung refreshes `computed_at` and drops out until
the window passes. Reaching the end of the table resets the cursor to zero rather than stopping.

There is no cap on how many pages may be cached, and no eviction. `cache-cleanup` only removes
superseded duplicates and old dead queue rows; a current result is never deleted to make room.
Growth is bounded by what the backfill enqueues, which is why its order is the design decision and
its rate is not.

### The on-wiki configuration page

One page per wiki holds every setting WikiPeople has, at `CONFIG_PAGE` — while the script is
personal, the maintainer's own `User:<name>/wikipeople-config.json`. Per-wiki starter copies are
published in [`config/`](../config); the full field reference and troubleshooting steps are in
[on-wiki setup](onwiki-setup.md).

The page has two readers with different latencies. The gadget fetches it in the browser, so an
edit to one of its six options propagates within minutes (`action=raw` is CDN-cached) and reaches a
given reader on their next browser session, or after 24 hours at the latest. The four options the
API applies are materialised by `config-sync` instead, so they take a quarter of an hour and then
apply to everyone at once, including a direct API caller.

Operationally this means the service has no say in what the page says, by design: installing,
configuring, and switching the script off all happen in user space with no rights and no
deployment. Expect to learn about a local opt-out from a page history, not from a ticket.

When a community adopts the script as a site-wide gadget, the page moves into that wiki's project
namespace, which is one environment variable here and one constant in `wikipeople.js`.

## Normal deployment

After merging a code change:

```bash
scp jobs.yaml login.toolforge.org:/mnt/nfs/labstore-secondary-tools-project/wikipeople/jobs.yaml
toolforge build start https://github.com/schiste/wikipeople
toolforge webservice buildservice restart
toolforge jobs load jobs.yaml
toolforge jobs restart attribution-worker
```

The first line is easy to forget and fails quietly. `toolforge build` reads the repository from
GitHub, but `toolforge jobs load` reads a copy of `jobs.yaml` that lives in the tool's home
directory and is not a checkout of anything. A deployment that adds or changes a job definition
loads the *old* file, reports success, and leaves the new job uncreated. `jobs load` prints one
line per job it loaded; count them against `jobs.yaml` before believing it.

The last line is not redundant. `jobs load` reconciles the job *definition*, and a deployment
that changes only code leaves that definition identical, so nothing is recreated and the
continuous worker keeps running the image it started with. The webservice restarts and the
workers do not, which is the worst version of a half-deployment: the API announces the new
`ALGORITHM_VERSION` while old workers compute the rows filed under it. Scheduled jobs need no
restart because each run starts a new pod.

Confirm the rollout before believing it:

```bash
toolforge jobs show attribution-worker    # "Started at" must be after the build
toolforge jobs list                       # every job in jobs.yaml must appear
curl -fsS https://wikipeople.toolforge.org/v1/stats
```

Then inspect webservice logs and check that both worker replicas are running.

## Job inventory

| Job | Type | Expected behavior |
| --- | --- | --- |
| `attribution-worker` | Continuous, two replicas | Claims durable jobs and calls WikiWho |
| `popular-prewarm` | Daily | Per active wiki, scans backward to enqueue seven available top-1000 lists at P50 |
| `demand-ranking` | Daily, 09:35 UTC | Streams one pageview dump into `page_demand` for `BACKFILL_WIKIS` entries; needs `mount: all` and 2 GiB |
| `gradual-backfill` | Hourly | Per `BACKFILL_WIKIS` entry, enqueues one batch of the most-wanted uncached articles at P10, falling back to the heaviest |
| `weak-metric-recompute` | Daily | Re-queues `RECOMPUTE_BATCH_SIZE` edit-count answers at P5 in case WikiWho has indexed them since |
| `cache-cleanup` | Weekly | Removes old failed work, superseded result revisions, and expired demand and usage rows |
| `config-sync` | Every 15 minutes | Per active wiki, materialises the on-wiki configuration page into `page_optout` and `wiki_display_policy` |
| `standing-sync` | Hourly | Per active wiki, refreshes block, lock and user-page status for named accounts into `contributor_standing` |

Live gadget misses and expired results enqueue P100 work. Prewarm and backfill skip any page with
a result younger than `PAGE_FRESHNESS_SECONDS`. Do not increase worker replicas until WikiWho
capacity and observed latency justify it.

Prewarm runtime grows with the number of active wikis, so watch its duration as wikis are
discovered. Each wiki is isolated: one unavailable wiki logs and is skipped rather than cancelling
the run. `gradual-backfill` does nothing while `BACKFILL_WIKIS` is empty, and so does `demand-ranking`.

`demand-ranking` is the only job that reads the dumps mount. Build-service images do not get it
by default, so its `mount: all` is load-bearing, and so is its `memory: 2Gi`: a day of one large
Wikipedia is held in a dictionary while the file streams. A missing dump is logged and skipped,
and a day already recorded is skipped too, so a re-run costs nothing.

## Monitoring

Check:

- `/healthz` for database reachability;
- `/v1/stats` occasionally for queue growth, dead items, the `active_wikis` list, `usage` (the
  last seven days of answers per wiki and outcome — a rising `unsupported` count means readers
  are loading the script on projects this deployment does not serve), `demand` (how much ranking
  the backfill has left), `metrics` (how much of the cache is still on the edit-count fallback),
  and the per-wiki `opted_out` counts — a count that drops to zero on a wiki that had entries means the
  list page was blanked, moved, or is being read from the wrong title;
- `/v2/{wiki}/pages/{page_id}?revision_id={revision_id}` for `is_fresh`, `refreshing`, and the
  `X-WikiPeople-Source-Revision` header on a known article;
- `toolforge webservice buildservice logs -f` for API errors;
- `toolforge jobs logs attribution-worker -f` for upstream or worker failures;
- ToolsDB size after 100,000, 500,000, and 1,000,000 ready rows;
- WikiWho latency, HTTP 408/429/5xx rates, and response-size failures.

Suggested initial alerts are: health failure for five minutes, no running worker, P100 queue growth
for one hour, or repeated WikiWho 429 responses.

## Common incidents

### Queue grows while workers run

Inspect WikiWho latency and rate responses. Pause `gradual-backfill` before adding worker replicas.
Reader-demand jobs have higher priority and should recover first.

### WikiWho is unavailable

Leave cached `200` responses online. V2 deliberately continues returning an expired result while
its refresh retries. Pause prewarm/backfill if the outage is prolonged. Jobs use exponential
backoff, and exhausted transient jobs can revive after `DEAD_RETRY_SECONDS`.

### A community wants the gadget off, or its wording changed

This is a local decision and needs no deployment. Each user sets `"enabled": false` in their own
`User:<name>/wikipeople-config.json`, or simply removes the import from their `common.js`. Removing
the wiki from `SUPPORTED_WIKIS` is the operator-side equivalent and is only needed when the wiki
must stop being served entirely.

### A wiki wants the count without the names, or wants a name unlinked rather than withheld

This needs no deployment. Set `contributorNames`, `sanctionedAccounts` or `anonymisedAccounts`
on the wiki's `CONFIG_PAGE`. Where that page does not exist yet, [`config/`](../config) holds a
starter copy per wiki: each states every option at its default and documents what each accepts.
`config-sync` picks it up within fifteen minutes.

A value the service does not recognise is not applied and not half-applied — the option keeps its
default — and the sync logs every key and value it ignored, which is the only signal that a typo
was saved. To see what a page will do before it takes effect:

```bash
python -m wikipeople.onwiki --wiki frwiki --dry-run
```

### An article should stop naming its contributors

This needs no deployment. Add the article — or a category it belongs to — as a string in the
`optOut` array on the wiki's `CONFIG_PAGE`. `config-sync` picks it up within fifteen minutes, and
readers see the change once their five-minute cache lapses. Removing the entry reverses it just as
quickly; nothing is recomputed either way.

To check what a list will cover before it takes effect:

```bash
python -m wikipeople.onwiki --wiki frwiki --dry-run
```

If a page seems not to be covered, the sync log names what it dropped and why: a redlinked title,
a title in a namespace that is neither article nor category, or a category past
`OPTOUT_CATEGORY_LIMIT`. A wiki whose Action API was unreachable keeps its previous list rather
than losing it, and says so in the log.

### A policy result is wrong

Do not edit cached rows manually. Fix the policy, increment `ALGORITHM_VERSION`, deploy, and let
requests create results in the new namespace. Keep the previous version available for rollback.

### Database schema must change

`create_all()` does not migrate existing tables. Take a backup, introduce a versioned migration,
test it against a copy, and deploy it before code that requires the new schema.

### Backfill must restart

Run `python -m wikipeople.backfill --restart` once, then let the scheduled job resume. Restarting
millions of pages is expensive; confirm the need first.

## Backups and retention

ToolsDB does not provide offline backups for tool-owned databases. Schedule or perform a
`mariadb-dump` before schema changes and periodically after substantial cache growth. Protect dump
permissions and store them outside the public repository.

Ready rows are reproducible from public upstream data. The durable queue and backfill cursor are
operationally useful but not irreplaceable. This reduces disaster-recovery urgency, not the need
to test restoration before a migration.

## Maintainer transfer checklist

- Add the new maintainer to GitHub and Toolforge before removing the previous one.
- Transfer the monitored email/user-page contact and update `WIKIPEOPLE_USER_AGENT`.
- Review Toolforge variables, job status, database name/size, and last backup.
- Share the WikiWho contact history and any agreed request-rate limits.
- Identify the on-wiki personal script/gadget pages and interface-administrator contacts.
- List the wikis in `active_wikis`, the on-wiki script and configuration pages in use on each,
  and any community agreements made when each wiki was enabled.
- Identify each wiki's opt-out list page and who watches it; a list nobody watches is a list that
  silently stops working.
- Review open incidents, dead queue reasons, algorithm version, and current gadget behavior.
- Rotate credentials that were personally controlled.
