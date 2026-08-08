"""Reconciliation scan: re-enqueue head versions missing fresh derived work (lode-i05.4).

The reconciliation scan is the **self-healing net** for crashes, dropped jobs, and
the tiny window between a version write and its enqueue (see ``docs/storage.md``
"Reconciliation scan on startup + periodically"). It runs:

- **at worker startup** — before the first drain pass, so any gap left by a
  crash or incomplete run is filled immediately;
- **periodically in ``--loop`` mode** — at the start of each drain tick, so
  the queue stays healthy over long-running worker sessions.

**Step registry** — mirrors :mod:`lode.worker`'s handler-registry shape:

- ``embed_gap`` — registered now (Phase A). Finds head versions missing a
  live (non-dead) embed job — i.e. the vector leg never ran, was dead-lettered,
  or somehow lost its job row — and re-enqueues an ``embed`` job for each.
  Signal re-keyed in lode-xyb: ``passages`` rows are now written synchronously
  on save, so their presence no longer implies vectors exist; the embed job
  status is the reliable proxy for "vector leg completed."
  Excludes soft-deleted (``op='delete'``) and purged (``purged_at IS NOT NULL``)
  heads. **Snapshot arm (lode-621):** the same gap query also covers each
  external's current ``head_snapshot_id`` — mirroring
  :func:`lode.retrieval.live_head_versions`'s notes-UNION-externals shape — since
  lode-w0h.8 made a
  snapshot a first-class retrieval candidate with its own async vector leg. A
  tombstone snapshot (no body to embed) and a superseded (non-head) snapshot are
  excluded, matching what ``live_head_versions`` itself admits. **Dead-letter
  ownership, settled (docs/decisions.md):** this sweep is the sole re-enqueue
  path for a dead ``embed`` job, for both notes and snapshots — distinct from
  lode-at8's worker terminal-transition hook, which is scoped to ``refresh``
  jobs recording a *permanent* fetch failure (a tombstone), not to retrying a
  still-valid body's embedding.
- ``enrich_gap`` — registered (E7, lode-npx.1). Finds head versions missing a
  live (non-dead) enrich job — i.e. Haiku extraction never ran, was
  dead-lettered, or otherwise lost its job row — and re-enqueues an ``enrich``
  job for each.  Excludes tombstones, purged versions, and ``no_egress`` notes
  (content that must never be sent to Haiku).  Also catches the
  **prompt/model-change** case (lode-0wj.9): a head whose enrich job already
  ran (``done``) but whose **job row's own** ``prompt_ver`` is not the current
  :data:`lode.enrich.ENRICH_PROMPT_VER` is treated as a gap too, so bumping the
  prompt version triggers corpus-wide re-enrichment on the next scan instead of
  only covering notes with no enrich history at all. **Job-identity-based, not
  content-based (lode-q47):** the signal is the ``done`` job's own
  ``prompt_ver`` column — stamped on completion by
  :func:`lode.worker.run_one` (immediate path) and
  :func:`lode.enrich.collect_enrich_batch` (Batches API path) — never whether
  a ``summary`` annotation exists. A head whose enrichment legitimately
  produced an empty summary (Haiku returned ``""`` for a content-free note, so
  no ``summary`` row was written) is therefore correctly seen as
  current-and-done instead of being re-flagged as a gap on every scan.
- ``lexical_gap`` — registered (lode-cyly). Finds live NOTE heads with zero
  ``passages_fts`` rows and heals them **inline, synchronously** — unlike
  every other step here, it does not enqueue a job at all. Chunking and the
  FTS5 write are model-free (:class:`~lode.lexical.LexicalCacheBackend`, the
  same seam :func:`lode.repository.Repository.save` already drives inline on
  every save, lode-xyb), so there is nothing for a worker to pick up later.
  Closes the silent coverage hole ``lode reindex-lexical`` (lode-x9lu) fixes
  only when a user thinks to run it manually — this step finds the same gap
  on the ordinary reconcile schedule and heals it without anyone asking.
- ``refresh_stale`` — registered (``lode-w0h.6``). The web-connector **refresh
  policy**: finds every external whose current head snapshot is non-tombstone
  and older than ``settings.refresh_ttl_s``, with no live
  (``pending``/``running``) ``refresh`` job already covering it, and
  re-enqueues one. This is the **scheduling** half of "TTL / on-access
  revalidation" (``docs/decisions.md`` "External refresh") — see the step's
  own docstring and ``docs/externals.md`` "Refresh policy" for why a periodic
  TTL sweep was chosen over a true on-access hook, and why a tombstoned
  external is deliberately excluded. Rides :func:`lode.drawdown.refresh_external`
  unchanged (already registered in :mod:`lode.worker` as
  the ``refresh`` handler) — this step adds only staleness detection +
  scheduling, never a second fetch path.

**Idempotency** — each step re-enqueues via :func:`lode.jobs.enqueue_derive_jobs`,
which uses ``INSERT … ON CONFLICT DO NOTHING`` against the ``idx_jobs_live``
partial unique index (lode-i05.6). Running the scan repeatedly produces no
duplicate jobs: a version whose embed job is already pending or running is a
silent no-op at the INSERT level. Re-enqueue after ``done``/``dead`` IS allowed
(the index is scoped to ``pending``/``running`` only) — so a re-derive after a
crash or a prompt-ver bump is handled correctly.

**Single enqueue path** — steps call :func:`lode.jobs.enqueue_derive_jobs`
(optionally with a ``types`` subset), never a second hand-rolled INSERT. The
INSERT SQL lives in one place: ``jobs.py``.

**Not this scan's job — a stuck ``'running'`` row (lode-aor):** every gap query
below excludes any job with status ``!= 'dead'``, including ``'running'``, by
design — a live claim is not a gap. That means a job left ``'running'`` forever
by a worker crash (SIGKILL between claim and completion) is invisible here too;
:func:`lode.worker._reclaim_stale_running` is what detects and reclaims it
(dead-letters or resets it for retry), run at the top of every
:func:`lode.worker.drain` pass. Once that step has run, a crash-abandoned job
is back to ``'dead'`` or ``'pending'`` and these gap queries see it correctly
without any change of their own.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from lode import jobs
from lode.config import Settings
from lode.enrich import ENRICH_PROMPT_VER
from lode.lexical import LexicalCacheBackend
from lode.progress import op_progress

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

#: Scan step signature: receives an open SQLite connection and the resolved
#: runtime :class:`~lode.config.Settings`; returns the count of gap versions
#: found and **handled** — for most steps that means a targeted
#: ``enqueue_derive_jobs`` call each, but ``lexical_gap`` (lode-cyly) heals its
#: gaps inline instead, so the total is "gaps dealt with", not "jobs queued";
#: nothing may read it as queue depth. Steps run
#: within no outer transaction of their own — each step opens ``with conn:``
#: for the batch enqueue internally.
#:
#: **Settings threading (lode-09n):** :func:`reconcile` resolves ``settings``
#: once (caller-supplied, or a fresh default) and passes that same instance
#: positionally to every step, so a step reading a runtime knob (e.g.
#: ``refresh_stale``'s ``settings.refresh_ttl_s``) sees the caller's actual
#: overrides rather than silently constructing its own default ``Settings()``.
#: One shared call shape across the registry: a step with no
#: settings-dependent behavior still accepts the parameter and ignores it.
StepFn = Callable[[sqlite3.Connection, Settings], int]

#: Module-level step registry — list of ``(name, fn)`` pairs in run order.
#:
#: Populated at module load by :func:`register_step`; the ``embed_gap`` step is
#: registered here. Tests inject a custom list into :func:`reconcile` instead of
#: touching this directly.
_STEPS: list[tuple[str, StepFn]] = []


def register_step(name: str, fn: StepFn) -> None:
    """Append ``fn`` to the module-level step registry under ``name``."""
    _STEPS.append((name, fn))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile(
    conn: sqlite3.Connection,
    settings: Settings | None = None,
    *,
    steps: list[tuple[str, StepFn]] | None = None,
) -> int:
    """Run all registered scan steps; return the total count of gaps **handled**.

    Each step queries for a specific gap (e.g. "head version with no passages")
    and deals with it. Most steps do that by calling
    :func:`lode.jobs.enqueue_derive_jobs` for each gap version via
    ``ON CONFLICT DO NOTHING``; ``lexical_gap`` (lode-cyly) instead heals its
    gaps **inline**, enqueueing nothing at all. Either way the scan is safe to
    run at any time and any frequency, and the returned total is "gaps dealt
    with", never queue depth — see :data:`StepFn` and ``docs/storage.md``
    ("Shape: a durable jobs table + a reconciliation safety net").

    ``settings`` is resolved once — the caller's instance, or a fresh
    ``Settings()`` default if omitted, mirroring :func:`lode.worker.drain`'s
    ``settings = settings or Settings()`` — and threaded into every step; see
    :data:`StepFn` for the contract (lode-09n).

    ``steps`` is injectable for tests and is **keyword-only**, so that adding
    ``settings`` ahead of it cannot silently rebind a positionally-passed step
    list onto ``settings``. Production callers omit it and the module-level
    :data:`_STEPS` list is used.  Returns ``0`` when no steps are registered or
    all steps find no gaps.

    **Progress instrumentation (lode-olmi.15):** each step call is wrapped in
    :func:`lode.progress.op_progress` (``reconcile.<name>``), so a plain
    ``lode work`` always logs which step is currently running (and, if a step
    hangs, a periodic heartbeat) rather than staying silent until the whole
    scan returns.
    """
    settings = settings or Settings()
    if steps is None:
        steps = _STEPS
    total = 0
    for name, step_fn in steps:
        with op_progress(
            f"reconcile.{name}",
            heartbeat_interval_s=settings.progress_heartbeat_interval_s,
        ):
            count = step_fn(conn, settings)
        if count:
            log.info("reconcile[%s]: %d gap version(s) handled", name, count)
        total += count
    return total


# ---------------------------------------------------------------------------
# Embed-gap step (registered at module load)
# ---------------------------------------------------------------------------


def _embed_gap_step(conn: sqlite3.Connection, settings: Settings | None = None) -> int:
    """Embed gap: re-enqueue embed jobs for live heads missing a live embed job.

    ``settings`` is unused here (see :data:`StepFn`).

    **Gap signal (lode-xyb):** since ``passages`` + ``passages_fts`` are now
    written synchronously on save by :class:`~lode.lexical.LexicalCacheBackend`,
    a ``passages`` row existing no longer means "embed ran" — it just means "save
    ran."  The reliable signal for "embedding completed (vectors in LanceDB)" is a
    non-dead embed job: a ``pending``/``running``/``done``/``failed`` embed job for
    the target means the vector work is either in-flight or completed; a ``dead``
    (max-retries exhausted) job or the total absence of a job means the vector leg
    is missing.

    **Gap query — notes arm:** live head versions — ``notes.head_version_id``
    joined to ``versions``, where the head op is not ``'delete'`` (not a
    soft-delete tombstone) and ``purged_at IS NULL`` (not hard-deleted/purged) —
    with no embed job in status ``pending``, ``running``, ``done``, or ``failed``.

    **Gap query — snapshot arm (lode-621):** the external analogue, mirroring
    :func:`lode.retrieval.live_head_versions`'s notes-UNION-externals shape —
    ``externals.head_snapshot_id`` joined to ``snapshots``, where the snapshot's
    ``status`` is not ``'tombstone'`` (a tombstone has no body to embed; sweeping
    it would enqueue a job that can only fail).  Only the current
    ``head_snapshot_id`` is read, so a superseded (non-head) snapshot is excluded
    by construction, same as a note's non-head version — matching what
    ``live_head_versions`` itself admits to the direct retrieval legs.  Before
    lode-w0h.8 unioned external heads into ``live_head_versions``, a dead
    snapshot embed job had no user-visible consequence (the snapshot was only
    graph-reachable, never a direct vector hit); now it does, so this arm closes
    the gap the notes-only query left.

    In both arms: no job at all, or all existing embed jobs ``dead``, means the
    vector leg is missing.  Each such target is re-enqueued.

    **Dead-letter ownership (docs/decisions.md):** this sweep is the sole
    re-enqueue mechanism for a dead ``embed`` job, for both arms — distinct from
    lode-at8's worker terminal-transition hook, which handles ``refresh`` jobs
    reaching ``dead`` by writing a tombstone (a *permanent* failure record), not
    by retrying.  An embed job's body is still valid after a dead embed job (the
    version/snapshot content didn't change), so a periodic blind re-enqueue is a
    safe, cheap recovery — no hook needed.

    **Enqueue:** calls :func:`lode.jobs.enqueue_derive_jobs` with
    ``types=("embed",)`` inside a single ``with conn:`` transaction.  The INSERT
    is ``ON CONFLICT DO NOTHING`` against ``idx_jobs_live``, so a target whose
    embed job is already pending or running produces no duplicate row — the scan
    is entirely idempotent.

    Returns the count of gap targets found (each triggered one
    ``enqueue_derive_jobs`` call; some may be no-ops for in-flight jobs).
    """
    gap_versions = conn.execute(
        """
        SELECT n.head_version_id
        FROM notes n
        JOIN versions v ON v.version_id = n.head_version_id
        WHERE n.head_version_id IS NOT NULL
          AND v.op != 'delete'
          AND v.purged_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'embed'
                AND j.target_version = n.head_version_id
                AND j.status != 'dead'
          )
        UNION
        SELECT e.head_snapshot_id
        FROM externals e
        JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id
        WHERE e.head_snapshot_id IS NOT NULL
          AND s.status != 'tombstone'
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'embed'
                AND j.target_version = e.head_snapshot_id
                AND j.status != 'dead'
          )
        """
    ).fetchall()

    if not gap_versions:
        return 0

    # Batch-enqueue all gap versions in a single transaction; enqueue_derive_jobs
    # is a plain INSERT (no own txn) so the `with conn:` here is the boundary.
    with conn:
        for (version_id,) in gap_versions:
            jobs.enqueue_derive_jobs(conn, version_id, types=("embed",))

    return len(gap_versions)


# Register the embed-gap step on module load.
register_step("embed_gap", _embed_gap_step)


# ---------------------------------------------------------------------------
# Enrich-gap step (registered at module load — lode-npx.1)
# ---------------------------------------------------------------------------


def _enrich_gap_step(conn: sqlite3.Connection, settings: Settings | None = None) -> int:
    """Enrich gap: re-enqueue enrich jobs for head versions missing fresh enrichment.

    ``settings`` is unused here (see :data:`StepFn`).

    **Gap signal, part 1 (job existence):** a non-tombstone, non-purged,
    non-``no_egress`` head version with no in-flight/retryable enrich job
    (``pending``, ``running``, or ``failed``).  A ``dead`` job (max-retries
    exhausted) or the total absence of a job is treated the same as before.

    **Gap signal, part 2 (prompt/model change, lode-0wj.9 / lode-q47):** even
    when a ``done`` enrich job exists for the head, it is still a gap unless
    that job's own ``prompt_ver`` column equals the *current*
    :data:`lode.enrich.ENRICH_PROMPT_VER`. ``prompt_ver`` is stamped on the job
    row itself at completion time (:func:`lode.worker.run_one` for the
    immediate path, :func:`lode.enrich.collect_enrich_batch` for the Batches
    API path) — a prior run under an older prompt/model left the job's
    ``prompt_ver`` stale (or, pre-lode-q47, NULL), so a fresh ``enrich`` job is
    re-enqueued. This is deliberately **job-identity-based, not
    content-based**: it does not consult the ``annotations`` table, so a head
    whose enrichment ran under the current prompt but produced an *empty*
    summary (no ``summary`` row written — mirrors an empty tag/entity list) is
    correctly recognized as current, instead of being perpetually re-flagged
    as a gap (the lode-q47 thrash bug). A ``pending``/``running``/``failed``
    job is left alone regardless (in-flight or about to be retried).

    **Gap query:** live head versions — ``notes.head_version_id`` joined to
    ``versions``, filtered to non-tombstone (``op != 'delete'``), non-purged
    (``purged_at IS NULL``), non-no_egress (``no_egress = 0``) — with no
    pending/running/failed enrich job, AND no ``done`` enrich job whose own
    ``prompt_ver`` matches the current prompt version.  Each such version is
    re-enqueued via :func:`lode.jobs.enqueue_derive_jobs`.

    **Enqueue:** ``ON CONFLICT DO NOTHING`` against ``idx_jobs_live`` ensures a
    version whose enrich job is already pending or running produces no duplicate
    row.  Re-enqueue after ``done``/``dead`` IS allowed (the index is scoped to
    live statuses only).

    Returns the count of gap versions found (each triggered one enqueue call).
    """
    gap_versions = conn.execute(
        """
        SELECT n.head_version_id
        FROM notes n
        JOIN versions v ON v.version_id = n.head_version_id
        WHERE n.head_version_id IS NOT NULL
          AND v.op != 'delete'
          AND v.purged_at IS NULL
          AND n.no_egress = 0
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'enrich'
                AND j.target_version = n.head_version_id
                AND j.status IN ('pending', 'running', 'failed')
          )
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'enrich'
                AND j.target_version = n.head_version_id
                AND j.status = 'done'
                AND j.prompt_ver = ?
          )
        """,
        (ENRICH_PROMPT_VER,),
    ).fetchall()

    if not gap_versions:
        return 0

    with conn:
        for (version_id,) in gap_versions:
            jobs.enqueue_derive_jobs(conn, version_id, types=("enrich",))

    return len(gap_versions)


# Register the enrich-gap step on module load.
register_step("enrich_gap", _enrich_gap_step)


# ---------------------------------------------------------------------------
# Refresh-stale step (registered at module load — lode-w0h.6)
# ---------------------------------------------------------------------------


def _refresh_stale_step(
    conn: sqlite3.Connection, settings: Settings | None = None
) -> int:
    """Refresh-stale: re-enqueue a ``refresh`` job for every external past its TTL.

    The web-connector **refresh policy** (``lode-w0h.6``): the staleness
    detection + scheduling this ticket adds on top of ``lode-w0h.3``'s
    fetch->ingest ``refresh`` handler (:func:`lode.drawdown.refresh_external`,
    unchanged — this step never fetches anything itself, only enqueues).

    ``settings`` defaults to ``None`` only for standalone callability (tests
    call this step directly); every production call arrives via
    :func:`reconcile`, which always passes the caller's resolved instance — so
    ``settings.refresh_ttl_s`` honors an override (lode-09n).

    **Policy choice (TTL sweep, not a true on-access hook):**
    ``docs/decisions.md``'s "External refresh" entry leaves each connector to
    choose between on-access revalidation and scheduled background refresh.
    A true on-access hook would need to run at *read* time (retrieval/Q&A),
    but every synchronous read path in this codebase is deliberately
    network-free (mirrors ``Repository.save``'s "no network I/O in save
    itself" rule for the write side) — bolting a blocking HTTP fetch onto an
    interactive Q&A call would trade a predictable, bounded citation latency
    for an unbounded one. Instead this step rides the reconciliation scan's
    existing periodic architecture (``lode.reconcile.reconcile``, run at
    worker startup and every ``--loop``/``--wait`` tick, ``docs/storage.md``
    "Reconciliation scan on startup + periodically") — a scheduled sweep
    bounded by ``settings.refresh_ttl_s``, which amounts to "revalidate the
    next time anything drains the queue, if the TTL has elapsed" rather than
    "revalidate the instant a citation reads it." See ``docs/externals.md``
    "Refresh policy" for the full write-up.

    **Staleness signal:** ``externals.head_snapshot_id`` joined to
    ``snapshots``, where the head snapshot's ``status != 'tombstone'`` and its
    ``fetched_at`` is at or before ``now - settings.refresh_ttl_s``. Only the
    current head is read (mirrors :func:`_embed_gap_step`'s snapshot arm), so
    a superseded (non-head) snapshot's age is irrelevant.

    **Tombstone exclusion (deliberate, not an oversight):** a tombstone head
    means the source already failed permanently (a genuine 4xx/empty-extract,
    or a transient failure that exhausted every retry and dead-lettered —
    ``docs/externals.md`` "Fetch-outcome taxonomy") — mirrors
    :func:`_embed_gap_step`'s own tombstone exclusion: blindly re-enqueuing a
    fetch for a source the draw-down machinery has already given up on would
    burn a retry budget on something the taxonomy has already classified as
    unfetchable-for-now, not silently heal a transient blip (a *first*
    dead-letter is already the terminal outcome of five backoff attempts,
    ``lode-i05.6``). If a dead link recovering later matters in practice,
    revisit — nothing here prevents a *user* from re-pasting the URL to force
    a fresh draw-down.

    **Idempotency:** the ``NOT EXISTS`` guard skips any external with a
    ``pending``/``running`` ``refresh`` job already in flight, mirroring
    :func:`_embed_gap_step`/:func:`_enrich_gap_step`'s own live-job guard;
    the underlying :func:`lode.jobs.enqueue_derive_jobs` INSERT is also
    ``ON CONFLICT DO NOTHING`` against ``idx_jobs_live``, so running this step
    repeatedly (or concurrently with the guard momentarily stale) enqueues no
    duplicate row.

    Returns the count of stale externals found (each triggered one
    ``enqueue_derive_jobs`` call).
    """
    settings = settings or Settings()
    # Format via jobs.iso (the one definition of the schema's ISO-8601 ms-Z
    # shape, lode-ajda), but still stamped from the RAW wall clock — deliberately
    # NOT jobs.now(). A backward wall-clock step here only refreshes an external
    # late; it cannot strand one, so this predicate does not need the queue
    # clock's forward-ratchet guarantee (lode-ajda scopes that out explicitly).
    cutoff = jobs.iso(datetime.now(UTC) - timedelta(seconds=settings.refresh_ttl_s))

    stale_externals = conn.execute(
        """
        SELECT e.external_id
        FROM externals e
        JOIN snapshots s ON s.snapshot_id = e.head_snapshot_id
        WHERE e.head_snapshot_id IS NOT NULL
          AND s.status != 'tombstone'
          AND s.fetched_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM jobs j
              WHERE j.type = 'refresh'
                AND j.target_version = e.external_id
                AND j.status IN ('pending', 'running')
          )
        """,
        (cutoff,),
    ).fetchall()

    if not stale_externals:
        return 0

    with conn:
        for (external_id,) in stale_externals:
            jobs.enqueue_derive_jobs(conn, external_id, types=("refresh",))

    return len(stale_externals)


# Register the refresh-stale step on module load.
register_step("refresh_stale", _refresh_stale_step)


# ---------------------------------------------------------------------------
# Lexical-gap step (registered at module load — lode-cyly)
# ---------------------------------------------------------------------------


#: The lexical-gap predicate — the ONE definition of "live NOTE head with no
#: ``passages_fts`` rows" (lode-cyly). Both readers below interpolate their own
#: select list in front of it, so the healer (:func:`_lexical_gap_step`) and
#: ``lode status``'s hint (``lode.cli._lexical_gap_count``) cannot disagree
#: about what a gap *is* — mirroring :func:`_stale_enrichment_heads`'s "status
#: says clean and the healer has work are structurally the same read" pattern.
#: Scope/filter rationale lives on :func:`_lexical_gap_step`.
_LEXICAL_GAP_FROM = """
    FROM notes n
    JOIN versions v ON v.version_id = n.head_version_id
    WHERE n.head_version_id IS NOT NULL
      AND v.op != 'delete'
      AND v.purged_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM passages_fts f
          WHERE f.target_version = n.head_version_id
      )
"""


def lexical_gap_heads(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Live NOTE heads with zero ``passages_fts`` rows: ``(note_id, version_id, body)``.

    The healer's read — it needs each body to re-index. A caller that only
    needs "how many" must use :func:`lexical_gap_count` instead, which shares
    :data:`_LEXICAL_GAP_FROM` but never materializes a body.
    """
    return conn.execute(
        "SELECT n.note_id, n.head_version_id, v.body" + _LEXICAL_GAP_FROM
    ).fetchall()


def lexical_gap_count(conn: sqlite3.Connection) -> int:
    """How many live NOTE heads currently have no ``passages_fts`` rows.

    Same predicate as :func:`lexical_gap_heads` (:data:`_LEXICAL_GAP_FROM`),
    so ``lode status``'s hint can never disagree with what the next
    ``lexical_gap`` reconcile pass will heal — but counted in SQLite rather
    than by reading every gap head's body into Python, which on the very
    corpus this hint exists for (a whole DB predating the lexical leg) is the
    entire note text, per ``lode status`` invocation.
    """
    return conn.execute("SELECT COUNT(*)" + _LEXICAL_GAP_FROM).fetchone()[0]


def _lexical_gap_step(
    conn: sqlite3.Connection, settings: Settings | None = None
) -> int:
    """Lexical gap: heal live NOTE heads with zero ``passages_fts`` rows, inline.

    Discovered while technically reviewing lode-x9lu, which added the manual
    ``lode reindex-lexical`` command: that command is correct, but the hole it
    fixes is silent -- a note missing from ``passages_fts`` simply never
    surfaces in Browse quick search or retrieval's lexical leg, with nothing
    telling the user so, and a user only runs the command if they already
    suspect their index is incomplete. This step closes the hole for
    everyone, on the ordinary reconcile schedule, rather than only for users
    who discover the command.

    **Not a job-queue step, unlike every sibling here.** Chunking and the
    FTS5 write are the same synchronous, model-free path
    :class:`~lode.lexical.LexicalCacheBackend` already runs inline on every
    save (lode-xyb) -- so there is nothing to defer to a worker. This step
    drives that same ``index()`` call directly, once per gap head, and the
    gap is closed before :func:`reconcile` returns.

    **Gap query:** live NOTE heads only (mirrors ``lode reindex-lexical``'s
    own notes-only scope -- an external snapshot's FTS rows are written by
    :func:`lode.externals.ingest_snapshot` at fetch time and are a different
    lifecycle entirely) -- ``notes.head_version_id`` joined to ``versions``,
    filtered to non-tombstone (``op != 'delete'``) and non-purged
    (``purged_at IS NULL``, matching :func:`_embed_gap_step`/
    :func:`_enrich_gap_step`'s own convention -- unlike ``reindex_lexical``'s
    CLI command, this step is a *gap-healer* re-run on every scan, not a
    one-shot *repair* tool, so it does not need that command's deliberate
    purged-head exception; a purge already re-indexes the live head via
    :meth:`lode.repository.Repository.purge`, so a purged head is not a gap
    here in the first place) -- with no ``passages_fts`` row for the head's
    ``target_version``.

    **Idempotent.** :meth:`~lode.lexical.LexicalCacheBackend.index` chunks
    the body (deterministic, content-addressed passage ids) and
    :meth:`~lode.lexical.LexicalIndex.replace_passages` deletes-then-inserts
    per ``target_version`` -- so a head already indexed is never re-flagged
    (the ``NOT EXISTS`` guard fails once any row exists).

    **One head shape never converges:** a body that chunks to zero passages
    writes no FTS row, so it is re-counted (and re-"healed", writing nothing)
    on every scan, and ``lode status``'s hint for it never clears. Left
    unguarded deliberately: every write path refuses an empty/whitespace-only
    body (``lode add``, :func:`lode.tui.services.capture.save_capture`,
    :func:`lode.tui.services.edit.save_edit`), so such a head is not
    reachable through the product — only by hand-inserted rows. Suppressing
    it would mean chunking every gap body inside the *status probe*, which is
    a much larger cost than the case is worth.

    Returns the count of gap heads found and healed.
    """
    gap_versions = lexical_gap_heads(conn)

    if not gap_versions:
        return 0

    cache = LexicalCacheBackend(conn, settings=settings)
    for note_id, version_id, body in gap_versions:
        cache.index(note_id, version_id, body)

    return len(gap_versions)


# Register the lexical-gap step on module load.
register_step("lexical_gap", _lexical_gap_step)
