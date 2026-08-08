"""Tests for lode.tools -- snapshot-then-cite ask-time fetch (lode-35nu.11.1).

Covers the ticket's acceptance criteria: an ask-time fetch produces a citable
snapshot reachable by _resolve_target-style lookups; repeated identical fetches
dedup on the existing external_id/snapshot_id machinery; discovered_via='ask'
is stamped on first snapshot only, with no note->external edge; a no_egress
destination (per-row flag or lode-35nu.11.8 scope rule, including a
no-row-yet candidate) is refused before any fetch; every fetch actually
attempted writes a purpose='tool' egress_log row with redacted arguments; and
a failed fetch (tombstone-worthy or a raised transient error) persists
nothing and is never citable.

All fetch-touching tests use the same stub Fetcher shape tests/test_drawdown.py
and tests/test_webfetch.py already use, so the gate never makes a real network
request.
"""

import json
from pathlib import Path

import pytest

from lode.config import Settings, load_settings
from lode.drawdown import SOURCE_TYPE_CONFLUENCE, SOURCE_TYPE_JIRA, SOURCE_TYPE_WEB
from lode.no_egress_scope import NoEgressScopeRule
from lode.storage import init_db
from lode.tools import ToolFetchError, fetch_for_ask
from lode.webfetch import RawResponse, TransientFetchError

_URL = "https://example.com/article"

_ARTICLE_HTML = (
    "<html><body><article><p>"
    + ("Real article content. " * 20)
    + "</p></article></body></html>"
)


@pytest.fixture
def conn(tmp_path: Path):
    c = init_db(tmp_path / "lode.db")
    try:
        yield c
    finally:
        c.close()


class _StubFetcher:
    def __init__(self, response=None, responses=None, raises=None) -> None:
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self._raises = raises
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        if self._raises is not None:
            raise self._raises
        if self._responses is not None:
            return self._responses.pop(0)
        return self._response


def _snapshot_row(conn, external_id: str):
    return conn.execute(
        "SELECT status, body FROM snapshots WHERE external_id = ? "
        "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (external_id,),
    ).fetchone()


def _externals_row(conn, external_id: str):
    return conn.execute(
        "SELECT source_type, discovered_via, no_egress FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()


def _tool_egress_rows(conn):
    return conn.execute(
        "SELECT purpose, model, destination, arguments, sent_targets "
        "FROM egress_log WHERE purpose = 'tool' ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# Web fetch success -> citable snapshot, first-snapshot provenance, no edge
# ---------------------------------------------------------------------------


class TestFetchForAskWeb:
    def test_ok_ingests_a_citable_snapshot(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )

        snapshot_id = fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        assert snapshot_id
        status, body = _snapshot_row(conn, _URL)
        assert status == "ok"
        assert "Real article content" in body
        # Reachable the same way cited_answer._resolve_target reaches it.
        (found_body,) = conn.execute(
            "SELECT body FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        assert found_body == body

    def test_first_snapshot_stamps_discovered_via_ask(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )
        fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        source_type, discovered_via, _ = _externals_row(conn, _URL)
        assert source_type == SOURCE_TYPE_WEB
        assert discovered_via == "ask"

    def test_no_note_external_edge_is_created(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )
        fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        (count,) = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE to_id = ?", (_URL,)
        ).fetchone()
        assert count == 0

    def test_repeated_identical_fetch_dedups_no_new_snapshot(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )
        settings = load_settings()
        first = fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=settings
        )
        second = fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=settings
        )

        assert first == second
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE external_id = ?", (_URL,)
        ).fetchone()
        assert n == 1
        # discovered_via is stamped once, from the first fetch, and is not
        # disturbed by the dedup refetch.
        _, discovered_via, _ = _externals_row(conn, _URL)
        assert discovered_via == "ask"

    def test_second_ask_of_already_drawn_down_resource_keeps_origin(self, conn) -> None:
        """A resource already drawn down (discovered_via NULL, the existing
        draw-down convention) that Ask later fetches keeps its true origin --
        discovered_via is never overwritten once a snapshot already exists."""
        conn.execute(
            "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
            (_URL, SOURCE_TYPE_WEB),
        )
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, status) "
            "VALUES ('sid-preexisting', ?, 'old body', 'ok')",
            (_URL,),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = 'sid-preexisting' WHERE external_id = ?",
            (_URL,),
        )
        conn.commit()

        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )
        fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        _, discovered_via, _ = _externals_row(conn, _URL)
        assert discovered_via is None


# ---------------------------------------------------------------------------
# Failure semantics: never persisted, never citable
# ---------------------------------------------------------------------------


class TestFetchForAskFailureSemantics:
    def test_tombstone_worthy_response_raises_and_persists_nothing(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=403, text="forbidden")
        )

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        assert (
            conn.execute(
                "SELECT 1 FROM snapshots WHERE external_id = ?", (_URL,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", (_URL,)
            ).fetchone()
            is None
        )

    def test_transient_failure_raises_toolfetcherror_not_transientfetcherror(
        self, conn
    ) -> None:
        """Diverges from refresh_external: this path makes one attempt and
        never lets a TransientFetchError (timeout/network/5xx) propagate to
        the caller for retry -- there is no queue on the ask path."""
        fetcher = _StubFetcher(raises=TransientFetchError("timeout"))

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", (_URL,)
            ).fetchone()
            is None
        )

    def test_failed_fetch_is_never_citable(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=404, text="not found")
        )
        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        # Nothing exists for a caller to cite -- no snapshot_id was ever
        # returned, and the table has no row a model could have quoted.
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# no_egress refusal -- per-row flag AND scope rule, including no-row-yet
# ---------------------------------------------------------------------------


class TestFetchForAskNoEgress:
    def test_per_row_no_egress_refuses_before_any_fetch(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) VALUES (?, ?, 1)",
            (_URL, SOURCE_TYPE_WEB),
        )
        conn.commit()
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        assert fetcher.calls == []
        assert _tool_egress_rows(conn) == []

    def test_redirect_to_a_scoped_host_persists_nothing(self, conn) -> None:
        """Redirect laundering: an unscoped start URL hopping to a scoped host.

        The request to the *starting* host has already gone out by the time
        the final URL is known, so this cannot be refused pre-fetch -- but
        nothing may be persisted under the scoped final id, and the audit row
        for the call that was made must still exist.
        """
        final_url = "https://secret.example.org/leaked"
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=final_url, status_code=200, text=_ARTICLE_HTML
            )
        )
        settings = Settings(
            no_egress_scopes=[
                NoEgressScopeRule(source_type="web", match="secret.example.org")
            ]
        )

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=settings
            )

        assert _snapshot_row(conn, final_url) is None
        assert _externals_row(conn, final_url) is None
        assert _externals_row(conn, _URL) is None
        assert len(_tool_egress_rows(conn)) == 1

    def test_scope_rule_refuses_a_never_before_seen_jira_issue(self, conn) -> None:
        """The case the per-row flag structurally cannot cover: no externals
        row exists at all yet."""
        settings = Settings(
            no_egress_scopes=[NoEgressScopeRule(source_type="jira", match="SEC")]
        )
        fetcher = _StubFetcher(
            response=RawResponse(final_url="unused", status_code=200, text="{}")
        )
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", ("SEC-1",)
            ).fetchone()
            is None
        )

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn,
                "SEC-1",
                SOURCE_TYPE_JIRA,
                api_base="https://acme.atlassian.net",
                fetcher=fetcher,
                settings=settings,
            )

        assert fetcher.calls == []
        assert (
            conn.execute(
                "SELECT 1 FROM externals WHERE external_id = ?", ("SEC-1",)
            ).fetchone()
            is None
        )

    def test_scope_rule_refuses_a_never_before_seen_web_host(self, conn) -> None:
        settings = Settings(
            no_egress_scopes=[
                NoEgressScopeRule(source_type="web", match="internal.example.com")
            ]
        )
        url = "https://internal.example.com/secret-doc"
        fetcher = _StubFetcher(
            response=RawResponse(final_url=url, status_code=200, text=_ARTICLE_HTML)
        )

        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, url, SOURCE_TYPE_WEB, fetcher=fetcher, settings=settings
            )

        assert fetcher.calls == []


# ---------------------------------------------------------------------------
# Egress audit: purpose='tool' row per attempted fetch, arguments redacted
# ---------------------------------------------------------------------------


class TestFetchForAskEgressAudit:
    def test_successful_fetch_writes_one_tool_egress_row(self, conn) -> None:
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=200, text=_ARTICLE_HTML)
        )
        fetch_for_ask(
            conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        rows = _tool_egress_rows(conn)
        assert len(rows) == 1
        purpose, model, destination, arguments, sent_targets = rows[0]
        assert purpose == "tool"
        assert model is None
        assert destination == _URL
        assert json.loads(arguments) == {"url": _URL}
        assert json.loads(sent_targets) == [_URL]

    def test_attempted_fetch_that_fails_still_writes_egress_row(self, conn) -> None:
        """The arguments left the box the moment the request was sent, whether
        or not the response was usable -- audited either way."""
        fetcher = _StubFetcher(
            response=RawResponse(final_url=_URL, status_code=403, text="forbidden")
        )
        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        rows = _tool_egress_rows(conn)
        assert len(rows) == 1
        assert rows[0][0] == "tool"

    def test_arguments_are_redacted_before_storage(self, conn) -> None:
        """The stored argument must not contain the secret the URL carried.

        Uses an AWS access-key id, one of the default
        ``redact_before_egress_patterns`` seed patterns, so this asserts the
        redaction actually fired rather than merely that a value was stored.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        secret_url = f"https://example.com/x?k={secret}"
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=secret_url, status_code=200, text=_ARTICLE_HTML
            )
        )
        fetch_for_ask(
            conn, secret_url, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
        )

        rows = _tool_egress_rows(conn)
        assert len(rows) == 1
        arguments = json.loads(rows[0][3])
        assert secret not in arguments["url"]
        assert arguments["url"].startswith("https://example.com/x?k=")

    def test_unparseable_redirect_target_is_a_tool_fetch_error_and_still_audited(
        self, conn
    ) -> None:
        """A server-supplied final_url that canonicalize_url cannot parse.

        The request already went out, so the audit row must exist; and the
        ValueError canonicalize_url raises must surface as ToolFetchError,
        not leak out of fetch_for_ask raw.
        """
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url="http://example.com:notaport/x",
                status_code=200,
                text=_ARTICLE_HTML,
            )
        )
        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn, _URL, SOURCE_TYPE_WEB, fetcher=fetcher, settings=load_settings()
            )

        assert len(_tool_egress_rows(conn)) == 1
        assert _snapshot_row(conn, _URL) is None
        assert _externals_row(conn, _URL) is None


# ---------------------------------------------------------------------------
# JIRA / Confluence legs
# ---------------------------------------------------------------------------


class TestFetchForAskAtlassian:
    def test_jira_ok_uses_api_base_and_ingests(self, conn) -> None:
        api_base = "https://acme.atlassian.net"
        issue_json = {
            "fields": {"summary": "Ask-fetched issue"},
            "renderedFields": {
                "description": "<p>" + ("Ask-time JIRA content. " * 20) + "</p>"
            },
        }
        empty_comments = {"startAt": 0, "maxResults": 0, "total": 0, "comments": []}
        fetcher = _StubFetcher(
            responses=[
                RawResponse(
                    final_url=f"{api_base}/rest/api/3/issue/ABC-1?expand=renderedFields",
                    status_code=200,
                    text=json.dumps(issue_json),
                ),
                RawResponse(
                    final_url=(
                        f"{api_base}/rest/api/3/issue/ABC-1/comment"
                        "?startAt=0&expand=renderedBody"
                    ),
                    status_code=200,
                    text=json.dumps(empty_comments),
                ),
            ]
        )

        snapshot_id = fetch_for_ask(
            conn,
            "ABC-1",
            SOURCE_TYPE_JIRA,
            api_base=api_base,
            fetcher=fetcher,
            settings=load_settings(),
        )

        assert snapshot_id
        status, _ = _snapshot_row(conn, "ABC-1")
        assert status == "ok"
        source_type, discovered_via, _ = _externals_row(conn, "ABC-1")
        assert source_type == SOURCE_TYPE_JIRA
        assert discovered_via == "ask"
        rows = _tool_egress_rows(conn)
        assert len(rows) == 1
        assert rows[0][2] == api_base  # destination

    def test_jira_falls_back_to_existing_row_api_base(self, conn) -> None:
        api_base = "https://acme.atlassian.net"
        conn.execute(
            "INSERT INTO externals (external_id, source_type, api_base) VALUES (?, ?, ?)",
            ("ABC-2", SOURCE_TYPE_JIRA, api_base),
        )
        conn.commit()
        issue_json = {
            "fields": {"summary": "Fallback api_base"},
            "renderedFields": {
                "description": "<p>" + ("Fallback content. " * 20) + "</p>"
            },
        }
        empty_comments = {"startAt": 0, "maxResults": 0, "total": 0, "comments": []}
        fetcher = _StubFetcher(
            responses=[
                RawResponse(
                    final_url=f"{api_base}/rest/api/3/issue/ABC-2?expand=renderedFields",
                    status_code=200,
                    text=json.dumps(issue_json),
                ),
                RawResponse(
                    final_url=(
                        f"{api_base}/rest/api/3/issue/ABC-2/comment"
                        "?startAt=0&expand=renderedBody"
                    ),
                    status_code=200,
                    text=json.dumps(empty_comments),
                ),
            ]
        )

        snapshot_id = fetch_for_ask(
            conn, "ABC-2", SOURCE_TYPE_JIRA, fetcher=fetcher, settings=load_settings()
        )
        assert snapshot_id

    def test_jira_no_api_base_anywhere_raises(self, conn) -> None:
        with pytest.raises(ToolFetchError):
            fetch_for_ask(
                conn,
                "ABC-3",
                SOURCE_TYPE_JIRA,
                fetcher=_StubFetcher(),
                settings=load_settings(),
            )

    def test_confluence_ok_ingests(self, conn) -> None:
        api_base = "https://acme.atlassian.net"
        page_html = (
            "<html><body><article><p>"
            + ("Ask-time page content. " * 20)
            + "</p></article></body></html>"
        )
        page_json = {"body": {"view": {"value": page_html}}}
        fetcher = _StubFetcher(
            response=RawResponse(
                final_url=f"{api_base}/wiki/rest/api/content/555?expand=body.view",
                status_code=200,
                text=json.dumps(page_json),
            )
        )

        snapshot_id = fetch_for_ask(
            conn,
            "555",
            SOURCE_TYPE_CONFLUENCE,
            api_base=api_base,
            fetcher=fetcher,
            settings=load_settings(),
        )

        assert snapshot_id
        status, _ = _snapshot_row(conn, "555")
        assert status == "ok"


def test_unsupported_source_type_raises_value_error(conn) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        fetch_for_ask(conn, "x", "search-result", settings=load_settings())
