"""JIRA Cloud fetch unit: REST v3 issue -> structured snapshot (lode-gpzn.3).

Mirrors :mod:`lode.webfetch`'s shape deliberately, so
:func:`lode.externals.ingest_fetch_result` consumes this unit's output
unchanged: this module reuses :class:`~lode.webfetch.FetchResult`,
:class:`~lode.webfetch.FetchStatus`, the :class:`~lode.webfetch.Fetcher`
protocol, and :class:`~lode.webfetch.TransientFetchError` directly rather
than defining parallel types — this is *the same seam*, applied to a second
connector, not a new one. :class:`JiraHttpFetcher` itself is a thin
:class:`~lode.webfetch.HttpxFetcher` subclass (lode-88iv): the httpx.Client
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

import httpx

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
    credentials become Basic auth and an ``Accept: application/json`` header,
    passed straight through to the parent's ``__init__``; ``fetch`` itself
    (the httpx.Client construction, the four-arm except ladder, and the
    classify_http_status/RawResponse handling) is entirely inherited, not
    re-housed here.
    """

    def __init__(
        self, credentials: AtlassianCredentials, settings: Settings | None = None
    ) -> None:
        super().__init__(
            settings,
            user_agent=_USER_AGENT,
            auth=httpx.BasicAuth(credentials.email, credentials.token),
            extra_headers={"Accept": "application/json"},
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
