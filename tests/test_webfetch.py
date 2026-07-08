"""Tests for lode.webfetch — the fetch + readability-extraction unit (lode-w0h.1).

Covers the acceptance criteria: url -> (clean_text, raw_html, status) is a pure
function (no DB writes); a JS-scaffold/403/paywall page yields a tombstone
status rather than scaffolding text; and a transient failure (429/5xx/network/
timeout) raises rather than tombstoning, so a caller can let it propagate to
the async queue's retry machinery. All tests use a stub :class:`~lode.webfetch.Fetcher`
so the gate never makes a real network request.
"""

import httpx
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


class _FakeResponse:
    """Stands in for an httpx.Response without any transport."""

    def __init__(self, status_code: int, url: str = _URL, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


def _fake_client_cls(status_code: int, captured: dict) -> type:
    """Build a stand-in for httpx.Client that always answers ``status_code``.

    HttpxFetcher constructs its own client, so this is how we reach its
    status-classification branch without a transport. ``captured`` receives the
    constructor kwargs, letting a test assert the config knobs are wired
    through.
    """

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def get(self, url: str) -> _FakeResponse:
            return _FakeResponse(status_code, url)

    return _FakeClient


class TestHttpxFetcher:
    """Exercises HttpxFetcher's own classification, entirely offline.

    Two kinds of test here, neither of which touches the network:

    * exception translation — driven against targets that fail before any DNS
      or non-loopback connect (an unparseable URL, and a refused connection to
      loopback port 1);
    * status-code classification — driven against a fake ``httpx.Client``, since
      HttpxFetcher constructs its own client and exposes no transport seam.

    "Does a live GET actually work" is left to the deliberately opt-in smoke
    suite convention this repo uses for real-network/model checks (mirrors
    tests/test_models_smoke.py's opt-in pattern).
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

    def test_unparseable_url_is_transient_not_uncaught(self):
        """httpx.InvalidURL is NOT an httpx.HTTPError — it must still be caught.

        Regression guard: an unparseable URL used to escape HttpxFetcher.fetch()
        entirely, reaching the caller as an unclassified exception rather than a
        TransientFetchError. Raised before any DNS/socket work, so still offline.
        """
        fetcher = HttpxFetcher(load_settings(fetch_timeout_s=1.0))

        with pytest.raises(TransientFetchError):
            fetcher.fetch("http://[::1")

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503])
    def test_transient_status_codes_raise(self, monkeypatch, status_code):
        """408/429 (the 'try again later' 4xx) and every 5xx are retryable.

        408 in particular is a server-reported timeout: tombstoning it would
        permanently mark a live URL dead, defeating link-rot immunity.
        """
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        fetcher = HttpxFetcher(load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch(_URL)

    @pytest.mark.parametrize("status_code", [200, 401, 403, 404, 410])
    def test_non_transient_status_codes_return_a_response(
        self, monkeypatch, status_code
    ):
        """Everything else comes back for fetch_and_extract to classify."""
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        fetcher = HttpxFetcher(load_settings())

        response = fetcher.fetch(_URL)

        assert response.status_code == status_code
        assert response.final_url == _URL

    def test_config_knobs_are_wired_into_the_client(self, monkeypatch):
        """fetch_max_redirects / fetch_timeout_s must actually reach httpx."""
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(200, captured))
        fetcher = HttpxFetcher(
            load_settings(fetch_max_redirects=3, fetch_timeout_s=2.5)
        )

        fetcher.fetch(_URL)

        assert captured["max_redirects"] == 3
        assert captured["timeout"] == 2.5
        assert captured["follow_redirects"] is True
