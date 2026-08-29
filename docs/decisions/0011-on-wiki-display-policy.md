# ADR-0011: A wiki decides how much of an attribution is shown

- Status: Accepted
- Date: 2026-08-29
- Algorithm version: unchanged (the policy changes what is presented, not what is computed)

## Context

Three separate complaints about the attribution box turn out to be one question asked
three ways.

The first is a bug. A name is drawn as a link to `User:Name`, and for a great many
contributors that page does not exist. The reader follows a blue link and lands on an
empty create form. The gadget cannot tell in advance without asking the wiki once per
article view, which is exactly the cost the wiki objected to when the box was proposed.

The second is worse than a bug. A global rename that the account holder does not name
themselves — which in practice means the right to vanish — replaces their username with
`Renamed user 4501e2a3c`. That string is what the page history carries, so it is what
WikiPeople prints: the article is credited to a number, linking to a user page that was
deleted on the way out. Dropping the account instead is not an option. It really did write
the text, and hiding it would move its share into "and 46 others", which is a
misattribution rather than a discretion.

The third is not a bug at all. Some editors want the count without the names, and some
want a sanctioned account's name shown without a link rather than withheld entirely
([ADR-0009](0009-sanctioned-contributor-visibility.md) withholds it). Both are reasonable,
both are the opposite of what someone else on the same wiki wants, and neither is a
question the person running the tool should be answering.

Which is the actual defect. `Settings.max_visible_block_seconds_for` already carries a
docstring saying the question "is a community's, not an operator's" — and then reads it
from an environment variable that only the operator can change. Every decision about how
much of an attribution is shown lives either in the code or in a `.env` file. The wiki
whose contributors are being named has no say in any of it.

## Decision

**A wiki states its display policy on an ordinary wiki page. The API applies it.**

- The page is `Project:WikiPeople/display`, resolved to each wiki's own project namespace
  the same way the opt-out list is. It is **wikitext, never a `.json` subpage**: MediaWiki
  locks `.json` pages to interface administrators, and "who gets named here" must not be a
  decision only an interface administrator can make.
- A setting is a bulleted `key: value` line. Everything else on the page — headings, the
  reasoning behind a setting, a link to the discussion that produced it — is ignored, and
  a duplicate key has no effect, both exactly as on the opt-out page. Two pages a
  community maintains should not have two rules. A starter copy is tracked at
  `docs/onwiki/display.fr.wiki` and `tests/test_displaypolicy.py` runs the parser over it.
- Three keys, with these defaults:

  | Key | Accepts | Default |
  | --- | --- | --- |
  | `contributor-names` | `show`, `hide` | `show` |
  | `sanctioned-accounts` | `hide`, `unlink`, `link` | `hide` |
  | `anonymised-accounts` | `label`, `hide`, `unlink`, `link` | `label` |

- **A value outside its list is not obeyed and not half-obeyed: the default applies**, and
  the sync logs what it ignored. This is the server twin of the gadget's `ALLOWED_VALUES`
  and exists for the same reason — `sanctioned-accounts: unlnik` must not become a fourth
  behaviour nobody wrote.
- `contributor-names: hide` is the count-only mode. It produces the same shape as an
  opt-out — empty `contributors`, `distinct_contributors` intact — but `opted_out` stays
  **false**, because that flag is about an article and nothing about the article was
  decided. The gadget needs no change to render it; it is the branch a page WikiWho could
  not rank has always taken.

**Each named contributor carries a `display` value, and `unlink` is deliberately opaque.**

`link` is the ordinary case, `unlink` is the name without a link, `label` replaces the name
with generic wording. `hide` is a policy value only: a hidden account is absent from the
list, which is not the same as being in it marked hidden.

`unlink` has three causes — an account with no user page, a wiki that unlinks sanctioned
accounts, a wiki that unlinks renamed ones — and they share one value on purpose.
[ADR-0009](0009-sanctioned-contributor-visibility.md) refuses to report a withheld name
because a flag for it would be a machine-readable announcement that one of the article's
main authors is banned. A `display` value that distinguished its causes would reintroduce
exactly that. So the gadget explains nothing about an unlinked name, and on the default
policy `unlink` only ever means the user page does not exist.

**Whether a user page exists is a fact the server carries, not a question the gadget asks.**

`contributor_standing` gains `has_user_page`, refreshed by `standing-sync` for every
tracked account on every run. MediaWiki answers about fifty titles per request, so this
costs what the block pass already costs — a few thousand accounts per wiki per hour — and
the gadget makes no extra request at all. The column is nullable because "nobody has looked
yet" and "there is no page" are opposite answers, and only one of them may take a link
away: an account nobody has looked at keeps its link.

**The policy is materialised by a scheduled job, not read on request.**

The serve path may not call MediaWiki. `display-policy-sync` runs every fifteen minutes
over the active wikis and writes one row per wiki into `wiki_display_policy`; the API does
one primary-key lookup per ready response, beside the one `is_opted_out` already does.

A failure to read the page is not a policy. The sync writes only after MediaWiki has
actually answered; anything else raises, that wiki is skipped, and its stored policy is
left exactly as it was. A **missing page is an answer** and means the wiki has decided
nothing, which is served as the defaults.

## Consequences

Editing the page takes effect within fifteen minutes plus the five-minute reader cache from
[ADR-0007](0007-cache-validation.md), with nothing recomputed, and reverses just as fast.
The defaults are what almost every wiki will run, so they are the policy rather than a
placeholder for one, and they are argued here rather than deferred:

- `sanctioned-accounts: hide` keeps ADR-0009 unchanged. A wiki that would rather show the
  name without the link now has somewhere to say so.
- `anonymised-accounts: label` is the only option that is neither a misattribution nor a
  credit to a number. `hide` moves the share into "and 46 others" and is offered because a
  wiki may prefer it, not because it is honest.

Detecting an anonymised account is a guess about a string — CentralAuth exposes no "this
name is a placeholder" flag — so `ANONYMISED_NAME_PATTERN` is anchored at both ends and
allows one alphanumeric tail token. The two errors are not symmetric: a placeholder missed
is shown unlinked and looks slightly odd, while an ordinary name mistaken for one erases a
real person's credit. `Vanished userland fan` is a username this could plausibly have eaten
and does not, at the price of missing a form nobody has seen yet. Same shape as the bot
rule in [ADR-0006](0006-bot-exclusion.md), failing the other way.

Because all of this is presentation, `ALGORITHM_VERSION` does not move and every cached row
stays valid — a wiki that changes its mind has its existing answers re-presented rather
than recomputed. `display` is an added field on an existing response, so `/v1` is not
broken; a client that ignores it behaves exactly as before. The gadget does not ignore it,
and the two ship together, because a client that ignores it would still print
`Renamed user 4501e2a3c`.

The page is editable by any registered user, with the same asymmetry as the opt-out list:
every setting except `link` only ever shows less, and the page history makes a change to
either direction visible.

`wiki_display_policy` is a new table, which `create_all()` creates on first start.
`has_user_page` is a new **column** on an existing table, which `create_all()` will not
add — see the [operations runbook](../operations.md) for the `ALTER TABLE` to run first.
