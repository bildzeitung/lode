"""Snapshot-then-cite: persist a live tool-call fetch before synthesis (lode-35nu.11.1).

The load-bearing plumbing for the whole tools sub-tree
(``docs/retrieval.md`` "Tool-augmented Ask: the tool path is the draw-down
path"). A live tool result has no ``version_id``/``snapshot_id``, so the
faithfulness gate (:mod:`lode.cited_answer`) has no stored bytes to verify a
span against. Rather than weaken the gate, every tool result that will be
cited is persisted as an ordinary external snapshot **before** synthesis
sees it, reusing :mod:`lode.drawdown` / :mod:`lode.externals` unchanged for
the write — this module is the one new, thin call site, not a second
snapshot writer.

**Identity is not this module's problem** (settled ``lode-35nu.11.5``,
``docs/externals.md`` "A query result has no identity"): :func:`fetch_for_ask`
only ever fetches an *addressable* resource whose ``external_id`` already has
a valid, pre-existing shape — a web URL, or a JIRA/Confluence semantic key
with its ``api_base``. A search/query result is never routed through here;
the tool-dispatch layer (``lode-35nu.11.2``) is responsible for calling this
only with resources it names, never with a query string.

## Egress (``lode-35nu.11.5`` decided; scope refined ``lode-35nu.11.8``)

This module owns the **fetch-path** egress obligations only:

1. **Refuse a no_egress destination before ever fetching it.** Evaluated
   against both the per-row ``externals.no_egress`` flag (only meaningful
   once a row exists) and the config-declared scope rules
   (:func:`lode.no_egress_scope.is_no_egress_scoped`, which covers a
   candidate with **no** row yet — precisely the case a fetch tool exists to
   reach). Either denying is a denial. The check runs strictly before any
   network call — refusing must never mint the ``externals`` row a
   per-row check would then (wrongly) find absent.
2. **Audit every fetch actually attempted.** One ``egress_log`` row
   (``purpose='tool'``, :data:`lode.egress.TOOL_PURPOSE`) per fetch this
   module performs, carrying the destination and the call's arguments *as
   sent* — after :func:`lode.redact.redact_before_egress_counting` (the same
   redaction :func:`lode.egress.gate_qa_egress` applies to passages, reused
   here over tool arguments rather than reimplemented; ``gate_qa_egress``
   itself is not callable here — it partitions/redacts/logs *passages*, not
   a tool call's arguments). A **refused** call never reaches the network, so
   it writes no row — nothing was sent for the audit trail to cover.

   The row is written **before** the request goes out, not after — the same
   ordering :func:`lode.egress.gate_qa_egress` uses for a Q&A send, and for
   the same reason. Logging afterwards makes the audit trail conditional on
   the fetch returning control normally, so *any* unexpected failure between
   the request and the log (a fetch unit raising something outside
   :class:`~lode.webfetch.FetchError`, an unparseable server-supplied
   redirect target, the process dying) silently loses the row for bytes that
   already left the box. ``docs/storage.md`` §8's rule is one audit row per
   egress; only pre-write makes that hold unconditionally.

## Failure semantics (human decision, ``lode-35nu.11.5`` /challenge 2nd pass)

**A fetch that fails on the ask path is never persisted.** No ``snapshots``
row, no ``externals`` row, no tombstone — :func:`fetch_for_ask` raises
:class:`ToolFetchError` instead, for the caller (the tool-dispatch layer) to
turn into an error the model sees. This is a deliberate divergence from the
draw-down path (:func:`lode.drawdown.refresh_external`), which *does*
tombstone: a draw-down failure is revisiting a source already in the corpus,
whereas a source discovered mid-question that fails to fetch was never in
the corpus and must not enter it as a dead row. It also closes a real hole a
tombstone would reopen: a tombstone is a genuine snapshot with a genuine
``snapshot_id`` and an inspectable body (:func:`lode.externals.
tombstone_body`), so the model could quote it and the faithfulness gate
would pass the quote for content that was never actually fetched.

## Fetch timeout (build-time decision, this ticket)

No new timeout knob. This path reuses ``Settings.fetch_timeout_s`` (the same
per-HTTP-request timeout :mod:`lode.webfetch` / :mod:`lode.jira_fetch` /
:mod:`lode.confluence` already enforce) and makes exactly **one** attempt —
unlike the async worker, this call never retries a
:class:`~lode.webfetch.TransientFetchError` (it is inside a synchronous LLM
tool loop already spending a Q&A budget, not the retry-friendly queue). A
timeout and any other fetch failure are handed back to the model identically
— both simply mean "could not retrieve this source right now" — via the same
:class:`ToolFetchError`.
"""

from __future__ import annotations

import sqlite3

from lode.config import Settings
from lode.confluence import fetch_confluence_page
from lode.drawdown import (
    SOURCE_TYPE_CONFLUENCE,
    SOURCE_TYPE_JIRA,
    SOURCE_TYPE_WEB,
    canonicalize_url,
)
from lode.egress import TOOL_PURPOSE, log_egress
from lode.externals import IngestResult, ingest_snapshot
from lode.jira_fetch import fetch_jira_issue
from lode.no_egress_scope import is_no_egress_scoped
from lode.redact import redact_before_egress, redact_before_egress_counting
from lode.webfetch import (
    Fetcher,
    FetchError,
    FetchResult,
    FetchStatus,
    fetch_and_extract,
)

#: source_types this module knows how to fetch. Anything else is a
#: programming error in the caller (the tool-dispatch layer), never a live
#: possibility from user input -- the same closed set drawdown.refresh_external
#: dispatches over.
_FETCHABLE_SOURCE_TYPES = (SOURCE_TYPE_WEB, SOURCE_TYPE_JIRA, SOURCE_TYPE_CONFLUENCE)


class ToolFetchError(Exception):
    """An ask-time fetch was refused or failed; nothing was persisted.

    Raised instead of returning any citable result -- see the module
    docstring's "Failure semantics" section. The message is intended to be
    relayed to the model as the tool's error result, so it carries only the
    ``external_id`` plus a short diagnostic: either the machine-readable
    reason tag the fetch unit produced (e.g. ``"http_403"``, the same
    convention :mod:`lode.drawdown` uses for tombstone reasons) or the
    :class:`~lode.webfetch.FetchError`'s own message, which by construction
    is a status/timeout/network summary (``webfetch``'s raise sites) and
    never a response body.
    """


def _no_egress_denied(
    conn: sqlite3.Connection, external_id: str, source_type: str, settings: Settings
) -> bool:
    """Whether ``external_id`` must be refused -- per-row flag OR scope rule.

    Checked strictly before any fetch (see module docstring). The per-row
    flag only ever fires for an ``external_id`` that already has an
    ``externals`` row (the common case for a source drawn down before);
    :func:`~lode.no_egress_scope.is_no_egress_scoped` is what lets this
    refuse a resource with **no** row yet -- the case a fetch tool exists to
    reach (``lode-35nu.11.8``).
    """
    row = conn.execute(
        "SELECT no_egress FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    row_denied = bool(row[0]) if row is not None else False
    return row_denied or is_no_egress_scoped(
        external_id, source_type, settings.no_egress_scopes
    )


def _redact_arguments(
    arguments: dict[str, str], settings: Settings
) -> tuple[dict[str, str], int]:
    """Redact every string value in ``arguments``; return it plus the total span count.

    Reuses :func:`lode.redact.redact_before_egress_counting` -- the same
    function :func:`lode.egress.gate_qa_egress` calls internally over
    passage text -- applied here to tool-call arguments instead (module
    docstring's egress section: ``gate_qa_egress`` itself partitions/logs
    *passages* and is not callable for this).
    """
    redacted: dict[str, str] = {}
    total = 0
    for key, value in arguments.items():
        clean, count = redact_before_egress_counting(value, settings)
        redacted[key] = clean
        total += count
    return redacted, total


def _log_tool_fetch(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    destination: str,
    arguments: dict[str, str],
    settings: Settings,
) -> None:
    """Write the ``purpose='tool'`` audit row for a fetch about to be attempted.

    Called once per network attempt this module makes, **immediately before**
    the request goes out -- so the row exists regardless of whether that
    attempt then succeeds, tombstones, or raises anything at all (module
    docstring, egress section item 2). ``destination`` is the endpoint the
    request is aimed at, which is all that is knowable pre-send: the URL as
    sent for web, the ``api_base`` for JIRA/Confluence. A redirect that
    resolves elsewhere is handled by :func:`_fetch_web`'s post-fetch
    re-check, not by rewriting this row.

    ``destination`` is redacted on the same terms as the arguments (lode-l87l).
    On the web leg it is character-for-character the ``{"url": ...}`` argument,
    so redacting one copy and persisting the other raw would durably store the
    very secret the audit row reports as stripped -- and ``egress_log`` is read
    by more than one surface (``lode egress`` renders the column since
    lode-l87l; sqlite3, backups and exports see it regardless). Its span count
    is deliberately NOT added to the per-target total: on the web leg that
    would double-count the same URL's secrets, which are already counted via
    the argument.
    """
    redacted_arguments, redaction_count = _redact_arguments(arguments, settings)
    log_egress(
        conn,
        TOOL_PURPOSE,
        None,
        [external_id],
        {external_id: redaction_count} if redaction_count else None,
        destination=redact_before_egress(destination, settings),
        arguments=redacted_arguments,
    )


def fetch_for_ask(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    *,
    api_base: str | None = None,
    fetcher: Fetcher | None = None,
    settings: Settings | None = None,
) -> str:
    """Fetch ``external_id`` live and persist it as a citable snapshot.

    ``external_id`` must already be an addressable resource: a fetchable URL
    for ``source_type=SOURCE_TYPE_WEB``, or a JIRA/Confluence semantic key
    with ``api_base`` given (or already persisted on an existing
    ``externals`` row) for ``SOURCE_TYPE_JIRA`` / ``SOURCE_TYPE_CONFLUENCE``.
    Never call this with a search/query string -- see the module docstring's
    "Identity is not this module's problem".

    Returns the resulting ``snapshot_id``, a first-class citation target
    :func:`lode.cited_answer._resolve_targets` can verify a span against like
    any other external. Raises :class:`ToolFetchError` -- persisting
    **nothing** -- when the destination is ``no_egress`` (per-row flag or
    scope rule) or the fetch itself fails; see the module docstring's
    "Failure semantics".
    """
    if source_type not in _FETCHABLE_SOURCE_TYPES:
        raise ValueError(f"fetch_for_ask: unsupported source_type={source_type!r}")
    settings = settings or Settings()

    if _no_egress_denied(conn, external_id, source_type, settings):
        raise ToolFetchError(
            f"{external_id} is no_egress (marked, or under a configured scope "
            "rule) and cannot be fetched for a cloud Q&A tool call."
        )

    if source_type == SOURCE_TYPE_WEB:
        return _fetch_web(conn, external_id, fetcher=fetcher, settings=settings)
    return _fetch_atlassian(
        conn,
        external_id,
        source_type,
        api_base=api_base,
        fetcher=fetcher,
        settings=settings,
    )


def _fetch_web(
    conn: sqlite3.Connection,
    external_id: str,
    *,
    fetcher: Fetcher | None,
    settings: Settings,
) -> str:
    """The ``SOURCE_TYPE_WEB`` leg of :func:`fetch_for_ask`.

    A redirect can resolve ``external_id`` to a different canonical URL
    (:func:`lode.drawdown.canonicalize_url` of
    :attr:`~lode.webfetch.FetchResult.final_url`, same as the draw-down
    path) -- the no_egress check is re-run against that final id before
    persisting, so a redirect can never be used to fetch a scoped/flagged
    destination under cover of an unscoped starting URL. ``final_url`` is
    server-supplied, so canonicalizing it can raise
    :class:`ValueError` (an unparseable port, a malformed IPv6 host); that is
    just another fetch failure and is surfaced as :class:`ToolFetchError`
    like any other, never leaked to the caller as a raw ``ValueError``.
    """
    _log_tool_fetch(
        conn,
        external_id=external_id,
        destination=external_id,
        arguments={"url": external_id},
        settings=settings,
    )
    try:
        result = fetch_and_extract(external_id, fetcher=fetcher, settings=settings)
        final_external_id = canonicalize_url(result.final_url, settings)
    except (FetchError, ValueError) as exc:
        raise ToolFetchError(f"fetch failed for {external_id}: {exc}") from exc

    if final_external_id != external_id and _no_egress_denied(
        conn, final_external_id, SOURCE_TYPE_WEB, settings
    ):
        raise ToolFetchError(
            f"{final_external_id} (redirected from {external_id}) is no_egress "
            "and cannot be fetched for a cloud Q&A tool call."
        )

    return _ingest_or_raise(
        conn, final_external_id, SOURCE_TYPE_WEB, result, settings=settings
    )


def _fetch_atlassian(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    *,
    api_base: str | None,
    fetcher: Fetcher | None,
    settings: Settings,
) -> str:
    """The JIRA/Confluence leg of :func:`fetch_for_ask` (shared, lode-40zj shape).

    ``api_base`` is the caller-supplied rebuild base (the tool-dispatch
    layer's own lookup, if any); when omitted, the ``externals`` row's own
    ``api_base`` (persisted at link-detection time, lode-gpzn.2) is used --
    the same fallback :func:`lode.drawdown._refresh_atlassian` performs for a
    previously-drawn-down issue/page the model now asks about again.
    """
    if not api_base:
        row = conn.execute(
            "SELECT api_base FROM externals WHERE external_id = ?",
            (external_id,),
        ).fetchone()
        api_base = row[0] if row is not None else None
    if not api_base:
        raise ToolFetchError(
            f"no api_base known for {external_id}; cannot fetch a "
            f"{source_type} resource with no rebuildable API URL."
        )

    fetch_fn = (
        fetch_jira_issue if source_type == SOURCE_TYPE_JIRA else fetch_confluence_page
    )
    _log_tool_fetch(
        conn,
        external_id=external_id,
        destination=api_base,
        arguments={"external_id": external_id, "api_base": api_base},
        settings=settings,
    )
    try:
        result = fetch_fn(external_id, api_base, fetcher=fetcher, settings=settings)
    except FetchError as exc:
        raise ToolFetchError(f"fetch failed for {external_id}: {exc}") from exc

    return _ingest_or_raise(conn, external_id, source_type, result, settings=settings)


def _ingest_or_raise(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    result: FetchResult,
    *,
    settings: Settings,
) -> str:
    """Persist an ``OK`` fetch result; raise (never persist) on a tombstone outcome.

    Diverges from :func:`lode.externals.ingest_fetch_result` deliberately --
    that helper always persists, tombstoning a permanent failure. This ticket's
    decided failure semantics forbid that on the ask path (module docstring).

    Stamps ``discovered_via='ask'`` (``lode-35nu.11.7`` schema,
    ``docs/externals.md`` "Ask-time snapshots") when -- and only when -- this
    call produces the external's **first-ever** snapshot: the pre-call head
    is read before :func:`~lode.externals.ingest_snapshot` runs, so a repeat
    ask about a resource already drawn down (or already fetched once via this
    same path) never overwrites the row's true origin. Provenance only --
    nothing branches on it (module docstring; ``docs/externals.md``).
    """
    if result.status is not FetchStatus.OK:
        raise ToolFetchError(
            f"fetch failed for {external_id}"
            + (f": {result.tombstone_reason}" if result.tombstone_reason else "")
        )
    pre_head = conn.execute(
        "SELECT head_snapshot_id FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    is_first_snapshot = pre_head is None or pre_head[0] is None

    ingest: IngestResult | None = ingest_snapshot(
        conn,
        external_id,
        source_type,
        result.clean_text or "",
        raw_payload=result.raw_html,
        status="ok",
        settings=settings,
    )
    assert ingest is not None  # no skip_if_head_at_or_after passed above

    if is_first_snapshot:
        with conn:
            conn.execute(
                "UPDATE externals SET discovered_via = 'ask' "
                "WHERE external_id = ? AND discovered_via IS NULL",
                (external_id,),
            )
    return ingest.snapshot_id


__all__ = ["ToolFetchError", "fetch_for_ask"]
