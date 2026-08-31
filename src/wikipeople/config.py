from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote_plus

from wikipeople.replica import DEFAULT_HOST_TEMPLATE
from wikipeople.sites import ALL_WIKIS, DEFAULT_WIKIWHO_LANGUAGES


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_by_wiki(value: str) -> tuple[tuple[str, int], ...]:
    """Parse "frwiki:2592000,enwiki:0" into per-wiki overrides.

    A malformed pair is dropped rather than raising. This is read at import time in a
    web process, and a typo in one wiki's override must not take the whole service down
    with it; the wiki simply keeps the global default.
    """
    overrides: list[tuple[str, int]] = []
    for item in _csv(value):
        wiki, separator, seconds = item.partition(":")
        if separator and wiki.strip() and seconds.strip().lstrip("-").isdigit():
            overrides.append((wiki.strip(), int(seconds.strip())))
    return tuple(overrides)


def _database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    toolsdb_user = os.getenv("TOOL_TOOLSDB_USER")
    toolsdb_password = os.getenv("TOOL_TOOLSDB_PASSWORD")
    if toolsdb_user and toolsdb_password:
        database_name = os.getenv("TOOLSDB_DATABASE", f"{toolsdb_user}__wikipeople")
        return (
            f"mysql+pymysql://{quote_plus(toolsdb_user)}:{quote_plus(toolsdb_password)}"
            f"@tools.db.svc.wikimedia.cloud/{quote_plus(database_name)}?charset=utf8mb4"
        )

    return "sqlite:///./wikipeople.db"


@dataclass(frozen=True)
class Settings:
    database_url: str
    user_agent: str
    algorithm_version: str
    supported_wikis: tuple[str, ...]
    prewarm_wikis: tuple[str, ...]
    backfill_wikis: tuple[str, ...]
    wikiwho_languages: frozenset[str]
    cors_origins: tuple[str, ...]
    cors_origin_regex: str
    wikiwho_base_url: str
    request_timeout_seconds: float
    wikiwho_timeout_seconds: float
    wikiwho_max_response_bytes: int
    candidate_pool_size: int
    minimum_tokens: int
    minimum_share: float
    top_editor_max_revisions: int
    worker_poll_seconds: float
    worker_lease_seconds: int
    worker_max_attempts: int
    dead_retry_seconds: int
    ready_cache_seconds: int
    page_freshness_seconds: int
    page_cache_seconds: int
    page_stale_while_revalidate_seconds: int
    config_page: str
    optout_category_limit: int
    hide_sanctioned_contributors: bool
    max_visible_block_seconds: int
    max_visible_block_seconds_by_wiki: tuple[tuple[str, int], ...]
    standing_lock_checks_per_run: int
    standing_lock_recheck_seconds: int
    methodology_url: str
    replica_user: str
    replica_password: str
    replica_host_template: str
    backfill_batch_size: int
    pageview_dump_root: str
    pageview_lookback_days: int
    pageview_minimum_views: int
    demand_top_pages: int
    requested_prewarm_days: int
    requested_prewarm_limit: int
    requested_recompute_seconds: int
    recompute_batch_size: int
    recompute_min_age_seconds: int

    def max_visible_block_seconds_for(self, wiki: str) -> int:
        """The longest block an account may carry on this wiki and still be named.

        Per-wiki rather than global because the question is a community's, not an
        operator's: what counts as a sanction serious enough to stop crediting someone
        differs between projects, and one number imposed on seventy wikis would be a
        policy decision dressed up as a default.
        """
        for configured_wiki, seconds in self.max_visible_block_seconds_by_wiki:
            if configured_wiki == wiki:
                return seconds
        return self.max_visible_block_seconds

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_database_url(),
            user_agent=os.getenv(
                "WIKIPEOPLE_USER_AGENT",
                "WikiPeople/0.1 (https://github.com/schiste/wikipeople)",
            ),
            # v2 adds the edit-count fallback below the token metric (ADR-0005). The name
            # dropped "surviving-tokens" because that is now one rung of a ladder rather
            # than the whole policy.
            algorithm_version=os.getenv("ALGORITHM_VERSION", "attribution-ladder-v3"),
            # "*" serves every wiki WikiWho covers, on demand. Scheduled bulk work is
            # opted into separately so universal serving cannot imply universal crawling.
            supported_wikis=_csv(os.getenv("SUPPORTED_WIKIS", ALL_WIKIS)),
            prewarm_wikis=_csv(os.getenv("PREWARM_WIKIS", "")),
            backfill_wikis=_csv(os.getenv("BACKFILL_WIKIS", "")),
            wikiwho_languages=(
                frozenset(_csv(os.environ["WIKIWHO_LANGUAGES"]))
                if os.getenv("WIKIWHO_LANGUAGES")
                else DEFAULT_WIKIWHO_LANGUAGES
            ),
            cors_origins=_csv(os.getenv("CORS_ORIGINS", "")),
            cors_origin_regex=os.getenv(
                "CORS_ORIGIN_REGEX",
                r"^https://[a-z0-9-]+\.(?:m\.)?wikipedia\.org$",
            ),
            wikiwho_base_url=os.getenv(
                "WIKIWHO_BASE_URL", "https://wikiwho-api.wmcloud.org"
            ).rstrip("/"),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            wikiwho_timeout_seconds=float(os.getenv("WIKIWHO_TIMEOUT_SECONDS", "90")),
            wikiwho_max_response_bytes=int(
                os.getenv("WIKIWHO_MAX_RESPONSE_BYTES", str(32 * 1024 * 1024))
            ),
            candidate_pool_size=int(os.getenv("CANDIDATE_POOL_SIZE", "50")),
            minimum_tokens=int(os.getenv("MINIMUM_TOKENS", "20")),
            minimum_share=float(os.getenv("MINIMUM_SHARE", "0.01")),
            # The edit-count fallback walks the history 500 revisions per request, so
            # this is a budget of ten calls. It only ever runs for a page the token
            # metric could not rank, which is a short page far more often than a long
            # one; a history past this length is left unranked rather than ranked from
            # its most recent slice.
            top_editor_max_revisions=int(os.getenv("TOP_EDITOR_MAX_REVISIONS", "5000")),
            worker_poll_seconds=float(os.getenv("WORKER_POLL_SECONDS", "2")),
            worker_lease_seconds=int(os.getenv("WORKER_LEASE_SECONDS", "300")),
            worker_max_attempts=int(os.getenv("WORKER_MAX_ATTEMPTS", "8")),
            dead_retry_seconds=int(os.getenv("DEAD_RETRY_SECONDS", "86400")),
            # How long a browser may reuse an answer without asking again. Short, because
            # the answer is not a property of the revision alone: a policy change reaches
            # readers only once their copy expires, and a day of that is a day of naming
            # accounts the policy has already stopped naming. Every response carries an
            # ETag, so the request this buys back is almost always a 304 with no body.
            ready_cache_seconds=int(os.getenv("READY_CACHE_SECONDS", "300")),
            page_freshness_seconds=int(os.getenv("PAGE_FRESHNESS_SECONDS", str(90 * 24 * 60 * 60))),
            page_cache_seconds=int(os.getenv("PAGE_CACHE_SECONDS", "300")),
            # Beyond the window above the cached copy is still shown immediately while the
            # refresh happens behind it, so shortening the window costs no waiting. It only
            # means the reader after this one sees the newer answer.
            page_stale_while_revalidate_seconds=int(
                os.getenv("PAGE_STALE_WHILE_REVALIDATE_SECONDS", "604800")
            ),
            # The one page a wiki configures WikiPeople on, and the same page the gadget
            # fetches in the browser. "User:" is a canonical namespace prefix MediaWiki
            # resolves to the local name — "Utilisateur:" on frwiki, "Benutzer:" on
            # dewiki — so one title reaches it on every wiki with no per-wiki table to
            # keep in step. It is the maintainer's own subpage because that is what a
            # personal script is: whoever installs it owns its settings, and no
            # interface-admin right is involved. When WikiPeople becomes a site-wide
            # gadget this moves to the project namespace, and it is one variable and one
            # constant in the gadget that change.
            config_page=os.getenv("CONFIG_PAGE", "User:Schiste/wikipeople-config.json"),
            # A ceiling on how far one entry of the opt-out list can reach. Categories are not
            # walked recursively, so this only bites on a genuinely enormous flat
            # category; the sync logs the truncation rather than silently covering part
            # of it, because "half of this category is opted out" is not an opt-out.
            optout_category_limit=int(os.getenv("OPTOUT_CATEGORY_LIMIT", "5000")),
            # On by default. Leaving it off would have been the conservative-looking
            # choice, but the default is what almost every wiki will run, so the default
            # is the policy; ADR-0009 argues it out rather than deferring it.
            hide_sanctioned_contributors=_flag("HIDE_SANCTIONED_CONTRIBUTORS", True),
            # Ninety days. Long enough that ordinary editorial sanctions — a week, a
            # month, a summer — leave a contributor named, short enough that a year-long
            # block or an indefinite one does not. Numerically equal to
            # PAGE_FRESHNESS_SECONDS and unrelated to it: that one is about when an
            # answer goes stale, this one about what a wiki has decided about a person.
            max_visible_block_seconds=int(os.getenv("MAX_VISIBLE_BLOCK_SECONDS", str(90 * 86400))),
            max_visible_block_seconds_by_wiki=_int_by_wiki(
                os.getenv("MAX_VISIBLE_BLOCK_SECONDS_BY_WIKI", "")
            ),
            # CentralAuth answers about one account per request, so the lock pass is
            # rationed and rotates. Blocks are refreshed for every tracked account on
            # every run; only locks queue.
            standing_lock_checks_per_run=int(os.getenv("STANDING_LOCK_CHECKS_PER_RUN", "500")),
            standing_lock_recheck_seconds=int(
                os.getenv("STANDING_LOCK_RECHECK_SECONDS", str(86400))
            ),
            methodology_url=os.getenv(
                "METHODOLOGY_URL",
                "https://github.com/schiste/wikipeople/blob/main/docs/architecture.md",
            ),
            # Toolforge sets the TOOL_REPLICA_* pair itself; empty outside Toolforge,
            # which is what makes the backfill fall back to the Action API in tests
            # and on a laptop rather than needing a flag to say so.
            replica_user=os.getenv("TOOL_REPLICA_USER", ""),
            replica_password=os.getenv("TOOL_REPLICA_PASSWORD", ""),
            replica_host_template=os.getenv("REPLICA_HOST_TEMPLATE", DEFAULT_HOST_TEMPLATE),
            backfill_batch_size=int(os.getenv("BACKFILL_BATCH_SIZE", "500")),
            # Where the Analytics team publishes complete pageviews on the Toolforge
            # NFS mount. Empty or missing off Toolforge, which is what makes the demand
            # job log and stop rather than needing a flag to say it is not there.
            pageview_dump_root=os.getenv(
                "PAGEVIEW_DUMP_ROOT", "/public/dumps/public/other/pageview_complete"
            ),
            # Yesterday's file usually lands during the morning and sometimes much
            # later, so the job looks back a few days for the newest one that exists.
            pageview_lookback_days=int(os.getenv("PAGEVIEW_LOOKBACK_DAYS", "5")),
            # A memory bound, not a threshold of merit. A large Wikipedia has over a
            # million pages opened at least once in a day and holding all of them costs
            # more memory than a Toolforge job is given; a page under five views a day
            # is not what a popularity ranking is deciding between.
            pageview_minimum_views=int(os.getenv("PAGEVIEW_MINIMUM_VIEWS", "5")),
            # How much of one day's ranking is kept. The backfill spends about twelve
            # thousand pages a day, so this is several days of work per wiki: enough
            # that the queue is never idle, small enough that the table stays a ranking
            # rather than a copy of the wiki.
            demand_top_pages=int(os.getenv("DEMAND_TOP_PAGES", "50000")),
            requested_prewarm_days=int(os.getenv("REQUESTED_PREWARM_DAYS", "30")),
            requested_prewarm_limit=int(os.getenv("REQUESTED_PREWARM_LIMIT", "2000")),
            # A week. An article somebody opened is worth re-checking against its
            # current text far sooner than the ninety days a page picked by a timer
            # gets, and no sooner than this: a heavily edited article changes revision
            # several times a day and would otherwise take the whole budget alone.
            requested_recompute_seconds=int(
                os.getenv("REQUESTED_RECOMPUTE_SECONDS", str(7 * 86400))
            ),
            recompute_batch_size=int(os.getenv("RECOMPUTE_BATCH_SIZE", "200")),
            # Two weeks before a fallback answer is worth retrying. WikiWho indexes a
            # revision within days when it indexes it at all, so anything sooner mostly
            # spends the shared rate budget confirming the same refusal.
            recompute_min_age_seconds=int(os.getenv("RECOMPUTE_MIN_AGE_SECONDS", str(14 * 86400))),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
