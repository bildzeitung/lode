"""``lode reembed`` -- force a fresh embed job for every live head (lode-g274.7)."""

import typer

from lode.cli import _DbOption, _open_db, app


@app.command(
    help=(
        "Force a fresh embed job for every live note/external head.\n\n"
        "Run it when 'lode status' says the index is mixed, or that the "
        "embedder's cached weights have moved past your vectors' revision. "
        "Whole-corpus, not targeted -- a model/cache change affects "
        "everything at once.\n\n"
        "Only queues the jobs -- run 'lode work' (or 'lode work --wait') to "
        "actually re-embed. Safe to re-run; resume an interrupted run with "
        "'lode work', not by re-running this. Embedder only, no lexical/FTS "
        "or enrichment effect. See docs/how-to/maintenance-commands.md."
    )
)
def reembed(db: _DbOption = None) -> None:
    """Force a fresh embed job for every live head (lode-g274.7).

    The deliberate counterpart to ``lode status``'s "the index is mixed" /
    "moved past the revision" hints (``lode-crh8.1``'s WARN, never REFUSE,
    mismatch behavior) -- this is the regeneration those hints point at.

    Enqueues one ``embed`` job per **live head** -- every note's current
    ``head_version_id`` and every external's current ``head_snapshot_id``
    (:func:`lode.retrieval.live_head_versions`, the same notes-UNION-externals
    set retrieval itself is scoped to; passages/embeddings are "heads only",
    docs/storage.md's data-shape sketch). Superseded (non-head) versions and
    snapshots are never re-embedded -- they carry no live vectors to begin
    with. This command has no whole-corpus-vs-targeted flag: it always
    re-embeds every live head, matching the corpus-wide nature of the
    triggering event (a model/cache change), never a single note or source.

    **Forces regeneration regardless of any prior job's outcome** -- unlike
    the passive reconciliation scan's embed-gap step (``lode.reconcile``),
    which only re-enqueues a head with no live (pending/running) embed job at
    all. A head whose embed job already reached ``done`` -- the overwhelming
    common case for an established corpus -- is exactly what needs a *fresh*
    job here, since ``done`` is what a stale ``model_revision`` looks like.
    Reuses :func:`lode.jobs.enqueue_derive_jobs`, the same enqueue primitive
    every capture uses (``types=("embed",)`` only -- this never touches
    ``enrich``): the live-job partial unique index still dedupes a version
    that already has a job in flight, so running this twice in a row before
    the queue drains enqueues nothing extra the second time.

    **Rebuild-in-place, not build-then-swap.** Each live head's vectors are
    replaced atomically as its own ``embed`` job runs
    (:meth:`lode.vectorstore.VectorStore.replace_vectors`'s existing
    delete-then-add), the same primitive every embed already goes through --
    no shadow index, no whole-corpus swap. The corpus is necessarily *mixed*
    for the run's duration (some heads already on the new revision, others
    still on the old one) -- this is not a new failure mode to guard against;
    it is exactly the per-vector ``model_revision`` state
    (docs/storage.md#model-provenance-the-embedder-revision-manifest-decided-lode-crh81)
    already designed to make a gradually-progressing regeneration detectable
    (``lode status``'s "mixed" hint) and safe, never silently corrupting.

    **Resumable by construction, no new machinery.** The enqueue above runs
    in one SQLite transaction -- interrupted before it commits, nothing is
    enqueued at all, so re-running this command is always safe to retry.
    Actually *running* the enqueued jobs is `lode work`'s job, not this
    command's: this only enqueues and returns, printing how many heads were
    queued and pointing at the next step. `lode work` is the async work
    queue's own durable, resumable execution engine (docs/storage.md "lag is
    safe by construction") -- interrupting *it* mid-drain leaves whatever was
    still pending exactly that: pending, safe to resume with another `lode
    work` (or `lode work --wait`) run. **Resume an interrupted regeneration
    with `lode work`, not by re-running `lode reembed`** -- this command has
    no notion of "already did this run," so calling it again would
    needlessly re-enqueue a job for every live head, including the ones a
    prior run already finished (harmless -- ``embed()`` is idempotent -- but
    wasted work at corpus scale).

    Once every enqueued job reaches ``done``, the manifest agrees with the
    index again: :meth:`~lode.vectorstore.VectorStore.model_revisions`
    returns a single revision and ``lode status`` stops warning, assuming
    nothing else changed the live cache mid-run.

    **The lexical/FTS leg is untouched.** FTS is written synchronously at
    save time from ``chunk()``'s deterministic output
    (:class:`lode.lexical.LexicalCacheBackend`, ``lode-xyb``) and carries no
    model of its own -- an embedding-model change cannot desync it, so this
    command enqueues no ``refresh`` of ``passages_fts``.

    **Embedder only.** Matches ``lode-crh8``'s own DB-invalidation scoping:
    the enrichment LLM (Claude) also persists into the DB and is tracked
    separately (docs/configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745);
    correcting an enrichment-model mismatch is a *targeted re-enrich*, out of
    scope here -- tracked separately as ``lode-14jr``.
    """
    from lode.jobs import enqueue_derive_jobs
    from lode.retrieval import live_head_versions

    conn = _open_db(db)
    try:
        heads = live_head_versions(conn)
        with conn:
            for version_id in heads:
                enqueue_derive_jobs(conn, version_id, types=("embed",))
    finally:
        conn.close()

    if heads:
        typer.echo(
            f"enqueued {len(heads)} embed job(s) for every live note/external "
            "head -- run 'lode work' (or 'lode work --wait') to run them."
        )
    else:
        typer.echo("no live heads to re-embed.")
