# ADR-0008: A wiki decides which articles are counted but not named

- Status: Accepted
- Date: 2026-08-18
- Revised: 2026-08-29 — the list moved onto the single configuration page as a JSON array. See
  [ADR-0011](0011-on-wiki-display-policy.md#where-the-policy-is-written) for why.
- Algorithm version: unchanged (the opt-out changes what is presented, not what is computed)

## Context

WikiPeople's whole purpose is to name people. That is desirable almost everywhere and
uncomfortable in a few places: a biography of a living person whose article is contested,
a page at the centre of a dispute, an article whose most prolific contributor would rather
not be advertised as such next to it. The attribution is not secret — it is the page
history, one click away — but a sentence under the title is not the page history. It is a
credit, and a credit can be unwelcome.

There is already a way to switch WikiPeople off: a reader removes it from their own
`common.js`, or a wiki sets `"enabled": false`. Both are all-or-nothing and both belong to
the wrong person. The reader is not who the credit is about.

What was missing is a per-article decision, made by the community rather than by the
operator, applying to every reader rather than to the one who set it.

## Decision

**A list of articles is maintained on-wiki. The API enforces it.**

- The list is the `optOut` array on `User:Schiste/wikipeople-config.json`, the one page
  WikiPeople is configured on, named by `CONFIG_PAGE`. It was `Project:WikiPeople/opt-out`
  in wikitext until 2026-08-29; the entries and every rule below are unchanged, only where
  they are written is.
- An entry is a page title or a category title, as a string. A leading colon is tolerated
  and underscores are read as spaces, because that is how a title reaches someone copying it
  out of a URL. Anything that is not a string is skipped with a warning rather than failing
  the run. The page has a history, a watchlist and a talk page, and MediaWiki validates it
  as JSON on save, so a malformed list cannot be stored in the first place. Starter copies
  are tracked under [`config/`](../../config) and the tests hold them to the parser.
- A **category** entry covers the articles directly in that category. Categories are not
  walked recursively: one line must not be able to reach an unbounded and unreviewable
  part of the wiki, and a tree that genuinely needs covering is several lines, which is
  the point at which somebody has to look at what they are covering.
- An opted-out page is served with an **empty `contributors` list, its
  `distinct_contributors` count intact**, and `opted_out: true`. The sentence becomes
  "written by 47 people" instead of disappearing: the count is not a name, and removing it
  would hide that the article has a history at all.
- Enforcement is on **both `/v1` and `/v2`**, in the response-building path. A list that
  only one endpoint honours is a list with a way round it.
- The gadget is **unchanged**. It already renders the count-only sentence when the API
  sends no contributors, which is the same branch a page WikiWho could not rank has always
  taken. Enforcing client-side was rejected outright: an opt-out the API ignores leaves the
  names one direct request away.
- **Nothing is deleted or skipped.** The stored row keeps its contributors, and workers go
  on computing them. The names are public page-history data; what the list governs is
  whether WikiPeople presents them.

**The list is materialised by a scheduled job, not read on request.**

The serve path may not call MediaWiki (see [architecture](../architecture.md)). So
`config-sync` runs every fifteen minutes over the active wikis, resolves each entry to
article page IDs, and writes them to `page_optout`. The API then does one primary-key
lookup per ready response.

Page IDs rather than titles, for two reasons: the lookup needs no title normalisation on a
path that cannot afford any, and a page keeps its ID across a rename, so the opt-out
follows the article rather than the name someone happened to list it under.

**A failure to read the list is not an empty list.**

An empty list is a legitimate instruction — it means nobody is opted out any more. A
network error is not, and neither is a page that does not parse. The sync only writes after
MediaWiki has actually answered *and* the answer was understood; anything else raises, that
wiki is skipped, and its stored list is left exactly as it was. A missing page *is* an
answer, and means nobody has opted out on that wiki.

## Consequences

Adding a page to the list takes effect within fifteen minutes plus the five-minute reader
cache from [ADR-0007](0007-cache-validation.md), with no recomputation. Removing one is
just as fast and just as cheap, which is what makes the list safe to edit: a mistake costs
twenty minutes, not a backfill.

The ETag work in ADR-0007 is what makes this land at all. Before it, a reader holding the
named answer would have gone on drawing it for a day, because nothing in the URL says
whether a page is opted out. `tests/test_onwiki.py` asserts that a copy holding the names
no longer validates as current.

The list is editable by whoever owns the configuration page, and that asymmetry is
deliberate: adding an entry only ever hides names, while removing one reveals them, and the
page history makes the second visible. Blanking the page un-opts-out the wiki until it is
reverted. That is the cost of putting the control where the people it concerns can reach
it, and `/v1/stats` reports the per-wiki count so a collapse is noticeable.

Because the opt-out is presentation and not computation, `ALGORITHM_VERSION` does not move
and cached rows stay valid. The corollary is that the stored table still holds the names of
an opted-out page. Anyone who considers that unacceptable is asking a different question —
whether WikiPeople should hold the data at all — which is a decision about retention, not
about this list.

`page_optout` is a new table. `create_all()` creates it on first start; no migration is
required.
