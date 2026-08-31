import re
from pathlib import Path

GADGET_PATH = Path(__file__).parents[1] / "wikipeople.js"
GADGET_SOURCE = GADGET_PATH.read_text()
GADGET_STYLES = (Path(__file__).parents[1] / "wikipeople.css").read_text()


def test_production_gadget_contains_no_page_fixture() -> None:
    assert "CONTRIBUTION_FIXTURES" not in GADGET_SOURCE
    assert "Victor Hugo" not in GADGET_SOURCE
    assert "Jean de la Fontaine" not in GADGET_SOURCE
    assert "ContributeursHumains" not in GADGET_SOURCE
    assert "contributeurs-humains" not in GADGET_SOURCE


def test_pending_attribution_is_retried_without_becoming_an_error() -> None:
    assert "data.status !== 'pending'" in GADGET_SOURCE
    assert "PENDING_RETRY_DELAYS_MS" in GADGET_SOURCE
    assert "Attribution en cours de calcul." not in GADGET_SOURCE


def test_gadget_uses_page_freshness_api_and_bounded_session_cache() -> None:
    assert "'/v2/'" in GADGET_SOURCE
    assert "CLIENT_CACHE_MAX_AGE_MS" in GADGET_SOURCE
    assert "computed_at" in GADGET_SOURCE
    cache_key_body = GADGET_SOURCE.split("function getCacheKey()", 1)[1].split(
        "function readCache", 1
    )[0]
    assert "wgCurRevisionId" not in cache_key_body


def test_gadget_is_wiki_agnostic() -> None:
    """The same file must ship unchanged on every Wikipedia."""
    assert "wgDBname" in GADGET_SOURCE
    assert "frwiki" not in GADGET_SOURCE
    assert "fr.wikipedia.org" not in GADGET_SOURCE
    # The session cache must not leak one wiki's attribution into another.
    cache_key_body = GADGET_SOURCE.split("function getCacheKey()", 1)[1].split(
        "function readCache", 1
    )[0]
    assert "wgDBname" in cache_key_body


def test_gadget_hardcodes_no_wiki_specific_page_titles() -> None:
    """Help and sandbox titles differ per wiki, so they come from local config."""
    assert "CONFIG_PAGE_SUFFIX" in GADGET_SOURCE
    assert "'/wikipeople-config.json'" in GADGET_SOURCE
    assert "Bac à sable" not in GADGET_SOURCE
    assert "Aide:Comment modifier une page" not in GADGET_SOURCE
    assert "'Utilisateur:'" not in GADGET_SOURCE


def test_every_config_value_the_gadget_reads_is_actually_requested() -> None:
    """mw.config.get returns only what it was asked for; a missing name is undefined.

    configPage() reads wgUserName, so leaving it out of the request list silently
    disables the configuration page for everyone instead of failing loudly.
    """
    requested = set(re.findall(r"'(wg\w+)'", GADGET_SOURCE.split("mw.config.get( [", 1)[1]))
    used = set(re.findall(r"config\.(wg\w+)", GADGET_SOURCE))

    assert used - requested == set()


def test_one_configuration_page_serves_every_reader_of_this_wiki() -> None:
    """A personal script must not require interface-admin rights to configure.

    It is the maintainer's subpage and not each reader's own, because the same page
    carries the opt-out list and the display policy, and those are answers a wiki gives
    once. A per-reader copy of them would be a reader deciding who gets named.
    """
    body = GADGET_SOURCE.split("function configPage()", 1)[1].split("\n\t}", 1)[0]
    assert "CONFIG_OWNER" in body
    # Namespace 2 by number, so the localised user-namespace name resolves per wiki.
    assert "CONFIG_PAGE_SUFFIX, 2" in body
    assert "wgUserName" not in body
    assert "'User:'" not in GADGET_SOURCE
    # The MediaWiki-namespace page is the future gadget location, a comment only.
    code = [line for line in GADGET_SOURCE.splitlines() if not line.lstrip().startswith("*")]
    assert not [line for line in code if "MediaWiki:" in line]


def test_the_gadget_reads_only_its_own_half_of_the_page() -> None:
    """The server's options share the file and are none of the browser's business.

    An opt-out applied here would leave the names one direct request away, so the
    gadget must not act on `optOut` even by accident. DEFAULT_CONFIG is the whole
    contract — a key that is not in it is not read — and this states the consequence.
    """
    for key in ("optOut", "contributorNames", "sanctionedAccounts", "anonymisedAccounts"):
        assert key not in GADGET_SOURCE


def test_custom_content_is_parsed_by_mediawiki_rather_than_built_here() -> None:
    """Wikitext gives images and video for free, and the parser does the sanitising.

    The gadget must never assemble markup from a configuration string: that is the
    difference between rich content and an injection point.
    """
    body = GADGET_SOURCE.split("function renderCustomContent(", 1)[1].split("\n\t}", 1)[0]
    assert "DOMParser" in body
    assert "innerHTML" not in GADGET_SOURCE
    # Wikitext cannot emit one, but the guarantee should survive reading this function
    # alone rather than the whole parser pipeline.
    assert "querySelectorAll( 'script' )" in body
    # Media in a note box loads when reached and never plays by itself.
    assert "'loading', 'lazy'" in body
    assert "removeAttribute( 'autoplay' )" in body


def test_custom_content_is_fetched_anonymously_and_falls_back_to_built_in_wording() -> None:
    fetch = GADGET_SOURCE.split("async function fetchParsedPage(", 1)[1].split("\n\t}", 1)[0]
    assert "action=parse" in fetch
    # Anonymous keeps the response CDN-cacheable and reader-independent.
    assert "credentials: 'omit'" in fetch
    assert "return null" in fetch

    load = GADGET_SOURCE.split("async function loadCustomContent(", 1)[1].split("\n\t}", 1)[0]
    # A configured but unwritten page must not cost a lookup on every history view.
    assert "writeCache( cacheKey, { html: html } )" in load

    intro = GADGET_SOURCE.split("async function addHistoryIntroduction(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    assert "if ( custom ) {" in intro
    assert "} else {" in intro
    # The edit link is built here in every case: a page parsed on its own cannot know
    # which article the reader is on, so wikitext magic words would name the wrong page.
    assert "createEditLink()" in intro.split("} else {", 1)[1].split("\n\t\t}", 1)[1]


def test_translations_live_on_language_subpages() -> None:
    """One reviewable page per language, unlike the language-blind messages object."""
    body = GADGET_SOURCE.split("function contentCandidates(", 1)[1].split("\n\t}", 1)[0]
    assert "base + '/' + language" in body
    assert "language.split( '-' )[ 0 ]" in body
    # The base title is the last resort, not the first choice.
    assert body.index("candidates.push( base )") > body.index("base + '/' + language")


def test_javascript_extension_uses_hooks_instead_of_code_in_configuration() -> None:
    """Config pages stay declarative; arbitrary JS belongs in the reader's common.js."""
    assert "mw.hook( 'wikipeople.history' ).fire(" in GADGET_SOURCE
    assert "mw.hook( 'wikipeople.summary' ).fire(" in GADGET_SOURCE
    assert "eval(" not in GADGET_SOURCE
    assert "new Function" not in GADGET_SOURCE


def test_contributor_count_is_injected_because_a_shared_page_cannot_hold_it() -> None:
    """One wikitext page serves every article, so the number cannot live in it.

    The page declares a slot and keeps its own wording inside it as a fallback; the
    script replaces that text only once a real number arrives.
    """
    body = GADGET_SOURCE.split("async function fillContributorCount(", 1)[1].split("\n\t}", 1)[0]
    assert "'.wikipeople-count'" in body
    assert "'.wikipeople-number'" in body
    # Replacing text, never markup: a slot is a place to put a number, not an injection point.
    assert "textContent" in body
    assert "innerHTML" not in body
    # The count reuses the translated, plural-aware messages rather than gluing a string.
    assert "'wikipeople-people'" in body
    assert "'wikipeople-at-least'" in body


def test_contributor_count_is_opt_in_and_never_rewrites_the_box_late() -> None:
    body = GADGET_SOURCE.split("async function fillContributorCount(", 1)[1].split("\n\t}", 1)[0]
    # No slot in the page means no request: a history view costs nothing by default.
    assert "if ( !phrases.length && !numbers.length ) {" in body
    # An empty delay list, so a pending result leaves the reader's wording in place.
    assert "contributionData( [] )" in body

    intro = GADGET_SOURCE.split("async function addHistoryIntroduction(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    # The introduction is inserted before the count is even requested.
    assert intro.index("insertBelowSubtitle( box )") < intro.index("fillContributorCount( box )")


def test_both_views_share_one_cached_attribution_request() -> None:
    """Reading an article and then its history must not cost two API calls."""
    summary = GADGET_SOURCE.split("async function addArticleSummary(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    assert "contributionData( PENDING_RETRY_DELAYS_MS )" in summary
    # The article view peeks at the cache before drawing, because an answer already in
    # hand needs no placeholder. Writing it back stays with the shared helper.
    assert "readCache( getCacheKey(), CLIENT_CACHE_MAX_AGE_MS )" in summary
    assert "writeCache" not in summary

    shared = GADGET_SOURCE.split("async function contributionData(", 1)[1].split("\n\t}", 1)[0]
    assert "readCache( cacheKey, CLIENT_CACHE_MAX_AGE_MS )" in shared
    assert "writeCache( cacheKey, data )" in shared


def test_source_holds_no_control_characters() -> None:
    """A literal NUL made grep treat the file as binary and silently find nothing.

    The list-formatter sentinel is still a NUL at runtime; it is spelled as an escape
    so the source stays plain text and ordinary tooling keeps working.
    """
    assert "\x00" not in GADGET_PATH.read_bytes().decode()
    assert "'\\u0000' + index" in GADGET_SOURCE


def test_the_box_holds_its_space_from_the_first_frame() -> None:
    """No answer reaches this gadget quickly, so waiting to draw only shifts the page.

    A fresh connection to another host costs DNS, TCP and TLS before the first byte:
    measured at roughly 280 ms even against warm data. Drawing on arrival would insert
    a box into an article the reader has already begun, so there is no state for an
    empty page and the placeholder is not conditional on the request being slow.
    """
    body = GADGET_SOURCE.split("function countDisplayState(", 1)[1].split("\n\t}", 1)[0]
    assert "'loading'" in body
    assert "'vague'" in body
    assert "'final'" in body
    # A result that is already in hand outranks every timer.
    assert body.index("'final'") < body.index("'loading'")
    # The threshold that used to delay the first paint is gone, not merely set to zero.
    assert "COUNT_ROLL_START_MS" not in GADGET_SOURCE

    summary = GADGET_SOURCE.split("async function addArticleSummary(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    # The request is in flight before anything is drawn, never after.
    assert summary.index("contributionData( PENDING_RETRY_DELAYS_MS )") < summary.index(
        "runCountPlaceholder("
    )

    driver = GADGET_SOURCE.split("async function runCountPlaceholder(", 1)[1].split("\n\t}", 1)[0]
    # Inserted before the first await, so the space is reserved in the same frame the
    # gadget runs rather than one timer later.
    assert driver.index("insertBelowSubtitle( box )") < driver.index("await")
    # Nothing redraws while it waits, so it waits once instead of every frame.
    assert "PENDING_SETTLE_MS - ( Date.now() - startedAt )" in driver


def test_a_cached_answer_skips_the_placeholder_entirely() -> None:
    """sessionStorage answers with no network, so a placeholder would invent the wait.

    Drawing first and reading the cache afterwards flashes "analysing" on every revisit
    — the common path for anyone moving between an article and its history — to explain
    a wait that never happened.
    """
    summary = GADGET_SOURCE.split("async function addArticleSummary(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    # The cache is consulted before the driver is ever started.
    assert summary.index("readCache( getCacheKey(), CLIENT_CACHE_MAX_AGE_MS )") < summary.index(
        "runCountPlaceholder("
    )
    # And the request, with its placeholder, runs only when the cache came up empty.
    guard = summary.split("readCache( getCacheKey(), CLIENT_CACHE_MAX_AGE_MS );", 1)[1]
    assert guard.lstrip().startswith("if ( !outcome.data ) {")


def test_the_waiting_box_says_what_it_is_doing_and_nothing_about_the_article() -> None:
    """A placeholder that resembles an answer will be read as one.

    This box used to be the count sentence with four random digits standing in for the
    number. Under `prefers-reduced-motion` the digits never turned, so the box sat there
    stating "written by 4827" as a fact — and even in motion it imitated a sentence
    shape ("written by N people") that a page with names never produces. A loading label
    reserves the same space and cannot be mistaken for a result.
    """
    pending = GADGET_SOURCE.split("function buildPendingSummary(", 1)[1].split("\n\t}", 1)[0]
    assert "'wikipeople-pending'" in pending
    assert "'aria-busy', 'true'" in pending
    # The label is stable and meaningful, so it is announced rather than hidden.
    assert "aria-hidden" not in pending
    # Nothing in the waiting box may claim anything about who wrote the article.
    assert "'wikipeople-summary-prefix'" not in pending
    assert "'wikipeople-people'" not in pending

    settle = GADGET_SOURCE.split("function settlePendingSummary(", 1)[1].split("\n\t}", 1)[0]
    assert "removeAttribute( 'aria-busy' )" in settle
    # Vague, but a complete and true sentence rather than a truncated one.
    assert "'wikipeople-many-people'" in settle
    assert "'wikipeople-summary-prefix'" in settle

    # No random digits anywhere, and no motion left to opt out of.
    assert "Math.random()" not in GADGET_SOURCE
    assert "prefersReducedMotion" not in GADGET_SOURCE
    assert "@keyframes" not in GADGET_STYLES
    assert "animation" not in GADGET_STYLES


def test_a_failed_request_is_told_apart_from_one_still_computing() -> None:
    """An unsupported wiki must leave no trace, not claim that many people wrote it.

    Both paths yield no data, so collapsing them would leave the vague wording on a
    404 — an assertion about an article the API never agreed to serve.
    """
    summary = GADGET_SOURCE.split("async function addArticleSummary(", 1)[1].split(
        "\n\tasync function", 1
    )[0]
    assert "outcome.failed" in summary
    assert "removeArticleSummary()" in summary
    # A pending result keeps whatever the placeholder settled on. Read the decisions
    # taken once the request has resolved: the guard that skips the request when the
    # cache already answered tests the same field earlier, for an unrelated reason.
    decisions = summary.split("await Promise.all(", 1)[1]
    assert decisions.index("if ( outcome.failed ) {") < decisions.index("if ( !outcome.data ) {")
    # The real sentence replaces the placeholder in place rather than joining it.
    assert "existing.replaceWith( summary )" in summary
    # The hook still carries real data, so it must not fire for a placeholder.
    fired = summary.split("mw.hook( 'wikipeople.summary' ).fire(", 1)[1]
    assert "outcome.data" in fired.split("\n", 1)[0]


def test_only_the_token_metric_earns_the_authorship_wording() -> None:
    """Edit counts rank a different thing, so they must not inherit "written by".

    The test is on the direction of the comparison. Written the other way round — an
    EDIT_COUNT_METRIC constant — a metric added to the API after this file ships would
    fall through to the strongest claim the gadget has, and call whoever it names an
    author on the strength of a string it has never seen.
    """
    assert "SURVIVING_TEXT_METRIC = 'wikiwho-surviving-alphanumeric-tokens'" in GADGET_SOURCE
    normalize = GADGET_SOURCE.split("function normalizeContributionData(", 1)[1].split("\n\t}", 1)[
        0
    ]
    assert "wroteTheText: data.metric === SURVIVING_TEXT_METRIC" in normalize

    summary = GADGET_SOURCE.split("function buildArticleSummary(", 1)[1].split("\n\t}", 1)[0]
    # Every place the wording differs reads the same flag, so the three cannot drift apart.
    assert "namedByEditCount ? 'wikipeople-tooltip-edits' : 'wikipeople-tooltip'" in summary
    assert (
        "namedByEditCount ? 'wikipeople-summary-prefix-edits' : 'wikipeople-summary-prefix'"
        in summary
    )
    assert "createEditorLink( editor, namedByEditCount )" in summary

    link = GADGET_SOURCE.split("function createEditorLink(", 1)[1].split("\n\t}", 1)[0]
    assert "byEditCount ? 'wikipeople-share-edits' : 'wikipeople-share'" in link


def test_a_box_with_no_names_makes_no_claim_about_where_names_came_from() -> None:
    """The bottom rung shows a count and nothing else, so both tooltips are wrong there.

    A page WikiWho refused outright reaches this branch, and the default tooltip would
    then credit WikiWho with an analysis it declined to perform. The weaker wording is
    equally wrong: it describes a ranking the reader is not being shown.
    """
    summary = GADGET_SOURCE.split("function buildArticleSummary(", 1)[1].split("\n\t}", 1)[0]
    assert "var namedByEditCount = topEditors.length > 0 && !data.wroteTheText;" in summary
    described = summary.split("if ( topEditors.length ) {", 1)[1]
    assert "wikipeople-tooltip" in described.split("\n\t\t}", 1)[0]
    # The computation date is true whatever was ranked, so it survives on its own.
    assert "box.title = tooltip.join( ' ' );" in summary


def test_gadget_localises_plurals_and_lists_rather_than_hardcoding_french() -> None:
    assert "PLURAL:" in GADGET_SOURCE
    assert "Intl.ListFormat" in GADGET_SOURCE
    assert "wgUserLanguage" in GADGET_SOURCE
    # An unparsable MediaWiki language code must not break rendering.
    assert "safeFormatter" in GADGET_SOURCE


def test_the_history_box_is_for_readers_who_have_not_seen_a_history_before() -> None:
    """Explaining a page history to someone reading it on purpose is noise.

    Logged in is not the same as knowing the wiki, but it is the only signal in the
    browser, and the reader it misjudges is the one who can set "always". The gate must
    stay a single named function: read inline, "anonymous" is a string that looks like a
    switch, and the third state would drift between the two views that consult it.
    """
    gate = GADGET_SOURCE.split("function showsHistoryIntro(", 1)[1].split("\n\t}", 1)[0]
    assert "'always'" in gate
    assert "'never'" in gate
    # The default falls through to the reader, not to a constant.
    assert "return !config.wgUserName;" in gate

    startup = GADGET_SOURCE.split("mw.loader.using(", 1)[1].split("/* ", 1)[0]
    assert "if ( showsHistoryIntro( wikiConfig ) ) {" in startup
    # Nothing else may test the raw value; three states cannot survive a boolean check.
    assert "wikiConfig.showHistoryIntro" not in startup


def test_a_configuration_page_cannot_hand_the_gadget_a_value_it_does_not_accept() -> None:
    """Hand-written JSON on a wiki, read years later by a newer script.

    Every value is checked against what its option accepts and a rejected one falls back
    to the default, so no consumer has to defend itself against "enabled": "false".
    """
    body = GADGET_SOURCE.split("function normalizeConfig( parsed ) {", 1)[1].split("\n\t}", 1)[0]
    # Only keys the gadget declares are copied, so an unknown one cannot reach a consumer.
    assert "Object.keys( DEFAULT_CONFIG ).forEach(" in body
    assert "ALLOWED_VALUES[ key ].indexOf( value ) !== -1" in body
    # Booleans meant something before showHistoryIntro grew a third state.
    assert "value ? 'always' : 'never'" in body

    startup = GADGET_SOURCE.split("mw.loader.using(", 1)[1].split("/* ", 1)[0]
    # Validated to a real boolean, so the switch is read as one rather than compared to
    # the single literal a page happened to be written with.
    assert "if ( !wikiConfig.enabled ) {" in startup


def test_an_api_refusal_is_remembered_instead_of_asked_again_on_every_page() -> None:
    """A script manager that loads this file on every project pays one request per page.

    A week of logs had 17% of all views arriving from Commons, Wikidata, Wikisource and
    Wikinews, where the answer is a 404 that `sites.py` derives without a network call
    and that cannot change between two pages. The gadget stays wiki-agnostic — it names
    no host and no project, and the API remains the only thing that decides — but it
    stops re-asking a question it has already had answered.
    """
    assert "UNSERVED_WIKI_MAX_AGE_MS" in GADGET_SOURCE
    guard = GADGET_SOURCE.split("readCache( unservedWikiKey()", 1)[1].split("}", 1)[0]
    assert "return" in guard

    remembered = GADGET_SOURCE.split("error.status === 404", 1)[1].split("}", 1)[0]
    assert "writeCache( unservedWikiKey()" in remembered
    # Only the 404. A timeout or a 503 says this request went wrong, not that the wiki
    # will never be served, and must not lock a reader out of a working deployment.
    assert "error.status === 503" not in GADGET_SOURCE


def test_the_refusal_outlives_the_tab_but_not_the_day() -> None:
    """The reader who pays for asking is the one who opens many tabs at once."""
    shared = GADGET_SOURCE.split("function sharedStorage()", 1)[1].split("}", 1)[0]
    assert "window.localStorage" in shared
    assert "24 * 60 * 60 * 1000" in GADGET_SOURCE
    # Not for ever: "not served" is also what a misconfigured deployment says.
    assert "Infinity" not in GADGET_SOURCE.split("UNSERVED_WIKI_MAX_AGE_MS", 1)[1][:200]


def test_a_backgrounded_tab_retries_when_it_is_looked_at_rather_than_on_a_clock() -> None:
    """Browsers throttle timers in background tabs, which is the tab this matters for.

    Opening a dozen articles with the middle mouse button fires a dozen first requests
    and then no retry until the reader arrives, long past the three- and thirteen-second
    marks. Over a week, 104 of the 153 views that showed nothing had made exactly one
    request while the answer they were waiting for was stored a median two seconds later.
    """
    retry_loop = GADGET_SOURCE.split("PENDING_RETRY_DELAYS_MS;", 1)[1].split(
        "function whenVisible", 1
    )[0]
    assert "await wait( delays[ attempt ] );" in retry_loop
    assert "await whenVisible();" in retry_loop

    visible = GADGET_SOURCE.split("function whenVisible()", 1)[1].split("\n\t}", 1)[0]
    assert "visibilitychange" in visible
    # A foreground tab must not wait for an event that will never fire.
    assert "document.visibilityState !== 'hidden'" in visible
    assert "removeEventListener" in visible


def test_a_name_the_api_did_not_link_is_not_drawn_as_a_link() -> None:
    """The blue link to a user page that does not exist, fixed on the drawing side.

    Whether the page exists is answered on the server — asking here would be one extra
    API request per article view — so the gadget's whole job is to believe the answer
    and build an element that makes no promise.
    """
    normalize = GADGET_SOURCE.split("function normalizeContributionData(", 1)[1].split("\n\t}", 1)[
        0
    ]
    # An older API, or a wiki whose standing table is empty, sends nothing. That is not
    # "unlinked", it is "nobody looked", and the pre-existing behaviour is the link.
    assert "display: editor.display || 'link'" in normalize

    link = GADGET_SOURCE.split("function createEditorLink(", 1)[1].split("\n\t}", 1)[0]
    assert "document.createElement( isLinked ? 'a' : 'span' )" in link
    # The href is inside the linked branch, so a span can never acquire one.
    assert link.split("if ( isLinked ) {", 1)[1].split("}", 1)[0].count("node.href") == 1


def test_an_unlinked_name_is_never_explained_to_the_reader() -> None:
    """One word, three causes, and the gadget must not guess which.

    "Unlinked" covers a missing user page, a rename, and a name the wiki asked to be
    shown without a link. A tooltip saying why would turn the third into a public
    accusation, which is exactly what withholding the link was chosen over.
    """
    link = GADGET_SOURCE.split("function createEditorLink(", 1)[1].split("\n\t}", 1)[0]
    # The only thing said about an unlinked name is its share, which is true whichever
    # cause applied. Everything else is pushed by the linked branch alone.
    assert link.count("tooltip.push(") == 2
    assert "wikipeople-user-title" in link.split("if ( isLinked ) {", 1)[1].split("}", 1)[0]
    for word in ("sanction", "block", "banni", "bloqu", "renamed", "vanished", "supprim"):
        assert word not in link.lower()


def test_an_anonymised_account_is_credited_as_an_account_rather_than_as_a_number() -> None:
    """A rename leaves "Renamed user 4501e2a3c" behind, and it really did write the text.

    Dropping it would move its share into "and 46 others" and misattribute the article,
    so the credit stays and only the label changes.
    """
    link = GADGET_SOURCE.split("function createEditorLink(", 1)[1].split("\n\t}", 1)[0]
    assert "mw.message( 'wikipeople-anonymised-account' ).text()" in link
    assert "wikipeople-anonymised" in link
    # Both built-in bundles carry it, so a reader on either language sees a phrase
    # rather than the key when no on-wiki translation exists yet.
    assert GADGET_SOURCE.count("'wikipeople-anonymised-account':") == 2
    assert ".wikipeople-anonymised" in GADGET_STYLES
    # An unlinked name keeps the weight a linked one has, so the list stays one list.
    assert ".wikipeople-unlinked" in GADGET_STYLES


def test_summary_has_a_slot_on_minerva_and_not_inside_the_article() -> None:
    """Minerva publishes none of Vector's slots, and Minerva is the whole mobile site."""
    body = GADGET_SOURCE.split("function insertBelowSubtitle(", 1)[1].split("\n\t}", 1)[0]
    assert "'mw-content-subtitle'" in body
    # Tried before the article container, which is the last resort and not the answer:
    # a note prepended into #bodyContent is inside what other tools read as article text.
    assert body.index("'mw-content-subtitle'") < body.index("'bodyContent'")
    # Vector fills both, and #siteSub is the narrower slot, so it keeps precedence.
    assert body.index("'siteSub'") < body.index("'mw-content-subtitle'")


def test_edit_invitation_opens_an_editor_on_the_mobile_site() -> None:
    """`veaction` is inert where MobileFrontend runs: the link would go nowhere."""
    assert "'wgMFMode'" in GADGET_SOURCE
    body = GADGET_SOURCE.split("function createEditLink()", 1)[1].split("\n\t}", 1)[0]
    assert "config.wgMFMode" in body
    assert "'#/editor/all'" in body
    assert "veaction: 'edit'" in body
    # The skin name is a different question: Minerva chosen as a desktop skin has the
    # desktop VisualEditor target and no MobileFrontend router.
    assert "minerva" not in body.lower()
