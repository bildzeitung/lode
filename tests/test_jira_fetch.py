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
* :class:`JiraHttpFetcher` itself against a fake ``httpx.Client`` (mirrors
  tests/test_webfetch.py's ``TestHttpxFetcher``) — proves the real
  transport actually wires Basic auth and reaches the shared classifier,
  not just that the pipeline *would* handle a transient status if it saw
  one.
"""

import json

import httpx
import pytest

from lode.config import AtlassianCredentials, load_settings
from lode.jira_fetch import JiraHttpFetcher, fetch_jira_issue
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
# JiraHttpFetcher — real transport wiring, entirely offline (fake httpx.Client)
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

        def __enter__(self) -> "_FakeClient":
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
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch(_ISSUE_URL)

    @pytest.mark.parametrize("status_code", [200, 401, 403, 404, 410])
    def test_non_transient_status_codes_return_a_response(
        self, monkeypatch, status_code
    ):
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        response = fetcher.fetch(_ISSUE_URL)

        assert response.status_code == status_code

    def test_basic_auth_credentials_are_wired_into_the_client(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(200, captured))
        fetcher = JiraHttpFetcher(_CREDS, load_settings())

        fetcher.fetch(_ISSUE_URL)

        auth = captured["auth"]
        assert isinstance(auth, httpx.BasicAuth)

    def test_config_knobs_are_wired_into_the_client(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(200, captured))
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


class TestFetchJiraIssueDefaultFetcherWiring:
    """Proves the production path (no fetcher override) really builds a
    JiraHttpFetcher and reaches real httpx -- the same lesson
    tests/test_drawdown.py::TestRefreshExternalRealWiring encodes for the
    web connector (an injectable seam that makes tests offline can leave
    the *default* implementation's wiring unexercised)."""

    def test_default_fetcher_reaches_real_httpx_client_with_credentials(
        self, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(200, captured))

        settings = load_settings(
            jira_enabled=True, jira_token="tok", jira_email="a@example.com"
        )

        with pytest.raises(json.JSONDecodeError):
            # No fetcher= override -- the exact call drawdown._refresh_atlassian
            # makes in production. The fake client returns empty text, so JSON
            # parsing fails -- proof enough that a *real* JiraHttpFetcher
            # (not a stub) was built and actually called through to httpx,
            # with credentials, without asserting anything about a full
            # successful fetch (that's fetch_jira_issue's own suite above).
            fetch_jira_issue(_KEY, _API_BASE, settings=settings)

        assert isinstance(captured["auth"], httpx.BasicAuth)

    def test_401_tombstone_never_echoes_the_token(self, monkeypatch):
        """lode-gpzn.5 acceptance: "the token value is never printed."

        Exercises the real, credential-consuming path (the default
        JiraHttpFetcher -- no stub fetcher override) with a real token
        wired all the way into httpx.BasicAuth, forces a 401, and asserts
        the resulting FetchResult -- the object drawdown._refresh_jira folds
        into 'lode work's visible outcome line (lode-gpzn.5) -- carries no
        trace of the token anywhere: not in the classified tombstone
        reason, not in the raw payload it keeps for provenance.
        """
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(401, captured))

        fake_token = "super-secret-jira-token-xyz"
        settings = load_settings(
            jira_enabled=True, jira_token=fake_token, jira_email="a@example.com"
        )

        result = fetch_jira_issue(_KEY, _API_BASE, settings=settings)

        # Proves the real credential-consuming path was reached (same check
        # as the sibling wiring test above), token included.
        auth = captured["auth"]
        assert isinstance(auth, httpx.BasicAuth)

        assert result.status is FetchStatus.TOMBSTONE
        assert result.tombstone_reason == "http_401"
        assert fake_token not in (result.tombstone_reason or "")
        assert fake_token not in (result.raw_html or "")
        assert fake_token not in (result.clean_text or "")
