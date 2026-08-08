"""Confluence Cloud fetch unit: REST page -> structured snapshot (lode-gpzn.4).

A **pure** fetch/extract unit, no storage coupling — the Confluence-Cloud
sibling of :mod:`lode.webfetch` (the web draw-down connector). Given a page's
semantic ``external_id`` (the numeric page id captured by
:mod:`lode.drawdown`'s Confluence link detection, lode-gpzn.2) and the
``api_base`` persisted alongside it on the ``externals`` row,
:func:`fetch_confluence_page` rebuilds the REST request, fetches the page,
and returns a :class:`~lode.webfetch.FetchResult` — the **exact same**
dataclass :mod:`lode.webfetch` uses, so
:func:`lode.externals.ingest_fetch_result` consumes it unchanged, with no
Confluence-specific branch needed there.

## Design decisions (owner, via ``/challenge``, 2026-07-17 — see bd lode-gpzn.4)

- **(A) URL reconstruction:** ``external_id`` is a *semantic* key (a page
  id), not a URL — since lode-gpzn.2, it is no longer directly fetchable the
  way a web ``external_id`` is. The request URL is rebuilt from
  ``{api_base}/wiki/rest/api/content/{external_id}?expand=body.view``, where
  ``api_base`` is exactly what :mod:`lode.drawdown` persisted onto the
  ``externals`` row at link-detection time (its own ``_resolve_api_base``) —
  this unit never re-derives it from the original pasted URL, which it never
  sees.
- **(E) Body representation:** request the server-rendered ``body.view``
  representation (``expand=body.view``), **not** raw storage-format XHTML.
  Confluence storage format is custom XHTML full of
  ``ac:structured-macro``/``ri:...`` macro elements that would need a
  bespoke parser to strip correctly; ``body.view`` is Confluence's own
  server-rendered HTML with every macro already expanded, so the
  **existing** :func:`lode.webfetch._extract` (trafilatura) — the identical
  extractor the web path and the JIRA unit (lode-gpzn.3) both use — turns it
  into ``clean_text`` with no Confluence-specific extraction code at all.
  The full raw JSON response (not just the ``body.view`` fragment) is kept
  as ``raw_payload`` for provenance, mirroring
  :attr:`~lode.webfetch.FetchResult.raw_html`'s field on the web leg even though what it
  holds
  here is JSON, not HTML.
- **(C) Classification:** every HTTP outcome is classified via the shared,
  connector-neutral :func:`lode.fetch_outcome.classify_http_status`
  (lode-gpzn.13) — 401/403/404 (and any other non-408/429 4xx) tombstone;
  429/any 5xx/network/timeout raise :class:`~lode.webfetch.TransientFetchError`, riding
  the worker's existing attempts/backoff/
  dead-letter machinery exactly like the web and JIRA legs. No local
  reimplementation of the status-code mapping.
- **Injectable offline seam:** :func:`fetch_confluence_page` accepts a
  ``fetcher`` implementing the exact same :class:`lode.webfetch.Fetcher`
  protocol (``fetch(url: str) -> RawResponse``) the web and JIRA units use —
  production supplies :class:`HttpxConfluenceFetcher` (Basic auth baked in
  at construction), tests supply a stub, so the offline gate never makes a
  real request.

## Explicitly out of scope for this ticket

- **Page comments:** unlike the JIRA unit (lode-gpzn.3), which paginates and
  includes issue comments, this unit maps **only the page body** — the
  acceptance criteria call for body-only mapping via ``body.view`` ->
  trafilatura, nothing more. A parity follow-up (comments-in-snapshot) is a
  separate future decision, not resolved here.
- **Wiring into :func:`lode.drawdown.refresh_external`'s dispatcher:** this
  ticket's acceptance criteria are scoped to the fetch unit itself (offline-
  tested in isolation) — the dispatcher wiring, alongside the JIRA leg (a
  sibling in-flight ticket touching the very same function), was left to a
  follow-up so the two don't race each other editing the same lines. That
  follow-up (lode-mfts) has since wired both legs in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote

from lode.config import AtlassianCredentials, Settings, resolve_confluence_credentials
from lode.fetch_outcome import HttpOutcome, classify_http_status
from lode.webfetch import (
    Fetcher,
    FetchResult,
    FetchStatus,
    HttpxFetcher,
    _extract,
    _tombstone,
)

#: Sent on every Confluence REST call. A bare product token, no ``(+<url>)``
#: contact link — same reasoning as :mod:`lode.webfetch`'s ``_USER_AGENT``
#: (lode-yzv): this project has no owned URL to publish, and the party
#: actually making the call is the end user's own machine, not a centrally
#: operated crawler.
_USER_AGENT = "lode-confluence/1"


class HttpxConfluenceFetcher(HttpxFetcher):
    """Default :class:`~lode.webfetch.Fetcher` for the Confluence Cloud REST API.

    A thin :class:`~lode.webfetch.HttpxFetcher` subclass (lode-88iv): the
    httpx.Client construction, the except ladder, and the
    classify_http_status/:class:`~lode.webfetch.RawResponse` handling are
    all inherited unchanged. This subclass supplies HTTP Basic auth (the
    resolved Confluence :class:`~lode.config.AtlassianCredentials`), an
    ``Accept: application/json`` header, and ``follow_redirects=False``: a
    REST API endpoint answering an authenticated GET has no legitimate
    reason to 3xx the way a user-pasted web page does (the parent follows up
    to ``fetch_max_redirects`` hops for exactly that reason; this fetcher has
    no analogous need and never sees a ``TooManyRedirectsError``).
    """

    def __init__(
        self, credentials: AtlassianCredentials, settings: Settings | None = None
    ) -> None:
        super().__init__(
            settings,
            user_agent=_USER_AGENT,
            auth=(credentials.email, credentials.token),
            extra_headers={"Accept": "application/json"},
            follow_redirects=False,
        )


def _build_url(external_id: str, api_base: str) -> str:
    """Rebuild the Confluence Cloud REST request URL from persisted fields.

    ``{api_base}/wiki/rest/api/content/{page_id}?expand=body.view`` —
    ``api_base`` is exactly what :mod:`lode.drawdown` persisted at
    detection time (already stripped of a trailing slash by its own
    ``_resolve_api_base``, but stripped again here defensively since this
    function has no other way to guarantee that invariant holds).
    """
    return (
        f"{api_base.rstrip('/')}/wiki/rest/api/content/"
        f"{quote(external_id, safe='')}?expand=body.view"
    )


def fetch_confluence_page(
    external_id: str,
    api_base: str,
    *,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> FetchResult:
    """Fetch one Confluence Cloud page and extract it into a :class:`~lode.webfetch.FetchResult`.

    Pure function, no DB writes — mirrors :func:`lode.webfetch.fetch_and_extract`'s
    contract exactly, so :func:`lode.externals.ingest_fetch_result` consumes the result
    unchanged. See the module docstring for the full design.

    1. Rebuild the request URL from ``external_id`` (semantic page id) +
       ``api_base`` (:func:`_build_url`).
    2. GET it via ``fetcher`` (production: :class:`HttpxConfluenceFetcher`,
       built from the resolved Confluence credentials when ``fetcher`` is
       not supplied — raises :class:`RuntimeError` if credentials are
       unresolved, since a caller reaching this function is expected to have
       already checked :func:`lode.config.confluence_active`).
    3. Classify the response status via
    :func:`~lode.fetch_outcome.classify_http_status`. A conforming ``fetcher`` already
    raises
       :class:`~lode.webfetch.TransientFetchError` for 408/429/5xx/network
       conditions before returning (see :class:`HttpxConfluenceFetcher`), so
       this only ever needs to turn a non-OK *returned* status (401/403/404/
       ...) into a tombstone — the same defensive "not OK -> tombstone"
       shape :func:`lode.webfetch.fetch_and_extract` uses, in case of a
       non-conforming custom ``fetcher``.
    4. Parse the JSON body and pull ``body.view.value`` (the server-rendered
       HTML). A response that isn't valid JSON, or lacks that path, is
       treated the same as an unextractable page: tombstoned rather than
       raised, since retrying an identical request yields an identical
       malformed response.
    5. Run the HTML through :func:`lode.webfetch._extract` (trafilatura) —
       the exact same extractor the web path and the JIRA unit use, no
       bespoke Confluence markup handling. ``None`` or too-short output (the
       existing ``fetch_min_extract_chars`` floor) tombstones exactly like
       the web leg's own "2xx but no real content" case.

    The full raw JSON response text is kept as ``raw_html`` on the returned
    ``FetchResult`` either way (on tombstone, when a response was actually
    read) — the field name is the web leg's, but here it carries the raw
    JSON payload per this ticket's decision, and :func:`ingest_fetch_result`
    stores it verbatim as ``raw_payload``.
    """
    settings = settings or Settings()
    if fetcher is None:
        credentials = resolve_confluence_credentials(settings)
        if credentials is None:
            raise RuntimeError(
                "fetch_confluence_page: Confluence Cloud credentials are "
                "unresolved -- the caller should have already checked "
                "lode.config.confluence_active() before reaching this unit"
            )
        fetcher = HttpxConfluenceFetcher(credentials, settings)

    url = _build_url(external_id, api_base)
    response = fetcher.fetch(url)

    # Defensive, mirrors fetch_and_extract's own "not OK -> tombstone" shape
    # (see its comment): a conforming Fetcher already raised
    # TransientFetchError for a 408/429/5xx before returning, so this
    # branch is only ever reached for a genuine tombstone status in
    # practice, but stays correct for a non-conforming custom fetcher too.
    if classify_http_status(response.status_code) is not HttpOutcome.OK:
        return _tombstone(
            final_url=response.final_url,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            raw_html=response.text,
        )

    try:
        payload = json.loads(response.text)
        html = payload["body"]["view"]["value"]
        if not isinstance(html, str):
            raise TypeError("body.view.value is not a string")
    except json.JSONDecodeError, KeyError, TypeError:
        return _tombstone(
            final_url=response.final_url,
            reason="malformed_response",
            http_status=response.status_code,
            raw_html=response.text,
        )

    clean_text = _extract(html)
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


# ---------------------------------------------------------------------------
# Confluence search (lode-8hsk) -- ids + titles only, CQL text search.
# ---------------------------------------------------------------------------


class ConfluenceSearchError(Exception):
    """A Confluence search request failed; carries no results (nothing to persist)."""


@dataclass(frozen=True)
class ConfluenceSearchHit:
    """One Confluence search result: an identifier and a title, nothing else.

    Mirrors :class:`lode.jira_fetch.JiraSearchHit` -- see its docstring for
    why the shape makes a body/snippet field impossible, not merely absent.
    """

    external_id: str
    title: str


def _cql_escape(text: str) -> str:
    """Escape free text for embedding inside a CQL double-quoted string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def search_confluence_pages(
    query: str,
    api_base: str,
    *,
    max_results: int = 25,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> list[ConfluenceSearchHit]:
    """Search Confluence pages by free text; returns identifiers and titles only.

    ``GET {api_base}/wiki/rest/api/content/search?cql=...`` -- ``type=page``
    scoped (excludes blogposts/attachments/comments) with a free-text
    ``text ~ "..."`` clause built from ``query``, CQL-escaped
    (:func:`_cql_escape`). Single page (``limit`` capped at ``max_results``)
    -- a tool-search call, not a full-space traversal.

    Raises :class:`ConfluenceSearchError` on any non-OK or malformed
    response; never persists anything -- unlike :func:`fetch_confluence_page`,
    a search result has no identity to snapshot (``docs/externals.md`` "A
    query result has no identity").
    """
    settings = settings or Settings()
    api_base = api_base.rstrip("/")
    if fetcher is None:
        credentials = resolve_confluence_credentials(settings)
        if credentials is None:
            raise ConfluenceSearchError(
                "search_confluence_pages: Confluence Cloud credentials are "
                "unresolved -- the caller should have already checked "
                "lode.config.confluence_active() before reaching this unit"
            )
        fetcher = HttpxConfluenceFetcher(credentials, settings)

    cql = f'type=page AND text ~ "{_cql_escape(query)}"'
    url = (
        f"{api_base}/wiki/rest/api/content/search"
        f"?cql={quote(cql, safe='')}&limit={max_results}"
    )
    response = fetcher.fetch(url)
    if classify_http_status(response.status_code) is not HttpOutcome.OK:
        raise ConfluenceSearchError(
            f"confluence search failed for {query!r}: http_{response.status_code}"
        )
    try:
        payload = json.loads(response.text)
        results = payload.get("results") or []
    except json.JSONDecodeError as exc:
        raise ConfluenceSearchError(
            f"confluence search returned a malformed response for {query!r}: {exc}"
        ) from exc

    hits: list[ConfluenceSearchHit] = []
    for result in results:
        page_id = result.get("id")
        if page_id is None:
            continue
        hits.append(
            ConfluenceSearchHit(
                external_id=str(page_id), title=result.get("title") or ""
            )
        )
    return hits


__all__ = [
    "ConfluenceSearchError",
    "ConfluenceSearchHit",
    "HttpxConfluenceFetcher",
    "fetch_confluence_page",
    "search_confluence_pages",
]
