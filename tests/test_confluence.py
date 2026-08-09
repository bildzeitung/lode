"""Tests for lode.confluence — the Confluence Cloud fetch unit (lode-gpzn.4).

Covers the acceptance criteria: a fixture page maps to non-empty text (via
body.view -> trafilatura); the request URL is rebuilt from external_id +
persisted API base; auth/404 tombstone and transient codes raise via the
shared gpzn.13 classifier; everything offline (a stub Fetcher, never a real
network request).
"""

import json
from typing import Self

import httpx
import pytest

from lode.config import AtlassianCredentials, load_settings
from lode.confluence import (
    ConfluenceSearchError,
    HttpxConfluenceFetcher,
    _build_url,
    fetch_confluence_page,
    search_confluence_pages,
)
from lode.webfetch import FetchStatus, RawResponse, TransientFetchError

_API_BASE = "https://acme.atlassian.net"
_PAGE_ID = "123456"

# A real Confluence body.view payload: server-rendered HTML, long enough to
# clear the default fetch_min_extract_chars floor after extraction.
_PAGE_JSON = json.dumps(
    {
        "id": _PAGE_ID,
        "title": "Runbook: Deploying the Widget Service",
        "body": {
            "view": {
                "value": (
                    "<div>"
                    "<h1>Runbook: Deploying the Widget Service</h1>"
                    "<p>This page documents the full deployment procedure for "
                    "the widget service, including the pre-flight checklist "
                    "and the rollback steps to take if the canary stage "
                    "reports an elevated error rate.</p>"
                    "<p>Start by confirming the on-call engineer has "
                    "acknowledged the deploy window, then proceed through "
                    "each stage in order, watching the dashboards closely "
                    "at every step of the rollout.</p>"
                    "</div>"
                ),
                "representation": "view",
            }
        },
    }
)

# A page whose body.view extracts to nothing usable (an empty content div) —
# the "2xx but no real content" tombstone case.
_EMPTY_PAGE_JSON = json.dumps(
    {"id": _PAGE_ID, "body": {"view": {"value": "<div></div>"}}}
)


class _StubFetcher:
    """Deterministic stand-in for :class:`~lode.webfetch.Fetcher`.

    Mirrors tests/test_webfetch.py's ``_StubFetcher`` — either returns a
    fixed :class:`RawResponse` or raises a fixed exception, recording the
    url it was called with.
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


# ---------------------------------------------------------------------------
# URL reconstruction
# ---------------------------------------------------------------------------


def test_build_url_rebuilds_request_from_external_id_and_api_base():
    url = _build_url(_PAGE_ID, _API_BASE)

    assert url == (f"{_API_BASE}/wiki/rest/api/content/{_PAGE_ID}?expand=body.view")


def test_build_url_strips_trailing_slash_on_api_base():
    url = _build_url(_PAGE_ID, f"{_API_BASE}/")

    assert url == (f"{_API_BASE}/wiki/rest/api/content/{_PAGE_ID}?expand=body.view")


def test_fetch_confluence_page_calls_fetcher_with_rebuilt_url():
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=200,
            text=_PAGE_JSON,
        )
    )

    fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert fetcher.calls == [_build_url(_PAGE_ID, _API_BASE)]


# ---------------------------------------------------------------------------
# OK: body.view -> trafilatura -> non-empty clean_text; raw JSON preserved
# ---------------------------------------------------------------------------


def test_ok_page_returns_clean_text_and_raw_json_payload():
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=200,
            text=_PAGE_JSON,
        )
    )

    result = fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.OK
    assert result.http_status == 200
    assert result.tombstone_reason is None
    assert result.clean_text is not None
    assert "full deployment procedure for the widget service" in result.clean_text
    # The full raw JSON response is preserved verbatim as raw_html (which
    # lode.externals.ingest_fetch_result stores as raw_payload) — not just
    # the extracted body.view fragment.
    assert result.raw_html == _PAGE_JSON
    assert json.loads(result.raw_html)["id"] == _PAGE_ID


# ---------------------------------------------------------------------------
# auth/404 -> tombstone via the shared classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [401, 403, 404, 410])
def test_auth_and_404_failures_yield_tombstone(status_code):
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=status_code,
            text='{"message": "not authorized"}',
        )
    )

    result = fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.http_status == status_code
    assert result.tombstone_reason == f"http_{status_code}"
    assert result.clean_text is None


# ---------------------------------------------------------------------------
# Transient (429/5xx/network) -> raises, never tombstoned
# ---------------------------------------------------------------------------


def test_transient_error_propagates_rather_than_tombstoning():
    """The caller (queue handler), not this unit, decides what to do on retry."""
    fetcher = _StubFetcher(raises=TransientFetchError("connection reset"))

    with pytest.raises(TransientFetchError):
        fetch_confluence_page(
            _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
        )


# ---------------------------------------------------------------------------
# Malformed / empty response handling
# ---------------------------------------------------------------------------


def test_non_json_response_yields_tombstone():
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=200,
            text="<html>not json</html>",
        )
    )

    result = fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "malformed_response"
    assert result.clean_text is None


def test_json_missing_body_view_yields_tombstone():
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=200,
            text=json.dumps({"id": _PAGE_ID, "title": "No body here"}),
        )
    )

    result = fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "malformed_response"


def test_empty_extract_below_length_floor_yields_tombstone():
    fetcher = _StubFetcher(
        response=RawResponse(
            final_url=_build_url(_PAGE_ID, _API_BASE),
            status_code=200,
            text=_EMPTY_PAGE_JSON,
        )
    )

    result = fetch_confluence_page(
        _PAGE_ID, _API_BASE, fetcher=fetcher, settings=load_settings()
    )

    assert result.status is FetchStatus.TOMBSTONE
    assert result.tombstone_reason == "empty_extract"
    assert result.clean_text is None
    # raw_html (the raw JSON) is still kept for provenance.
    assert result.raw_html == _EMPTY_PAGE_JSON


# ---------------------------------------------------------------------------
# Credential resolution (no injected fetcher)
# ---------------------------------------------------------------------------


def test_missing_credentials_raises_without_a_fetcher(monkeypatch):
    monkeypatch.delenv("LODE_CONFLUENCE_TOKEN", raising=False)
    monkeypatch.delenv("LODE_CONFLUENCE_EMAIL", raising=False)
    settings = load_settings(confluence_enabled=True)

    with pytest.raises(RuntimeError, match="credentials are\\s+unresolved"):
        fetch_confluence_page(_PAGE_ID, _API_BASE, settings=settings)


# ---------------------------------------------------------------------------
# HttpxConfluenceFetcher: Basic auth, headers, status classification
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stands in for an httpx.Response without any transport."""

    def __init__(self, status_code: int, url: str = "", text: str = "") -> None:
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


class TestHttpxConfluenceFetcher:
    def test_basic_auth_and_headers_wired_into_the_client(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(200, captured))
        creds = AtlassianCredentials(email="me@example.com", token="secret-tok")
        fetcher = HttpxConfluenceFetcher(creds, load_settings())

        fetcher.fetch(_build_url(_PAGE_ID, _API_BASE))

        assert captured["auth"] == ("me@example.com", "secret-tok")
        assert captured["headers"]["Accept"] == "application/json"
        assert captured["headers"]["User-Agent"] == "lode-confluence/1"
        # No redirect-following knobs: a REST API GET follows none.
        assert (
            "follow_redirects" not in captured or captured["follow_redirects"] is False
        )

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503])
    def test_transient_status_codes_raise(self, monkeypatch, status_code):
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        creds = AtlassianCredentials(email="me@example.com", token="tok")
        fetcher = HttpxConfluenceFetcher(creds, load_settings())

        with pytest.raises(TransientFetchError, match=str(status_code)):
            fetcher.fetch(_build_url(_PAGE_ID, _API_BASE))

    @pytest.mark.parametrize("status_code", [200, 401, 403, 404, 410])
    def test_non_transient_status_codes_return_a_response(
        self, monkeypatch, status_code
    ):
        monkeypatch.setattr(httpx, "Client", _fake_client_cls(status_code, {}))
        creds = AtlassianCredentials(email="me@example.com", token="tok")
        fetcher = HttpxConfluenceFetcher(creds, load_settings())

        response = fetcher.fetch(_build_url(_PAGE_ID, _API_BASE))

        assert response.status_code == status_code

    def test_connection_error_is_transient(self):
        """A refused connection (nothing listening) is a network error -> transient."""
        creds = AtlassianCredentials(email="me@example.com", token="tok")
        fetcher = HttpxConfluenceFetcher(creds, load_settings(fetch_timeout_s=1.0))

        with pytest.raises(TransientFetchError):
            fetcher.fetch("http://127.0.0.1:1/wiki/rest/api/content/1")


# ---------------------------------------------------------------------------
# search_confluence_pages (lode-8hsk) -- ids + titles only, CQL text search
# ---------------------------------------------------------------------------


class TestSearchConfluencePages:
    def test_hits_return_id_and_title_only(self) -> None:
        payload = {
            "results": [
                {"id": "111", "title": "Runbook A"},
                {"id": "222", "title": "Runbook B"},
            ]
        }
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text=json.dumps(payload),
            )
        )

        hits = search_confluence_pages("runbook", _API_BASE, fetcher=fetcher)

        assert [(h.external_id, h.title) for h in hits] == [
            ("111", "Runbook A"),
            ("222", "Runbook B"),
        ]
        assert not hasattr(hits[0], "body")
        assert not hasattr(hits[0], "snippet")

    def test_request_targets_the_cql_search_endpoint_scoped_to_pages(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text=json.dumps({"results": []}),
            )
        )
        search_confluence_pages("widget deploy", _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert called_url.startswith(f"{_API_BASE}/wiki/rest/api/content/search?cql=")
        assert "type%3Dpage" in called_url or "type=page" in called_url

    def test_query_text_is_cql_escaped(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text=json.dumps({"results": []}),
            )
        )
        search_confluence_pages('say "hi"', _API_BASE, fetcher=fetcher)
        (called_url,) = fetcher.calls
        from urllib.parse import unquote

        assert '\\"hi\\"' in unquote(called_url)

    def test_api_base_trailing_slash_is_stripped(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text=json.dumps({"results": []}),
            )
        )
        search_confluence_pages("q", f"{_API_BASE}/", fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "//wiki" not in called_url

    def test_non_ok_response_raises_search_error(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=403,
                text="forbidden",
            )
        )
        with pytest.raises(ConfluenceSearchError):
            search_confluence_pages("q", _API_BASE, fetcher=fetcher)

    def test_malformed_response_raises_search_error(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text="not json",
            )
        )
        with pytest.raises(ConfluenceSearchError):
            search_confluence_pages("q", _API_BASE, fetcher=fetcher)

    def test_max_results_is_sent_as_limit(self) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{_API_BASE}/wiki/rest/api/content/search",
                status_code=200,
                text=json.dumps({"results": []}),
            )
        )
        search_confluence_pages("q", _API_BASE, max_results=5, fetcher=fetcher)
        (called_url,) = fetcher.calls
        assert "limit=5" in called_url

    def test_unresolved_credentials_raise_search_error(self) -> None:
        settings = load_settings(confluence_enabled=True)
        with pytest.raises(ConfluenceSearchError):
            search_confluence_pages("q", _API_BASE, settings=settings)
