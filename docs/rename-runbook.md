# Renaming the tool: WikiFame → WikiPeople

This runbook covers one specific migration and can be deleted once it is done. It exists
because the rename is not one change but two of opposite natures, and doing them in the
wrong order breaks the gadget for readers.

## Why this is not a search-and-replace

**A Toolforge tool cannot be renamed.** The tool account, its `toolforge.org` subdomain and
its ToolsDB database prefix are all the same name, and the documented answer is to create a
new tool account and migrate to it
([Help:Toolforge/Tool accounts](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Tool_accounts)).
So the work splits in two:

| Half | Size | Reversible? |
| --- | --- | --- |
| Inside the repository | 444 occurrences, 4 casings, 20 renamed paths, 81 imports | yes, it is a commit |
| Outside the repository | new tool, new database, new URL, on-wiki pages | no, and it is visible to readers |

The second half is what this runbook is for. The first half is mechanical and the test
suite proves it.

## The ordering constraint

The gadget hard-codes the API base URL and the CSS class names its stylesheet targets.
Those three — the URL that answers, the published gadget, the on-wiki CSS — must change in
the same sitting. Everything else is prepared cold, before anyone notices.

**The single-writer rule.** Two tools running their jobs against two databases diverge from
the moment the data is copied. So the new tool is stood up and validated *without ever
writing*, and becomes the only writer at the moment of the switch. The divergence window is
then minutes, not days.

## Rules that hold throughout

- **`ALGORITHM_VERSION` does not move.** The current values — `surviving-tokens-v1`,
  `attribution-ladder-v3` — do not contain the tool name, so the rename cannot touch them by
  accident, but verify it before committing. Moving it would send 20,000 stored results back
  through a WikiWho that killed 548 jobs in the week this was written.
- **Keep the WikiWho contact continuous.** The user agent is what its operators have on file
  against the agreed rate. Renaming the variable is fine; changing the contact address in the
  same breath makes the tool look like a new client asking for capacity nobody granted.
- **Never commit credentials.** The database name and the environment variable *values* are
  deployment state. This file records variable names only.

## Phase 1 — Stand the new tool up, unused

1. **Maintainer:** create the `wikipeople` tool in
   [toolsadmin](https://toolsadmin.wikimedia.org/), then log out of SSH and back in. Tool
   creation cannot be automated from here.

   The tool's ToolsDB account is granted by a periodic job, not by tool creation. Until it
   runs, `mysql -h tools.db.svc.wikimedia.cloud` answers `ERROR 1045 Access denied` with
   perfectly correct credentials. Wait rather than debug it; it took about half an hour.
2. Build the image and start the web service:

   ```bash
   toolforge build start https://github.com/schiste/wikifame
   toolforge webservice buildservice start --mount=none
   ```

3. Create the database, as in the first-deployment procedure in
   [operations.md](operations.md): `CREATE DATABASE ${TOOL_TOOLSDB_USER}__wikipeople`
   (`CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`). The application creates the nine
   tables itself on first start.
4. Copy the configuration. `.env.example` documents around forty variables, but the old tool
   only ever *set* four beyond the `TOOL_REPLICA_*` and `TOOL_TOOLSDB_*` pairs that Toolforge
   injects on its own; everything else runs on its default.

   | Variable | Carried across |
   | --- | --- |
   | `BACKFILL_WIKIS`, `PREWARM_WIKIS` | verbatim |
   | `CONFIG_PAGE` | new name, and it replaces `OPTOUT_PAGE`: one page now carries the opt-out list and the display policy, so set the new variable and do not carry the old one across |
   | `WIKIFAME_USER_AGENT` → `WIKIPEOPLE_USER_AGENT` | product name only; **the contact address must not move** |

   Copy them tool to tool on the bastion rather than retyping, so the contact address cannot
   drift by a keystroke, and diff the result against the source with only the product name
   substituted. `TOOLSDB_DATABASE` is not needed: the default is `${TOOL_TOOLSDB_USER}__wikipeople`,
   which is already right. Neither is `METHODOLOGY_URL`, which defaults to the repository.
5. **Create no jobs.**

**Gate:** `https://wikipeople.toolforge.org/healthz` and `/v1/stats` answer, `/v1/stats`
reports `attribution-ladder-v3` and an empty cache, and all six tables exist with zero rows.

**Do not request a page from the new tool.** "Nothing writes" is true of the jobs, not of the
web service: a cache miss enqueues durable work and registers the wiki as active, so one
curiosity request leaves rows behind and the phase-3 migration then refuses the target as
non-empty. The two gate endpoints are read-only. Nobody else knows the URL yet.

## Phase 2 — Rename inside the repository

One commit, no reader impact, guarded by 198 tests and `node --check`.

| What changes | Count | Risk |
| --- | --- | --- |
| `src/wikifame/` → `src/wikipeople/`, and imports | 81 | none; the suite proves it |
| `wikifame.js` / `wikifame.css` → `wikipeople.*` | 2 files | none until published |
| CSS class names `wikifame-*` | 89 | **breaks on-wiki styling — phase 3** |
| `mw.hook( 'wikifame.summary' / '.history' )` | 2 | **breaks on-wiki scripts — phase 3** |
| `/wikifame-config.json` suffix | 1 | **breaks reader config pages — phase 3** |
| `TOOLFORGE_API_BASE` in the gadget | 1 | points at the new tool |
| `WIKIFAME_USER_AGENT` → `WIKIPEOPLE_USER_AGENT` | 6 | set in phase 1 |
| Docs, ADRs, README, `jobs.yaml`, `Procfile` | ~260 | none |

The hooks and class names are the gadget's public extension point (ADR-0004). They are
renamed in one go rather than shimmed: there is one known consumer, and a compatibility
layer emitting both sets is cruft that outlives its reason.

Also in this phase, the **migration script**, because it is code and belongs under test.
`src/wikipeople/migrate.py`, four tests.

### Why a script and not `mysqldump`

There is no `mysqldump` on the Toolforge bastion — only the `mysql` client. Dumping through
`SELECT` into TSV is not an option either: block and lock reasons are free text and now
legitimately contain tabs and newlines. A reason wrapped across lines would come back
truncated at the first tab, which is the erasure defect the column exists to prevent,
reintroduced by the export.

So migration reuses what the project already has. `models.py` defines the schema,
`create_schema()` creates it in the new database, and `migrate` streams each table through
the same SQLAlchemy metadata that wrote it. Six tables, 17 MB, ~34,000 rows.

It names both databases by environment variable rather than by argument, because a DSN
carries a password and arguments are visible to every user in `ps`:

| Variable | Points at |
| --- | --- |
| `MIGRATE_SOURCE_URL` | the old tool's database |
| `MIGRATE_TARGET_URL` | the new tool's database |

Both are required even though the second could be inferred, so that the script can never
write to the database it is reading.

**Where it runs.** On the new tool, as a one-off job: the module only exists in the new
image, and ToolsDB grants follow the user (`s…@%`), not the host, so the old tool's DSN
works from there. `toolforge jobs run` has no `--env`, so the two variables are set as tool
environment variables and removed straight after — see phase 3.

It refuses a non-empty target. That refusal is the safety story: four of the six tables have
composite primary keys, but `attribution_results` and `work_queue` autoincrement, so a
second pass would append rather than conflict and the doubled counts would read as success.
`--force-empty` is the recovery path from a half-finished run, and it has to be typed.

## Phase 3 — The switch

The only window where anything can look broken. Do it in one sitting.

1. Stop the old tool's jobs. There is now no writer.

   ```bash
   toolforge jobs delete attribution-worker    # and each scheduled job
   ```

2. Sweep the queue, so the copy does not carry the breakage across:

   ```bash
   toolforge jobs run cleanup-once --image tool-wikifame/tool-wikifame:latest \
     --command "python -m wikifame.cleanup --queue-days 0" --mount all --wait
   toolforge jobs logs cleanup-once && toolforge jobs delete cleanup-once
   ```

   `--queue-days 0` is the point. `cache-cleanup` runs weekly with a 30-day cutoff, and the
   dead rows worth dropping are hours old — WikiWho refuses in bursts, so the queue collects
   `upstream_unavailable` faster than any age-based rule will clear it. Zero means "every row
   already in state `dead` or `superseded`", and nothing else: `pending` and `leased` are
   untouched, and so are results. This has to run *after* the jobs stop, or the worker will
   have made more dead rows by the time the copy starts.

   A rehearsal on 2026-08-18 removed 726 rows (578 dead, 148 superseded) and 0 results.
   Results prune to zero because they are upserted on `uq_result_revision_algorithm`, so no
   superseded copy is ever left behind to find.

3. Run the migration from the new tool and compare the six table counts it prints:

   ```bash
   # `envvars create` reads stdin when the value is omitted, so build each DSN inside
   # the owning tool's account and pipe it. The password never reaches argv.
   become wikifame python3 dsn.py s57988__wikifame \
     | become wikipeople toolforge envvars create MIGRATE_SOURCE_URL
   become wikipeople python3 dsn.py s57997__wikipeople \
     | become wikipeople toolforge envvars create MIGRATE_TARGET_URL

   toolforge jobs run migrate-once --image tool-wikipeople/tool-wikipeople:latest \
     --command "python -m wikipeople.migrate --force-empty" --mount all --wait
   toolforge jobs logs migrate-once

   # redirect: this command echoes the value it deletes. See the warning below.
   toolforge envvars delete MIGRATE_SOURCE_URL --yes-im-sure > /dev/null 2>&1
   toolforge envvars delete MIGRATE_TARGET_URL --yes-im-sure > /dev/null 2>&1
   ```

   Build the new tool's image *before* this step, not in step 4 — the `migrate` module
   only exists in a build made after the phase-2 commit, and a stale image fails here
   with `No module named wikipeople.migrate`.

   `--force-empty` will be needed if the gadget went live first: the new host starts
   collecting enqueued work from the first reader who loads a page. Those rows are minutes
   old and re-enqueue on the next view, so replacing them costs nothing.

   It exits non-zero if any table's three counts disagree, but read them anyway.

   > **`toolforge envvars delete` prints the deleted value in clear text.** Not the name —
   > the value. Deleting a variable that held a DSN writes a database password to the
   > terminal and to whatever captured it: scrollback, a session log, an agent transcript.
   > There is no quiet flag, so redirect the output. That redirection is the whole fix, and
   > it is cheaper than everything below.

   If it is echoed anyway, measure the blast radius before asking anyone to rotate. The
   credential grants `ALL PRIVILEGES` on `sNNNNN__%` and `SELECT` on `%_p` — nothing else,
   no wiki rights and no shell — and `tools.db.svc.wikimedia.cloud` is not routable from
   outside WMCS, so spending it requires an already-approved developer account. The damage
   that would actually reach a third party is not deletion, which the workers recompute,
   but false rows in `attribution_results`, which the gadget renders under an article as a
   contributor's name.

   There is no self-service rotation. `replica.my.cnf` is written by the `maintain-dbusers`
   service and carries the immutable bit; an admin must run

   ```
   sudo /usr/local/sbin/maintain-dbusers delete tools.<tool> --account-type=tool
   ```

   on the cloudcontrol host that runs the service, after which it recreates the account.
   Reading its source is what makes this safe to ask for: `delete_account()` issues
   `DROP USER` on every host in `labsdbs.hosts` (ToolsDB is the `legacy`-grant one) and
   deletes the `account` row holding the hash, so the recreate generates a fresh password.
   `DROP USER` does not touch databases, and the username is `s<uidNumber>` from LDAP and
   therefore stable, so the rebuilt grants cover the same data. The catch: `TOOL_TOOLSDB_*`
   are documented as set once, not resynchronised, so the tool may keep presenting the old
   password until someone redeploys. Ask about that in the ticket rather than finding out.
4. Deploy the renamed repository to the new tool and start its jobs:

   ```bash
   scp jobs.yaml login.toolforge.org:/mnt/nfs/labstore-secondary-tools-project/wikipeople/jobs.yaml
   toolforge build start https://github.com/schiste/wikifame
   toolforge webservice buildservice restart
   toolforge jobs load jobs.yaml
   ```

   `jobs load` prints one line per job; count them against `jobs.yaml` before believing it,
   and check that `jobs show attribution-worker` reports a start time after the build. That
   restart has silently no-op'd more than once.
5. **Maintainer, in one session on-wiki**, four pages that are one step because between the
   first and the last the gadget is loading against names nothing answers to:

   | Page | What it is |
   | --- | --- |
   | `User:<name>/wikipeople.js` | the script |
   | `User:<name>/wikipeople.css` | its styles — the 89 renamed class names live here |
   | `User:<name>/common.js` | the two `importScript` / `importStylesheet` lines |
   | `User:<name>/wikipeople-config.json` | every setting, matching `CONFIG_PAGE` |

   The configuration page absorbs the old `wikipeople-optout` list as its `optOut` array;
   move the entries across in the same session and delete the wikitext page afterwards, not
   before. The gadget treats a missing page as "no settings" and the sync treats it as "this
   wiki decided nothing", so a gap between the two breaks nothing loudly — it just serves the
   defaults, which names everyone the list was hiding.
6. Replace the old tool's web service with 301 redirects to the new host. Stop the build
   service and start a lighttpd one over a `~/.lighttpd.conf` holding:

   ```
   url.redirect-code = 301
   url.redirect = ( "^/(.*)$" => "https://wikipeople.toolforge.org/$1" )
   ```

   ```bash
   toolforge webservice buildservice stop && toolforge webservice php8.2 start
   ```

   The capture group carries the path, and lighttpd appends the query string on its own —
   check a `/v2/...?revision_id=...` URL, not just `/v1/stats`, before believing it.

**Gate:** a known frwiki article returns the same names as before the switch, and
`wikifame.toolforge.org/v2/...` redirects.

This phase is also the moment to clear a known on-wiki defect, since the pages are being
rewritten anyway: `docs/onwiki/presentation.en.wiki` is stale. The old opt-out page's own two
defects — a `[[Discussion Wikipédia:…]]` link (namespace 5) where it meant the maintainer's
user talk page (namespace 3), and a lowercase `wikipeople` — disappear with the page itself.

## Phase 4 — Retire the old tool

Only after the grace period, and in this order:

1. Rename the GitHub repository. GitHub keeps a redirect, so a `toolforge build start`
   against the old URL keeps working; update it anyway.
2. Disable `wikifame` in toolsadmin. **Irreversible**, and it stops the redirects — so it is
   the last step, taken deliberately, not the tidy-up at the end of a long day.

## Rollback

| Phase | How to undo |
| --- | --- |
| 1 | Delete the new tool. Nothing else was touched. |
| 2 | `git revert`. Production still runs the old commit. |
| 3, before step 5 | Restart the old tool's jobs. It still holds its data; only minutes are lost. |
| 3, after step 5 | Re-publish the previous gadget and CSS from git history, and point it back. |
| 4 | None. This is what makes it the last step. |
