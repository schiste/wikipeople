# Agent guide

These instructions apply to the whole repository.

## Mission and current scope

WikiPeople powers a Wikipedia personal-script prototype that displays three registered, non-bot,
non-temporary accounts associated with surviving WikiWho tokens and links the remaining historical
contributor count to page history. Toolforge is the prototype backend. One unmodified gadget file
serves every Wikipedia WikiWho covers.

## Invariants

- Never call WikiWho, MediaWiki, or Analytics from the FastAPI request path. Cache misses enqueue
  durable work and return `202`.
- Stored-result identity is `(wiki, page_id, revision_id, algorithm_version)`; do not key by
  title. V2 may serve the newest page/algorithm result until its configured freshness expires.
- Never highlight IPs, anonymous actors, temporary usernames (`~…`), missing users, or bots.
- A semantic policy change requires a new `ALGORITHM_VERSION` and documented decision.
- A breaking API change requires a new version rather than silently changing `/v1`.
- Do not store raw WikiWho token responses; retain only compact aggregate results.
- Do not commit secrets, Toolforge account files, database URLs, dumps, or `.env`.
- Keep the gadget backed exclusively by the Toolforge API; do not add production-page fixtures.
- Keep the gadget wiki-agnostic: no wiki name, host, namespace prefix, page title, plural form, or
  list separator may be hard-coded. Per-wiki settings belong in the on-wiki configuration page
  (`User:<name>/wikipeople-config.json` while this is a personal script).
- Nothing from a configuration page is ever executed or inserted as markup. Rich content comes from
  a wikitext page through MediaWiki's parser; JavaScript comes from `mw.hook`. No `eval`, no
  `innerHTML`.
- Capability and enablement stay separate. Whether a wiki can be analysed is derived in
  `sites.py`; whether it is served is configuration. Capability always wins.
- Universal serving never implies universal crawling. `BACKFILL_WIKIS` stays an explicit opt-in.
- An opt-out is enforced where the answer is built, never in the gadget, and on every endpoint. A
  page on the on-wiki list is served with no contributors and its full count. Failing to read the
  configuration page — unreachable, or no longer valid JSON — leaves the stored answer alone; an
  empty list is an instruction, an error is not.

## Repository map

- `wikipeople.js`, `.css`: MediaWiki gadget/personal script.
- `src/wikipeople/app.py`: cache-only HTTP API.
- `src/wikipeople/worker.py`: asynchronous calculation orchestration.
- `src/wikipeople/clients.py`: all external HTTP calls.
- `src/wikipeople/sites.py`: database name → WikiWho language → Wikipedia host, and enablement.
- `src/wikipeople/policy.py`, `attribution.py`: product rules and pure aggregation.
- `src/wikipeople/repository.py`, `models.py`: durable cache, queue, leases, retention.
- `prewarm.py`, `backfill.py`, `cleanup.py`, `onwiki.py`: scheduled jobs.
- `docs/api.md`: consumer contract.
- `docs/operations.md`: deployment and incident runbook.
- `docs/onwiki-setup.md`, `config/`: on-wiki configuration reference and per-wiki defaults.
- `docs/decisions/`: accepted product/architecture decisions.

## Required validation

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
node --check wikipeople.js
git diff --check
```

Tests must remain offline and deterministic. A live smoke test is optional and read-only.

## Database caution

SQLAlchemy `create_all()` only creates missing tables. It does not migrate deployed schemas. Once
production data exists, accompany model changes with a reviewed, backed-up, versioned migration.

## Handoff expectation

Update README links, API documentation, operations steps, ADRs, environment examples, and tests in
the same change whenever behavior or deployment requirements change. State explicitly what remains
local, uncommitted, undeployed, or dependent on an external decision.
