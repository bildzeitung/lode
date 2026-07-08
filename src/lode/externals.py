"""External-source ingest: the write path for mirrored content (lode-w0h.2).

The mirrored analogue of :meth:`lode.repository.Repository.save` for owned
notes, over the ``externals``/``snapshots`` tables (``docs/storage.md`` §8,
``docs/externals.md``) instead of ``notes``/``versions``. Where a note's
identity is asserted by the user, an external's identity (``external_id`` —
a normalized URL, ``JIRA-1234``, ``repo@path@commit``, ...) and content
(``body``) are asserted by whatever connector fetched it — first, the web
draw-down unit (``lode-w0h.1``, :mod:`lode.webfetch`).

**Scope, pinned by decision (bd lode-w0h.2, 2026-07-07):** this module is
**write-path only**. It writes the ``externals``/``snapshots`` rows and
enqueues the ``embed`` derive job so a later worker can index the snapshot —
it does **not** wire a :class:`~lode.repository.CacheBackend` (no synchronous
FTS write) and does **not** make a snapshot directly retrievable. Direct
retrievability (allow-list union into ``live_head_versions``, polymorphic
embed-body resolution for a ``snapshot_id`` target, and the synchronous FTS
leg) is a distinct read-enablement concern, ``lode-w0h.8``. Until it lands,
the ``embed`` job enqueued here sits ``pending`` (or fails loudly on a
``KeyError``, per that ticket's own gap analysis) rather than resolving —
enqueuing it now is still correct: the job exists the instant the snapshot
does, so nothing needs re-enqueuing once w0h.8 teaches the worker to resolve
a snapshot body.

## Dedup and head-move (docs/externals.md "Snapshot churn")

``snapshot_id = H(framed(external_id) ‖ framed(body))`` (:func:`lode.hashing.
content_snapshot_id`) makes an *identical* refetch free: recomputing the same
external's same body yields the same id, so :func:`ingest_snapshot` writes no
new row and enqueues no job — exactly the no-op-dedup shape
:func:`lode.versions._save_core` uses for an unchanged note body, but keyed
on content alone (a snapshot carries no ``parent`` dimension the way a
version does, so reverting to an exact prior body reproduces that prior
snapshot's id rather than minting a new one — content-addressing working as
designed, not a collision).

A *changed* body computes a new ``snapshot_id``, is inserted, and
``externals.head_snapshot_id`` moves to it — mirroring the note head
pointer. Unlike the note save path there is no caller-supplied ``parent`` to
compare-and-swap against: draw-down is machine-triggered (a queue job), not a
live multi-pane edit session, so there is no "changed since you opened it"
race to guard against the way :class:`~lode.versions.HeadConflictError`
does. The insert itself still uses ``ON CONFLICT ... DO NOTHING`` as a cheap
idempotency net (re-ingesting a snapshot id that already exists as a
non-head row, e.g. from a prior revert, is a safe no-op).

## Tombstones (docs/externals.md "Draw-down rules")

``snapshots.body`` is ``NOT NULL``, so a fetch failure cannot leave it
empty; :func:`tombstone_body` gives the failure a stable, inspectable body
(``"[tombstone: <reason>]"``) that is itself content-addressed the same way
real content is — a URL that fails the same way on every refetch (e.g.
still ``404``) produces the same tombstone body and therefore the same
``snapshot_id``, so a persistently-dead source does not spam new rows either.

## Embed-only enqueue (docs/externals.md "Re-embed on any change")

Every non-deduped ``ok`` ingest enqueues exactly one ``embed`` job
(:func:`lode.jobs.enqueue_derive_jobs` with an explicit ``types=("embed",)``
subset, the same targeted-enqueue shape the reconciliation scan's embed-gap
step already uses). ``enrich`` is deliberately **not** enqueued here:
re-enrichment is gated on a *material* change (embedding-similarity delta,
decided post-embed), which is ``lode-w0h.5``'s job, not this write path's.

A ``tombstone`` ingest enqueues **no** ``embed`` job (decision, bd
lode-w0h.2, 2026-07-08): a failed fetch must not produce a
retrievable/citable vector. ``vector_search`` has no score floor (top-k
always returns k), and a tombstone's body *is* its own quoted span, so an
embedded placeholder would pass the verbatim-span faithfulness check and
could surface as a citation for content that was never actually fetched —
"a hallucination wearing the uniform of a verified fact" (``docs/design.md``
§2). This mirrors the owned-note delete path, which likewise does not
enqueue ``embed`` and is filtered from retrieval by ``live_head_versions``'
``op != 'delete'`` guard. Fail closed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from lode import jobs
from lode.config import Settings
from lode.hashing import content_snapshot_id
from lode.webfetch import FetchResult, FetchStatus

#: The two snapshot outcomes the schema's CHECK constraint allows
#: (``src/lode/schema.sql``).
SnapshotStatus = Literal["ok", "tombstone"]


@dataclass(frozen=True)
class IngestResult:
    """The outcome of an :func:`ingest_snapshot` call.

    ``snapshot_id`` is the content-address id of the snapshot now current for
    ``external_id`` — the just-written one, or the unchanged existing head
    when ``deduped`` is True (mirrors :class:`lode.versions.SaveResult`).
    """

    external_id: str
    snapshot_id: str
    status: SnapshotStatus
    deduped: bool = False


def tombstone_body(reason: str) -> str:
    """The stable body text a tombstone snapshot carries for ``reason``.

    ``reason`` is a short machine-readable tag — the fetch unit's
    ``tombstone_reason`` (e.g. ``"http_403"``, ``"empty_extract"``,
    ``"too_many_redirects"``) or, for a caller reacting to a job that hit
    ``dead`` after exhausting the async queue's retry budget (docs/
    externals.md "TRANSIENT failure"), a tag like ``"dead"``. Exposed so
    other callers (e.g. the draw-down job handler, ``lode-w0h.3``) that
    write a tombstone without going through :func:`ingest_fetch_result` use
    the same convention rather than inventing their own body text.
    """
    return f"[tombstone: {reason}]"


def _external_head(
    conn: sqlite3.Connection, external_id: str
) -> tuple[bool, str | None]:
    """Return ``(exists, head_snapshot_id)`` for ``external_id``.

    ``exists`` distinguishes "no externals row yet" from "row exists with a
    NULL head" (impossible once :func:`ingest_snapshot` has run once, but
    real immediately after the row insert within the same call) — mirroring
    :func:`lode.versions._head`'s ``(head, head_body)`` shape for notes.
    """
    row = conn.execute(
        "SELECT head_snapshot_id FROM externals WHERE external_id = ?",
        (external_id,),
    ).fetchone()
    if row is None:
        return False, None
    return True, row[0]


def ingest_snapshot(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    body: str,
    *,
    raw_payload: str | None = None,
    status: SnapshotStatus = "ok",
    settings: Settings | None = None,
) -> IngestResult:
    """Create/dedup one snapshot of ``external_id`` and move its head, atomically.

    Creates the ``externals`` row on first sight of ``external_id``
    (dedup on ``external_id`` — one canonical node per source, never one row
    per citing note, ``docs/externals.md``). Computes ``snapshot_id =
    H(external_id, body)``; if it equals the current head, this is an
    identical refetch and is a no-op (no row, no enqueue, ``deduped=True``).
    Otherwise inserts the new snapshot row and moves
    ``externals.head_snapshot_id`` to it; an ``"ok"`` snapshot also enqueues
    one ``embed`` job keyed on the new ``snapshot_id`` (see the module
    docstring — ``enrich`` is not enqueued here). A ``"tombstone"`` snapshot
    enqueues no job at all — see the module docstring's "Embed-only enqueue"
    section for why a failed fetch must not become a retrievable vector.

    ``status`` records the fetch outcome (``"ok"`` for real content,
    ``"tombstone"`` for a permanent failure); callers writing a tombstone
    should pass ``body=tombstone_body(reason)`` for the stable, inspectable
    convention. The whole write — externals upsert, snapshot insert, head
    move, and the (status-gated) enqueue — runs in one ``with conn:``
    transaction, so a crash between steps never leaves an ``ok`` snapshot
    without its derive job or a head pointing at a row that was never
    committed.
    """
    settings = settings or Settings()
    with conn:
        exists, head_snapshot_id = _external_head(conn, external_id)
        if not exists:
            conn.execute(
                "INSERT INTO externals (external_id, source_type) VALUES (?, ?)",
                (external_id, source_type),
            )
        snapshot_id = content_snapshot_id(external_id, body, settings)
        if snapshot_id == head_snapshot_id:
            return IngestResult(external_id, snapshot_id, status, deduped=True)
        conn.execute(
            "INSERT INTO snapshots (snapshot_id, external_id, body, raw_payload, status) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (snapshot_id) DO NOTHING",
            (snapshot_id, external_id, body, raw_payload, status),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )
        if status != "tombstone":
            jobs.enqueue_derive_jobs(conn, snapshot_id, types=("embed",))
        return IngestResult(external_id, snapshot_id, status, deduped=False)


def ingest_fetch_result(
    conn: sqlite3.Connection,
    external_id: str,
    source_type: str,
    result: FetchResult,
    *,
    settings: Settings | None = None,
) -> IngestResult:
    """Ingest a ``lode.webfetch.fetch_and_extract`` output as a mirrored snapshot.

    The literal "ingest a fetched page" entry point: adapts a w0h.1
    :class:`~lode.webfetch.FetchResult` onto :func:`ingest_snapshot` —
    ``FetchStatus.OK`` becomes ``status="ok"`` with ``body=result.clean_text``,
    ``FetchStatus.TOMBSTONE`` becomes ``status="tombstone"`` with
    ``body=tombstone_body(result.tombstone_reason)``. ``result.raw_html``
    (populated on OK, and on a tombstone that had an HTTP response — 401/403/
    empty-extract — but not on a redirect-cap tombstone, see
    :class:`~lode.webfetch.FetchResult`) becomes ``raw_payload`` either way.
    """
    if result.status is FetchStatus.OK:
        return ingest_snapshot(
            conn,
            external_id,
            source_type,
            result.clean_text or "",
            raw_payload=result.raw_html,
            status="ok",
            settings=settings,
        )
    return ingest_snapshot(
        conn,
        external_id,
        source_type,
        tombstone_body(result.tombstone_reason or "unknown"),
        raw_payload=result.raw_html,
        status="tombstone",
        settings=settings,
    )
