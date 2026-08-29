import re
from pathlib import Path

from pytest import MonkeyPatch

from wikipeople.config import Settings


def test_toolforge_credentials_build_default_toolsdb_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TOOL_TOOLSDB_USER", "s12345")
    monkeypatch.setenv("TOOL_TOOLSDB_PASSWORD", "secret:/ value")
    monkeypatch.delenv("TOOLSDB_DATABASE", raising=False)

    settings = Settings.from_env()

    assert settings.database_url == (
        "mysql+pymysql://s12345:secret%3A%2F+value@"
        "tools.db.svc.wikimedia.cloud/s12345__wikipeople?charset=utf8mb4"
    )


def test_explicit_database_url_wins_over_toolforge_credentials(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///explicit.db")
    monkeypatch.setenv("TOOL_TOOLSDB_USER", "s12345")
    monkeypatch.setenv("TOOL_TOOLSDB_PASSWORD", "secret")

    assert Settings.from_env().database_url == "sqlite:///explicit.db"


def test_page_freshness_defaults_are_explicit(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("PAGE_FRESHNESS_SECONDS", raising=False)
    monkeypatch.delenv("PAGE_CACHE_SECONDS", raising=False)
    monkeypatch.delenv("PAGE_STALE_WHILE_REVALIDATE_SECONDS", raising=False)

    settings = Settings.from_env()

    assert settings.page_freshness_seconds == 90 * 24 * 60 * 60
    # Three different clocks, deliberately far apart. How long a stored answer stays
    # usable is ninety days; how long a reader may reuse one without checking is five
    # minutes, because that is the delay a policy change has to wait out.
    assert settings.page_cache_seconds == 5 * 60
    assert settings.ready_cache_seconds == 5 * 60
    assert settings.page_stale_while_revalidate_seconds == 7 * 24 * 60 * 60


def test_the_configuration_page_title_is_one_title_for_every_wiki(
    monkeypatch: MonkeyPatch,
) -> None:
    """ "User:" is a canonical prefix MediaWiki resolves per wiki.

    It reaches "Utilisateur:Schiste/…" on frwiki and "Benutzer:Schiste/…" on dewiki
    without this service holding a table of namespace names it would have to keep in
    step with seventy communities. It is a user subpage rather than a project page
    because that is what a personal script is: the maintainer owns its settings, and
    no interface-admin right is involved.
    """
    monkeypatch.delenv("CONFIG_PAGE", raising=False)
    monkeypatch.delenv("OPTOUT_CATEGORY_LIMIT", raising=False)

    settings = Settings.from_env()

    assert settings.config_page == "User:Schiste/wikipeople-config.json"
    assert settings.optout_category_limit == 5000


def test_the_server_and_the_gadget_fetch_the_same_page() -> None:
    """One page or it is not one configuration.

    The gadget builds the title from a namespace number and an owner; this service
    builds it from a canonical prefix and the same owner. Two constructions of one
    title is exactly how they would drift apart, so they are held together here.
    """
    gadget = (Path(__file__).resolve().parents[1] / "wikipeople.js").read_text(encoding="utf-8")
    owner = re.search(r"CONFIG_OWNER = '([^']+)'", gadget)
    suffix = re.search(r"CONFIG_PAGE_SUFFIX = '([^']+)'", gadget)
    assert owner is not None and suffix is not None

    assert Settings.from_env().config_page == f"User:{owner.group(1)}{suffix.group(1)}"
