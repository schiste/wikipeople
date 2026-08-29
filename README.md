# WikiPeople

WikiPeople makes the people behind Wikipedia articles visible. Its first interface is a MediaWiki
gadget that displays a short attribution below an article title:

> Article rédigé par Alice, Bob, Charlie et 44 autres personnes.

> Article written by Alice, Bob, Charlie and 44 other people.

The three names link to user pages; the remainder links to the article history. The sentence is
localised into the reader's interface language, and each wiki can adjust its own wording.

One file runs everywhere. The gadget reports which wiki it is on, and the API serves any Wikipedia
WikiWho covers — around seventy language editions — with no per-wiki code change.

## Acknowledgements

The original idea—and the real creative brains behind it—came from **Amir Aharoni, his wife,
and their daughter**. This project turns their family idea into an open Wikimedia prototype.

## Components

- `wikipeople.js` and `wikipeople.css`: wiki-agnostic, localised personal-script prototype.
- `src/wikipeople/sites.py`: resolves a database name to a WikiWho language and Wikipedia host.
- `src/wikipeople/app.py`: read-only FastAPI service for the gadget.
- `src/wikipeople/worker.py`: durable WikiWho calculation worker.
- `src/wikipeople/prewarm.py`: preloads popular articles from Wikimedia pageviews.
- `src/wikipeople/backfill.py`: resumable, low-priority long-tail coverage.
- `src/wikipeople/cleanup.py`: queue and old-revision retention.
- `src/wikipeople/optout.py`: reads each wiki's on-wiki opt-out list into servable rows.

The gadget uses a page-level result for up to 90 days. After that period, the API serves the
last known attribution while a worker refreshes it asynchronously. Stored results still record
the exact source revision and calculation date for auditability.

See [the architecture and scaling rules](docs/architecture.md) for cache identity, attribution
policy, update behavior, privacy, and the path toward millions of articles.

## Documentation

- [Architecture and scaling](docs/architecture.md): data flow, cache identity, priorities,
  attribution rules, privacy, and capacity limits.
- [API contract](docs/api.md): stable request and response shapes consumed by the gadget.
- [Operations runbook](docs/operations.md): Toolforge deployment, jobs, monitoring, incidents,
  backups, and maintainer transfer.
- [On-wiki setup](docs/onwiki-setup.md): installing the script on a wiki and configuring it,
  for anyone who wants to run it.
- [Rename runbook](docs/rename-runbook.md): migrating the tool to the WikiPeople name. Delete
  this entry and the file once the migration is done.
- [ADR-0001](docs/decisions/0001-attribution-policy.md): accepted attribution policy and its
  known limitations.
- [ADR-0002](docs/decisions/0002-page-freshness.md): 90-day page freshness and stale-while-
  revalidate behavior.
- [ADR-0003](docs/decisions/0003-universal-wiki-support.md): universal wiki support, demand-driven
  prewarming, and per-wiki on-wiki configuration.
- [ADR-0004](docs/decisions/0004-on-wiki-extensibility.md): what a wiki may change from on-wiki
  pages, and what it may not.
- [ADR-0005](docs/decisions/0005-attribution-ladder.md): the fallback ladder below the token
  metric.
- [ADR-0006](docs/decisions/0006-bot-exclusion.md): what counts as a bot, beyond the local flag.
- [ADR-0007](docs/decisions/0007-cache-validation.md): why cached answers carry an `ETag` and
  expire in minutes rather than days.
- [ADR-0008](docs/decisions/0008-article-opt-out.md): the on-wiki list of articles counted but
  not named.
- [ADR-0009](docs/decisions/0009-sanctioned-contributor-visibility.md): why an account the wiki
  has lastingly excluded is not named, why duration draws the line, and why a block and a lock
  that say the same thing are read in opposite directions.
- [ADR-0010](docs/decisions/0010-demand-and-usage-counters.md): what is recorded about being
  used, why it is two counter tables and not a log, and how the backfill came to be ordered by
  readership rather than by article size.
- [ADR-0011](docs/decisions/0011-on-wiki-display-policy.md): why how much of an attribution is
  shown is a wiki's decision rather than the operator's, how a wiki states it, and why an
  unlinked name deliberately says nothing about why it is unlinked.
- [Contributing](CONTRIBUTING.md): local workflow and change checklist.
- [Agent guide](AGENTS.md): repository invariants and commands for coding agents.

## Local development

Python 3.11 or newer is required.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/uvicorn wikipeople.app:app --reload
```

Environment variables are read directly by the process. Export the values from `.env` with the
environment manager of your choice; the application intentionally does not load arbitrary files.

Initialize and exercise the asynchronous path:

```bash
.venv/bin/python -c 'from wikipeople.runtime import build_runtime; build_runtime().database.create_schema()'
.venv/bin/python -m wikipeople.worker --once
.venv/bin/python -m wikipeople.prewarm --days 1
```

Run the test suite:

```bash
.venv/bin/pytest
```

## Personal-script installation

Copy the repository files to your user-page subpages on any covered Wikipedia — for example
`User:YOUR_USERNAME/wikipeople.js` and `User:YOUR_USERNAME/wikipeople.css`, or their localised
namespace name such as `Utilisateur:` on French Wikipedia. Optionally add
`User:YOUR_USERNAME/wikipeople-config.json` from [`config/`](config). Then load the first two from
your `common.js`:

```javascript
importScript( 'User:YOUR_USERNAME/wikipeople.js' );
importStylesheet( 'User:YOUR_USERNAME/wikipeople.css' );
```

The same unmodified files work on every wiki. Where the API does not serve a wiki, the script
renders nothing.

Do not load the previous `ContributeursHumains` pages at the same time: both scripts would request
the same attribution and attempt to render a summary.

## Per-wiki configuration

Alongside the script, you can create `User:YOUR_USERNAME/wikipeople-config.json` on the same wiki. It
is optional — without it the script uses its built-in text in your interface language. Its one real
job is supplying the two local titles the script cannot guess:

```json
{
	"enabled": true,
	"showHistoryIntro": "anonymous",
	"editHelpPage": "Aide:Comment modifier une page",
	"sandboxPage": "Wikipédia:Bac à sable",
	"historyIntroPage": null,
	"messages": {}
}
```

The published files also carry a `"//"` block stating what each option accepts, which the script
ignores: a page edited on a wiki has to explain itself, and JSON has no comments.

Defaults per wiki are published in [`config/`](config): [`enwiki.json`](config/enwiki.json),
[`frwiki.json`](config/frwiki.json). Copy the one for your wiki; send a pull request if you work
out the titles for a wiki that has none yet.

Keeping this in user space means installing and configuring WikiPeople needs no special rights. When
a community adopts it as a site-wide gadget, the same file moves to
`MediaWiki:Wikipeople-config.json`.

Full instructions, field reference, and troubleshooting: [on-wiki setup](docs/onwiki-setup.md).

## Going further than settings

The JSON page stays declarative on purpose, so two escape hatches exist for anything it cannot
express:

- `historyIntroPage` names a **wikitext page** whose parsed content replaces the history-box text.
  Images, galleries, Commons video, and templates all work, because MediaWiki does the parsing and
  the sanitising. Translations go on `/fr`-style language subpages.
- `mw.hook( 'wikipeople.history' )` and `mw.hook( 'wikipeople.summary' )` fire with the rendered
  element, so arbitrary JavaScript goes in your own `common.js` rather than in a configuration
  page.

Nothing in a configuration page is ever executed or treated as markup. See
[ADR-0004](docs/decisions/0004-on-wiki-extensibility.md).

## Toolforge deployment

Follow the [operations runbook](docs/operations.md). Deployment requires a Toolforge tool,
ToolsDB database, maintainer contact in `WIKIPEOPLE_USER_AGENT`, Build Service image, webservice,
and the jobs declared in `jobs.yaml`. Toolforge-injected `TOOL_TOOLSDB_USER` and
`TOOL_TOOLSDB_PASSWORD` are used automatically; `DATABASE_URL` remains an explicit override.

## License

WikiPeople is licensed under the [GNU Affero General Public License v3.0](LICENSE). WikiWho API data
is published separately under CC BY-SA 4.0, and Wikimedia content remains subject to its own
licenses and terms.
