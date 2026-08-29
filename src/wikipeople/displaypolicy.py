"""Materialise each wiki's on-wiki display policy into rows the API can read.

How much of an attribution should be shown is a community's decision, not an operator's.
Until now it was neither: the defaults were in the code and the one adjustable part lived
in an environment variable only the person running the tool could change. This job moves
the decision onto the wiki, on the same model as the opt-out list — an ordinary wikitext
page, read on a schedule, left behind as one row per wiki.

It has to be the server that reads it. The API withholds a sanctioned name entirely, so
no gadget setting could ever show one; a page that only the gadget honoured would be a
policy the API ignores, which is not a policy. It also has to be per wiki rather than per
reader: "name the people who wrote this" is a statement a project makes about its own
contributors, and a reader is not who it is about.

Only bulleted "key: value" lines are read. Headings, prose, the discussion that produced
a setting, and a worked example of a value nobody chose all cost nothing.
"""

from __future__ import annotations

import argparse
import logging
import re

from wikipeople.clients import MediaWikiClient
from wikipeople.errors import WikiPeopleError
from wikipeople.policy import DisplayPolicy, normalize_display_policy
from wikipeople.runtime import Runtime, build_runtime, configure_logging

LOGGER = logging.getLogger(__name__)

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Wiki markup around a key or a value is decoration, not part of it. `<code>` and bold
# quotes are what a maintainer reaches for to make a settings list readable, and a page
# that reads well should not thereby stop working.
_MARKUP = re.compile(r"<[^>]*>|''+|`")
# A bullet, a name, a colon or an equals sign, and the rest of the line. The name may
# not contain either separator, so "sanctioned-accounts : hide" splits where it looks
# like it splits.
_SETTING = re.compile(r"^\s*\*+\s*([^:=\n]+?)\s*[:=]\s*(\S.*?)\s*$")


def parse_display_policy_page(wikitext: str) -> dict[str, str]:
    """Return the settings a page states, as raw key/value strings, first one winning.

    Forgiving about everything except the shape of a setting, like the opt-out parser,
    and duplicates behave the same way there and here: the first line wins and a repeat
    has no effect. Two pages a community maintains should not have two rules.

    Nothing here decides whether a key or a value means anything —
    `normalize_display_policy` does, and it is the only thing that does.
    """
    settings: dict[str, str] = {}
    for line in _COMMENT.sub("", wikitext).splitlines():
        match = _SETTING.match(_MARKUP.sub("", line))
        if match is None:
            continue
        key = match.group(1).strip().lower().replace("_", "-").replace(" ", "-")
        value = match.group(2).strip().strip("\"'«»").strip().lower()
        if key and value:
            settings.setdefault(key, value)
    return settings


def _understood(stated: dict[str, str], policy: DisplayPolicy) -> dict[str, str]:
    """The subset of what a page stated that actually took effect.

    Recomputed from the resulting policy rather than tracked through the normaliser, so
    that the warning below cannot drift from what was really applied. A key that names a
    real option but sets it to the value it already had is not reported: nothing was
    ignored.
    """
    applied = {
        "contributor-names": "show" if policy.show_contributor_names else "hide",
        "sanctioned-accounts": policy.sanctioned_accounts,
        "anonymised-accounts": policy.anonymised_accounts,
    }
    return {key: value for key, value in stated.items() if applied.get(key) == value}


def sync_wiki(
    runtime: Runtime,
    mediawiki: MediaWikiClient,
    wiki: str,
    dry_run: bool = False,
) -> tuple[DisplayPolicy, bool]:
    """Bring one wiki's stored policy in line with its on-wiki page.

    Returns (policy, changed). Raises if MediaWiki cannot be reached, which leaves the
    stored policy exactly as it was: an unreachable wiki must not read as "we changed our
    minds back to the defaults" any more than it reads as "everybody withdrew their
    opt-out".
    """
    settings = runtime.settings
    wikitext = mediawiki.get_wikitext(wiki, settings.display_policy_page)
    if wikitext is None:
        # No page is a real answer and it means this wiki has decided nothing, which is
        # served as the defaults. Only reachable because the Action API replied; a
        # failure raised above.
        LOGGER.info("%s: no %s page, defaults apply", wiki, settings.display_policy_page)
        wikitext = ""

    stated = parse_display_policy_page(wikitext)
    policy = normalize_display_policy(stated)
    unread = sorted(set(stated) - set(_understood(stated, policy)))
    for key in unread:
        LOGGER.warning("%s: ignored setting %r = %r", wiki, key, stated[key])

    if dry_run:
        LOGGER.info("%s: page states %s", wiki, policy)
        return policy, False

    changed = runtime.repository.save_display_policy(wiki, policy)
    if changed:
        LOGGER.info("%s: display policy now %s", wiki, policy)
    return policy, changed


def resolve_target_wikis(runtime: Runtime, explicit: str | None) -> list[str]:
    """Wikis worth reading a policy for: the ones that have produced a result."""
    if explicit:
        return [explicit]
    return [wiki for wiki in runtime.repository.active_wikis() if runtime.resolver.is_capable(wiki)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the on-wiki WikiPeople display policies")
    parser.add_argument("--wiki", default=None, help="Sync one wiki instead of every active one")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what the page states without changing what is served",
    )
    args = parser.parse_args()
    configure_logging()

    runtime = build_runtime()
    runtime.database.create_schema()
    wikis = resolve_target_wikis(runtime, args.wiki)
    if not wikis:
        LOGGER.info("no active wiki to read a display policy for yet")
        return

    mediawiki = MediaWikiClient(
        runtime.settings.user_agent, runtime.settings.request_timeout_seconds, runtime.resolver
    )
    try:
        changed = 0
        for wiki in wikis:
            try:
                changed += int(sync_wiki(runtime, mediawiki, wiki, args.dry_run)[1])
            except WikiPeopleError as error:
                # One unreachable wiki keeps its previous policy rather than losing it.
                LOGGER.warning("%s: policy sync skipped, policy unchanged (%s)", wiki, error)
        LOGGER.info("%s of %s wikis changed their display policy", changed, len(wikis))
    finally:
        mediawiki.close()


if __name__ == "__main__":
    main()
