class WikiPeopleError(Exception):
    code = "wikipeople_error"
    permanent = False


class RetryableUpstreamError(WikiPeopleError):
    code = "upstream_unavailable"


class PermanentDataError(WikiPeopleError):
    code = "invalid_page"
    permanent = True


class ResponseTooLargeError(PermanentDataError):
    code = "wikiwho_response_too_large"


class InvalidConfigPageError(WikiPeopleError):
    """The on-wiki configuration page was fetched but could not be understood.

    Deliberately not a `PermanentDataError`: nothing is discarded and nothing retries
    forever. It behaves exactly like a failed fetch — the stored configuration is left
    alone — because a stray comma in a JSON page must not read as "nobody is opted out
    any more" and un-hide every article on the wiki at once.
    """

    code = "invalid_config_page"
