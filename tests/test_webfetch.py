"""Tests for lode.webfetch — the fetch + readability-extraction unit (lode-w0h.1).

Covers the acceptance criteria: url -> (clean_text, raw_html, status) is a pure
function (no DB writes); a JS-scaffold/403/paywall page yields a tombstone
status rather than scaffolding text; and a transient failure (429/5xx/network/
timeout) raises rather than tombstoning, so a caller can let it propagate to
the async queue's retry machinery. All tests use a stub :class:`~lode.webfetch.Fetcher`
so the gate never makes a real network request.
"""

import socket
from typing import Self

import httpx2
import pytest

from lode.config import load_settings
from lode.webfetch import (
    FetchStatus,
    GuardedHttpxFetcher,
    HttpxFetcher,
    RawResponse,
    TooManyRedirectsError,
    TransientFetchError,
    UnsafeWebDestinationError,
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

# A realistically messy page: nav chrome, a cookie/consent banner, a
# related-articles rail, a comment CTA, a footer, and inline <script>/<style>
# alongside a real multi-paragraph article. This pins trafilatura's ACTUAL
# extraction behavior (lode-g274.3) rather than merely its documented tag
# list — note the cookie banner, related-articles rail, and comment CTA are
# deliberately built from <div>, not <nav>/<aside>/<footer>: trafilatura's
# own MANUALLY_CLEANED/MANUALLY_STRIPPED tag lists (settings.py, verified
# against the installed 2.1.0) do NOT cover <div> or <header> at all — their
# removal here depends entirely on trafilatura's XPath/density-based
# main-content discovery, not on a fixed tag rule. A too-minimal fixture
# would not exercise that path (see the module note below); this one is
# deliberately sized and shaped like a real news-article page so it does.
_MESSY_ARTICLE_HTML = """
<html>
<head>
<title>Why Local-First Note Apps Are Having a Moment</title>
<style>
  body { font-family: sans-serif; margin: 0; }
  .cookie-banner { position: fixed; bottom: 0; background: #222; color: #fff; }
  .related-articles a { color: blue; }
</style>
<script src="https://cdn.example.com/analytics.js"></script>
</head>
<body>

<div class="cookie-banner" id="consent-banner">
  <p>We use cookies and similar technologies to personalize content, tailor
  and measure ads, and provide a better experience. By clicking "Accept All"
  you agree to this use of cookies as described in our Cookie Policy.</p>
  <button>Accept All</button>
  <button>Reject Non-Essential</button>
  <a href="/cookie-policy">Cookie Policy</a>
</div>

<header>
  <div class="logo"><a href="/">DailyDev Times</a></div>
  <nav class="main-nav">
    <ul>
      <li><a href="/">Home</a></li>
      <li><a href="/world">World</a></li>
      <li><a href="/technology">Technology</a></li>
      <li><a href="/sports">Sports</a></li>
      <li><a href="/subscribe">Subscribe</a></li>
    </ul>
  </nav>
</header>

<main>
<article>
<h1>Why Local-First Note Apps Are Having a Moment</h1>
<p class="byline">By Jordan Alvarez | Published March 3, 2026 | 6 min read</p>

<p>For years, the default assumption in productivity software was that your
notes belonged in the cloud: synced instantly across devices, backed up
automatically, and searchable from anywhere with a browser. That assumption
is now being challenged by a wave of local-first tools that store data on
your own machine first and treat sync as an optional, secondary concern
rather than the whole point of the product.</p>

<p>The appeal is partly about resilience. A local-first application keeps
working when the network does not: on a plane, in a basement office with
spotty wifi, or during a provider outage that takes an entire cloud service
offline for hours. Because the canonical copy of your data lives on disk
rather than behind an API a company can change or shut down, users describe
a sense of ownership that syncing services rarely offer.</p>

<p>It is also about speed. Round-tripping every keystroke to a remote server
adds latency that becomes noticeable the moment you start typing quickly, or
searching across a large archive of past notes. Tools built local-first can
lean on fast, embedded indexes instead, returning results in milliseconds
rather than the hundreds of milliseconds a network round trip usually costs,
which changes how the software feels to use every single day.</p>

<p>None of this is free. Local-first tools still need a story for syncing
across a laptop and a phone, for backup when a hard drive fails, and for
collaboration when more than one person needs to see the same notes. The
best implementations treat these as solvable engineering problems rather
than reasons to fall back to a cloud-first design, and the last two years
have produced noticeably more mature answers to all three.</p>

</article>
</main>

<aside class="related-articles">
  <h2>Related Stories</h2>
  <ul>
    <li><a href="/story/offline-first-databases">Offline-First Databases Explained</a></li>
    <li><a href="/story/crdt-primer">A Gentle Primer on CRDTs</a></li>
    <li><a href="/story/sync-engines-2026">The State of Sync Engines in 2026</a></li>
  </ul>
</aside>

<div class="comments-cta">
  <h3>Join the Discussion</h3>
  <p>Sign in to leave a comment. 128 people are already talking about this.</p>
  <button>View 128 Comments</button>
</div>

<footer>
  <p>&copy; 2026 DailyDev Times. All rights reserved.</p>
  <nav class="footer-links">
    <a href="/privacy">Privacy Policy</a> |
    <a href="/terms">Terms of Service</a> |
    <a href="/contact">Contact Us</a> |
    <a href="/advertise">Advertise</a>
  </nav>
</footer>

<script>
  (function() {
    window.dataLayer = window.dataLayer || [];
    console.log("analytics pixel fired");
  })();
</script>

</body>
</html>
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


def test_extract_strips_realistic_chrome_and_keeps_article_body():
    """Characterization test (lode-g274.3): pins trafilatura's ACTUAL
    extraction behavior against a realistically messy page, not just its
    documented tag-strip lists.

    Goes through :func:`fetch_and_extract` (the lode-level call path, same
    ``include_comments=False, include_tables=True`` configuration production
    uses) rather than calling ``trafilatura.extract`` directly, so this also
    covers our own call configuration, not just the library in isolation.

    Pinned against trafilatura 2.1.0 (the version the ``trafilatura>=2.1,<2.2``
    bound in ``pyproject.toml`` resolves, lode-g274.1) — moving off 2.1.0 is a
    deliberate act that re-baselines these fixtures first (epic lode-g274,
    acceptance criteria).

    A version bump that starts leaking nav/cookie-banner/related-articles/
    comment-CTA/footer/script chrome into ``clean_text`` — or starts dropping
    real article paragraphs — must fail this test rather than silently
    degrading every downstream embedding and citation.
    """
    fetcher = _StubFetcher(
        response=RawResponse(final_url=_URL, status_code=200, text=_MESSY_ARTICLE_HTML)
    )

    result = fetch_and_extract(_URL, fetcher=fetcher, settings=load_settings())

    assert result.status is FetchStatus.OK
    assert result.clean_text is not None
    text = result.clean_text

    # The real article survives: title and a distinguishing phrase from
    # every paragraph, so a bump that truncates the body (not just chrome)
    # is caught too.
    assert "Why Local-First Note Apps Are Having a Moment" in text
    assert "local-first tools that store data on" in text
    assert "sense of ownership that syncing services rarely offer" in text
    assert "Round-tripping every keystroke to a remote server" in text
    assert "solvable engineering problems rather than reasons to fall back" in text

    # Nav chrome (<nav>, plus the <header> logo — <header> is in NEITHER of
    # trafilatura's strip lists, so this leg exercises density, not the tag
    # rule).
    assert "DailyDev Times" not in text
    assert "Sports" not in text
    # Cookie/consent banner (a <div> — density-only, not tag-stripped).
    assert "Accept All" not in text
    assert "Cookie Policy" not in text
    # Related-articles rail (<aside>).
    assert "Related Stories" not in text
    assert "Offline-First Databases Explained" not in text
    # Comment CTA (a <div> — density-only, not tag-stripped).
    assert "Join the Discussion" not in text
    assert "128 Comments" not in text
    # Footer (<footer>).
    assert "Privacy Policy" not in text
    assert "Advertise" not in text
    # Inline <script>/<style>.
    assert "dataLayer" not in text
    assert "analytics pixel" not in text
    assert "position: fixed" not in text


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
    """Stands in for an httpx2.Response without any transport."""

    def __init__(self, status_code: int, url: str = _URL, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


def _fake_client_cls(status_code: int, captured: dict) -> type:
    """Build a stand-in for httpx2.Client that always answers ``status_code``.

    HttpxFetcher constructs its own client, so this is how we reach its
    status-classification branch without a transport. ``captured`` receives the
    constructor kwargs, letting a test assert the config knobs are wired
    through.
    """

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def __enter__(self) -> Self:
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
    * status-code classification — driven against a fake ``httpx2.Client``, since
      HttpxFetcher constructs its own client and exposes no transport seam.

    "Does a live GET actually work" is left to the deliberately opt-in smoke
    suite convention this repo uses for real-network/model checks (mirrors
    tests/test_models_smoke.py's opt-in pattern).
    """

    def test_invalid_scheme_is_transient(self):
        """An unsupported/garbage URL surfaces as a transient httpx2 error.

        HttpxFetcher has no special-cased "malformed input" path (see its
        module docstring) — it maps any otherwise-unclassified httpx2.HTTPError
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
        """httpx2.InvalidURL is NOT an httpx2.HTTPError — it must still be caught.

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
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(status_code, {}))
        fetcher = HttpxFetcher(load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch(_URL)

    @pytest.mark.parametrize("status_code", [200, 401, 403, 404, 410])
    def test_non_transient_status_codes_return_a_response(
        self, monkeypatch, status_code
    ):
        """Everything else comes back for fetch_and_extract to classify."""
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(status_code, {}))
        fetcher = HttpxFetcher(load_settings())

        response = fetcher.fetch(_URL)

        assert response.status_code == status_code
        assert response.final_url == _URL

    def test_config_knobs_are_wired_into_the_client(self, monkeypatch):
        """fetch_max_redirects / fetch_timeout_s must actually reach httpx2."""
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = HttpxFetcher(
            load_settings(fetch_max_redirects=3, fetch_timeout_s=2.5)
        )

        fetcher.fetch(_URL)

        assert captured["max_redirects"] == 3
        assert captured["timeout"] == 2.5
        assert captured["follow_redirects"] is True

    def test_user_agent_is_a_bare_product_token(self, monkeypatch):
        """The UA reaches httpx2 and advertises no ``(+<url>)`` contact link.

        Regression guard (lode-yzv): this constant once shipped a fabricated
        ``+<url>`` pointing at a repo that did not exist. A ``+<url>`` names the
        party responsible for the traffic so an operator can contact/block it;
        for a locally-run personal KB that party is the end user, never the repo
        or its maintainer. Asserted as a property, not a literal, so bumping the
        version does not break the guard.
        """
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = HttpxFetcher(load_settings())

        fetcher.fetch(_URL)

        user_agent = captured["headers"]["User-Agent"]
        assert user_agent.startswith("lode-webfetch/")
        assert "+" not in user_agent

    def test_no_transport_by_default(self, monkeypatch):
        """lode-lq9u: plain HttpxFetcher (the web draw-down path) gets no
        explicit transport -- only the JIRA/Confluence connector subclasses
        pass one. ``None`` here means httpx2's own default (zero
        connection-establishment retries)."""
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = HttpxFetcher(load_settings())

        fetcher.fetch(_URL)

        assert captured["transport"] is None


class _GuardedFakeResponse:
    """Stands in for an httpx2.Response, with the bits GuardedHttpxFetcher reads.

    ``url`` is a real ``httpx2.URL`` (not a bare string) so ``.join(location)``
    behaves exactly as it does against a real response. ``server_addr``
    defaults to a public address so the fail-closed peer check in
    :func:`lode.webfetch._refuse_if_unsafe_peer` passes; pass
    ``server_addr=None`` to model a response with no ``network_stream``
    extension at all, and ``peer_oserror=True`` to model httpcore raising
    ``OSError`` from ``get_extra_info`` on a released socket.

    ``read()``/``close()`` mirror the streaming API
    :meth:`~lode.webfetch.GuardedHttpxFetcher._get_one` drives, and ``reads``
    records their ordering against the peer check so a test can assert the
    body was never pulled from a refused peer.
    """

    def __init__(
        self,
        status_code: int,
        url: str,
        *,
        text: str = "",
        location: str | None = None,
        server_addr: str | None = "93.184.216.34",
        peer_oserror: bool = False,
    ) -> None:
        self.status_code = status_code
        self.url = httpx2.URL(url)
        self.text = text
        self.headers = {"location": location} if location else {}
        self.read_count = 0
        self.closed = False
        extensions: dict = {}
        if server_addr is not None or peer_oserror:
            extensions["network_stream"] = _FakeNetworkStream(
                server_addr, oserror=peer_oserror
            )
        self.extensions = extensions

    def read(self) -> bytes:
        self.read_count += 1
        return self.text.encode()

    def close(self) -> None:
        self.closed = True


class _FakeNetworkStream:
    def __init__(self, server_addr: str | None, *, oserror: bool = False) -> None:
        self._server_addr = server_addr
        self._oserror = oserror

    def get_extra_info(self, name: str):
        if self._oserror:
            raise OSError(9, "Bad file descriptor")
        if name == "server_addr" and self._server_addr is not None:
            return (self._server_addr, 0)
        return None


def _guarded_fake_client_cls(responses: list, calls: list) -> type:
    """Stand in for ``httpx2.Client``, answering ``responses`` in order.

    ``calls`` records every URL a request was actually issued against -- the
    assertion surface for "a redirect to an internal address does not issue
    the request at all" (a refused hop must never appear here). The
    ``build_request``/``send(stream=True)`` pair mirrors the real API
    :meth:`~lode.webfetch.GuardedHttpxFetcher._get_one` uses, so these tests
    exercise the production code path rather than a shape only the double has.
    """

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def build_request(self, method: str, url: str):
            return (method, url)

        def send(self, request, *, stream: bool = False):
            assert stream is True, (
                "_get_one must stream so the peer is checked pre-body"
            )
            calls.append(request[1])
            return responses[len(calls) - 1]

    return _FakeClient


class TestGuardedHttpxFetcher:
    """Exercises the redirect-chain + DNS-rebinding closure (lode-xwah), offline.

    Every test drives GuardedHttpxFetcher through a monkeypatched httpx2.Client
    (no real socket ever opens) plus, where DNS matters, a monkeypatched
    socket.getaddrinfo -- the same offline-fixture convention TestHttpxFetcher
    above uses.
    """

    def test_literal_private_initial_url_is_refused_before_any_request(
        self, monkeypatch
    ):
        """A literal loopback/private host needs no DNS -- refused immediately."""
        calls: list = []
        monkeypatch.setattr(httpx2, "Client", _guarded_fake_client_cls([], calls))
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://127.0.0.1/admin")

        assert calls == []

    def test_public_url_with_no_redirect_is_returned(self, monkeypatch):
        response = _GuardedFakeResponse(200, "http://93.184.216.34/", text="ok")
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        result = fetcher.fetch("http://93.184.216.34/")

        assert result.status_code == 200
        assert result.text == "ok"
        assert calls == ["http://93.184.216.34/"]

    def test_redirect_to_private_address_never_issues_that_hop(self, monkeypatch):
        """The core acceptance case: a redirect at an internal address is
        refused before its own request goes out -- not merely discarded after.
        """
        first = _GuardedFakeResponse(
            302, "http://93.184.216.34/", location="http://127.0.0.1/admin"
        )
        calls: list = []
        monkeypatch.setattr(httpx2, "Client", _guarded_fake_client_cls([first], calls))
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://93.184.216.34/")

        # Only the first (allowed) hop's request was ever issued -- the
        # refused second hop never reached the fake transport at all.
        assert calls == ["http://93.184.216.34/"]

    def test_redirect_chain_to_public_addresses_is_followed(self, monkeypatch):
        first = _GuardedFakeResponse(
            302, "http://93.184.216.34/", location="http://93.184.216.35/final"
        )
        second = _GuardedFakeResponse(200, "http://93.184.216.35/final", text="landed")
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([first, second], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        result = fetcher.fetch("http://93.184.216.34/")

        assert result.status_code == 200
        assert result.final_url == "http://93.184.216.35/final"
        assert calls == ["http://93.184.216.34/", "http://93.184.216.35/final"]

    def test_too_many_redirects_raises(self, monkeypatch):
        settings = load_settings(fetch_max_redirects=1)
        hop0 = _GuardedFakeResponse(
            302, "http://93.184.216.34/", location="http://93.184.216.35/"
        )
        hop1 = _GuardedFakeResponse(
            302, "http://93.184.216.35/", location="http://93.184.216.36/"
        )
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([hop0, hop1], calls)
        )
        fetcher = GuardedHttpxFetcher(settings)

        with pytest.raises(TooManyRedirectsError):
            fetcher.fetch("http://93.184.216.34/")

    def test_dns_rebinding_is_caught_via_actual_connected_peer(self, monkeypatch):
        """A domain name that resolves PUBLIC at the pre-hop check but whose
        connection actually lands on a private peer (simulating a short-TTL
        rebind between the two separate lookups) is still refused -- and the
        response is never returned to the caller.
        """
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, 6, "", ("93.184.216.34", 0))],
        )
        response = _GuardedFakeResponse(
            200, "http://rebind.example.com/", text="secret", server_addr="127.0.0.1"
        )
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://rebind.example.com/")

    def test_public_peer_matching_public_host_is_returned(self, monkeypatch):
        """A response whose network_stream extension confirms a PUBLIC peer
        passes through normally -- the peer check is not a no-op that always
        refuses when the extension is present.
        """
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, 6, "", ("93.184.216.34", 0))],
        )
        response = _GuardedFakeResponse(
            200,
            "http://example.com/",
            text="ok",
            server_addr="93.184.216.34",
        )
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        result = fetcher.fetch("http://example.com/")

        assert result.text == "ok"

    def test_unresolvable_host_is_refused_fail_closed(self, monkeypatch):
        def _raise(*a, **k):
            raise socket.gaierror("name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _raise)
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://does-not-resolve.example.com/")

    @pytest.mark.parametrize("status_code", [408, 429, 500])
    def test_transient_status_codes_raise(self, monkeypatch, status_code):
        response = _GuardedFakeResponse(status_code, "http://93.184.216.34/")
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch("http://93.184.216.34/")

    def test_follow_redirects_kwarg_cannot_be_overridden(self):
        """Passing follow_redirects=True must not defeat manual per-hop control.

        This is a defense-in-depth guard against a future caller accidentally
        (or maliciously) re-enabling httpx2's own follower and bypassing the
        per-hop checks entirely -- verified via the private attribute
        HttpxFetcher.__init__ actually sets.
        """
        fetcher = GuardedHttpxFetcher(load_settings(), follow_redirects=True)

        assert fetcher._follow_redirects is False

    @pytest.mark.parametrize(
        "address",
        [
            "100.64.1.1",  # RFC 6598 carrier-grade NAT -- ipaddress flags nothing
            "fec0::1",  # RFC 3879 IPv6 site-local -- ipaddress reports is_global
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped cloud metadata
            "169.254.169.254",
            "10.0.0.1",
            "224.0.0.1",
            "0.0.0.0",
            "::1",
        ],
    )
    def test_internal_address_families_are_all_refused(self, monkeypatch, address):
        """Every range the guard claims to cover is actually covered.

        The first three entries are the interesting ones: no ``ipaddress``
        attribute flags them, so an attribute-only check lets them straight
        through to an internal host (lode-xwah review).
        """
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, 6, "", (address, 0))],
        )
        calls: list = []
        monkeypatch.setattr(httpx2, "Client", _guarded_fake_client_cls([], calls))
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://internal.example.com/")

        assert calls == []

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x"])
    def test_non_http_scheme_is_refused(self, monkeypatch, url):
        """A non-http(s) hop is refused outright, not left to httpx2 to reject
        as a (retryable) transient client error.
        """
        calls: list = []
        monkeypatch.setattr(httpx2, "Client", _guarded_fake_client_cls([], calls))
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError, match="scheme"):
            fetcher.fetch(url)

        assert calls == []

    def test_unverifiable_peer_fails_closed_when_extension_is_absent(self, monkeypatch):
        """No ``network_stream`` means the peer cannot be verified -- refuse."""
        response = _GuardedFakeResponse(
            200, "http://93.184.216.34/", text="ok", server_addr=None
        )
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://93.184.216.34/")

    def test_unverifiable_peer_fails_closed_on_oserror(self, monkeypatch):
        """httpcore raises OSError from get_extra_info once the socket is
        released; an unverifiable peer is refused, never waved through.
        """
        response = _GuardedFakeResponse(200, "http://93.184.216.34/", text="ok")
        response.extensions["network_stream"] = _FakeNetworkStream(None, oserror=True)
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://93.184.216.34/")

    def test_refused_peer_body_is_never_read(self, monkeypatch):
        """The peer check runs on the still-streaming response, so a rebound
        connection's body never crosses the wire at all.
        """
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, 6, "", ("93.184.216.34", 0))],
        )
        response = _GuardedFakeResponse(
            200, "http://rebind.example.com/", text="secret", server_addr="127.0.0.1"
        )
        calls: list = []
        monkeypatch.setattr(
            httpx2, "Client", _guarded_fake_client_cls([response], calls)
        )
        fetcher = GuardedHttpxFetcher(load_settings())

        with pytest.raises(UnsafeWebDestinationError):
            fetcher.fetch("http://rebind.example.com/")

        assert response.read_count == 0
        assert response.closed is True
