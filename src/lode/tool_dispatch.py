"""Tool-augmented Ask: the read-only search/fetch tool set + dispatch (lode-8hsk).

The load-bearing plumbing this ticket adds on top of :mod:`lode.tools`
(``lode-35nu.11.1``, the fetch-and-persist primitive) and
:mod:`lode.llm_provider`'s ``run_tool_turns`` seam (``lode-35nu.11.6``, the
free-tool-turn loop). This module owns the **tool set** the Q&A synthesis
call (:mod:`lode.qa`) may offer, and the **dispatch** from a model-issued
tool call to the connector that serves it.

## Read-only by construction

Only two verbs exist: **search** (``search_jira``/``search_confluence``,
ids + titles only) and **fetch** (delegates wholly to
:func:`lode.tools.fetch_for_ask`). No write verb is defined anywhere in
:func:`build_ask_tools` -- there is nothing to disable, because there is
nothing that writes to JIRA, Confluence, or the web.

## Search returns ids + titles only

A search result is never routed through :func:`~lode.tools.fetch_for_ask`
and never persisted (``docs/externals.md`` "A query result has no
identity" -- ``lode-35nu.11.5``). :class:`~lode.jira_fetch.JiraSearchHit` and
:class:`~lode.confluence.ConfluenceSearchHit` each carry exactly
``external_id`` + ``title`` -- no body/snippet field exists on either
dataclass, so the schema makes one impossible, not merely absent (the
acceptance criterion this ticket names). To read a hit's actual content, the
model must call ``fetch`` on the id search returned, which persists a
citable snapshot via :func:`~lode.tools.fetch_for_ask`.

## Egress (search leg -- carried over from the bounced branch's structure)

Each search call writes one ``purpose='tool'`` audit row via
:func:`lode.tools.log_tool_egress` **before** the request goes out (same
ordering, same reasoning as the fetch legs -- ``lode.tools``' module
docstring), with the query text redacted through the same
:func:`~lode.redact.redact_before_egress_counting` path. ``sent_targets=()``
-- a query has no resolved target id yet, so nothing is "sent" as a
citation-eligible id (only ``fetch`` ever populates ``sent_targets``).

**Search results are then filtered through the exact same no_egress
predicate** (:func:`lode.tools.no_egress_denied` -- per-row flag OR
config-declared scope rule, ``lode-35nu.11.8``) a fetch call already
enforces pre-fetch. A denied hit is dropped **whole** -- id and title
together -- so a scoped/flagged resource's title never reaches the model
even though nothing was fetched for it. (The fetch leg for that id would
refuse it anyway; filtering it out of search results keeps the model from
wasting a budgeted call finding that out.)

## Per-ask tool-call budget (:class:`ToolBudget`)

Search and fetch share **one counter** (``settings.ask_tool_budget``,
default 6) -- consumed by :meth:`ToolBudget.consume` before any dispatch,
search or fetch alike, so a chain of searches cannot dodge the cap that
bounds fetches. A call past the budget is refused with an error string the
model sees (never dispatched), not silently dropped and not an exception
that would abort the whole ``run_tool_turns`` call. Distinct from
:data:`lode.llm_provider._DEFAULT_MAX_TOOL_TURNS` (a provider-level free-turn
cap -- one turn is not assumed to be one tool call).

## Config flag disables both tool kinds

:func:`build_ask_tools` returns ``()`` outright when
``settings.ask_tools_enabled`` is ``False`` -- the single place this is
checked. Because :func:`lode.qa.answer_question` derives its system prompt's
tool-awareness from whether the ``tools`` tuple it receives is non-empty
(never from a second flag), an empty tuple here reproduces today's
notes-only behaviour **byte-for-byte**, regardless of what a caller passes
as ``answer_question``'s own ``tools_enabled`` argument.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lode.config import Settings, confluence_active, jira_active
from lode.confluence import ConfluenceSearchError, search_confluence_pages
from lode.drawdown import SOURCE_TYPE_CONFLUENCE, SOURCE_TYPE_JIRA, SOURCE_TYPE_WEB
from lode.jira_fetch import JiraSearchError, search_jira_issues
from lode.llm_provider import ToolSpec
from lode.tools import ToolFetchError, fetch_for_ask, log_tool_egress, no_egress_denied
from lode.webfetch import Fetcher, FetchError

#: Tool names -- the wire names offered to the model and matched in dispatch.
SEARCH_JIRA = "search_jira"
SEARCH_CONFLUENCE = "search_confluence"
FETCH = "fetch"

_BUDGET_EXHAUSTED_MESSAGE = (
    "error: tool-call budget exhausted for this question -- no more search "
    "or fetch calls are available."
)

_FETCH_SOURCE_TYPES = (SOURCE_TYPE_WEB, SOURCE_TYPE_JIRA, SOURCE_TYPE_CONFLUENCE)


@dataclass
class ToolBudget:
    """Per-ask tool-call budget: search and fetch share ONE counter.

    :meth:`consume` is called once per dispatched tool call (search or
    fetch alike) before the call actually runs; a caller at or past
    ``max_calls`` gets ``False`` back and the call is refused rather than
    dispatched.
    """

    max_calls: int
    used: int = 0

    def consume(self) -> bool:
        """Try to consume one call; ``False`` (and no state change) if exhausted."""
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def build_ask_tools(settings: Settings) -> tuple[ToolSpec, ...]:
    """The tool set to offer the Q&A synthesis call, per ``settings``.

    ``()`` when ``settings.ask_tools_enabled`` is ``False`` -- see the module
    docstring's "Config flag disables both tool kinds". Otherwise:

    - ``search_jira`` -- only when JIRA is active (``jira_active``, flagged
      on AND credentials resolve) AND ``settings.jira_base_url`` is
      configured (search has no pasted link to infer an ``api_base`` from,
      unlike a fetch of an already-drawn-down issue).
    - ``search_confluence`` -- the same shape for Confluence.
    - ``fetch`` -- always offered when tools are enabled at all, covering
      web/JIRA/Confluence alike; a web fetch needs no connector flag (the
      generic web connector is always available), and an unresolvable
      JIRA/Confluence fetch fails closed inside :func:`~lode.tools.fetch_for_ask`
      itself (``ToolFetchError``, "no api_base known").
    """
    if not settings.ask_tools_enabled:
        return ()
    tools: list[ToolSpec] = []
    if jira_active(settings) and settings.jira_base_url:
        tools.append(
            ToolSpec(
                name=SEARCH_JIRA,
                description=(
                    "Search JIRA issues in the configured JIRA Cloud instance "
                    "by free text. Returns a JSON list of {external_id, title} "
                    "objects (issue keys and summaries) -- never the issue "
                    "body or comments. Call fetch afterwards with source_type="
                    '"jira" and the external_id to retrieve a specific '
                    "issue's full content as a citable snapshot."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text search query.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )
    if confluence_active(settings) and settings.confluence_base_url:
        tools.append(
            ToolSpec(
                name=SEARCH_CONFLUENCE,
                description=(
                    "Search Confluence pages in the configured Confluence "
                    "Cloud instance by free text. Returns a JSON list of "
                    "{external_id, title} objects (page ids and titles) -- "
                    "never the page body. Call fetch afterwards with "
                    'source_type="confluence" and the external_id to '
                    "retrieve a specific page's full content as a citable "
                    "snapshot."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text search query.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            )
        )
    tools.append(
        ToolSpec(
            name=FETCH,
            description=(
                "Fetch one addressable resource live and persist it as a "
                "citable snapshot. Returns a JSON object {snapshot_id: ...}; "
                "cite that snapshot_id in a claim's support.snapshot_id "
                "field -- it is a legitimate citation target, verified "
                "against the stored snapshot bytes exactly like any other "
                'source. source_type="web": external_id is a fetchable '
                'URL. source_type="jira": external_id is an issue key (e.g. '
                '"ABC-123"), typically one returned by search_jira. '
                'source_type="confluence": external_id is a page id, '
                "typically one returned by search_confluence. A fetch that "
                "fails or is refused returns an error -- nothing is "
                "persisted, and nothing is citable."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_type": {
                        "type": "string",
                        "enum": [
                            SOURCE_TYPE_WEB,
                            SOURCE_TYPE_JIRA,
                            SOURCE_TYPE_CONFLUENCE,
                        ],
                    },
                    "external_id": {
                        "type": "string",
                        "description": (
                            "A URL (web), an issue key (jira), or a page id "
                            "(confluence)."
                        ),
                    },
                },
                "required": ["source_type", "external_id"],
                "additionalProperties": False,
            },
        )
    )
    return tuple(tools)


def make_tool_result(
    conn: sqlite3.Connection,
    budget: ToolBudget,
    settings: Settings,
    *,
    jira_fetcher: Fetcher | None = None,
    confluence_fetcher: Fetcher | None = None,
    web_fetcher: Fetcher | None = None,
) -> Callable[[str, dict[str, Any]], str]:
    """Build the ``tool_result`` callback :meth:`LLMProvider.run_tool_turns` calls.

    ``jira_fetcher``/``confluence_fetcher``/``web_fetcher`` are test/offline
    seams threaded straight to :func:`~lode.jira_fetch.search_jira_issues` /
    :func:`~lode.confluence.search_confluence_pages` /
    :func:`~lode.tools.fetch_for_ask` -- production leaves them ``None`` and
    each connector builds its own default authenticated fetcher, exactly as
    those functions already do on their own.
    """

    def _tool_result(name: str, tool_input: dict[str, Any]) -> str:
        if not budget.consume():
            return _BUDGET_EXHAUSTED_MESSAGE
        try:
            if name == SEARCH_JIRA:
                return _dispatch_search(
                    conn,
                    tool_input,
                    settings,
                    jira_fetcher,
                    api_base=settings.jira_base_url,
                    source_type=SOURCE_TYPE_JIRA,
                    search_fn=search_jira_issues,
                )
            if name == SEARCH_CONFLUENCE:
                return _dispatch_search(
                    conn,
                    tool_input,
                    settings,
                    confluence_fetcher,
                    api_base=settings.confluence_base_url,
                    source_type=SOURCE_TYPE_CONFLUENCE,
                    search_fn=search_confluence_pages,
                )
            if name == FETCH:
                return _dispatch_fetch(
                    conn,
                    tool_input,
                    settings,
                    jira_fetcher=jira_fetcher,
                    confluence_fetcher=confluence_fetcher,
                    web_fetcher=web_fetcher,
                )
        except (
            JiraSearchError,
            ConfluenceSearchError,
            ToolFetchError,
            FetchError,
        ) as exc:
            # FetchError covers the SEARCH legs specifically: a 408/429/5xx,
            # network error or timeout surfaces as TransientFetchError (and a
            # redirect loop as TooManyRedirectsError) straight out of the
            # connector's fetcher, which search_jira_issues /
            # search_confluence_pages deliberately do not convert. The fetch
            # leg needs no such arm -- fetch_for_ask already folds FetchError
            # into ToolFetchError itself. Without this, a routine 429 during a
            # search would abort the entire run_tool_turns run (and so the
            # whole ask) instead of telling the model the call failed.
            return f"error: {exc}"
        raise AssertionError(
            f"unexpected tool call {name!r}({tool_input!r}) -- not one of the "
            "tools build_ask_tools offers"
        )

    return _tool_result


def _dispatch_search(
    conn: sqlite3.Connection,
    tool_input: dict[str, Any],
    settings: Settings,
    fetcher: Fetcher | None,
    *,
    api_base: str,
    source_type: str,
    search_fn: Callable[..., list[Any]],
) -> str:
    """The one search leg, shared by ``search_jira`` and ``search_confluence``.

    The two connectors differ only in which base URL, source type, and search
    function they use; everything the *ticket* cares about -- the pre-request
    ``purpose='tool'`` egress row, the ``no_egress`` drop-whole filter, and
    the ids+titles-only JSON shape -- is identical, and is written once here
    so the two can never drift apart.
    """
    query = str(tool_input.get("query") or "").strip()
    if not query:
        return "error: query must be non-empty"
    api_base = api_base.rstrip("/")
    log_tool_egress(
        conn,
        destination=api_base,
        arguments={"query": query},
        settings=settings,
    )
    hits = search_fn(query, api_base, fetcher=fetcher, settings=settings)
    return json.dumps(
        [
            {"external_id": h.external_id, "title": h.title}
            for h in hits
            if not no_egress_denied(conn, h.external_id, source_type, settings)
        ]
    )


def _dispatch_fetch(
    conn: sqlite3.Connection,
    tool_input: dict[str, Any],
    settings: Settings,
    *,
    jira_fetcher: Fetcher | None,
    confluence_fetcher: Fetcher | None,
    web_fetcher: Fetcher | None,
) -> str:
    source_type = str(tool_input.get("source_type") or "")
    external_id = str(tool_input.get("external_id") or "")
    if source_type not in _FETCH_SOURCE_TYPES:
        return f"error: unsupported source_type {source_type!r}"
    if not external_id:
        return "error: external_id must be non-empty"
    fetcher = {
        SOURCE_TYPE_WEB: web_fetcher,
        SOURCE_TYPE_JIRA: jira_fetcher,
        SOURCE_TYPE_CONFLUENCE: confluence_fetcher,
    }[source_type]
    # A JIRA/Confluence external_id the model just got from search_jira /
    # search_confluence has NO externals row yet -- fetch_for_ask's own
    # fallback (read api_base off the row) would find nothing and raise "no
    # api_base known". Pass the configured base explicitly, exactly as the
    # search call above already used to build its own request; a fetch of an
    # id already drawn down still works identically either way (fetch_for_ask
    # prefers an explicit api_base over the row's when both are available).
    # rstrip'd on the same terms as the search legs above: fetch_jira_issue
    # interpolates api_base straight into its URL without stripping (unlike
    # confluence._build_url, which strips defensively), so a jira_base_url
    # carrying a trailing slash would otherwise produce a double slash.
    api_base = {
        SOURCE_TYPE_JIRA: settings.jira_base_url.rstrip("/") or None,
        SOURCE_TYPE_CONFLUENCE: settings.confluence_base_url.rstrip("/") or None,
    }.get(source_type)
    snapshot_id = fetch_for_ask(
        conn,
        external_id,
        source_type,
        api_base=api_base,
        fetcher=fetcher,
        settings=settings,
    )
    return json.dumps({"snapshot_id": snapshot_id})


__all__ = [
    "FETCH",
    "SEARCH_CONFLUENCE",
    "SEARCH_JIRA",
    "ToolBudget",
    "build_ask_tools",
    "make_tool_result",
]
