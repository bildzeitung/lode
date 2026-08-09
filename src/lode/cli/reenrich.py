"""``lode reenrich`` -- force a fresh enrich job for every live head whose annotations are stale (lode-14jr)."""

import typer

from lode import cli
from lode.cli import _DbOption, _open_db, app
from lode.enrichment_view import stale_enrichment_heads


@app.command(
    help=(
        "Force a fresh enrich job for every live head whose AI annotations "
        "are stale.\n\n"
        "Run it when 'lode status' says the enrichment store's AI "
        "annotations disagree with the currently configured "
        "enrichment_llm. Targeted, not whole-corpus -- unlike 'lode "
        "reembed', since each re-enrich costs a cloud LLM call. Notes/"
        "externals marked no_egress are never swept in.\n\n"
        "Only queues the jobs -- run 'lode work' (or 'lode work --wait') to "
        "actually re-enrich. Safe to re-run. See "
        "docs/how-to/maintenance-commands.md."
    )
)
def reenrich(db: _DbOption = None) -> None:
    """Force a fresh enrich job for every live head whose annotations are stale (lode-14jr).

    The enrichment-LLM counterpart to ``lode reembed`` (``lode-g274.7``) --
    but scoped **targeted, not whole-corpus**, unlike that command.
    Re-enrichment costs a Claude API call per head (``lode reembed``'s embed
    leg is free, local ONNX inference), so blindly re-enriching every live
    head on every ``enrichment_llm`` config bump would be needlessly
    expensive; this only force-enqueues the heads that actually need it
    (``docs/storage.md#re-enriching-the-corpus-deliberately-targeted-lode-14jr``).

    **Stale means "disagrees with the currently configured `enrichment_llm` OR the
    currently active provider," detected by the exact same query ``lode status``'s hint
    reads** (:func:`lode.enrichment_view.stale_enrichment_heads`, shared with
    :func:`lode.cli.status._enrichment_model_stale`, lode-o9k3/lode-568v.6 -- so "status
    says clean" and "reenrich has work" cannot disagree). A live head -- every note's
    current ``head_version_id`` and every external's current ``head_snapshot_id``,
    mirroring :func:`lode.retrieval.live_head_versions`'s notes-UNION-externals scope --
    is force-enqueued only if it has at least one ``'ai'`` annotation
    (``source_version`` = the head id) whose ``model`` column differs from
    ``settings.enrichment_llm``, or whose ``provider`` column disagrees with the
    currently active provider (:func:`lode.llm_provider.provider_identity`), right now.
    Provider awareness closes the gap where the same model/deployment string means a
    different vendor across a provider switch -- the ``model`` comparison alone would
    miss that (lode-568v.6). A head with **no** ai annotations at all is not stale -- it
    is simply unenriched, which the passive reconciliation scan's ``enrich_gap`` step
    already covers on its own, ordinary schedule; duplicating that here would only
    re-enqueue work ``lode work`` was already going to do.

    **``no_egress`` content is never swept in, even if its stored annotations
    happen to be stale.** Unlike embedding, enrichment leaves the box (a
    Claude API call) -- ``no_egress`` exists precisely to keep a note or
    external's content from ever making that trip, so this command excludes
    it the same way ``reconcile``'s ``enrich_gap`` step does, rather than
    delegating to ``live_head_versions`` (which has no notion of
    ``no_egress`` -- it is scoped to retrieval, not egress).

    Reuses :func:`lode.jobs.enqueue_derive_jobs` (``types=("enrich",)`` only
    -- this never touches ``embed``) -- the same primitive every capture
    uses. The live-job partial unique index still dedupes a head that
    already has an enrich job in flight, so running this twice before the
    queue drains enqueues nothing extra the second time; a ``done`` job under
    a stale model -- the whole point here -- is not what that index guards
    against, so this still forces a fresh job past it, exactly as ``lode
    reembed`` does for embed.

    **Resumable by construction**, same as ``lode reembed``: the enqueue
    runs in one SQLite transaction, and draining is ``lode work``'s job, not
    this command's -- interrupting ``lode work`` mid-drain just leaves the
    rest pending, safe to resume with another ``lode work`` run.

    Once every enqueued job reaches ``done`` and rewrites its ``annotations``
    rows under the current model, ``lode status``'s enrichment hint clears on
    its own -- there is no separate manifest to reconcile, per the same
    decision the detection above reads.
    """
    from lode.jobs import enqueue_derive_jobs

    settings = cli._resolve_settings()
    conn = _open_db(db)
    try:
        stale = stale_enrichment_heads(
            conn, settings.enrichment_llm.model, cli.provider_identity(settings)
        )
        with conn:
            for version_id in stale:
                enqueue_derive_jobs(conn, version_id, types=("enrich",))
    finally:
        conn.close()

    if stale:
        typer.echo(
            f"enqueued {len(stale)} enrich job(s) for live head(s) whose "
            "annotations disagree with the current enrichment_llm -- run "
            "'lode work' (or 'lode work --wait') to run them."
        )
    else:
        typer.echo("no stale enrichment found -- nothing to re-enrich.")
