"""The published configuration defaults are an interface contract.

People copy these files onto wikis we do not control and cannot fix. A key the gadget stopped
reading, or a message key that no longer exists, would fail silently for that reader, so the
defaults and their documentation are checked against the gadget source rather than by hand.
"""

import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GADGET_SOURCE = (REPOSITORY_ROOT / "wikipeople.js").read_text(encoding="utf-8")
SETUP_GUIDE = (REPOSITORY_ROOT / "docs/onwiki-setup.md").read_text(encoding="utf-8")
DEFAULTS = sorted((REPOSITORY_ROOT / "config").glob("*.json"))
# The only options a published page is expected to set to something of its own.
TITLES = {"editHelpPage", "sandboxPage"}


def _block(source: str, opening: str) -> str:
    start = source.index(opening) + len(opening)
    depth = 1
    for offset, character in enumerate(source[start:]):
        depth += {"{": 1, "}": -1}.get(character, 0)
        if depth == 0:
            return source[start : start + offset]
    raise AssertionError(f"unbalanced braces after {opening!r}")


def _js_literal(source: str) -> object:
    """One JavaScript scalar as the Python value it stands for."""
    if source in ("true", "false"):
        return source == "true"
    if source == "null":
        return None
    if source == "{}":
        return {}
    return json.loads(source.replace("'", '"'))


def _defaults() -> dict[str, object]:
    """Every option the gadget reads, with the value that applies when it is unset."""
    block = _block(GADGET_SOURCE, "DEFAULT_CONFIG = {")
    pairs = re.findall(r"^\t\t(\w+): (.+?),?$", block, re.MULTILINE)
    return {key: _js_literal(value) for key, value in pairs}


def _config_keys() -> set[str]:
    return set(_defaults())


def _allowed_values() -> dict[str, list[object]]:
    """The options whose value is a choice rather than a title, and the choices."""
    block = _block(GADGET_SOURCE, "ALLOWED_VALUES = {")
    return {
        key: [_js_literal(item.strip()) for item in values.split(",")]
        for key, values in re.findall(r"^\t\t(\w+): \[ (.+?) \],?$", block, re.MULTILINE)
    }


def _message_keys() -> set[str]:
    return set(re.findall(r"'(wikipeople-[a-z-]+)':", _block(GADGET_SOURCE, "MESSAGES = {")))


def test_defaults_are_published_for_the_wikis_the_gadget_already_speaks() -> None:
    assert [path.name for path in DEFAULTS] == ["enwiki.json", "frwiki.json"]


def test_defaults_are_valid_json_using_only_keys_the_gadget_reads() -> None:
    known = _config_keys()
    assert known == {
        "enabled",
        "showHistoryIntro",
        "editHelpPage",
        "sandboxPage",
        "historyIntroPage",
        "messages",
    }

    for path in DEFAULTS:
        default = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(default, dict), path.name
        # Every option, stated. A published page is the only reference its reader has,
        # and an option it leaves out is one they will never know they can set.
        assert set(default) - {"//"} == known, path.name
        # The help sentence renders only when both titles are present.
        assert bool(default["editHelpPage"]) == bool(default["sandboxPage"]), path.name
        # Everything but the two local titles repeats the built-in default, so the day
        # a default changes here the published copies change with it. Rich content in
        # particular stays opt-in: a default must never point at a page that only
        # exists on the wiki it was copied from.
        stated = {key: value for key, value in _defaults().items() if key not in TITLES}
        assert {key: default[key] for key in stated} == stated, path.name
        assert set(default["messages"]) <= _message_keys(), path.name


def test_published_pages_state_every_option_with_its_default_and_its_allowed_values() -> None:
    """A wiki page is read where the repository is not.

    Someone edits this file on a wiki, months later, with no view of the gadget source
    and no reason to look for a repository. JSON has no comments, so the page carries a
    "//" block the gadget ignores, and that block has to answer what an option accepts
    without anyone having to try a value to find out.
    """
    defaults = _defaults()
    allowed = _allowed_values()

    for path in DEFAULTS:
        notes = json.loads(path.read_text(encoding="utf-8"))["//"]
        assert set(defaults) <= set(notes), path.name

        for key, default in defaults.items():
            assert json.dumps(default) in notes[key], (path.name, key)
            for value in allowed.get(key, []):
                assert json.dumps(value) in notes[key], (path.name, key, value)


def test_every_option_is_read_somewhere_other_than_where_it_is_validated() -> None:
    """An option nobody consumes is a promise the page makes and the script does not keep.

    normalizeConfig() touches all six by iterating DEFAULT_CONFIG, so it is cut out
    before looking: it proves a value survives validation, not that anything acts on it.
    """
    validation = _block(GADGET_SOURCE, "function normalizeConfig( parsed ) {")
    consuming = GADGET_SOURCE.replace(validation, "")
    unread = [key for key in _config_keys() if f"wikiConfig.{key}" not in consuming]

    assert unread == []


def test_setup_guide_documents_every_field_and_message_key() -> None:
    missing = [key for key in _config_keys() | _message_keys() if f"`{key}`" not in SETUP_GUIDE]

    assert missing == []


def test_setup_guide_states_the_values_each_option_accepts() -> None:
    """A rejected value falls back to the default and says nothing about having done so.

    That is the right behaviour and a miserable thing to debug, so what an option
    accepts has to be readable before the page is saved rather than inferred from the
    box that failed to appear.
    """
    missing = [
        (key, value)
        for key, values in _allowed_values().items()
        for value in values
        if f"`{json.dumps(value)}`" not in SETUP_GUIDE
    ]

    assert missing == []


def test_setup_guide_names_the_page_the_gadget_actually_fetches() -> None:
    suffix = re.search(r"CONFIG_PAGE_SUFFIX = '([^']+)'", GADGET_SOURCE)
    assert suffix is not None
    assert suffix.group(1).lstrip("/") in SETUP_GUIDE
    # The guide must not send people to a page only interface admins can create.
    assert "MediaWiki:Wikipeople-config.json" not in SETUP_GUIDE.split("## Later", 1)[0]
