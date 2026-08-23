"""Web fetch + readability extraction unit (lode-w0h.1, first child of E12).

A **pure** fetch/extract unit with **no storage coupling** — the raw material
every other web-draw-down connector child consumes (``docs/externals.md``
"Draw-down rules"). Given a URL, :func:`fetch_and_extract` returns a
:class:`FetchResult` carrying ``(clean_text, raw_html, status)``: fetch the
page, run readability extraction (strip nav/ads/chrome), and on a permanent
failure (403/paywall/JS-scaffold) return a tombstone status instead of
garbage. This unit fetches **one page only** — one-hop-then-stop across a
note's *own* links is enforced by the edge/trigger layer (w0h.3), not here;
the redirect-following this module does is a different, narrower thing (see
below).

w0h.2 (ingest) writes ``clean_text`` as a snapshot's ``body`` and ``raw_html``
as its ``raw_payload``; w0h.3 (URL-detect + edge + draw-down) is the caller
that invokes this unit and owns queueing/retries around it.

## Fetch-outcome taxonomy (decision, bd lode-w0h.1, debate round 3, 2026-07-08)

**The HTTP-status half of this taxonomy — which statuses are OK/TOMBSTONE/
TRANSIENT — now lives in :mod:`lode.fetch_outcome` (lode-gpzn.13), a
connector-neutral module the Atlassian connectors (JIRA/Confluence) also
call, so the mapping is defined once rather than copied per connector. Only
the extractor-driven "2xx but empty/short content" tombstone signal (b,
below) stays here — it is trafilatura-specific, not part of the HTTP
classifier.**

- **(a) 2xx + extractable text** → :data:`FetchStatus.OK` — job done.
- **(b) PERMANENT failure** (retrying will not help) → :data:`FetchStatus.TOMBSTONE`:
    - 401/403 and any other 4xx response, **except** the two codes HTTP itself
      flags as "try again later" — 408 Request Timeout and 429 Too Many
      Requests, both of which are transient (see (c)). Every other 4xx means an
      identical request gets an identical response, so retrying is pointless;
    - a 2xx response whose extracted text is ``None`` **or** shorter than
      ``fetch_min_extract_chars`` (a config knob) — the *one* testable signal
      that covers JS-rendered scaffolding, paywalled teasers, and genuinely
      empty pages alike, with no bespoke per-cause heuristic;
    - a redirect chain longer than ``fetch_max_redirects`` — whether a true
      loop or a legitimate-but-too-long chain, we refuse it the same way and a
      retry would refuse it identically.
- **(c) TRANSIENT failure** (retrying might help) → raises
  :class:`TransientFetchError`: 408, 429, any 5xx, or a network/timeout error.
  (408 is a server-reported timeout — the same condition as a client-side
  timeout, just observed by the other end — so it belongs here, not in (b).) This
  module has **no queue and no opinion on retries** — it just raises, and the
  caller (the w0h.2/w0h.3 job handler) lets the exception propagate so
  ``worker.py``'s existing attempts/backoff/dead-letter machinery retries it
  (``failed`` → ``pending`` retry, → ``dead`` at ``retry_max_attempts``,
  PINNED lode-i05.6); on ``dead`` the *caller* writes a tombstone snapshot so
  the note edge still resolves — this module is not involved in that step.
- **(d) 3xx** → followed transparently by the fetcher, up to
  ``fetch_max_redirects`` hops. The caller only ever sees the **final**
  resolved URL (:attr:`FetchResult.final_url`), which w0h.3 canonicalizes into
  ``external_id`` (the ticket's "redirect wrinkle": a note edge created
  pre-fetch on the *pasted* URL may need re-pointing to the *final* URL's
  canonical form).

**JS-rendered pages are a PERMANENT tombstone here, by design** — actually
rendering them (headless browser / JS execution) is an explicit deferred
follow-on (lode-oni), not first-connector scope.

``fetch_max_redirects`` (this module: caps one HTTP fetch's own redirect
chain) is **not** the same knob as ``drawdown_hop_limit``
(``docs/configuration.md``, w0h.3's concern: whether the fetched page's *own
outbound links* get crawled). Conflating them would silently couple two
unrelated policies.

## Library choice (SPIKE deliverable, per the ticket)

**HTTP client — httpx2**, over `requests` and stdlib `urllib.request`:

- vs `requests`: the honest differentiator is maintenance status, *not*
  features. `requests` is in long-term maintenance mode; httpx2 is its actively
  developed equivalent with the same synchronous call shape this module needs.
  Both expose a redirect cap (``requests.Session.max_redirects``) and a typed
  exception hierarchy (``TooManyRedirects``/``Timeout``/``ConnectionError``), so
  neither of those is a reason to prefer httpx2 over `requests` — only over
  stdlib. httpx2 additionally ships an async client if a later connector wants
  one; this module does not.
- vs stdlib ``urllib.request``: it would avoid a dependency, but its redirect
  cap is a hardcoded class attribute (``HTTPRedirectHandler.max_redirections``
  = 10), not a per-request knob, so the ``fetch_max_redirects`` config knob in
  (d) could not be honored without subclassing the handler. It also has no
  connect/read timeout split and a much less ergonomic exception model — not
  worth hand-rolling for a first connector.
- httpx2's exception hierarchy maps onto the taxonomy above
  (``TooManyRedirects`` / ``TimeoutException`` / ``NetworkError``), but note
  ``httpx2.InvalidURL`` is **not** an ``HTTPError`` subclass — an unparseable
  URL must be caught by name or it escapes uncaught (see :meth:`HttpxFetcher.fetch`).

**Readability extraction — trafilatura**, named in the decision itself
("extractor None/empty (trafilatura-style)"):

- ``trafilatura.extract()`` returns ``str | None`` — ``None`` on a failed/empty
  extraction *is* the testable signal (b) calls for, no adaptation needed.
- Verified against the three synthetic fixtures in ``tests/test_webfetch.py``
  (trafilatura 2.1.0): the multi-paragraph article extracts a 405-char body;
  the JS-shell page (an empty ``<div id="root">`` + a script tag) returns
  ``None``; the paywall teaser ("Subscribe to continue reading.") returns a
  40-char non-``None`` string. Since 40 < the 200-char default floor < 405,
  the length-floor knob (:attr:`~lode.config.Settings.fetch_min_extract_chars`)
  is genuinely load-bearing — the ``None`` check alone would let the teaser
  through as ``ok``.
- Alternatives considered: ``readability-lxml`` (no active PyPI releases in
  years, weaker boilerplate removal per its own project notes) and
  ``boilerpy3`` (thinner API, no built-in metadata handling trafilatura
  already ships for later connector needs). trafilatura is also the only one
  of the three named in the ticket's own decision trail.

This docstring is the ticket's required "documented evaluation + basis for
the choice" — no further library spike is flagged as needed for *this*
module.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2
import trafilatura

from lode.config import Settings
from lode.fetch_outcome import HttpOutcome, classify_http_status

#: Sent on every fetch so a server sees an identifiable, non-empty UA rather
#: than a bare httpx2 default (some sites 403 a missing/generic UA outright).
#:
#: Deliberately a bare product token, with no ``(+<url>)`` contact link. That
#: convention identifies a *centrally operated* crawler, so an operator can
#: reach whoever is responsible for the traffic. lode is neither: it fetches
#: one user-pasted page, one hop, from the end user's own machine ("Recursion =
#: unbounded web crawler, not a notes app" — docs/externals.md, "Draw-down
#: rules"). The only real URL this project has is a maintainer's personal
#: GitHub account, which names someone who is *not* the party fetching, and
#: broadcasts that identity to every third-party host the user draws down from.
#: The product token stays greppable and blockable — the one affordance an
#: operator can actually act on. Restore ``(+<url>)`` only if lode ever gains a
#: project-owned URL. (lode-yzv)
_USER_AGENT = "lode-webfetch/1"


class FetchStatus(str, Enum):
    """Outcome of one :func:`fetch_and_extract` call — see module docstring."""

    OK = "ok"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True)
class FetchResult:
    """The pure output of :func:`fetch_and_extract` — no DB/queue coupling.

    ``final_url`` is the URL after following any redirect chain (== ``url`` when
    there was none, or when the fetch failed before any response was read); it
    is what w0h.3 canonicalizes into ``external_id``. ``clean_text``/``raw_html``
    are populated on :attr:`FetchStatus.OK`; on a tombstone whose failure came
    with an HTTP response (401/403/empty-extract), ``raw_html`` is still kept
    for provenance/debugging even though ``clean_text`` is ``None`` — a
    redirect-cap tombstone has neither, since no response body was read.
    """

    status: FetchStatus
    final_url: str
    clean_text: str | None
    raw_html: str | None
    http_status: int | None
    tombstone_reason: str | None


class FetchError(Exception):
    """Base class for errors a :class:`Fetcher` may raise."""


class TransientFetchError(FetchError):
    """Retryable fetch failure — 408, 429, 5xx, or a network/timeout error.

    Deliberately **not** caught by :func:`fetch_and_extract`: it propagates to
    the caller (the queue job handler), which lets the existing worker
    attempts/backoff/dead-letter machinery retry it (see the module
    docstring's taxonomy, case (c)).
    """


class TooManyRedirectsError(FetchError):
    """The redirect chain exceeded ``fetch_max_redirects``.

    Non-retryable: the chain is deterministic, so an identical request follows
    the identical hops and exceeds the identical cap — true of a redirect loop
    and of a merely-too-long chain alike. :func:`fetch_and_extract` therefore
    turns this into a tombstone rather than letting it propagate like
    :class:`TransientFetchError`.
    """


@dataclass(frozen=True)
class RawResponse:
    """What a :class:`Fetcher` hands back for a completed HTTP exchange."""

    final_url: str
    status_code: int
    text: str


class Fetcher(Protocol):
    """The one seam between this unit and the network.

    Production uses :class:`HttpxFetcher`; tests pass a stub so the offline
    gate never makes a real request (``docs/w0h.1`` acceptance: "injectable
    fetcher seam ... so tests run fully offline").
    """

    def fetch(self, url: str) -> RawResponse:
        """GET ``url``, following redirects up to the configured cap.

        Returns a :class:`RawResponse` for any outcome :func:`fetch_and_extract`
        can classify itself (2xx, 401/403, other permanent 4xx). Raises
        :class:`TransientFetchError` for 408/429/5xx/network/timeout conditions
        (and for a URL httpx2 cannot parse), and :class:`TooManyRedirectsError`
        if the redirect chain exceeds the cap. It raises nothing else.
        """
        ...


@contextmanager
def _httpx_errors_classified() -> Iterator[None]:
    """Map httpx2's exception surface onto this module's fetch-error taxonomy.

    The single home for that mapping, so :meth:`HttpxFetcher.fetch` and
    :meth:`GuardedHttpxFetcher._get_one` cannot drift apart on how a given
    httpx2 failure is classified (they did, as two hand-copied ladders, before
    this was extracted).

    Anything not raised by httpx2 -- notably
    :class:`UnsafeWebDestinationError` from the guard checks that run inside
    this block -- passes straight through unclassified, which is what keeps a
    refused destination non-retryable.
    """
    try:
        yield
    except httpx2.TooManyRedirects as exc:
        raise TooManyRedirectsError(str(exc)) from exc
    except httpx2.TimeoutException as exc:
        raise TransientFetchError(f"timeout: {exc}") from exc
    except httpx2.NetworkError as exc:
        raise TransientFetchError(f"network error: {exc}") from exc
    except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
        # Any other httpx2-level failure (e.g. a malformed response, or a
        # URL httpx2 cannot even parse) — no sharper classification is
        # available, so default to retryable rather than silently
        # tombstoning on an unrecognized condition. httpx2.InvalidURL is
        # NOT an httpx2.HTTPError subclass, so it must be named explicitly
        # or it escapes this method entirely, unclassified.
        raise TransientFetchError(f"http client error: {exc}") from exc


class HttpxFetcher:
    """Default :class:`Fetcher`: a single synchronous GET via ``httpx2``.

    The JIRA and Confluence connectors
    (:class:`lode.jira_fetch.JiraHttpFetcher`,
    :class:`lode.confluence.HttpxConfluenceFetcher`) subclass this rather
    than re-housing :meth:`fetch`'s try/except+classify+:class:`RawResponse`
    ladder (lode-88iv) — the only real per-connector deltas are the
    ``User-Agent`` value, an optional auth credential, extra headers (e.g.
    ``Accept: application/json``), whether redirects are followed at all,
    and whether connection *establishment* is retried (``retry_connect``,
    JIRA and Confluence only, lode-lq9u —
    :attr:`~lode.config.Settings.atlassian_connect_retries` carries the why)
    — so those are the seams exposed here rather than duplicating the whole
    method. ``retry_connect`` defaults to ``False``, and
    :class:`GuardedHttpxFetcher` builds its own client via its own
    :meth:`_client` regardless, so the ask path's one-hop
    verify-the-peer-before-reading design is unaffected either way.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        user_agent: str = _USER_AGENT,
        auth: httpx2.Auth | tuple[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        retry_connect: bool = False,
    ) -> None:
        self._settings = settings or Settings()
        self._headers = {"User-Agent": user_agent, **(extra_headers or {})}
        self._auth = auth
        self._follow_redirects = follow_redirects
        # Built once per fetcher, not once per fetch: a caller-supplied
        # transport is the one httpx2.Client kwarg that stops Client from
        # constructing (and CA-bundle-parsing) a transport of its own on
        # every request. None leaves that per-request default in place.
        self._transport = (
            httpx2.HTTPTransport(retries=self._settings.atlassian_connect_retries)
            if retry_connect
            else None
        )

    def _client(self, **overrides: Any) -> httpx2.Client:
        """Construct an ``httpx2.Client`` from this fetcher's settings.

        ``overrides`` replaces individual kwargs, so a subclass can express
        its client as a delta -- :class:`GuardedHttpxFetcher`'s per-hop
        client is this construction minus redirect-following and minus the
        shared transport.

        ``max_redirects`` is a Client-constructor knob, not accepted by the
        module-level ``httpx2.get()`` shortcut. It is harmless to pass even
        when ``follow_redirects=False`` (a subclass's choice, e.g.
        :class:`~lode.confluence.HttpxConfluenceFetcher`) -- httpx2 simply
        never consults it then.
        """
        kwargs: dict[str, Any] = {
            "follow_redirects": self._follow_redirects,
            "max_redirects": self._settings.fetch_max_redirects,
            "timeout": self._settings.fetch_timeout_s,
            "headers": self._headers,
            "auth": self._auth,
            "transport": self._transport,
        }
        kwargs.update(overrides)
        return httpx2.Client(**kwargs)

    @cached_property
    def _pooled_client(self) -> httpx2.Client:
        """One client per fetcher, shared by every :meth:`fetch` and never closed.

        ``Client.close()`` closes its transport unconditionally, including a
        caller-supplied one, so a ``with``-scoped client would drain
        ``_transport``'s connection pool on every call (lode-s54x) -- exactly
        what the per-fetcher transport exists to avoid.

        Cached rather than built in ``__init__`` because
        :class:`GuardedHttpxFetcher` inherits that constructor but never calls
        this class's :meth:`fetch`: it would otherwise pay CA-bundle parsing on
        every ask-path fetch for a client it never uses.
        """
        return self._client()

    def fetch(self, url: str) -> RawResponse:
        with _httpx_errors_classified():
            response = self._pooled_client.get(url)

        if classify_http_status(response.status_code) is HttpOutcome.TRANSIENT:
            raise TransientFetchError(f"http {response.status_code}")

        return RawResponse(
            final_url=str(response.url),
            status_code=response.status_code,
            text=response.text,
        )


class UnsafeWebDestinationError(FetchError):
    """A fetch destination -- the initial URL, a redirect hop, or the actual
    peer connected to -- is a private/loopback/link-local/reserved/multicast
    address (lode-xwah).

    Raised by :class:`GuardedHttpxFetcher` only; the base :class:`HttpxFetcher`
    has no address policy at all. Non-retryable: an attacker-controlled
    destination re-resolves to the same disallowed answer (or a differently
    disallowed one) on retry, so it must never be treated as transient.

    :func:`fetch_and_extract` does **not** catch it (it catches only
    :class:`TooManyRedirectsError`), so it propagates as a plain
    :class:`FetchError` to the ask path's :func:`lode.tools._fetch_web`, whose
    ``except (FetchError, ValueError)`` turns it into a
    :class:`~lode.tools.ToolFetchError` with nothing persisted -- no
    ``externals`` row, no tombstone. That is the intended outcome: a refused
    destination should leave no trace of itself in the corpus, which a
    tombstone would not achieve.
    """


#: Per IP version, ranges that no ``ipaddress`` attribute flags but that are
#: still never a legitimate public fetch destination. ``100.64.0.0/10`` is
#: carrier-grade NAT (RFC 6598) -- routinely a container/ISP-internal network,
#: and classified as neither private nor reserved. ``fec0::/10`` is deprecated
#: IPv6 site-local (RFC 3879), which ``ipaddress`` likewise does not flag (it
#: even reports ``is_global`` True). Both are live bypasses of the attribute
#: check alone, verified against the pinned interpreter (lode-xwah review).
_EXTRA_DISALLOWED_NETWORKS = {
    4: (ipaddress.ip_network("100.64.0.0/10"),),
    6: (ipaddress.ip_network("fec0::/10"),),
}


def _is_disallowed_address(addr_str: str) -> bool:
    """Whether ``addr_str`` (a literal IPv4/IPv6 address) must never be fetched.

    Fail-closed: an address this module cannot even parse is treated as
    disallowed rather than let through unclassified.

    An IPv4-mapped IPv6 address (``::ffff:127.0.0.1``) is unwrapped and judged
    as the IPv4 address it really is -- the IPv6 attributes alone do not
    reliably see through the mapping, and the mapping is exactly how a caller
    would smuggle a v4 internal address past a v6-shaped check.
    """
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or any(addr in net for net in _EXTRA_DISALLOWED_NETWORKS[addr.version])
    )


def _resolve_host_addresses(host: str, port: int) -> list[str]:
    """Resolve ``host`` to every address it answers to right now.

    A literal IP host (``http://127.0.0.1/...``) is returned as-is with no
    DNS lookup -- both because none is needed and because a literal-IP host
    can't rebind between two lookups the way a domain name can. A name is
    resolved via :func:`socket.getaddrinfo`, the same resolver httpx2's own
    transport uses, so a public-then-private multi-answer response is
    inspected in full rather than only its first answer.
    """
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise UnsafeWebDestinationError(f"cannot resolve host {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def _refuse_if_unsafe_host(url: str) -> None:
    """Refuse ``url`` before it is ever fetched, if any resolved address is unsafe.

    This is the redirect-chain half of the guard (lode-xwah): called on the
    *original* URL and again on every redirect ``Location``, before that
    hop's request is issued -- so a redirect straight at an internal address
    never reaches the network at all, not merely fails to persist.

    Also enforces an http(s) scheme allowlist. Without it a ``file://`` or
    ``ftp://`` hop is not judged here at all, and reaches httpx2 to fail as an
    unrecognized-protocol :class:`httpx2.HTTPError` -- which this module
    classifies as *transient*, so the async worker would retry a destination
    that should have been refused outright.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeWebDestinationError(
            f"{url}: scheme {parts.scheme!r} is not permitted"
        )
    host = parts.hostname
    if not host:
        raise UnsafeWebDestinationError(f"no host in URL {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    for addr in _resolve_host_addresses(host, port):
        if _is_disallowed_address(addr):
            raise UnsafeWebDestinationError(
                f"{url} resolves to disallowed address {addr}"
            )


def _refuse_if_unsafe_peer(response: httpx2.Response) -> None:
    """Refuse a response whose *actual* connected peer is unsafe.

    This is the DNS-rebinding half of the guard (lode-xwah): the pre-hop
    check in :func:`_refuse_if_unsafe_host` resolves the host itself, a
    lookup a hostile short-TTL resolver can answer differently a moment
    later when httpx2's transport resolves the same host again for the real
    connection. This checks the address httpx2 actually connected to
    (``response.extensions['network_stream']``), so a rebind that fools the
    pre-check is still caught before the response is used for anything.

    **Fails closed.** A missing ``network_stream`` extension, a missing
    ``server_addr``, or an :class:`OSError` from the underlying socket all
    mean the peer could not be verified -- and an unverifiable peer is
    refused, never waved through. :meth:`GuardedHttpxFetcher._get_one` always
    calls this against a *still-streaming* response for exactly this reason:
    once the body has been read the connection may already be released, and
    the transport's ``get_extra_info('server_addr')`` then raises
    ``OSError: Bad file descriptor`` (reproduced against any ``Connection:
    close`` / HTTP/1.0 server -- lode-xwah review).
    """
    network_stream = response.extensions.get("network_stream")
    if network_stream is None:
        raise UnsafeWebDestinationError(
            f"{response.url}: no network_stream to verify the connected peer against"
        )
    try:
        server_addr = network_stream.get_extra_info("server_addr")
    except OSError as exc:
        raise UnsafeWebDestinationError(
            f"{response.url}: could not read the connected peer address: {exc}"
        ) from exc
    if not server_addr:
        raise UnsafeWebDestinationError(
            f"{response.url}: connected peer address is unavailable"
        )
    addr = server_addr[0]
    if _is_disallowed_address(addr):
        raise UnsafeWebDestinationError(
            f"{response.url} actually connected to disallowed peer {addr}"
        )


class GuardedHttpxFetcher(HttpxFetcher):
    """:class:`HttpxFetcher` for the ask path ONLY (lode-xwah): closes the two
    gaps a model-chosen ``web_fetch`` destination could otherwise exploit that
    the draw-down path's ``lode.tools`` guard does not close on its own --
    see that module's history and ``docs/externals.md`` "Web-fetch destination
    guard".

    1. **Redirect chains.** Every hop's destination -- the original URL, and
       every redirect ``Location`` -- is validated via
       :func:`_refuse_if_unsafe_host` *before* that hop's request is issued.
       Redirects are therefore followed manually here
       (``follow_redirects=False`` passed to every per-hop client), never
       left to httpx2's own follower, which has no per-hop hook.
    2. **DNS rebinding / TOCTOU.** Even a validated hop's *actual* connected
       peer is re-checked via :func:`_refuse_if_unsafe_peer` -- post-connect
       but *pre-body*, on the still-streaming response (see
       :meth:`_get_one`) -- since the pre-hop check's resolution and httpx2's
       own transport resolution are two separate lookups a hostile short-TTL
       resolver can answer differently. Unverifiable peers are refused, not
       waved through.

    Constructed **only** by the ask path (:mod:`lode.tools`'s ``_fetch_web``)
    and injected via the ``fetcher=`` seam :func:`fetch_and_extract` /
    :func:`~lode.tools.fetch_for_ask` already thread -- the draw-down path,
    and the JIRA/Confluence connectors, are unaffected (module docstring's
    "whether redirects are followed at all" is exactly the kind of
    per-connector delta :class:`HttpxFetcher` is built to let a subclass
    override).
    """

    def __init__(self, settings: Settings | None = None, **kwargs: Any) -> None:
        # This subclass drives redirects itself -- overriding a caller-passed
        # follow_redirects would silently defeat the whole guard, so it is
        # not accepted as a kwarg here (HttpxFetcher.__init__'s signature
        # still types it; passing it explicitly is simply not supported).
        kwargs.pop("follow_redirects", None)
        super().__init__(settings, follow_redirects=False, **kwargs)

    def fetch(self, url: str) -> RawResponse:
        settings = self._settings
        current_url = url
        # One client for the whole chain: the hops of a redirect chain are
        # ordinary same-session requests (http->https, bare->www, a tracker),
        # so a client per hop would throw away the connection pool and pay a
        # fresh TCP+TLS handshake each time. httpx2's own follower reuses one
        # client for exactly this reason; driving the loop by hand should not
        # cost more than delegating it.
        with _httpx_errors_classified(), self._client() as client:
            for _hop in range(settings.fetch_max_redirects + 1):
                _refuse_if_unsafe_host(current_url)
                # _get_one has already verified the connected peer, before it
                # read a single byte of the body -- see its docstring.
                response = self._get_one(client, current_url)

                if classify_http_status(response.status_code) is HttpOutcome.TRANSIENT:
                    raise TransientFetchError(f"http {response.status_code}")

                location = response.headers.get("location")
                if 300 <= response.status_code < 400 and location:
                    current_url = str(response.url.join(location))
                    continue

                return RawResponse(
                    final_url=str(response.url),
                    status_code=response.status_code,
                    text=response.text,
                )
        raise TooManyRedirectsError(
            f"{url}: exceeded {settings.fetch_max_redirects} redirects"
        )

    def _client(self, **overrides: Any) -> httpx2.Client:
        # transport=None keeps the ask path on httpx2's own default transport:
        # the shared one is the connectors' retrying transport (lode-lq9u),
        # which this path never opts into. follow_redirects=False is stated
        # here rather than left to __init__'s forcing, so the guarded semantics
        # (lode-xwah) survive any future change to that default.
        return super()._client(follow_redirects=False, transport=None, **overrides)

    def _get_one(self, client: httpx2.Client, url: str) -> httpx2.Response:
        """Issue exactly one hop, verifying the connected peer before reading it.

        The request is sent with ``stream=True`` and
        :func:`_refuse_if_unsafe_peer` runs on the still-open stream, *before*
        :meth:`httpx2.Response.read`. Two reasons, both load-bearing:

        * a rebound connection to an internal host is refused before any of
          its body is pulled across, not merely before that body is used; and
        * ``network_stream.get_extra_info('server_addr')`` is only answerable
          while the connection is live -- after a non-streaming ``get()`` has
          read the body, a ``Connection: close`` server's socket is already
          gone and the call raises ``OSError: Bad file descriptor``, which
          under the fail-closed peer check would refuse every such (entirely
          legitimate) server. Streaming is what makes fail-closed correct
          rather than merely strict (lode-xwah review).

        httpx2-level failures are classified by the caller's
        :func:`_httpx_errors_classified` block, the same mapping
        :meth:`HttpxFetcher.fetch` uses.
        """
        request = client.build_request("GET", url)
        response = client.send(request, stream=True)
        try:
            _refuse_if_unsafe_peer(response)
            response.read()
        finally:
            response.close()
        return response


def _extract(html: str) -> str | None:
    """Readability-extract ``html``; ``None`` on failed/empty extraction."""
    return trafilatura.extract(html, include_comments=False, include_tables=True)


def _tombstone(
    *,
    final_url: str,
    reason: str,
    http_status: int | None = None,
    raw_html: str | None = None,
) -> FetchResult:
    return FetchResult(
        status=FetchStatus.TOMBSTONE,
        final_url=final_url,
        clean_text=None,
        raw_html=raw_html,
        http_status=http_status,
        tombstone_reason=reason,
    )


def fetch_and_extract(
    url: str,
    *,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> FetchResult:
    """Fetch ``url`` and readability-extract it into a :class:`FetchResult`.

    Pure function, no DB writes: url -> (clean_text, raw_html, status). See the
    module docstring for the full fetch-outcome taxonomy. A transient failure
    (429/5xx/network/timeout) is **not** caught here — :class:`TransientFetchError`
    propagates to the caller, which is expected to let it reach the async
    worker's retry/backoff/dead-letter machinery (this module has no queue).
    """
    settings = settings or Settings()
    fetcher = fetcher or HttpxFetcher(settings)

    try:
        response = fetcher.fetch(url)
    except TooManyRedirectsError:
        return _tombstone(final_url=url, reason="too_many_redirects")

    # Any non-OK status here is a permanent tombstone from this function's
    # perspective (``not OK`` is exactly ``>= 400``), via the shared
    # classifier rather than re-deriving the threshold locally. A conforming
    # Fetcher (see the Fetcher protocol contract) raises TransientFetchError for
    # 408/429/5xx before returning, so a TRANSIENT status never reaches here
    # in practice; testing for ``not OK`` rather than ``is TOMBSTONE`` keeps
    # the original defensive behavior for a non-conforming custom fetcher
    # (tombstone the error response) instead of silently extracting it as if
    # it were content.
    if classify_http_status(response.status_code) is not HttpOutcome.OK:
        return _tombstone(
            final_url=response.final_url,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            raw_html=response.text,
        )

    clean_text = _extract(response.text)
    if clean_text is None or len(clean_text) < settings.fetch_min_extract_chars:
        return _tombstone(
            final_url=response.final_url,
            reason="empty_extract",
            http_status=response.status_code,
            raw_html=response.text,
        )

    return FetchResult(
        status=FetchStatus.OK,
        final_url=response.final_url,
        clean_text=clean_text,
        raw_html=response.text,
        http_status=response.status_code,
        tombstone_reason=None,
    )
