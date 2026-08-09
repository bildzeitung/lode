"""``lode reindex-lexical`` -- rebuild passages_fts for every live note head (lode-x9lu)."""

import typer

from lode.cli import _DbOption, _open_db, app
from lode.lexical import LexicalCacheBackend
from lode.notes_read import live_note_heads_with_body


@app.command(
    help=(
        "Rebuild the lexical search index for every live note head.\n\n"
        "Optional: run it when 'lode status' flags a lexical-index gap. "
        "'lode work' closes the same gap on its own next reconcile pass, so "
        "this just does it sooner. Safe to re-run any time; external "
        "snapshots are left untouched."
    )
)
def reindex_lexical(db: _DbOption = None) -> None:
    """Rebuild ``passages_fts`` for every live NOTE head from its current body (lode-x9lu).

    Closes the coverage hole ``reembed``'s own docstring calls out ("the
    lexical/FTS leg is untouched") from the other side: a note saved before
    the lexical leg landed (``lode-x6r.4``) has no rows in ``passages_fts`` at
    all, so it silently never surfaces in Browse quick search
    (:func:`lode.notes_read.search_notes`) or retrieval's lexical leg
    (:func:`lode.retrieval.lexical_search`) -- both scope to the live-head set
    and simply find nothing there for it. Unlike ``reembed``, this needs no
    async job: chunking and the FTS5 write are the same synchronous,
    model-free path :class:`lode.lexical.LexicalCacheBackend` already runs
    inline on every save (``lode-xyb``), so this command drives that same
    ``index()`` call directly, once per live note head, and returns done --
    no ``lode work`` step, no queue.

    **Notes only, not externals.** Every external snapshot's own FTS rows are
    written by :func:`lode.externals.ingest_snapshot` at fetch time and are
    untouched here -- this command walks ``notes``/``versions`` directly
    rather than reusing :func:`lode.retrieval.live_head_versions` (which
    unions in external heads too), so an external's rows are neither read nor
    rewritten.

    **Idempotent.** :meth:`~lode.lexical.LexicalCacheBackend.index` chunks the
    body (deterministic, content-addressed passage ids) and
    :meth:`~lode.lexical.LexicalIndex.replace_passages` deletes-then-inserts
    per ``target_version`` -- so re-running this command against a head
    already indexed just re-writes the same rows, changing nothing.

    **``op != 'delete'`` alone, deliberately no ``purged_at`` guard.** Every
    other regeneration path (:mod:`lode.reconcile`, :mod:`lode.enrich`) also
    requires ``purged_at IS NULL``; this one must not. A hard purge does not
    leave the note out of the index --
    :meth:`lode.repository.Repository.purge` evicts the whole chain and then
    re-indexes the live head from the ``[purged ...]`` marker body, so a purged
    note is *present* in ``passages_fts`` as the marker. Skipping purged heads
    here would diverge from the path this command exists to reproduce. Proved
    by ``test_reindex_lexical_indexes_a_purged_note_head_as_the_marker``.

    Every live head is rewritten, not just the ones missing rows: that is what
    makes this a *repair* tool rather than only a backfill -- a head with stale
    or half-written rows is fixed too, which a "only where absent" filter would
    silently skip.
    """
    conn = _open_db(db)
    try:
        # fetchall, not a streaming cursor: index() commits on this same
        # connection, which would invalidate a cursor still being iterated.
        rows = live_note_heads_with_body(conn)
        cache = LexicalCacheBackend(conn)
        for note_id, version_id, body in rows:
            cache.index(note_id, version_id, body)
        if rows:
            typer.echo(f"reindexed {len(rows)} live note head(s) into passages_fts.")
        else:
            typer.echo("no live note heads to reindex.")
    finally:
        conn.close()
