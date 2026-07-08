"""Tests for lode.webfetch — the fetch + readability-extraction unit (lode-w0h.1).

Covers the acceptance criteria: url -> (clean_text, raw_html, status) is a pure
function (no DB writes); a JS-scaffold/403/paywall page yields a tombstone
status rather than scaffolding text; and a transient failure (429/5xx/network/
timeout) raises rather than tombstoning, so a caller can let it propagate to
the async queue's retry machinery. All tests use a stub :class:`~lode.webfetch.Fetcher`
so the gate never makes a real network request.
"""

import pytest

from lode.config import load_settings
from lode.webfetch import (
    FetchStatus,
    HttpxFetcher,
    RawResponse,
    TooManyRedirectsError,
    TransientFetchError,
    fetch_and_extract,
)

_URL = "https://example.com/article"

# A real multi-paragraph article: long enough to clear the default
# fetch_min_extract_chars floor after readability extraction.
_ARTICLE_HTML = """
<html><head><title>Test</title></head>
<body>
<nav>Home About Contact</nav>
<article>
<h1>A Real Article</h1>
<p>This is the first paragraph of real content that discusses something
interesting at length, providing enough text for extraction heuristics to
succeed reliably and clear any reasonable length floor.</p>
<p>This is a second paragraph continuing the discussion with more substantive
information about the topic at hand, so the whole passage reads naturally as
an article body rather than a fragment.</p>
</article>
<footer>Copyright 2024</footer>
</body></html>
"""

# A JS-rendered scaffold: no real content for the extractor to find.
_JS_SHELL_HTML = """
<html><head><title>App</title></head>
<body>
<div id="root"></div>
<script src="/static/js/main.js"></script>
</body></html>
"""

# A paywall teaser: the extractor returns *some* text, but far short of a
# real article — this is what the length-floor knob exists to catch.
_PAYWALL_HTML = """
<html><head><title>Paywalled</title></head>
<body>
<article><h1>Big Story</h1><p>Subscribe to continue reading.</p></article>
</body></html>
"""


class _StubFetcher:
    """Deterministic stand-in for :class:`~lode.webfetch.Fetcher`.

    Either returns a fixed :class:`RawResponse` or raises a fixed exception,
    recording the url it was called with so tests can assert on call shape.
    """

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        return self._response


def test_ok_article_returns_clean_text_and_raw_html():
    fetcher = _StubFetcher(
        response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.OK
    assert result.final_url == _URL
    assert result.http_status == 200
    assert result.tombstone_reason is None
    assert result.raw_html == _ARTICLE_HTML
    assert result.clean_text is not None
    assert "A Real Article" in result.clean_text
    # No scaffolding/chrome leaked through.
    assert "Home About Contact" not in result.clean_text
    assert fetcher.calls == [_URL]


def test_js_scaffold_yields_tombstone_not_scaffolding_text():
    """A 2xx JS-shell page (extractor returns None) -> tombstone, not garbage."""
    fetcher = _StubFetcher(
        response=RawResponse(final_url=_URL, status_code=200, text=_JS_SHELL_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.TOMBSTONE
    assert result.clean_text is None
    assert result.tombstone_reason == "empty_extract"
    # raw_html is kept for provenance even though the extract failed.
    assert result.raw_html == _JS_SHELL_HTML


def test_paywall_teaser_below_length_floor_yields_tombstone():
    """Non-None but short extracted text (a paywall teaser) also tombstones."""
    settings = load_settings(fetch_min_extract_chars=200)
    fetcher = _StubFetcher(
        response=RawResponse(final_url=_URL, status_code=200, text=_PAYWALL_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=settings)

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "empty_extract"
    assert result.clean_text is None


def test_short_extract_floor_is_configurable():
    """Lowering the floor admits the same paywall teaser as OK."""
    settings = load_settings(fetch_min_extract_chars=10)
    fetcher = _StubFetcher(
        response=RawResponse(final_url=_URL, status_code=200, text=_PAYWALL_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=settings)

    assert result.status is FetchStatus.OK
    assert result.clean_text is not None


@pytest.mark.parametrize("status_code", [401, 403, 404, 410])
def test_permanent_http_failure_yields_tombstone(status_code):
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_URL, status_code=status_code, text="<html>nope</html>"
        )
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.TOMBSTONE
    assert result.http_status == status_code
    assert result.tombstone_reason == f"http_{status_code}"
    assert result.clean_text is None


def test_too_many_redirects_yields_tombstone():
    fetcher = _StubFetcher(raises=TooManyRedirectsError("redirect loop"))

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "too_many_redirects"
    assert result.clean_text is None
    assert result.raw_html is None


def test_transient_error_propagates_rather_than_tombstoning():
    """The caller (queue handler), not this unit, decides what to do on retry."""
    fetcher = _StubFetcher(raises=TransientFetchError("connection reset"))

    with pytest.raises(TransientFetchError):
        fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())


def test_redirect_final_url_differs_from_requested_url():
    """A followed 3xx surfaces the FINAL url, not the originally-requested one."""
    final = "https://example.com/moved-article"
    fetcher = _StubFetcher(
        response=RawResponse(final_url=final, status_code=200, text=_ARTICLE_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.OK
    assert result.final_url == final


class TestHttpxFetcher:
    """Exercises HttpxFetcher's own classification against a local http server.

    Uses pytest-httpserver-free local sockets would add a new test dependency;
    instead this drives HttpxFetcher directly against unreachable/invalid
    targets to exercise its exception-translation paths offline, and leaves
    the "does a live GET actually work" concern to the deliberately opt-in
    smoke suite convention this repo uses for real-network/model checks
    (mirrors tests/test_models_smoke.py's opt-in pattern; no live network call
    is made in this gate).
    """

    def test_invalid_scheme_is_transient(self):
        """An unsupported/garbage URL surfaces as a transient httpx error.

        HttpxFetcher has no special-cased "malformed input" path (see its
        module docstring) — it maps any otherwise-unclassified httpx.HTTPError
        to TransientFetchError, which is the documented, deliberate default.
        """
        fetcher = HttpxFetcher(load_settings(fetch_timeout_s=1.0))

        with pytest.raises(TransientFetchError):
            fetcher.fetch("not-a-valid-url")

    def test_connection_error_is_transient(self):
        """A refused connection (nothing listening) is a network error -> transient."""
        fetcher = HttpxFetcher(load_settings(fetch_timeout_s=1.0))

        with pytest.raises(TransientFetchError):
            fetcher.fetch("http://127.0.0.1:1/")
