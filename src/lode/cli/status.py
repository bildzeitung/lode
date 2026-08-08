"""``lode status`` -- work-queue health, dead-letters, egress summary, action hints."""

import json
import logging
from enum import Enum
from pathlib import Path

# Printing here goes through ``cli.console``, NOT a plain
# ``from lode.cli import console`` -- this is the one module a test rebinds the
# name for (``tests/test_cli.py``'s
# ``test_status_dead_line_is_uniformly_danger_not_repr_highlighted``), and a
# plain import would freeze a reference the rebind can never reach, silently
# capturing nothing. Every other command module imports ``console`` plainly;
# see ``lode.cli``'s module docstring (lode-1bfn).
from lode import cli
from lode.cli import _DbOption, _open_db, _tabular_table, app
from lode.config import Settings, default_db_path, lance_dir
from lode.enrichment_view import stale_enrichment_heads
from lode.ids import SHORT_VERSION_ID_LENGTH, short_version_id
from lode.jobs_read import dead_letter_jobs, egress_purpose_counts, job_status_counts
from lode.reconcile import lexical_gap_count

log = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """The ``jobs.status`` enum from ``schema.sql`` — accepted by ``--status``.

    Lifecycle: ``pending -> running -> done`` (success); ``running -> failed``
    (transient error; worker resets to ``pending`` for retry); ``failed -> dead``
    (terminal: max-attempts gate). ``dead`` is the dead-letter terminal surfaced
    by ``lode status``; ``failed`` is the transient last-error state.
    """

    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    dead = "dead"


class EgressPurpose(str, Enum):
    """The ``egress_log.purpose`` enum from ``schema.sql`` — accepted by ``--purpose``."""

    enrich = "enrich"
    qa = "qa"


def _short(target_version: str) -> str:
    """Abbreviate a version-id digest for a one-line listing (full id is a hash).

    Delegates to the shared :func:`lode.ids.short_version_id` (lode-0bs), adding
    the ``…`` suffix that marks the id as truncated -- a listing-specific touch
    the bare log-line call sites elsewhere don't want.
    """
    return (
        target_version
        if len(target_version) <= SHORT_VERSION_ID_LENGTH
        else f"{short_version_id(target_version)}…"
    )


def _format_sent(sent_targets: str) -> str:
    """Render the JSON ``sent_targets`` array as shortened, comma-joined ids."""
    ids = json.loads(sent_targets)
    return ", ".join(_short(i) for i in ids) if ids else "(none)"


def _format_redactions(redactions: str | None) -> str:
    """Render the JSON ``redactions`` summary as ``id×count`` pairs (or ``none``).

    ``redactions`` is the per-target span count written by ``gate_qa_egress``
    (``{target_id: n}``), or ``NULL`` when nothing was stripped.
    """
    by_target = json.loads(redactions) if redactions else {}
    if not by_target:
        return "none"
    return ", ".join(f"{_short(t)}×{n}" for t, n in by_target.items())


def _cache_hit(hf_source: str, model_file: str) -> bool:
    """Is ``model_file`` inside ``hf_source`` actually cached and complete?

    Thin wrapper over ``huggingface_hub.try_to_load_from_cache`` -- the
    supported, network-free cache query -- against :func:`lode.config.
    model_cache_dir`. Deliberately NOT a ``Path.iterdir()``/dir-exists check:
    HuggingFace's downloader creates ``models--X/blobs/`` with an
    ``.incomplete`` file *before* a download finishes, so a dir-exists probe
    reads an INTERRUPTED ``lode models pull`` as warm (verified empirically --
    lode-l38d.6 review). ``try_to_load_from_cache`` resolves the actual
    ``refs/snapshots`` chain HuggingFace's own loaders use, so a partial
    download correctly returns ``None`` here, not a false warm.

    Reaching ``try_to_load_from_cache`` costs ~110ms warm and adds ~123 modules
    on top of what ``lode.cli`` has already imported, pulling in NONE of
    fastembed's onnxruntime/numpy graph -- the point of pinning cache identity
    in :data:`lode.config._MODEL_CACHE_IDENTITY` rather than resolving it via
    ``fastembed.list_supported_models()``. That constant's comment carries the
    benchmark and is the one authority for these figures; don't re-quote them.

    Measure this in PRODUCTION shape (call :func:`_cold_model_cache` after
    importing ``lode.cli``, diff ``sys.modules``) -- never as a bare ``import
    huggingface_hub``, which binds only huggingface_hub's LAZY module shell (19
    modules, ~11ms) and understates the real cost ~10x. Actually touching
    ``try_to_load_from_cache`` triggers the submodule import behind that shell.
    """
    from huggingface_hub import try_to_load_from_cache

    hit = try_to_load_from_cache(
        repo_id=hf_source, filename=model_file, cache_dir=str(cli.model_cache_dir())
    )
    return isinstance(hit, str)


def _model_cache_probe(model_name: str) -> bool | None:
    """Cheap filesystem check: is ``model_name``'s fastembed weights cache warm?

    Looks up ``model_name`` in :func:`lode.config.model_cache_identity` first
    -- lode's two pinned models (lode-txh.6) resolve this way with NO
    ``import fastembed`` at all (see :func:`_cache_hit`'s docstring for why
    that import is worth avoiding).

    A model id OUTSIDE the pinned set (a user's custom ``config.toml``
    override) falls back to fastembed's own registries. The embedder and the
    reranker/NLI cross-encoder ship SEPARATE supported-model lists, so both are
    searched and the first hit wins -- safe because the two lists are disjoint
    (verified: 30 embedding ids, 6 cross-encoder ids, no overlap), so "which
    list" carries no information the id itself doesn't. Searching both rather
    than being TOLD which to search is also the safer shape: a caller naming
    the wrong registry would probe the right id against the wrong list, get
    ``None``, and print a false all-clear -- decision 3's failure mode reached
    by a typo, exactly like the casing bug below. ``list_supported_models()``
    is a static in-memory list (no network, no model load), and reaching either
    costs the same single ``import fastembed`` (importing the cross-encoder
    submodule executes ``fastembed/__init__`` anyway) -- expected here, since
    an unpinned model is already off lode's fast path.

    Returns ``True`` if warm, ``False`` if confirmed cold, or ``None`` if the
    probe could not judge at all (unknown model id, a GCS-only source with no
    HuggingFace repo id to check, or any error). Never raises -- lode-l38d.6
    requires this probe to be non-fatal; callers treat ``None`` the same as
    "not cold" (no hint).
    """
    from lode.config import model_cache_identity

    try:
        identity = model_cache_identity(model_name)
        if identity is not None:
            hf_source, model_file = identity
            return _cache_hit(hf_source, model_file)

        from fastembed import TextEmbedding
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        # Case-INSENSITIVELY, matching fastembed's own resolution
        # (ModelManagement._get_model_description compares
        # model_name.lower()). An exact-match probe silently disagrees with the
        # loaders: a config.toml carrying "baai/bge-reranker-base" loads
        # everywhere else in lode but probes as None ("cannot judge") here, so
        # the cold hint could never fire for it -- decision 3's failure mode
        # ("an absent hint must not read as an absent check") reached by a
        # typo's worth of casing.
        entry = next(
            (
                m
                for registry in (TextEmbedding, TextCrossEncoder)
                for m in registry.list_supported_models()
                if m["model"].lower() == model_name.lower()
            ),
            None,
        )
        if entry is None:
            return None
        hf_source = entry["sources"].get("hf")
        model_file = entry.get("model_file")
        if not hf_source or not model_file:
            return None
        return _cache_hit(hf_source, model_file)
    except Exception:
        log.debug("_model_cache_probe: probe failed for %s", model_name, exc_info=True)
        return None


def _cold_model_cache(settings: Settings) -> bool:
    """True if ANY of the three resolved models' fastembed cache is cold.

    "Cold" is defined per lode-l38d.6's /challenge decision as ANY resolved
    model missing its cached weight file (:func:`_cache_hit`, via
    ``huggingface_hub.try_to_load_from_cache`` -- not a dir-exists-or-empty
    stat, which an INTERRUPTED download can satisfy, see :func:`_cache_hit`'s
    docstring) -- not a single stat on the cache root -- so a partial warm
    (embedder pulled, reranker/NLI not) still surfaces the hint rather than
    reading as fully warm.

    Dedupes the resolved ids before probing: ``rerank_model`` and
    ``entailment_model`` default to the same pinned id (lode-txh.6), so the
    common case is two filesystem probes, not three. A probe that could not
    judge (``None`` -- unknown id, GCS-only source, or any error) is treated as
    "not cold", per this function's non-fatal contract -- it can only ever make
    ``lode status`` print an extra hint line, never fail the command.
    """
    probes = {
        settings.embedding_model,
        settings.rerank_model,
        settings.entailment_model,
    }
    return any(_model_cache_probe(model_id) is False for model_id in probes)


def _model_revision_status(
    settings: Settings, lance_dir_path: Path | str
) -> tuple[bool, bool]:
    """``(mixed, drift)`` for the embedder's live per-vector ``model_revision`` record.

    Per ``lode-crh8.1``'s decision (``docs/storage.md``
    #model-provenance-the-embedder-revision-manifest-decided-lode-crh81) the
    "manifest" is the aggregate of the per-vector ``model_revision`` field
    already on every ``embeddings`` row (:meth:`lode.vectorstore.VectorStore.
    model_revisions`), not a separate committed artifact -- this reads that
    aggregate and compares it against a fresh live probe.

    - ``mixed`` — the live store currently holds more than one distinct
      ``model_revision`` for ``settings.embedding_model``: some passages were
      embedded under a different resolved revision than others (e.g. a
      mid-corpus cache eviction and re-pull). A pure LanceDB metadata scan, no
      network.
    - ``drift`` — a fresh ``huggingface_hub.model_info(repo).sha`` probe (the
      embedder's *currently* resolvable revision) disagrees with at least one
      recorded, non-``None`` revision. ``False`` if the probe could not judge
      (offline, rate-limited, an unpinned model id) or the index has never
      been embedded under this model -- an absent verdict must never read as
      "agrees" any more than ``_model_cache_probe``'s ``None`` reads as
      "warm" (lode-l38d.6's same non-fatal contract).

    Never raises: any failure (a corrupt LanceDB dir, an import error) is
    reported as ``(False, False)`` -- this can only ever add hint lines to
    ``lode status``, never fail the command.
    """
    try:
        from lode.embedding import resolve_model_revision
        from lode.vectorstore import VectorStore

        recorded = VectorStore(lance_dir_path, settings).model_revisions(
            settings.embedding_model
        )
        if not recorded:
            return False, False
        mixed = len(recorded) > 1
        current = resolve_model_revision(
            settings.embedding_model, timeout_s=settings.hf_probe_timeout_s
        )
        drift = current is not None and any(
            rev is not None and rev != current for rev in recorded
        )
        return mixed, drift
    except Exception:
        log.debug("_model_revision_status: probe failed", exc_info=True)
        return False, False


def _enrichment_model_stale(
    db: Path | None, enrichment_llm: str, current_provider: str | None
) -> bool:
    """Whether any live head's recorded AI annotation disagrees with `enrichment_llm` or `current_provider` right now (lode-o9k3/lode-568v.6).

    Supersedes the old 2+-distinct "mixed" check that used to live here
    (``COUNT(DISTINCT model) FROM annotations WHERE source = 'ai'``,
    lode-14jr) -- that check missed the primary intended workflow:
    deliberately bumping ``enrichment_llm`` on a corpus that was uniformly
    enriched under the OLD model leaves exactly ONE distinct stored model,
    so ``COUNT(DISTINCT model) > 1`` stayed ``False`` while ``lode reenrich``
    would in fact re-enqueue the entire corpus. See
    docs/configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745
    for the recorded decision to replace (not supplement) that check with
    this one.

    `current_provider` extends that same "identity, not just model string"
    principle to the LLM vendor (lode-568v.6): the same model/deployment
    string can mean different providers, so a provider switch alone -- with
    ``enrichment_llm`` held constant -- must also mark the corpus stale. Pass
    :func:`lode.llm_provider.provider_identity`'s return value here, never
    ``settings.llm_provider`` directly -- see :func:`~lode.enrichment_view.
    stale_enrichment_heads` for why.

    Reads :func:`lode.enrichment_view.stale_enrichment_heads` -- the
    identical, live-head-scoped query ``lode reenrich`` force-enqueues from
    -- and asks only "is that list non-empty." There is still no ``drift``
    counterpart: unlike the embedder, this code never probes for a fresher
    revision, so the recorded value is the only signal there is. See
    docs/configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745
    and the open question it raises (lode-sdjb).

    Opens its own connection (mirroring :func:`_model_revision_status`'s
    independent LanceDB read below) rather than reusing ``status``'s early,
    already-closed one -- this runs later, once ``settings`` is resolved.

    Never raises: any failure (a locked DB, an unexpectedly old schema, a
    failed connection open) is reported as ``False`` -- this can only ever
    add a hint line to ``lode status``, never fail the command, mirroring
    the other status-hint probes' non-fatal contract.
    """
    try:
        conn = _open_db(db)
        try:
            return bool(stale_enrichment_heads(conn, enrichment_llm, current_provider))
        finally:
            conn.close()
    except Exception:
        log.debug("_enrichment_model_stale: probe failed", exc_info=True)
        return False


def _lexical_gap_count(db: Path | None) -> int:
    """Count of live note heads with zero ``passages_fts`` rows right now (lode-cyly).

    Reads :func:`lode.reconcile.lexical_gap_count` -- the identical predicate
    the ``lexical_gap`` reconcile step heals from -- so this count can never
    disagree with what the next reconcile pass (worker startup, or the start
    of any ``--loop``/``--wait`` drain tick) is about to fix. It is a plain
    read only: unlike the reconcile step itself, this probe never calls
    :class:`~lode.lexical.LexicalCacheBackend` and never writes anything --
    ``lode status`` reports health, it does not perform repair.

    Opens its own connection, mirroring :func:`_enrichment_model_stale`'s
    independent-connection convention (this runs after ``status``'s own
    early connection has already closed).

    Never raises: any failure (a locked DB, an unexpectedly old schema, a
    failed connection open) is reported as ``0`` -- this can only ever add a
    hint line to ``lode status``, never fail the command, mirroring the
    other status-hint probes' non-fatal contract.
    """
    try:
        conn = _open_db(db)
        try:
            return lexical_gap_count(conn)
        finally:
            conn.close()
    except Exception:
        log.debug("_lexical_gap_count: probe failed", exc_info=True)
        return 0


@app.command()
def status(db: _DbOption = None) -> None:
    """Show work-queue health: job counts, dead-letters, an egress summary, and what needs your attention.

    Counts of jobs in each status (pending, running, done, failed, dead --
    the dead count highlighted when nonzero), the dead-letter jobs with
    their last error, and how much content has left the box, by purpose.

    Status lifecycle: pending -> running -> done (success); running ->
    failed (transient error, retried); failed -> dead (terminal, once
    retries are exhausted).

    Below that, an action-hint footer tells you what to do next: run
    "lode work" if anything is pending or still-retryable, run
    "lode models pull" if the local model cache is cold, flag the embedder's
    live vectors if their recorded model_revision is mixed or has drifted from
    what the cache currently resolves (lode-crh8.1), flag the enrichment
    store if any live head's AI annotations disagree with the currently
    configured enrichment_llm or the currently active provider
    (lode-14jr/lode-o9k3/lode-568v.6 -- no drift counterpart; see
    docs/configuration.md#model-provenance-the-enrichment-llm-decided-lode-g2745
    for why), flag any live note head missing its lexical (FTS5) index rows
    -- the same signal the ``lexical_gap`` reconcile step self-heals on its
    own schedule, surfaced here as a count so a user isn't waiting on that
    schedule blind (lode-cyly) -- or an explicit
    "No action needed." if none of those apply. Dead-letter jobs get no hint
    -- they are already listed above with their errors, and won't be
    retried.
    """
    db_path = db or default_db_path()
    conn = _open_db(db)
    try:
        job_counts = job_status_counts(conn)
        dead_letters = dead_letter_jobs(conn)
        egress_counts = egress_purpose_counts(conn)
    finally:
        conn.close()

    # dead_letters is the authority for "how many dead jobs", not
    # job_counts["dead"]: the two are the same number by construction (both read
    # the `jobs` table over one connection, one grouped by status, the other
    # filtered to status='dead'), so carrying both invited the table and the
    # prose line below to disagree with nothing but a comment promising they
    # can't. One value instead makes that structural.
    dead_style = "danger" if dead_letters else None

    # No header_style= here: rich's Table already defaults it to "table.header",
    # the name CLI_STYLES declares (see the palette comment above), so passing it
    # would restate rich's own default and read as a deliberate override.
    #
    # _tabular_table() (lode-9tmd) is the shared seam every CLI table with
    # column-semantic headers builds through, both for the markup-injection
    # guard (SafeTable.add_row) and for the one house box/header style --
    # cell values below are passed as bare str, same as before; SafeTable
    # wraps them in Text() structurally, so there is no per-cell wrapping to
    # remember here.
    table = _tabular_table()
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("Pending", str(job_counts.get("pending", 0)))
    table.add_row("Running", str(job_counts.get("running", 0)))
    table.add_row("Done", str(job_counts.get("done", 0)))
    table.add_row("Failed", str(job_counts.get("failed", 0)))
    table.add_row("Dead", str(len(dead_letters)), style=dead_style)
    cli.console.print(table)

    total_egress = sum(n for _, n in egress_counts)
    by_purpose = ", ".join(f"{purpose}: {n}" for purpose, n in egress_counts) or "none"
    # Two cli.console.print defaults must be turned OFF on every prose line below --
    # each is a behaviour change the typer.echo -> rich switch would otherwise
    # smuggle in, since the typer.echo these replaced printed plain, verbatim
    # text:
    #
    # markup=False -- cli.console.print parses "[...]" as rich markup, so a job's
    #   last_error is no longer safe to interpolate: "HTTP 500 [/v1/embed]
    #   failed" raises MarkupError and takes the whole command down (exit 1),
    #   and "[red]oops" is silently swallowed to "oops". Both land precisely
    #   where they hurt most -- `lode status` is what you run when jobs are
    #   already failing, and the dead-letter's error text is the payload you
    #   came for. Styling goes through `style=` instead, which needs no parsing.
    #
    # highlight=False -- rich runs ReprHighlighter over plain strings by
    #   default, re-styling numbers/paths from its own repr.* palette, which
    #   OVERRIDES the line's style= and defeats this ticket's headline
    #   requirement: with it on, "dead-letters (dead jobs): 3" renders the 3 in
    #   repr.number CYAN while the rest goes danger red -- the one character
    #   distinguishing 3 from 0 is the only one not red. repr.* is also
    #   undeclared colour arriving from rich's inherited defaults, cutting
    #   against lode-l38d.11's rule that colour comes from CLI_STYLES by
    #   semantic name. Same defect lode-re0s found in sibling .5; fixed at the
    #   CALL SITE here, per that ticket -- hoisting the flag onto the shared
    #   Console (lode.cli's own module namespace) is lode-re0s's call to make
    #   once .4/.6/.10 land,
    #   and taking it here would conflict with sibling branches live on this
    #   file. Table cells need none of this: rich runs no highlighter over them.
    cli.console.print(
        f"egress: {total_egress} sends ({by_purpose})", markup=False, highlight=False
    )

    cli.console.print(
        f"dead-letters (dead jobs): {len(dead_letters)}",
        style=dead_style,
        markup=False,
        highlight=False,
    )
    for job_id, job_type, target_version, last_error in dead_letters:
        cli.console.print(
            f"  job {job_id} ({job_type}) target={_short(target_version)}: "
            f"{last_error or 'no error recorded'}",
            style="danger",
            markup=False,
            highlight=False,
        )

    pending_or_failed = job_counts.get("pending", 0) + job_counts.get("failed", 0)
    # _resolve_settings() is the "I need valid settings to do my job, abort
    # otherwise" contract every OTHER command wants: on a malformed
    # $LODE_HOME/config.toml it echoes a message and raises typer.Exit(1)
    # (lode-40g). `lode status` reports QUEUE health, which needs no config at
    # all -- so calling it UNGUARDED made a single config.toml typo exit 1 after
    # the table had already printed, killing the footer outright. That is both a
    # regression against trunk (where status never read the config and exited 0)
    # and a direct breach of lode-l38d.6's "a probe error means no hint, never a
    # failed `lode status`" -- and it lands on decision 3's exact failure mode,
    # an absent hint read as an absent check. The guard inside
    # _model_cache_probe could never catch this: it sits BELOW settings
    # resolution. The stderr message _resolve_settings already emitted still
    # reaches the user, so a broken config stays visible -- it just no longer
    # takes the command down.
    #
    # The try covers ONLY the resolution, and _cold_model_cache stays OUTSIDE
    # it, deliberately: that function documents "Never raises" and
    # test_cold_model_cache_is_never_fatal pins it, so folding it in here would
    # swallow a future bug in its internal guard into a silently-wrong "No
    # action needed." and leave the contract unenforceable.
    #
    # `except Exception` rather than `except typer.Exit`, equally deliberately:
    # typer.Exit is only the raise _resolve_settings RAISES ITSELF: an
    # unreadable config.toml propagates PermissionError straight through it
    # (verified), so catching the narrow type would leave that case fatal --
    # the same bug in a smaller box.
    try:
        settings = cli._resolve_settings()
    except Exception:
        log.debug("status: _resolve_settings failed", exc_info=True)
        settings = None
    cache_cold = False if settings is None else cli._cold_model_cache(settings)
    # Same non-fatal contract as cache_cold above, and the same reason it stays
    # OUTSIDE the settings try: _model_revision_status documents "Never raises"
    # in its own right, so folding it into the settings guard would swallow a
    # future bug in ITS internal guard too.
    revision_mixed = False
    revision_drift = False
    if settings is not None:
        revision_mixed, revision_drift = _model_revision_status(
            settings, lance_dir(db_path)
        )
    # Same non-fatal contract, and the same "outside the settings try" reason
    # as cache_cold/revision_mixed above -- _enrichment_model_stale documents
    # "Never raises" in its own right (lode-o9k3).
    enrichment_stale = False
    if settings is not None:
        enrichment_stale = _enrichment_model_stale(
            db, settings.enrichment_llm.model, cli.provider_identity(settings)
        )
    # Independent of `settings` (the query needs none), unlike the three
    # probes above -- but still non-fatal (returns 0 on any failure) and run
    # in the same "outside any try, own connection" style, per lode-cyly.
    lexical_gaps = _lexical_gap_count(db)
    cli.console.print()
    # markup stays ON here -- these strings are author-written, not DB-derived,
    # so the [warn]/[ok] tags are the point. highlight stays OFF for the same
    # reason as above: the job count and the quoted 'lode work' would otherwise
    # pick up rich's undeclared repr.* colours mid-sentence.
    if pending_or_failed > 0:
        cli.console.print(
            f"[warn]Action needed:[/warn] {pending_or_failed} job(s) pending or "
            "failed -- run 'lode work' to drain the queue.",
            highlight=False,
        )
    if cache_cold:
        cli.console.print(
            "[warn]Action needed:[/warn] the local model cache is cold -- run "
            "'lode models pull' to warm it.",
            highlight=False,
        )
    if revision_mixed:
        cli.console.print(
            "[warn]Action needed:[/warn] the embedder's live vectors carry more "
            "than one model revision -- the index is mixed; re-embed to make it "
            "consistent again.",
            highlight=False,
        )
    if revision_drift:
        cli.console.print(
            "[warn]Action needed:[/warn] the embedder's cached weights have "
            "moved past the revision your vectors were embedded with -- "
            "re-embed to pick up the change.",
            highlight=False,
        )
    if enrichment_stale:
        cli.console.print(
            "[warn]Action needed:[/warn] the enrichment store's AI annotations "
            "disagree with the currently configured enrichment_llm -- run "
            "'lode reenrich' to make it consistent again.",
            highlight=False,
        )
    if lexical_gaps:
        cli.console.print(
            f"[warn]Action needed:[/warn] {lexical_gaps} live note head(s) "
            "have no lexical (FTS5) index rows -- run 'lode reindex-lexical' to "
            "make them keyword-findable now, or wait for 'lode work' to heal "
            "them automatically on its next reconcile pass.",
            highlight=False,
        )
    if (
        pending_or_failed == 0
        and not cache_cold
        and not revision_mixed
        and not revision_drift
        and not enrichment_stale
        and not lexical_gaps
    ):
        cli.console.print("[ok]No action needed.[/ok]", highlight=False)
