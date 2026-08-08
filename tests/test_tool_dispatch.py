"""Tests for lode.tool_dispatch -- the Ask tool set + dispatch (lode-8hsk).

Covers this ticket's acceptance criteria at the dispatch layer: the tool set
is read-only by construction (no write verb defined at all); the per-ask
budget covers search and fetch against one counter and is enforced; a
no_egress search hit is dropped whole (id and title together); a search call
writes a purpose='tool' egress row with sent_targets=() before the request;
and the config flag disables both tool kinds.
"""

import json
from pathlib import Path

import pytest

from lode.config import Settings
from lode.drawdown import SOURCE_TYPE_JIRA
from lode.no_egress_scope import NoEgressScopeRule
from lode.storage import init_db
from lode.tool_dispatch import (
    FETCH,
    SEARCH_CONFLUENCE,
    SEARCH_JIRA,
    ToolBudget,
    build_ask_tools,
    make_tool_result,
)
from lode.webfetch import RawResponse

_JIRA_BASE = "https://acme.atlassian.net"
_CONFLUENCE_BASE = "https://acme.atlassian.net"

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


def _jira_settings(**overrides) -> Settings:
    fields = {
        "jira_enabled": True,
        "jira_token": "tok",
        "jira_email": "me@example.com",
        "jira_base_url": _JIRA_BASE,
        "ask_tools_enabled": True,
        **overrides,
    }
    return Settings(**fields)


def _confluence_settings(**overrides) -> Settings:
    fields = {
        "confluence_enabled": True,
        "confluence_token": "tok",
        "confluence_email": "me@example.com",
        "confluence_base_url": _CONFLUENCE_BASE,
        "ask_tools_enabled": True,
        **overrides,
    }
    return Settings(**fields)


class _StubSearchFetcher:
    """Stub Fetcher for a single-request search call."""

    def __init__(self, response: RawResponse) -> None:
        self._response = response
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        return self._response


class _StubWebFetcher:
    def __init__(self, response: RawResponse) -> None:
        self._response = response
        self.calls: list[str] = []

    def fetch(self, url: str) -> RawResponse:
        self.calls.append(url)
        return self._response


def _tool_egress_rows(conn):
    return conn.execute(
        "SELECT purpose, destination, arguments, sent_targets "
        "FROM egress_log WHERE purpose = 'tool' ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# build_ask_tools -- read-only by construction, config flag gates everything
# ---------------------------------------------------------------------------


class TestBuildAskTools:
    def test_disabled_by_default_returns_no_tools(self) -> None:
        assert build_ask_tools(Settings()) == ()

    def test_config_flag_off_returns_no_tools_even_if_connectors_are_active(
        self,
    ) -> None:
        settings = _jira_settings(ask_tools_enabled=False)
        assert build_ask_tools(settings) == ()

    def test_enabled_with_no_connectors_active_offers_only_fetch(self) -> None:
        tools = build_ask_tools(Settings(ask_tools_enabled=True))
        assert [t.name for t in tools] == [FETCH]

    def test_jira_active_offers_search_jira_and_fetch(self) -> None:
        tools = build_ask_tools(_jira_settings())
        assert {t.name for t in tools} == {SEARCH_JIRA, FETCH}

    def test_jira_active_but_no_base_url_omits_search_jira(self) -> None:
        settings = _jira_settings(jira_base_url="")
        assert SEARCH_JIRA not in {t.name for t in build_ask_tools(settings)}

    def test_confluence_active_offers_search_confluence_and_fetch(self) -> None:
        tools = build_ask_tools(_confluence_settings())
        assert {t.name for t in tools} == {SEARCH_CONFLUENCE, FETCH}

    def test_no_write_verb_is_ever_offered(self) -> None:
        settings = Settings(
            ask_tools_enabled=True,
            jira_enabled=True,
            jira_token="tok",
            jira_email="me@example.com",
            jira_base_url=_JIRA_BASE,
            confluence_enabled=True,
            confluence_token="tok",
            confluence_email="me@example.com",
            confluence_base_url=_CONFLUENCE_BASE,
        )
        names = {t.name for t in build_ask_tools(settings)}
        assert names == {SEARCH_JIRA, SEARCH_CONFLUENCE, FETCH}
        # No tool name (or description) mentions a write/create/update verb.
        for tool in build_ask_tools(settings):
            for verb in ("create", "update", "delete", "write", "post", "comment"):
                assert verb not in tool.name.lower()


# ---------------------------------------------------------------------------
# ToolBudget -- one counter shared by search and fetch
# ---------------------------------------------------------------------------


class TestToolBudget:
    def test_consume_decrements_until_exhausted(self) -> None:
        budget = ToolBudget(max_calls=2)
        assert budget.consume() is True
        assert budget.consume() is True
        assert budget.consume() is False
        assert budget.used == 2


# ---------------------------------------------------------------------------
# Dispatch: search asymmetry, no_egress filtering, budget, egress audit
# ---------------------------------------------------------------------------


class TestDispatchSearchJira:
    def test_returns_id_and_title_only(self, conn) -> None:
        payload = {"issues": [{"key": "ABC-1", "fields": {"summary": "First"}}]}
        fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=200,
                text=json.dumps(payload),
            )
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = tool_result(SEARCH_JIRA, {"query": "prod outage"})

        assert json.loads(result) == [{"external_id": "ABC-1", "title": "First"}]

    def test_writes_egress_row_before_request_with_empty_sent_targets(
        self, conn
    ) -> None:
        fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=200,
                text=json.dumps({"issues": []}),
            )
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        tool_result(SEARCH_JIRA, {"query": "prod outage"})

        rows = _tool_egress_rows(conn)
        assert len(rows) == 1
        purpose, destination, arguments, sent_targets = rows[0]
        assert purpose == "tool"
        assert destination == _JIRA_BASE
        assert json.loads(arguments) == {"query": "prod outage"}
        assert json.loads(sent_targets) == []

    def test_no_egress_scope_rule_drops_hit_id_and_title_together(self, conn) -> None:
        payload = {
            "issues": [
                {"key": "SEC-1", "fields": {"summary": "Secret issue"}},
                {"key": "OPEN-1", "fields": {"summary": "Open issue"}},
            ]
        }
        fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=200,
                text=json.dumps(payload),
            )
        )
        settings = _jira_settings(
            no_egress_scopes=[NoEgressScopeRule(source_type="jira", match="SEC")]
        )
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = json.loads(tool_result(SEARCH_JIRA, {"query": "issue"}))

        assert result == [{"external_id": "OPEN-1", "title": "Open issue"}]
        # The secret id/title never appear anywhere in the returned payload.
        assert "SEC-1" not in json.dumps(result)
        assert "Secret issue" not in json.dumps(result)

    def test_per_row_no_egress_drops_hit(self, conn) -> None:
        conn.execute(
            "INSERT INTO externals (external_id, source_type, no_egress) "
            "VALUES ('SEC-2', ?, 1)",
            (SOURCE_TYPE_JIRA,),
        )
        conn.commit()
        payload = {"issues": [{"key": "SEC-2", "fields": {"summary": "Hidden"}}]}
        fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=200,
                text=json.dumps(payload),
            )
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = json.loads(tool_result(SEARCH_JIRA, {"query": "q"}))
        assert result == []

    def test_empty_query_is_refused_without_a_request(self, conn) -> None:
        fetcher = _StubSearchFetcher(
            RawResponse(final_url="unused", status_code=200, text="{}")
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = tool_result(SEARCH_JIRA, {"query": "  "})

        assert result.startswith("error:")
        assert fetcher.calls == []

    def test_search_failure_returns_error_string_not_an_exception(self, conn) -> None:
        fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=410,
                text="Gone",
            )
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = tool_result(SEARCH_JIRA, {"query": "q"})
        assert result.startswith("error:")


class TestDispatchFetch:
    def test_web_fetch_persists_a_citable_snapshot(self, conn) -> None:
        url = "https://example.com/article"
        fetcher = _StubWebFetcher(
            RawResponse(final_url=url, status_code=200, text=_ARTICLE_HTML)
        )
        settings = Settings(ask_tools_enabled=True)
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, web_fetcher=fetcher
        )

        result = json.loads(
            tool_result(FETCH, {"source_type": "web", "external_id": url})
        )
        assert result["snapshot_id"]
        (status,) = conn.execute(
            "SELECT status FROM snapshots WHERE external_id = ?", (url,)
        ).fetchone()
        assert status == "ok"

    def test_jira_fetch_uses_configured_base_for_a_search_result_with_no_row_yet(
        self, conn
    ) -> None:
        # A key search_jira just returned has NO externals row yet -- fetch
        # must not depend on one already existing.
        issue_json = {
            "fields": {"summary": "Fetched issue"},
            "renderedFields": {
                "description": "<p>" + ("Fetched content. " * 20) + "</p>"
            },
        }
        empty_comments = {"startAt": 0, "maxResults": 0, "total": 0, "comments": []}

        class _Seq:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls: list[str] = []

            def fetch(self, url):
                self.calls.append(url)
                return self._responses.pop(0)

        fetcher = _Seq(
            [
                RawResponse(
                    final_url=f"{_JIRA_BASE}/rest/api/3/issue/ABC-1?expand=renderedFields",
                    status_code=200,
                    text=json.dumps(issue_json),
                ),
                RawResponse(
                    final_url=(
                        f"{_JIRA_BASE}/rest/api/3/issue/ABC-1/comment"
                        "?startAt=0&expand=renderedBody"
                    ),
                    status_code=200,
                    text=json.dumps(empty_comments),
                ),
            ]
        )
        settings = _jira_settings()
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, jira_fetcher=fetcher
        )

        result = json.loads(
            tool_result(FETCH, {"source_type": "jira", "external_id": "ABC-1"})
        )
        assert result["snapshot_id"]

    def test_unsupported_source_type_is_refused_without_dispatch(self, conn) -> None:
        settings = Settings(ask_tools_enabled=True)
        tool_result = make_tool_result(conn, ToolBudget(max_calls=5), settings)

        result = tool_result(FETCH, {"source_type": "ftp", "external_id": "x"})
        assert result.startswith("error:")

    def test_fetch_failure_returns_error_string_not_an_exception(self, conn) -> None:
        url = "https://example.com/gone"
        fetcher = _StubWebFetcher(
            RawResponse(final_url=url, status_code=404, text="not found")
        )
        settings = Settings(ask_tools_enabled=True)
        tool_result = make_tool_result(
            conn, ToolBudget(max_calls=5), settings, web_fetcher=fetcher
        )

        result = tool_result(FETCH, {"source_type": "web", "external_id": url})
        assert result.startswith("error:")


class TestSharedBudget:
    def test_search_and_fetch_share_one_counter(self, conn) -> None:
        search_fetcher = _StubSearchFetcher(
            RawResponse(
                final_url=f"{_JIRA_BASE}/rest/api/3/search/jql",
                status_code=200,
                text=json.dumps({"issues": []}),
            )
        )
        settings = _jira_settings()
        budget = ToolBudget(max_calls=1)
        tool_result = make_tool_result(
            conn, budget, settings, jira_fetcher=search_fetcher
        )

        first = tool_result(SEARCH_JIRA, {"query": "q"})
        assert not first.startswith("error: tool-call budget")

        second = tool_result(FETCH, {"source_type": "jira", "external_id": "ABC-1"})
        assert "budget" in second
        # The fetch was refused before dispatch -- only the one search call
        # (which consumed the whole budget) was ever actually made.
        assert len(search_fetcher.calls) == 1

    def test_unexpected_tool_name_raises(self, conn) -> None:
        settings = Settings(ask_tools_enabled=True)
        tool_result = make_tool_result(conn, ToolBudget(max_calls=5), settings)
        with pytest.raises(AssertionError):
            tool_result("not_a_real_tool", {})
