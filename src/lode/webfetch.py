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

**HTTP client — httpx**, over `requests` and stdlib `urllib.request`:

- vs `requests`: the honest differentiator is maintenance status, *not*
  features. `requests` is in long-term maintenance mode; httpx is its actively
  developed equivalent with the same synchronous call shape this module needs.
  Both expose a redirect cap (``requests.Session.max_redirects``) and a typed
  exception hierarchy (``TooManyRedirects``/``Timeout``/``ConnectionError``), so
  neither of those is a reason to prefer httpx over `requests` — only over
  stdlib. httpx additionally ships an async client if a later connector wants
  one; this module does not.
- vs stdlib ``urllib.request``: it would avoid a dependency, but its redirect
  cap is a hardcoded class attribute (``HTTPRedirectHandler.max_redirections``
  = 10), not a per-request knob, so the ``fetch_max_redirects`` config knob in
  (d) could not be honored without subclassing the handler. It also has no
  connect/read timeout split and a much less ergonomic exception model — not
  worth hand-rolling for a first connector.
- httpx's exception hierarchy maps onto the taxonomy above
  (``TooManyRedirects`` / ``TimeoutException`` / ``NetworkError``), but note
  ``httpx.InvalidURL`` is **not** an ``HTTPError`` subclass — an unparseable
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

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import httpx
import trafilatura

from lode.config import Settings

#: Sent on every fetch so a server sees an identifiable, non-empty UA rather
#: than a bare httpx default (some sites 403 a missing/generic UA outright).
_USER_AGENT = "lode-webfetch/1 (+https://github.com/anthropics/lode)"

#: The 4xx codes HTTP itself flags as "try again later" — everything else in
#: the 4xx range is a permanent tombstone. 408 Request Timeout (RFC 9110
#: §15.5.9: "the client MAY repeat the request") and 429 Too Many Requests.
_TRANSIENT_4XX = frozenset({408, 429})

#: At and above this, every status is a 5xx server error — always retryable.
_TRANSIENT_STATUS_FLOOR = 500


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
        (and for a URL httpx cannot parse), and :class:`TooManyRedirectsError`
        if the redirect chain exceeds the cap. It raises nothing else.
        """
        ...


class HttpxFetcher:
    """Default :class:`Fetcher`: a single synchronous GET via ``httpx``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def fetch(self, url: str) -> RawResponse:
        settings = self._settings
        try:
            # max_redirects is a Client-constructor knob, not accepted by the
            # module-level httpx.get() shortcut — a short-lived client is the
            # correct way to set it for one request.
            with httpx.Client(
                follow_redirects=True,
                max_redirects=settings.fetch_max_redirects,
                timeout=settings.fetch_timeout_s,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = client.get(url)
        except httpx.TooManyRedirects as exc:
            raise TooManyRedirectsError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise TransientFetchError(f"timeout: {exc}") from exc
        except httpx.NetworkError as exc:
            raise TransientFetchError(f"network error: {exc}") from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Any other httpx-level failure (e.g. a malformed response, or a
            # URL httpx cannot even parse) — no sharper classification is
            # available, so default to retryable rather than silently
            # tombstoning on an unrecognized condition. httpx.InvalidURL is
            # NOT an httpx.HTTPError subclass, so it must be named explicitly
            # or it escapes this method entirely, unclassified.
            raise TransientFetchError(f"http client error: {exc}") from exc

        if (
            response.status_code in _TRANSIENT_4XX
            or response.status_code >= _TRANSIENT_STATUS_FLOOR
        ):
            raise TransientFetchError(f"http {response.status_code}")

        return RawResponse(
            final_url=str(response.url),
            status_code=response.status_code,
            text=response.text,
        )


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

    if response.status_code >= 400:
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
