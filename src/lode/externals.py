"""External-source ingest: the write path for mirrored content (lode-w0h.2).

The mirrored analogue of :meth:`lode.repository.Repository.save` for owned
notes, over the ``externals``/``snapshots`` tables (``docs/storage.md`` §8,
``docs/externals.md``) instead of ``notes``/``versions``. Where a note's
identity is asserted by the user, an external's identity (``external_id`` —
a normalized URL, ``JIRA-1234``, ``repo@path@commit``, ...) and content
(``body``) are asserted by whatever connector fetched it — first, the web
draw-down unit (``lode-w0h.1``, :mod:`lode.webfetch`).

**Scope, pinned by decision (bd lode-w0h.2, 2026-07-07):** this module is the
write path. It writes the ``externals``/``snapshots`` rows, enqueues the
``embed`` derive job so the async worker can index the snapshot's vector leg
(:func:`lode.embedding.embed`, which resolves a ``snapshot_id`` target
polymorphically — ``lode-c5l``), and drives the **synchronous** FTS leg
itself (:func:`_index_snapshot_fts`) the same way :meth:`lode.repository.
Repository.save` drives :class:`~lode.repository.CacheBackend` for owned
notes — so a freshly ingested ``ok`` snapshot is a direct keyword hit the
instant :func:`ingest_snapshot` returns, and a direct vector hit once the
embed worker drains. The allow-list union that admits a snapshot's current
head into ``live_head_versions`` lives in :mod:`lode.retrieval` (``lode-c5l``,
rebuild of the bounced ``lode-w0h.8``).

**Redact-before-index on the FTS leg (lode-n60, the lode-c5l bounce fix):**
:func:`_index_snapshot_fts` chunks ``redact_before_index(body, settings)``,
never the raw ``body`` — mirroring exactly what :meth:`Repository.save` does
for the lexical leg of an owned note, and what :func:`lode.embedding.embed`
independently does for the vector leg. The bounced predecessor of this
ticket chunked the raw body on the FTS leg only, so a secret in a fetched
page landed in ``passages_fts``/``passages`` verbatim while the vector leg
redacted it — a split-brain that also broke the deterministic-``passage_id``
assumption :class:`~lode.lexical.LexicalCacheBackend` documents (both legs
must chunk *identical* text for the embed worker's later ``INSERT OR
REPLACE`` to land on the same rows rather than orphan a trailing one).
``snapshots.body`` itself stays untouched — only the text handed to
:func:`lode.chunking.chunk` is redacted, exactly as ``versions.body`` is
never touched by the note-side redaction either.

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
decided post-embed), which is :func:`gate_reenrich`'s job, not this write
path's — it runs from :func:`lode.worker._embed_handler`, after the new
snapshot's own vectors exist (bd lode-w0h.5 decision C: materiality can only
be judged once there is something to compare).

A ``tombstone`` ingest enqueues **no** ``embed`` job (decision, bd
lode-w0h.2, 2026-07-08): a failed fetch must not produce a
retrievable/citable vector. ``vector_search`` has no score floor (top-k
always returns k), and a tombstone's body *is* its own quoted span, so an
embedded placeholder would pass the verbatim-span faithfulness check and
could surface as a citation for content that was never actually fetched —
"a hallucination wearing the uniform of a verified fact" (``docs/design.md``
§2). This mirrors the owned-note delete path, which likewise does not
enqueue ``embed`` and is filtered from retrieval by ``live_head_versions``'
``op != 'delete'`` guard. Fail closed. A tombstone never reaches
:func:`gate_reenrich` either, for the same reason: no embed job is ever
enqueued for one, so the post-embed hook that calls it never fires.

## Material-change re-enrich gating (docs/externals.md "Snapshot churn",
lode-w0h.5)

:func:`gate_reenrich` is the cost-control gate a *chatty* source needs: a
one-comment PR update re-embeds (above, always) but should not always pay for
a fresh Haiku extraction too. Materiality signal (bd lode-w0h.5 decision C,
pinned after debate): **embedding-similarity delta** between the new
snapshot's and its immediate predecessor's mean-pooled passage vectors — not
size, despite ``docs/externals.md``'s looser "size / similarity" phrasing;
size was dropped because it has no defined ordering relative to the async
embed job the similarity signal already depends on. Below
``settings.reenrichment_materiality_threshold`` the change is immaterial: no
``enrich`` job is enqueued, and the predecessor's AI-derived annotations/edges
are carried forward by *re-anchoring* them (:func:`lode.staleness.
reanchor_annotations` / ``reanchor_edges``) to the new snapshot — the same
quoted-text mechanism :meth:`lode.repository.Repository.save` already uses
for a note update, reused here rather than duplicated. At/above the
threshold — or when there is no predecessor vector to compare against at all
(the external's first-ever snapshot, or a predecessor that was never
embedded, e.g. a tombstone) — the change is material and an ``enrich`` job is
enqueued for the new ``snapshot_id``.

The ``enrich`` job this enqueues resolves polymorphically (lode-7qi):
:func:`lode.enrich.enrich_version` (and the Batches API route,
:func:`~lode.enrich.submit_enrich_batch` / :func:`~lode.enrich.
collect_enrich_batch`, which actually claims a pending ``enrich`` job first
in production) resolve ``target_version`` against ``versions``/``notes``
first, falling back to ``snapshots``/``externals`` — the same blind
resolution :func:`lode.embedding._version_body` already uses for the
``embed`` leg. A material change therefore runs real Haiku extraction over
the snapshot body and writes annotations/edges against ``external_id``.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lode import jobs, staleness
from lode.config import Settings
from lode.hashing import content_snapshot_id
from lode.ids import short_version_id
from lode.lexical import LexicalCacheBackend
from lode.redact import redact_before_index
from lode.vectorstore import VectorStore
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


def head_snapshot_info(
    conn: sqlite3.Connection, external_id: str
) -> tuple[SnapshotStatus, str] | None:
    """Return ``(status, fetched_at)`` for ``external_id``'s current head snapshot.

    ``None`` if ``external_id`` has no ``externals`` row yet, or (the narrow,
    transient window inside :func:`ingest_snapshot`'s own transaction) its
    ``head_snapshot_id`` is still NULL — never observable to another caller
    outside that transaction.

    Exposed (unlike the private, id-only :func:`_external_head`) for
    :func:`lode.worker._refresh_dead_letter_hook` (lode-uda1), which needs to
    know not just *that* a head exists but *when* it was fetched and whether
    it is real content — to tell a dead-letter verdict apart from a fact that
    already supersedes it.
    """
    row = conn.execute(
        "SELECT s.status, s.fetched_at FROM externals e "
        "JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id "
        "WHERE e.external_id = ?",
        (external_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _index_snapshot_fts(
    conn: sqlite3.Connection,
    external_id: str,
    snapshot_id: str,
    body: str,
    *,
    settings: Settings,
) -> None:
    """Drive the synchronous FTS leg for a just-committed ``ok`` snapshot.

    Called by :func:`ingest_snapshot` **after** its write transaction has
    committed (mirroring :meth:`lode.repository.Repository.save`'s
    cache-after-commit ordering) — never nested inside that transaction,
    since a nested ``with conn:`` would COMMIT early and could leave a
    half-done irreplaceable write flushed before it was actually complete.

    ``body`` is passed through :func:`lode.redact.redact_before_index` before
    it is chunked — see the module docstring's "Redact-before-index on the
    FTS leg" section for why this must never chunk the raw body. Reuses
    :class:`~lode.lexical.LexicalCacheBackend`, which is ``target_version``-
    generic (it chunks + persists ``passages`` + replaces the
    ``passages_fts`` rows for whatever id it's given); ``external_id`` rides
    along positionally as the backend's ``note_id`` parameter, which the
    lexical backend never reads (the seam is note-shaped but content-id
    agnostic).
    """
    redacted = redact_before_index(body, settings)
    LexicalCacheBackend(conn, settings=settings).index(
        external_id, snapshot_id, redacted
    )


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
    identical refetch and is a no-op (no row, no enqueue, no FTS write,
    ``deduped=True``). Otherwise inserts the new snapshot row and moves
    ``externals.head_snapshot_id`` to it; an ``"ok"`` snapshot also enqueues
    one ``embed`` job keyed on the new ``snapshot_id`` (see the module
    docstring — ``enrich`` is not enqueued here) and drives the synchronous
    FTS leg (:func:`_index_snapshot_fts`). A ``"tombstone"`` snapshot does
    neither — no ``embed`` enqueue, no FTS write — see the module
    docstring's "Embed-only enqueue" section for why a failed fetch must not
    become a retrievable/citable hit on either leg.

    ``status`` records the fetch outcome (``"ok"`` for real content,
    ``"tombstone"`` for a permanent failure); callers writing a tombstone
    should pass ``body=tombstone_body(reason)`` for the stable, inspectable
    convention. The externals upsert, snapshot insert, head move, and the
    (status-gated) embed enqueue run in one ``with conn:`` transaction, so a
    crash between steps never leaves an ``ok`` snapshot without its derive
    job or a head pointing at a row that was never committed. The FTS write
    runs **after** that transaction commits (mirroring :meth:`lode.
    repository.Repository.save`'s cache-after-commit ordering) — the cache
    is regenerable, so it is deliberately kept out of the irreplaceable
    write's atomic scope, but the embed enqueue stays inside it since
    nothing currently re-discovers a snapshot with no derive job the way
    :func:`lode.reconcile._embed_gap_step` does for notes.

    ``fetched_at`` is stamped explicitly from :func:`lode.jobs.now_iso` rather
    than left to the schema's raw SQLite ``strftime('now')`` DEFAULT
    (``lode-bmg9``): :func:`lode.worker._refresh_dead_letter_hook`'s
    late-success guard (``lode-uda1``) compares this column against
    ``jobs.claimed_at``, which is *always* stamped from the same
    forward-ratcheted queue clock — comparing a ratcheted reading against a
    raw ``CLOCK_REALTIME`` one is only safe in one direction (see ``jobs.now``'s
    own docstring, guarantee 2), and the guard needed the other. Stamping both
    sides from the one clock closes that mismatch outright rather than merely
    narrowing it. This function is the **only** production writer of
    ``snapshots``, so nothing else can reintroduce the raw-clock stamp; the
    schema's DEFAULT survives for test/ad-hoc inserts only, and a new
    production writer of this table must stamp ``fetched_at`` the same way (see
    ``schema.sql``'s note on the column). ``docs/storage.md`` records the fix
    and the one place this ripples (``lode.reconcile``'s refresh-staleness
    cutoff, deliberately still raw — a backward step there delays a refresh by
    the step's magnitude but can never strand one, so it is not a new instance
    of the guard's clobber; note the skew persists for the process's lifetime
    rather than self-correcting, which ``docs/storage.md`` spells out).
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
            "INSERT INTO snapshots "
            "(snapshot_id, external_id, body, raw_payload, status, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (snapshot_id) DO NOTHING",
            (snapshot_id, external_id, body, raw_payload, status, jobs.now_iso()),
        )
        conn.execute(
            "UPDATE externals SET head_snapshot_id = ? WHERE external_id = ?",
            (snapshot_id, external_id),
        )
        if status != "tombstone":
            jobs.enqueue_derive_jobs(conn, snapshot_id, types=("embed",))
    # The write transaction above has committed (context-manager exit ==
    # COMMIT on normal return). The FTS leg is driven now, outside it — see
    # the docstring above for why (mirrors Repository.save's cache-after-
    # commit; also the lode-c5l bounce fix: a tombstone must never reach
    # here, same fail-closed rule as the embed enqueue).
    if status != "tombstone":
        _index_snapshot_fts(conn, external_id, snapshot_id, body, settings=settings)
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


# ---------------------------------------------------------------------------
# no_egress control surface (lode-w0h.7) — see docs/externals.md "No-egress
# tier". The enforcement path (excluded from enrich/Q&A egress, cited as
# withheld) already reads ``externals.no_egress`` generically via
# lode.egress/lode.cited_answer/lode.enrich; this is the one thing that was
# missing — a way to actually set the flag on an existing external_id.
# ---------------------------------------------------------------------------


def set_no_egress(
    conn: sqlite3.Connection, external_id: str, no_egress: bool = True
) -> bool:
    """Set (or clear) ``externals.no_egress`` for ``external_id``.

    A pure flag flip on an existing row — it never touches indexing or
    retrieval (``docs/externals.md`` "No-egress tier": "no_egress gates
    egress only"), so a just-marked source stays keyword/vector-retrievable
    immediately; only the next enrich/Q&A egress send excludes it (via
    :func:`lode.egress.partition_egress`, consumed generically by
    :func:`lode.enrich.enrich_version` and :func:`lode.cited_answer.ask`
    through :func:`lode.cited_answer._resolve_target`'s ``externals`` join —
    no separate wiring needed here).

    Returns ``True`` if ``external_id`` had a row to flip, ``False`` if no
    such external exists (the caller — :mod:`lode.cli` — turns that into a
    clean "no such external source" error rather than silently no-opping).
    """
    with conn:
        cur = conn.execute(
            "UPDATE externals SET no_egress = ? WHERE external_id = ?",
            (int(no_egress), external_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Material-change re-enrich gating (lode-w0h.5) — see the module docstring's
# "Material-change re-enrich gating" section for the design.
# ---------------------------------------------------------------------------


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """Elementwise mean of ``vectors`` — a document-level stand-in for a snapshot.

    ``vectors`` must be non-empty and share one dimension (both true of any
    set of passage vectors returned by :meth:`lode.vectorstore.VectorStore.
    vectors_for` for a single ``target_version``, since the pinned
    ``embedding_vector_dim`` fixes the width for the whole table).
    """
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i, component in enumerate(vector):
            sums[i] += component
    n = len(vectors)
    return [s / n for s in sums]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector.

    Plain Python (no numpy dependency) — the vectors here are two
    mean-pooled, document-level vectors, not a passage-by-passage ANN
    workload, so there's no case for pulling in a heavier dependency or
    routing through LanceDB's own cosine metric for this.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _predecessor_snapshot(
    conn: sqlite3.Connection, external_id: str, snapshot_id: str
) -> str | None:
    """Return the snapshot immediately before ``snapshot_id`` for ``external_id``.

    "Immediately before" = the most recently ``fetched_at`` (ties broken by
    ``rowid``, SQLite's implicit insertion-order column) row for this
    external, excluding ``snapshot_id`` itself. By the time this runs,
    ``snapshot_id`` is already the external's head (:func:`ingest_snapshot`
    moves the head inside its own transaction, and the embed job that
    triggers :func:`gate_reenrich` only runs after that commits) — so this is
    genuinely the predecessor head, not an arbitrary sibling. ``None`` if
    ``snapshot_id`` is this external's first-ever snapshot.
    """
    row = conn.execute(
        "SELECT snapshot_id FROM snapshots WHERE external_id = ? AND snapshot_id != ? "
        "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
        (external_id, snapshot_id),
    ).fetchone()
    return row[0] if row is not None else None


def gate_reenrich(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    lance_dir: str | Path,
    settings: Settings | None = None,
) -> str | None:
    """Decide whether ``snapshot_id``'s change is material enough to re-enrich.

    Called by :func:`lode.worker._embed_handler` immediately after a
    snapshot's own vectors are written — materiality is decided **post-embed**
    (bd lode-w0h.5 decision C) since the signal needs those vectors to exist.
    A no-op (returns ``None``) for anything that isn't a live ``"ok"``
    snapshot: a note ``version_id`` (the embed handler runs for both
    polymorphically), or a snapshot somehow re-embedded after being
    tombstoned (never happens via the normal ingest path — see the module
    docstring — but this stays defensive rather than assuming it can't).

    **Materiality signal:** cosine similarity between the new snapshot's and
    its predecessor's mean-pooled passage vectors (:func:`_mean_pool` +
    :func:`_cosine_similarity`) — a single document-level vector per
    snapshot, not a passage-by-passage comparison. ``delta = 1 -
    similarity``; material iff ``delta >= settings.
    reenrichment_materiality_threshold``. There is no predecessor to compare
    against (treated as unconditionally material, i.e. ``delta`` reads as
    "no baseline") when:

    - this is the external's first-ever snapshot (:func:`_predecessor_snapshot`
      returns ``None`` — nothing to carry forward from either), or
    - either snapshot has zero passage vectors (the predecessor was never
      embedded — e.g. it was a tombstone — or the new snapshot's body chunked
      to zero passages).

    **Material** → enqueues one ``enrich`` job for ``snapshot_id``
    (:func:`lode.jobs.enqueue_derive_jobs`, idempotent — a live job already
    pending/running is a no-op).

    **Immaterial** → enqueues nothing; instead carries the predecessor's
    AI-derived annotations/edges forward by re-anchoring them
    (:func:`lode.staleness.reanchor_annotations` / ``reanchor_edges``) against
    ``snapshot_id``'s own body — a verbatim ``quoted_text`` match advances
    ``source_version`` to the new snapshot (still "fresh"); a changed-context
    or vanished anchor is marked ``stale``/``orphaned`` exactly as it would be
    for a note update. Both calls target ``external_id`` (the schema's
    ``annotations.target`` / ``edges.from_id`` are polymorphic — note_id or
    external_id — by design, ``src/lode/schema.sql``), so this reuses the
    identical mechanism :meth:`lode.repository.Repository.save` already runs
    for a note, rather than hand-rolling a copy-the-rows variant.

    Returns a one-line human-readable outcome for the worker's outcome-echo
    (mirrors every other handler's return contract, lode-1gr.4), or ``None``
    when ``snapshot_id`` isn't a live ``"ok"`` snapshot at all.
    """
    settings = settings or Settings()
    row = conn.execute(
        "SELECT external_id, body, status FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None or row[2] != "ok":
        return None
    external_id, new_body, _status = row

    store = VectorStore(lance_dir, settings)
    new_vectors = store.vectors_for(snapshot_id)

    predecessor_id = _predecessor_snapshot(conn, external_id, snapshot_id)
    predecessor_vectors = store.vectors_for(predecessor_id) if predecessor_id else []

    delta: float | None
    if not new_vectors or not predecessor_vectors:
        delta = None
    else:
        similarity = _cosine_similarity(
            _mean_pool(new_vectors), _mean_pool(predecessor_vectors)
        )
        delta = 1.0 - similarity

    threshold = settings.reenrichment_materiality_threshold
    material = delta is None or delta >= threshold
    short = short_version_id(snapshot_id)

    if material:
        jobs.enqueue_derive_jobs(conn, snapshot_id, types=("enrich",))
        reason = (
            "no baseline vectors to compare"
            if delta is None
            else f"delta={delta:.3f} >= threshold={threshold}"
        )
        return f"material change ({reason}): enqueued enrich {short}"

    ann_counts = staleness.reanchor_annotations(
        conn, external_id, snapshot_id, new_body
    )
    edge_counts = staleness.reanchor_edges(conn, external_id, snapshot_id, new_body)
    return (
        f"immaterial change (delta={delta:.3f} < threshold={threshold}): "
        f"carried forward enrichment to {short} "
        f"(annotations fresh={ann_counts['fresh']} stale={ann_counts['stale']} "
        f"orphaned={ann_counts['orphaned']}; edges fresh={edge_counts['fresh']} "
        f"stale={edge_counts['stale']} orphaned={edge_counts['orphaned']})"
    )
