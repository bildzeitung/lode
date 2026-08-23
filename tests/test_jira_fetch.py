"""Tests for lode.jira_fetch — JIRA Cloud fetch unit (lode-gpzn.3).

Covers the acceptance criteria: a fixture issue JSON maps to a non-empty
structured body; the request URL is rebuilt from external_id + persisted
API base; 401/403/404 tombstone and 429/5xx raise transient via the shared
lode.fetch_outcome classifier (not a local copy); comments beyond page 1
are included; no live network is made anywhere in this file.

Two layers of test, mirroring tests/test_webfetch.py's own split:

* :func:`fetch_jira_issue` against a stub :class:`~lode.webfetch.Fetcher`
  (``_QueueFetcher``) — the fetch/classify/render pipeline, entirely
  transport-agnostic.
* :class:`JiraHttpFetcher` itself against a fake ``httpx2.Client`` (mirrors
  tests/test_webfetch.py's ``TestHttpxFetcher``) — proves the real
  transport actually wires Basic auth and reaches the shared classifier,
  not just that the pipeline *would* handle a transient status if it saw
  one.
"""

import json
from typing import Self

import httpx2
import pytest

from lode.config import AtlassianCredentials, load_settings
from lode.jira_fetch import (
    JiraHttpFetcher,
    JiraSearchError,
    fetch_jira_issue,
    search_jira_issues,
)
from lode.webfetch import (
    FetchStatus,
    RawResponse,
    TooManyRedirectsError,
    TransientFetchError,
)

_API_BASE = "https://acme.atlassian.net"
_KEY = "ABC-123"
_ISSUE_URL = f"{_API_BASE}/rest/api/3/issue/{_KEY}?expand=renderedFields"


def _comment_url(start_at: int) -> str:
    return (
        f"{_API_BASE}/rest/api/3/issue/{_KEY}/comment"
        f"?startAt={start_at}&expand=renderedBody"
    )


_ISSUE_JSON = {
    "fields": {"summary": "Something broke in prod"},
    "renderedFields": {
        "description": (
            "<p>This issue describes a real production incident that "
            "impacted several downstream services and needs careful "
            "triage before it can be closed out properly.</p>"
        )
    },
}

_COMMENT_PAGE_1 = {
    "startAt": 0,
    "maxResults": 1,
    "total": 2,
    "comments": [
        {
            "author": {"displayName": "Alice"},
            "renderedBody": (
                "<p>I can reproduce this locally with the staging config "
                "and a fresh checkout of the affected service.</p>"
            ),
        }
    ],
}

_COMMENT_PAGE_2 = {
    "startAt": 1,
    "maxResults": 1,
    "total": 2,
    "comments": [
        {
            "author": {"displayName": "Bob"},
            "renderedBody": (
                "<p>Second-page comment confirming the root cause is a "
                "stale cache entry that never expired correctly.</p>"
            ),
        }
    ],
}


class _QueueFetcher:
    """Stub :class:`~lode.webfetch.Fetcher` returning canned responses in order.

    Each queued item is either a :class:`RawResponse` to return or an
    exception instance to raise — mirrors the shape of the ``_StubFetcher``
    used throughout tests/test_webfetch.py and tests/test_drawdown.py, but
    supports the multiple sequential calls (issue, then N comment pages)
    :func:`fetch_jira_issue` makes.
    """

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(payload: dict, *, url: str, status_code: int = 200) -> RawResponse:
    return RawResponse(final_url=url, status_code=status_code, text=json.dumps(payload))


# ---------------------------------------------------------------------------
# fetch_jira_issue — fetch/classify/render pipeline
# ---------------------------------------------------------------------------


def test_ok_issue_maps_to_nonempty_structured_body_including_paginated_comments():
    fetcher = _QueueFetcher(
        [
            _response(_ISSUE_JSON, url=_ISSUE_URL),
            _response(_COMMENT_PAGE_1, url=_comment_url(0)),
            _response(_COMMENT_PAGE_2, url=_comment_url(1)),
        ]
    )

    result = fetch_jira_issue(
        _KEY, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.OK
    assert result.tombstone_reason is None
    assert result.clean_text is not None
    assert "production incident" in result.clean_text
    # The acceptance criterion: comments beyond page 1 are included.
    assert "stale cache entry" in result.clean_text
    # Raw JSON kept verbatim for provenance.
    raw = json.loads(result.raw_html)
    assert raw["issue"] == _ISSUE_JSON
    assert len(raw["comments"]) == 2


def test_request_url_rebuilt_from_external_id_and_api_base():
    fetcher = _QueueFetcher(
        [
            _response(_ISSUE_JSON, url=_ISSUE_URL),
            _response(_COMMENT_PAGE_1, url=_comment_url(0)),
            _response(_COMMENT_PAGE_2, url=_comment_url(1)),
        ]
    )

    fetch_jira_issue(_KEY, _API_BASE, fetcher=fetcher, settings=load_settings())

    # No externals-table URL is ever passed in -- the issue URL is rebuilt
    # purely from external_id (the issue key) + api_base.
    assert fetcher.calls[0] == _ISSUE_URL
    assert fetcher.calls[1] == _comment_url(0)
    assert fetcher.calls[2] == _comment_url(1)


@pytest.mark.parametrize("status_code", [401, 403, 404, 410])
def test_permanent_http_failure_on_issue_yields_tombstone(status_code):
    fetcher = _QueueFetcher(
        [RawResponse(final_url=_ISSUE_URL, status_code=status_code, text="denied")]
    )

    result = fetch_jira_issue(
        _KEY, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == f"http_{status_code}"
    assert result.http_status == status_code
    assert result.clean_text is None
    # Tombstoned before ever reaching the comment endpoint.
    assert fetcher.calls == [_ISSUE_URL]


def test_transient_failure_on_issue_propagates_uncaught():
    """A conforming Fetcher already raises for 429/5xx (see JiraHttpFetcher
    tests below) -- fetch_jira_issue itself does not catch it, mirroring
    lode.webfetch.fetch_and_extract's identical contract."""
    fetcher = _QueueFetcher([TransientFetchError("http 503")])

    with pytest.raises(TransientFetchError):
        fetch_jira_issue(_KEY, _API_BASE, fetcher=fetcher, settings=load_settings())


def test_transient_failure_on_comment_page_propagates_uncaught():
    fetcher = _QueueFetcher(
        [
            _response(_ISSUE_JSON, url=_ISSUE_URL),
            TransientFetchError("http 429"),
        ]
    )

    with pytest.raises(TransientFetchError):
        fetch_jira_issue(_KEY, _API_BASE, fetcher=fetcher, settings=load_settings())


def test_too_many_redirects_on_issue_yields_tombstone():
    """A redirect-exhaustion is a permanent condition -- tombstoned in one
    attempt (reason=too_many_redirects), mirroring
    lode.webfetch.fetch_and_extract, not left to ride the transient-retry
    cycle."""
    fetcher = _QueueFetcher([TooManyRedirectsError("too many redirects")])

    result = fetch_jira_issue(
        _KEY, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "too_many_redirects"
    assert result.clean_text is None


def test_empty_extract_yields_tombstone():
    tiny_issue = {"fields": {"summary": "x"}, "renderedFields": {"description": ""}}
    fetcher = _QueueFetcher(
        [
            _response(tiny_issue, url=_ISSUE_URL),
            _response(
                {"startAt": 0, "maxResults": 0, "total": 0, "comments": []},
                url=_comment_url(0),
            ),
        ]
    )

    result = fetch_jira_issue(
        _KEY, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "empty_extract"


def test_permanent_failure_on_comment_page_stops_pagination_not_whole_fetch():
    """A non-OK comment page (e.g. comments restricted) stops pagination but
    does not fail the issue fetch -- the issue's own content still ingests."""
    # Description alone must clear fetch_min_extract_chars (200) since no
    # comment ever contributes text in this scenario -- _ISSUE_JSON's shorter
    # description relies on the comments to clear the floor in other tests.
    issue_with_long_description = {
        "fields": {"summary": "Something broke in prod"},
        "renderedFields": {
            "description": "<p>" + ("A real production incident. " * 10) + "</p>"
        },
    }
    fetcher = _QueueFetcher(
        [
            _response(issue_with_long_description, url=_ISSUE_URL),
            RawResponse(final_url=_comment_url(0), status_code=403, text="denied"),
        ]
    )

    result = fetch_jira_issue(
        _KEY, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.OK
    assert "production incident" in result.clean_text
    raw = json.loads(result.raw_html)
    assert raw["comments"] == []


def test_default_fetcher_raises_without_resolvable_credentials():
    with pytest.raises(RuntimeError, match=_KEY):
        fetch_jira_issue(_KEY, _API_BASE, settings=load_settings())


# ---------------------------------------------------------------------------
# JiraHttpFetcher — real transport wiring, entirely offline (fake httpx2.Client)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, url: str, text: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.text = text


def _fake_client_cls(status_code: int, captured: dict) -> type:
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


_CREDS = AtlassianCredentials(email="a@example.com", token="tok")


class TestJiraHttpFetcher:
    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503])
    def test_transient_status_codes_raise_via_shared_classifier(
        self, monkeypatch, status_code
    ):
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(status_code, {}))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch(_ISSUE_URL)

    @pytest.mark.parametrize("status_code", [200, 401, 403, 404, 410])
    def test_non_transient_status_codes_return_a_response(
        self, monkeypatch, status_code
    ):
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(status_code, {}))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        response = fetcher.fetch(_ISSUE_URL)

        assert response.status_code == status_code

    def test_basic_auth_credentials_are_wired_into_the_client(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        fetcher.fetch(_ISSUE_URL)

        auth = captured["auth"]
        assert isinstance(auth, httpx2.BasicAuth)

    def test_config_knobs_are_wired_into_the_client(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = JiraHttpFetcher(
            _CREDS, load_settings(fetch_max_redirects=3, fetch_timeout_s=2.5)
        )

        fetcher.fetch(_ISSUE_URL)

        assert captured["max_redirects"] == 3
        assert captured["timeout"] == 2.5
        assert captured["follow_redirects"] is True

    def test_connection_error_is_transient(self):
        fetcher = JiraHttpFetcher(_CREDS, load_settings(fetch_timeout_s=1.0))

        with pytest.raises(TransientFetchError):
            fetcher.fetch("http://127.0.0.1:1/")

    def test_carries_a_connection_retrying_transport(self, monkeypatch):
        """lode-lq9u: the client is built with an explicit httpx2.HTTPTransport
        carrying Settings.atlassian_connect_retries, not the bare default (zero
        connection-establishment retries)."""
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))
        fetcher = JiraHttpFetcher(_CREDS, load_settings(atlassian_connect_retries=3))

        fetcher.fetch(_ISSUE_URL)

        transport = captured["transport"]
        assert isinstance(transport, httpx2.HTTPTransport)
        assert transport._pool._retries == 3


class TestFetchJiraIssueDefaultFetcherWiring:
    """Proves the production path (no fetcher override) really builds a
    JiraHttpFetcher and reaches real httpx2 -- the same lesson
    tests/test_drawdown.py::TestRefreshExternalRealWiring encodes for the
    web connector (an injectable seam that makes tests offline can leave
    the *default* implementation's wiring unexercised)."""

    def test_default_fetcher_reaches_real_httpx_client_with_credentials(
        self, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(200, captured))

        settings = load_settings(
            jira_enabled=True, jira_token="tok", jira_email="a@example.com"
        )

        with pytest.raises(json.JSONDecodeError):
            # No fetcher= override -- the exact call drawdown._refresh_atlassian
            # makes in production. The fake client returns empty text, so JSON
            # parsing fails -- proof enough that a *real* JiraHttpFetcher
            # (not a stub) was built and actually called through to httpx2,
            # with credentials, without asserting anything about a full
            # successful fetch (that's fetch_jira_issue's own suite above).
            fetch_jira_issue(_KEY, _API_BASE, settings=settings)

        assert isinstance(captured["auth"], httpx2.BasicAuth)

    def test_401_tombstone_never_echoes_the_token(self, monkeypatch):
        """lode-gpzn.5 acceptance: "the token value is never printed."

        Exercises the real, credential-consuming path (the default
        JiraHttpFetcher -- no stub fetcher override) with a real token
        wired all the way into httpx2.BasicAuth, forces a 401, and asserts
        the resulting FetchResult -- the object drawdown._refresh_jira folds
        into 'lode work's visible outcome line (lode-gpzn.5) -- carries no
        trace of the token anywhere: not in the classified tombstone
        reason, not in the raw payload it keeps for provenance.
        """
        captured: dict = {}
        monkeypatch.setattr(httpx2, "Client", _fake_client_cls(401, captured))

        fake_token = "super-secret-jira-token-xyz"
        settings = load_settings(
            jira_enabled=True, jira_token=fake_token, jira_email="a@example.com"
        )

        result = fetch_jira_issue(_KEY, _API_BASE, settings=settings)

        # Proves the real credential-consuming path was reached (same check
        # as the sibling wiring test above), token included.
        auth = captured["auth"]
        assert isinstance(auth, httpx2.BasicAuth)

        assert result.status is FetchStatus.TOMBSTONE
        assert result.tombstone_reason == "http_401"
        assert fake_token not in (result.tombstone_reason or "")
        assert fake_token not in (result.raw_html or "")
        assert fake_token not in (result.clean_text or "")


# ---------------------------------------------------------------------------
# search_jira_issues (lode-8hsk) -- ids + titles only, /search/jql endpoint
# ---------------------------------------------------------------------------


class TestSearchJiraIssues:
    def test_hits_return_id_and_title_only(self) -> None:
        payload = {
            "issues": [
                {"key": "ABC-1", "fields": {"summary": "First issue"}},
                {"key": "ABC-2", "fields": {"summary": "Second issue"}},
            ],
            "nextPageToken": None,
        }
        fetcher = _QueueFetcher(
            [_response(payload, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )

        hits = search_jira_issues("prod outage", _API_BASE, fetcher=fetcher)

        assert [(h.external_id, h.title) for h in hits] == [
            ("ABC-1", "First issue"),
            ("ABC-2", "Second issue"),
        ]
        # No body/snippet attribute exists on the hit at all -- the schema
        # makes it impossible, not merely absent.
        assert not hasattr(hits[0], "body")
        assert not hasattr(hits[0], "snippet")

    def test_targets_the_search_jql_replacement_endpoint_never_the_retired_one(
        self,
    ) -> None:
        # lode-6nwu verified finding: GET/POST /rest/api/3/search is retired
        # under CHANGE-2046 -- this must never be requested.
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues("q", _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert called_url.startswith(f"{_API_BASE}/rest/api/3/search/jql?")
        assert "/rest/api/3/search?" not in called_url

    def test_fields_is_passed_explicitly_as_summary(self) -> None:
        # lode-6nwu's single most likely migration bug: /search/jql defaults
        # to returning IDs only unless `fields` is passed explicitly.
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues("q", _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "fields=summary" in called_url

    def test_jql_carries_a_bounding_text_clause(self) -> None:
        # /search/jql rejects an unbounded jql (e.g. a bare "order by ...").
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues("prod outage", _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "jql=text" in called_url.replace("%20", " ").replace("+", " ")

    def test_query_text_is_jql_escaped(self) -> None:
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues('say "hi"', _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        # The embedded quote must be escaped, not left to break the JQL string.
        from urllib.parse import unquote

        assert '\\"hi\\"' in unquote(called_url)

    def test_api_base_trailing_slash_is_stripped(self) -> None:
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues("q", f"{_API_BASE}/", fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "//rest" not in called_url

    def test_non_ok_response_raises_search_error(self) -> None:
        fetcher = _QueueFetcher(
            [
                RawResponse(
                    final_url=f"{_API_BASE}/rest/api/3/search/jql",
                    status_code=410,
                    text="Gone",
                )
            ]
        )
        with pytest.raises(JiraSearchError):
            search_jira_issues("q", _API_BASE, fetcher=fetcher)

    def test_max_results_is_sent_on_the_wire(self) -> None:
        fetcher = _QueueFetcher(
            [_response({"issues": []}, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        search_jira_issues("q", _API_BASE, max_results=5, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "maxResults=5" in called_url

    def test_malformed_response_raises_search_error(self) -> None:
        # Same shape as confluence.search_confluence_pages, and load-bearing:
        # tool_dispatch.make_tool_result converts JiraSearchError into an
        # error string for the model, but a raw json.JSONDecodeError would
        # escape the tool_result callback and abort the whole ask.
        fetcher = _QueueFetcher(
            [
                RawResponse(
                    final_url=f"{_API_BASE}/rest/api/3/search/jql",
                    status_code=200,
                    text="<html>not json</html>",
                )
            ]
        )
        with pytest.raises(JiraSearchError):
            search_jira_issues("q", _API_BASE, fetcher=fetcher)

    def test_issue_without_a_key_is_skipped_not_a_keyerror(self) -> None:
        # Mirrors search_confluence_pages' `id is None -> continue` guard: a
        # KeyError here would escape make_tool_result the same way.
        payload = {
            "issues": [
                {"fields": {"summary": "no key"}},
                {"key": "ABC-9", "fields": {"summary": "has key"}},
            ]
        }
        fetcher = _QueueFetcher(
            [_response(payload, url=f"{_API_BASE}/rest/api/3/search/jql")]
        )
        hits = search_jira_issues("q", _API_BASE, fetcher=fetcher)
        assert [(h.external_id, h.title) for h in hits] == [("ABC-9", "has key")]
