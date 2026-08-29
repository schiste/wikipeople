from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

BOT_NAME_PATTERN = re.compile(r"bot$", re.IGNORECASE)

# Stewards write lock reasons as free text, but the sanctioning ones are formulaic:
# "long-term abuse", "spam-only account", "lock evasion", "cross-wiki abuse",
# "globally or WMF banned user". None of the courtesy reasons share this vocabulary.
# A local block is imposed by one wiki on one account, and like a global lock the same
# mechanism serves opposite purposes: administrators block abusers, and they block the
# accounts of people who asked to be stopped from editing, whose password was stolen, or
# who have died. These are the wordings those blocks actually carry on the wikis served.
# The list is expected to grow — a courtesy phrased in a way it does not know stays
# withheld, so anything found missing belongs here.
BLOCK_COURTESY_PATTERN = re.compile(
    r"à sa demande|a sa demande|à la demande d|sa propre demande|own request|self-?request"
    r"|at their request|at his request|at her request|has retired|par sa demande"
    r"|compte compromis|compromised|compromis|hacked|piratag|piraté|mot de passe"
    r"|по (?:собственной )?(?:просьбе|запросу)|взлом|скомпрометирован"
    r"|right to vanish|\bRTV\b|vanish|deceas|décéd|decede|умер|скончал",
    re.IGNORECASE,
)

# "determined to not be compromised" is a real enwiki block reason. Without this the
# courtesy test would read the denial as the thing it denies.
BLOCK_COURTESY_NEGATION = re.compile(
    r"not (?:be )?compromised|non compromis|pas compromis|no evidence of compromise",
    re.IGNORECASE,
)

LOCK_SANCTION_PATTERN = re.compile(
    r"abuse|abusiv|\bspam|\blta\b|lock evasion|block evasion|\bbanned\b|\bban\b"
    r"|sock|vandal|harass|phishing|troll|\bufa\b|unauthori[sz]ed bot",
    re.IGNORECASE,
)

GlobalGroupLookup = Callable[[str], frozenset[str]]


@dataclass(frozen=True)
class ResolvedUser:
    user_id: int
    username: str
    groups: frozenset[str]
    missing: bool = False


def is_countable_token(value: str) -> bool:
    """Count words/numbers, while ignoring pure whitespace and wikitext punctuation."""
    return any(character.isalnum() for character in value)


def is_registered_editor(editor: str) -> bool:
    return editor.isdigit() and int(editor) > 0


def has_bot_name(username: str) -> bool:
    """Return whether a username ends in "bot", in any case.

    This is a guess about who an account belongs to, not a measurement of what it did,
    and it is wrong for every human whose pseudonym happens to end that way — Talbot,
    Abbot, Thibot. ADR-0006 records that cost being accepted deliberately: bot operators
    on Wikimedia wikis are required to name their accounts this way, so the rule catches
    unflagged and retired bots that no group membership identifies.
    """
    return BOT_NAME_PATTERN.search(username) is not None


def is_bot_group(group: str) -> bool:
    """Match any group whose name contains "bot", local or global.

    CentralAuth spells the same idea several ways (`bot`, `local-bot`, `global-bot`) and
    may add another. Matching the substring keeps a new spelling from silently making
    bots nameable again, at the cost of matching a hypothetical future group that
    contains "bot" without meaning one.
    """
    return "bot" in group.lower()


def should_highlight_contributor(
    user: ResolvedUser,
    global_groups: GlobalGroupLookup | None = None,
) -> bool:
    """Return whether an editor may appear among the three highlighted accounts.

    ADR-0001 excludes missing users, bots, and temporary accounts; ADR-0006 widens "bot"
    beyond the local flag. Checks run cheapest-first, and `global_groups` is consulted
    only for an account nothing else has already excluded, because it costs one HTTP
    request per account.

    Passing no lookup skips the CentralAuth check rather than failing: a caller with no
    client still gets the local rule. Both rankings pass one.

    Any change to this rule requires a new algorithm version so that cached results
    cannot mix two attribution policies.
    """
    if user.missing or user.username.startswith("~"):
        return False
    if any(is_bot_group(group) for group in user.groups):
        return False
    if has_bot_name(user.username):
        return False
    # Kept as a fourth "if ...: return False" rather than inlined into the return, so the
    # exclusion criteria read as one list. Adding a fifth should mean copying a line.
    if global_groups is not None and any(  # noqa: SIM103
        is_bot_group(group) for group in global_groups(user.username)
    ):
        return False
    return True


@dataclass(frozen=True)
class AccountStanding:
    """What a wiki has formally decided about an account, at the moment it was read.

    Deliberately separate from `ResolvedUser`. That one describes what an account *is*
    and is consumed while a result is being computed; this one describes what has been
    *decided about it*, changes without anyone touching the article, and is consumed
    while a response is being built. Keeping them apart is what stops a sanction from
    being baked into a stored row (ADR-0009).

    `blocked_at` is None when no block is active. `block_expires_at` is None for an
    indefinite block, which is why the two fields cannot be collapsed into a duration:
    "no block" and "a block that never ends" are opposite answers.
    """

    user_id: int
    username: str
    blocked_at: datetime | None = None
    block_expires_at: datetime | None = None
    block_partial: bool = False
    block_reason: str | None = None
    globally_locked: bool = False
    lock_reason: str | None = None


def is_sanction_lock(standing: AccountStanding) -> bool:
    """Return whether a global lock was imposed as a sanction rather than as a courtesy.

    CentralAuth has one lock mechanism and uses it for opposite purposes. Stewards lock
    abusers, and they also lock the accounts of editors who have died, who have exercised
    the right to vanish, or whose account was compromised. Reading the flag alone treats
    a memorial as a ban: the first production run of ADR-0009 withheld the credit of five
    deceased Wikipedians, which is the exact opposite of what the rule is for.

    So the flag is not enough and the reason decides. A lock withholds a name only when
    its reason affirmatively reads as a sanction; an unreadable, missing or unfamiliar
    reason leaves the account nameable. That is the same principle the rest of this
    module runs on — absence of data is not a finding — and it fails the safe way: the
    cost of missing a sanction is a name that stays up, while the cost of guessing wrong
    in the other direction is erasing someone who did nothing wrong.
    """
    if not standing.globally_locked:
        return False
    if not standing.lock_reason:
        return False
    return LOCK_SANCTION_PATTERN.search(standing.lock_reason) is not None


def is_courtesy_block(standing: AccountStanding) -> bool:
    """Return whether a local block was a courtesy to the account rather than a sanction.

    The mirror of `is_sanction_lock`, and it fails the other way round, deliberately. A
    lock is usually a courtesy, so a lock withholds a name only on proof that it is not.
    A block is usually a sanction — of the accounts withheld on the first live run, 536
    of 556 were blocked for something nobody would call a kindness — so a block withholds
    a name unless the reason proves otherwise.

    Requiring proof in the direction the data actually runs keeps both errors small. The
    residual cost is a courtesy worded in a way this does not recognise, which stays
    withheld; that is why the pattern above is meant to be extended rather than admired.

    Someone blocked at their own request has not been sanctioned: they asked to be
    stopped from editing, which is the opposite of a wiki withdrawing its trust. Someone
    whose account was compromised did nothing at all. Neither has forfeited the credit
    for what they wrote.
    """
    reason = standing.block_reason
    if not reason:
        return False
    if BLOCK_COURTESY_NEGATION.search(reason):
        return False
    return BLOCK_COURTESY_PATTERN.search(reason) is not None


def is_nameable_account(
    standing: AccountStanding | None,
    max_visible_block_seconds: int,
    now: datetime,
) -> bool:
    """Return whether an account may still be named among an article's contributors.

    The rule is about lasting exclusion from the community, not about sanction as such.
    A week's block is a normal editorial event and says nothing about the text the
    account wrote; being locked or blocked for years is the wiki saying this person is
    not one of its editors any more, and a credit line is a poor place to learn that.

    Evaluated when the response is built, never when the result is computed, so the
    threshold can be changed or the whole rule switched off without recomputing
    anything. See ADR-0009.

    An unknown account is nameable. Absence from the standing table means nobody has
    looked yet, not that a lookup came back clean — and a sync that never ran must not
    silently blank the names off a wiki. Same reasoning as the opt-out list: an empty
    answer is an instruction only when it is an answer.
    """
    if standing is None:
        return True
    if is_sanction_lock(standing):
        # A lock imposed as a sanction has no expiry to compare against; it is the
        # strongest statement the movement makes about an account, and it is global, so
        # no local threshold applies to it.
        return False
    if standing.blocked_at is None:
        return True
    if standing.block_partial:
        # A block scoped to a page or a namespace is a targeted editorial remedy, not a
        # ban. Someone barred from one article may be a respected contributor elsewhere.
        return True
    if is_courtesy_block(standing):
        # Blocked at their own request, or because the account was compromised. The
        # wiki acted for this person, not against them.
        return True
    if standing.block_expires_at is None:
        return False
    if standing.block_expires_at <= now:
        # The row outlived the block it describes. MediaWiki keeps no trace of an expired
        # block, so the next sync will drop it; until then, treat it as already gone
        # rather than serving a sanction the wiki has stopped applying.
        return True
    duration = standing.block_expires_at - standing.blocked_at
    return duration.total_seconds() <= max_visible_block_seconds


# The placeholder a rename leaves behind. A global rename that the account holder does
# not name themselves — which in practice means the right to vanish — replaces the
# username with "Renamed user 4501e2a3c" or "Vanished user 12345", and that string then
# appears in the page history as the author of everything they wrote. It is a real
# account and it really wrote the text, so it must not be dropped: hiding it would move
# its share into "and 46 others" and misattribute the article. But printing it credits
# the article to a number, and links to a user page that was deleted on the way out.
#
# Anchored at both ends, and the tail is one alphanumeric token, because the cost of the
# two errors is not symmetric. A placeholder read as an ordinary name is shown unlinked
# and looks slightly odd; an ordinary name read as a placeholder erases a real person's
# credit. "Vanished userland fan" is a username this could plausibly have eaten, and does
# not, at the price of missing a form nobody has seen yet. Either separator is a space
# in the same name — a title carries underscores in a URL and spaces in an answer.
ANONYMISED_NAME_PATTERN = re.compile(r"(?:renamed|vanished)[ _]?user[ _]?[0-9a-z]*", re.IGNORECASE)

#: How a name may be presented once it is shown at all. `link` is the ordinary case.
#: `unlink` is the name with no link, and it is deliberately one value covering several
#: different reasons — see `contributor_display`. `label` replaces the name with generic
#: wording, for an account whose name is a placeholder rather than a name.
DISPLAY_LINK = "link"
DISPLAY_UNLINK = "unlink"
DISPLAY_LABEL = "label"

#: Only ever a policy value, never a `display` value: an account a wiki has chosen to
#: hide is absent from the list, which is not the same as being in it marked hidden.
DISPLAY_HIDE = "hide"

SHOW = "show"

#: Every option the on-wiki policy page can set, and the values each one accepts. A key
#: that is not here is not read, and a value outside its list is not obeyed and not
#: half-obeyed: the default applies. This is the server twin of the gadget's
#: `ALLOWED_VALUES`, and for the same reason — a page carrying a typo, or copied from a
#: later revision of the service, must change nothing rather than something unpredictable.
DISPLAY_POLICY_VALUES: dict[str, tuple[str, ...]] = {
    "contributor-names": (SHOW, DISPLAY_HIDE),
    "sanctioned-accounts": (DISPLAY_HIDE, DISPLAY_UNLINK, DISPLAY_LINK),
    "anonymised-accounts": (DISPLAY_LABEL, DISPLAY_HIDE, DISPLAY_UNLINK, DISPLAY_LINK),
}


def is_anonymised_account(username: str) -> bool:
    """Return whether a username is the placeholder left behind by a rename.

    A guess about a string, not a fact read from anywhere — CentralAuth does not expose
    "this name is a placeholder" — so it is written to fail towards leaving a name alone.
    See `ANONYMISED_NAME_PATTERN`.
    """
    return ANONYMISED_NAME_PATTERN.fullmatch(username.strip()) is not None


@dataclass(frozen=True)
class DisplayPolicy:
    """How much of an attribution a wiki wants shown, as its community decided.

    Every field has a default, and the defaults are what the service does with no page
    at all — which is what almost every wiki will run, so the defaults are the policy
    rather than a placeholder for one. ADR-0011 argues them rather than deferring them.

    Not a filter on what is computed. Applied when the response is built, like the
    opt-out and the sanction rule, so a wiki that changes its mind is served differently
    within the hour with nothing recomputed and `algorithm_version` unmoved.
    """

    show_contributor_names: bool = True
    sanctioned_accounts: str = DISPLAY_HIDE
    anonymised_accounts: str = DISPLAY_LABEL


def normalize_display_policy(values: dict[str, str]) -> DisplayPolicy:
    """Turn what a page says into a policy, keeping only what it says unambiguously.

    Absence of an instruction is not an instruction: a key the page does not mention
    keeps its default, and so does a key whose value is not one this understands. That
    is the same rule the gadget applies to its configuration page and the same reason —
    "sanctioned-accounts: unlnik" must not become a fourth behaviour nobody wrote.
    """
    accepted = {
        key: value
        for key, value in values.items()
        if key in DISPLAY_POLICY_VALUES and value in DISPLAY_POLICY_VALUES[key]
    }
    return DisplayPolicy(
        show_contributor_names=accepted.get("contributor-names", SHOW) == SHOW,
        sanctioned_accounts=accepted.get("sanctioned-accounts", DisplayPolicy.sanctioned_accounts),
        anonymised_accounts=accepted.get("anonymised-accounts", DisplayPolicy.anonymised_accounts),
    )


def contributor_display(
    username: str,
    standing: AccountStanding | None,
    has_user_page: bool | None,
    policy: DisplayPolicy,
    max_visible_block_seconds: int,
    now: datetime,
) -> str | None:
    """How one account should appear, or None if it should not appear at all.

    Three questions in one place because their answers interact. The first two are
    policy — what this wiki wants done about a sanctioned account and about a renamed
    one — and the third is a fact: whether the user page a name would link to exists.
    Policy chooses between hiding, labelling and naming; the fact only ever decides
    whether a name that is being shown carries a link.

    Sanction is settled before anonymisation because it is the stronger statement, and
    because an account can be both: the right to vanish is usually accompanied by a
    lock, which `is_sanction_lock` already reads as the courtesy it is.

    `unlink` is one value with several causes — a wiki that unlinks sanctioned accounts,
    a wiki that unlinks renamed ones, and an account with no user page — and that is the
    point. There is no flag for a withheld name, and there must be no flag that can be
    read as one; an unlinked name says nothing about why, and on the default policy it
    only ever means the user page does not exist.
    """
    if standing is not None and not is_nameable_account(standing, max_visible_block_seconds, now):
        chosen = policy.sanctioned_accounts
    elif is_anonymised_account(username):
        chosen = policy.anonymised_accounts
    else:
        chosen = DISPLAY_LINK

    if chosen == DISPLAY_HIDE:
        return None
    if chosen == DISPLAY_LABEL:
        return DISPLAY_LABEL
    if chosen == DISPLAY_UNLINK:
        return DISPLAY_UNLINK
    # `has_user_page` is None when nobody has looked yet, and an account nobody has
    # looked at keeps its link: absence of data is not a finding, and a wiki whose first
    # sync has not run must not lose every link to say so.
    return DISPLAY_UNLINK if has_user_page is False else DISPLAY_LINK
