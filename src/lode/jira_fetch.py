"""JIRA Cloud fetch unit: REST v3 issue -> structured snapshot (lode-gpzn.3).

Mirrors :mod:`lode.webfetch`'s shape deliberately, so
:func:`lode.externals.ingest_fetch_result` consumes this unit's output
unchanged: this module reuses :class:`~lode.webfetch.FetchResult`,
:class:`~lode.webfetch.FetchStatus`, the :class:`~lode.webfetch.Fetcher`
protocol, and :class:`~lode.webfetch.TransientFetchError` directly rather
than defining parallel types — this is *the same seam*, applied to a second
connector, not a new one. :class:`JiraHttpFetcher` itself is a thin
:class:`~lode.webfetch.HttpxFetcher` subclass (lode-88iv): the httpx2.Client
construction, the four-arm except ladder, the classify_http_status/
:class:`~lode.webfetch.RawResponse` handling all live once in the parent —
this module supplies only the auth/header deltas.

## Request URL reconstruction

Unlike the web connector, ``external_id`` here is a semantic JIRA issue key
(e.g. ``"ABC-123"``), not itself a fetchable URL (lode-gpzn.2 decision A) —
the request is rebuilt from ``external_id`` plus the ``api_base`` persisted
on the ``externals`` row at link-detection time
(:func:`lode.drawdown._resolve_api_base`). :func:`fetch_jira_issue` takes
``api_base`` as an explicit parameter; the caller (:mod:`lode.drawdown`'s
shared ``_refresh_atlassian`` leg, lode-40zj) is responsible for reading it
off the row.

## Body representation (owner decision E, ``/challenge`` 2026-07-17)

JIRA REST v3 issue/comment bodies are Atlassian Document Format (ADF) — a
nested JSON doc, not a string. Rather than writing a bespoke ADF walker, this
unit requests JIRA's own server-side HTML rendering (``expand=renderedFields``
on the issue; ``expand=renderedBody`` on each comment) and runs that HTML
through the *existing* readability extractor,
:func:`lode.webfetch._extract` (trafilatura) — reusing the web path's
extraction step rather than inventing a second one. The raw JSON response(s)
are kept verbatim as ``raw_payload`` (via :attr:`FetchResult.raw_html`, the
same field name web fetches use for provenance) for anyone who later wants
the ADF.

## Classification (owner decision C, ``/challenge`` 2026-07-17)

HTTP status classification goes through the shared, connector-neutral
:func:`lode.fetch_outcome.classify_http_status` — the same function
:class:`~lode.webfetch.HttpxFetcher` uses — not a local re-derivation: 401/
403/404 (and any other non-408/429 4xx) become a tombstone;
408/429/5xx/network/timeout raise :class:`~lode.webfetch.TransientFetchError`
inside the inherited :meth:`~lode.webfetch.HttpxFetcher.fetch` and propagate
uncaught to the caller (the worker's existing attempts/backoff/dead-letter
machinery).

## Comments

JIRA's issue-get response embeds only a page of comments; this unit fetches
the dedicated, paginated comment endpoint
(``/rest/api/3/issue/{key}/comment?startAt=N&expand=renderedBody``) in a
loop until ``startAt + maxResults >= total``, so a multi-page comment
thread is included in full (acceptance criterion).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx2

from lode.config import AtlassianCredentials, Settings, resolve_jira_credentials
from lode.fetch_outcome import HttpOutcome, classify_http_status
from lode.webfetch import (
    Fetcher,
    FetchResult,
    FetchStatus,
    HttpxFetcher,
    TooManyRedirectsError,
    _extract,
    _tombstone,
)

log = logging.getLogger(__name__)

#: See lode.webfetch's _USER_AGENT for the rationale (bare product token, no
#: contact URL — lode-yzv). A distinct token per connector keeps a server
#: operator's logs attributable to which lode fetch path made a request.
_USER_AGENT = "lode-jira-fetch/1"


class JiraHttpFetcher(HttpxFetcher):
    """Default JIRA :class:`~lode.webfetch.Fetcher`: one authenticated GET.

    A thin :class:`~lode.webfetch.HttpxFetcher` subclass (lode-88iv) —
    credentials become Basic auth, an ``Accept: application/json`` header,
    and a connection-establishment-retrying transport
    (:attr:`~lode.config.Settings.atlassian_connect_retries`, lode-lq9u),
    passed straight through to the parent's ``__init__``; ``fetch`` itself
    (the httpx2.Client construction, the four-arm except ladder, and the
    classify_http_status/RawResponse handling) is entirely inherited, not
    re-housed here.
    """

    def __init__(
        self, credentials: AtlassianCredentials, settings: Settings | None = None
    ) -> None:
        settings = settings or Settings()
        super().__init__(
            settings,
            user_agent=_USER_AGENT,
            auth=httpx2.BasicAuth(credentials.email, credentials.token),
            extra_headers={"Accept": "application/json"},
            # Connection-establishment-only retries (lode-lq9u) -- never a
            # sent request, so idempotency-safe. See
            # Settings.atlassian_connect_retries for the rationale and why
            # this is deliberately not applied to webfetch.py's ask-path
            # GuardedHttpxFetcher.
            transport=httpx2.HTTPTransport(retries=settings.atlassian_connect_retries),
        )


def _default_fetcher(settings: Settings, external_id: str) -> JiraHttpFetcher:
    creds = resolve_jira_credentials(settings)
    if creds is None:
        raise RuntimeError(
            f"fetch_jira_issue: JIRA credentials unresolved for "
            f"external_id={external_id!r} -- cannot build a default fetcher"
        )
    return JiraHttpFetcher(creds, settings)


def _fetch_comments(fetcher: Fetcher, api_base: str, external_id: str) -> list[dict]:
    """Paginate ``/issue/{key}/comment`` until every comment is collected.

    A non-OK response on any page beyond the first stops pagination (logged,
    not raised) rather than failing the whole issue fetch over a partial
    comment thread; a :class:`TransientFetchError` from ``fetcher.fetch``
    still propagates uncaught, same as any other request this module makes.
    """
    comments: list[dict] = []
    while True:
        # len(comments) IS the next page's startAt — it grows monotonically by
        # construction (every non-empty page is extended in), so there is no
        # separate offset to track and no way to stall short of `total`.
        url = (
            f"{api_base}/rest/api/3/issue/{external_id}/comment"
            f"?startAt={len(comments)}&expand=renderedBody"
        )
        response = fetcher.fetch(url)
        if classify_http_status(response.status_code) is not HttpOutcome.OK:
            log.warning(
                "jira_fetch: comment page for %r returned http %s; stopping "
                "pagination with %d comment(s) collected so far",
                external_id,
                response.status_code,
                len(comments),
            )
            break
        payload = json.loads(response.text)
        page = payload.get("comments") or []
        comments.extend(page)
        # An empty page guards against a server that under-reports `total`;
        # otherwise stop once we have collected every comment it claims.
        if not page or len(comments) >= payload.get("total", len(comments)):
            break
    return comments


def _render_issue_html(issue: dict, comments: list[dict]) -> str:
    """Assemble a synthetic HTML doc for :func:`lode.webfetch._extract`.

    Uses JIRA's own server-rendered HTML (``renderedFields``/``renderedBody``)
    rather than walking ADF (owner decision E) — see module docstring.
    """
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or ""
    rendered = issue.get("renderedFields") or {}
    description_html = rendered.get("description") or ""

    parts = [f"<h1>{html_lib.escape(summary)}</h1>", description_html]
    for comment in comments:
        author = ((comment.get("author") or {}).get("displayName")) or "unknown"
        body_html = comment.get("renderedBody") or ""
        parts.append(
            f"<div><p><strong>{html_lib.escape(author)}</strong></p>{body_html}</div>"
        )
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def fetch_jira_issue(
    external_id: str,
    api_base: str,
    *,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> FetchResult:
    """Fetch + structure one JIRA Cloud issue into a :class:`FetchResult`.

    Pure function, no DB writes: ``(external_id, api_base) -> FetchResult`` —
    see the module docstring for the full fetch/classify/render pipeline. A
    transient failure (429/5xx/network/timeout) is **not** caught here:
    :class:`~lode.webfetch.TransientFetchError` propagates to the caller,
    exactly like :func:`lode.webfetch.fetch_and_extract`.
    """
    settings = settings or Settings()
    fetcher = fetcher or _default_fetcher(settings, external_id)

    issue_url = f"{api_base}/rest/api/3/issue/{external_id}?expand=renderedFields"
    try:
        response = fetcher.fetch(issue_url)
    except TooManyRedirectsError:
        # A conforming Fetcher raises this as a named, permanent outcome (see
        # JiraHttpFetcher.fetch); tombstone it in one attempt exactly as
        # lode.webfetch.fetch_and_extract does, rather than letting it ride the
        # worker's transient-retry/backoff cycle before it dead-letters.
        return _tombstone(final_url=issue_url, reason="too_many_redirects")

    if classify_http_status(response.status_code) is not HttpOutcome.OK:
        return _tombstone(
            final_url=response.final_url,
            reason=f"http_{response.status_code}",
            http_status=response.status_code,
            raw_html=response.text,
        )

    issue = json.loads(response.text)
    comments = _fetch_comments(fetcher, api_base, external_id)
    raw_payload = json.dumps({"issue": issue, "comments": comments})

    clean_text = _extract(_render_issue_html(issue, comments))
    if clean_text is None or len(clean_text) < settings.fetch_min_extract_chars:
        return _tombstone(
            final_url=response.final_url,
            reason="empty_extract",
            http_status=response.status_code,
            raw_html=raw_payload,
        )

    return FetchResult(
        status=FetchStatus.OK,
        final_url=response.final_url,
        clean_text=clean_text,
        raw_html=raw_payload,
        http_status=response.status_code,
        tombstone_reason=None,
    )


# ---------------------------------------------------------------------------
# JIRA search (lode-8hsk) -- ids + titles only, targeting the CHANGE-2046
# replacement endpoint (verified finding, bd lode-6nwu).
# ---------------------------------------------------------------------------


class JiraSearchError(Exception):
    """A JIRA search request failed; carries no results (nothing to persist)."""


@dataclass(frozen=True)
class JiraSearchHit:
    """One JIRA search result: an identifier and a title, nothing else.

    Deliberately shaped to make a body/snippet field impossible, not merely
    absent -- the acceptance criterion this ticket names ("Search tools
    return identifiers and titles ONLY ... asserted ... that the schema
    makes one impossible rather than merely absent").
    """

    external_id: str
    title: str


def _jql_escape(text: str) -> str:
    """Escape free text for embedding inside a JQL double-quoted string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def search_jira_issues(
    query: str,
    api_base: str,
    *,
    max_results: int = 25,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> list[JiraSearchHit]:
    """Search JIRA issues by free text; returns identifiers and titles only.

    Targets ``GET /rest/api/3/search/jql`` -- the CHANGE-2046 replacement for
    the retired ``GET/POST /rest/api/3/search``, which this module never
    calls (verified finding, bd ``lode-6nwu``). Two migration traps that
    finding names, both handled here:

    - ``fields`` is passed **explicitly** as ``summary``. ``/search/jql``
      documents "By default, this resource returns IDs only" -- omitting
      ``fields`` would silently degrade this function's ids+titles contract
      to ids alone.
    - ``jql`` is always a **bounded** query (a ``text ~ "..."`` clause over
      the caller's ``query``, never a bare/unfiltered JQL string) --
      ``/search/jql`` rejects an unbounded ``jql``.

    Fetches a **single page** (``maxResults`` capped at ``max_results``) --
    a tool-search call is not a full corpus traversal, so this never follows
    ``nextPageToken``; a caller wanting more/different results asks a
    narrower query. (Full pagination contract for a future full-traversal
    caller, if one is ever built: terminate on the *absence* of
    ``nextPageToken`` -- there is no ``total``/``startAt`` any more -- see
    ``bd lode-6nwu``'s design field.)

    Raises :class:`JiraSearchError` on any non-OK **or malformed** response
    -- the same shape as :func:`lode.confluence.search_confluence_pages`, and
    load-bearing rather than cosmetic: :func:`lode.tool_dispatch.make_tool_result`
    turns this exception into an error string the model sees, whereas a raw
    ``json.JSONDecodeError`` would escape the tool-result callback and abort
    the whole ``run_tool_turns`` run. Never persists
    anything (unlike :func:`fetch_jira_issue`, a search result has no
    identity to snapshot -- ``docs/externals.md`` "A query result has no
    identity").
    """
    settings = settings or Settings()
    fetcher = fetcher or _default_fetcher(settings, query)
    api_base = api_base.rstrip("/")
    jql = f'text ~ "{_jql_escape(query)}" ORDER BY updated DESC'
    url = (
        f"{api_base}/rest/api/3/search/jql"
        f"?jql={quote(jql, safe='')}&maxResults={max_results}&fields=summary"
    )
    response = fetcher.fetch(url)
    if classify_http_status(response.status_code) is not HttpOutcome.OK:
        raise JiraSearchError(
            f"jira search failed for {query!r}: http_{response.status_code}"
        )
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise JiraSearchError(
            f"jira search returned a malformed response for {query!r}: {exc}"
        ) from exc
    hits: list[JiraSearchHit] = []
    for issue in payload.get("issues") or []:
        key = issue.get("key")
        if key is None:
            continue
        summary = ((issue.get("fields") or {}).get("summary")) or ""
        hits.append(JiraSearchHit(external_id=str(key), title=summary))
    return hits
