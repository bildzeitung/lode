"""lode command-line entry point.

A Typer app wired to the ``lode`` console-script (``lode --help`` lists the
subcommand surface). ``add`` (capture + save, lode-y42.1) saves a note and
enqueues its derive jobs; ``work`` (lode-i05.3) drains the async work queue
(chunk + embed + FTS via the registered ``embed`` handler); the operational
``status`` / ``jobs`` read-outs (lode-y42.3), and the ``egress`` audit read-out
(E8, lode-fk8.3) are real; ``purge`` (E8, lode-7cx) hard-deletes a note via
:meth:`lode.repository.Repository.purge`; ``notes`` (lode-1gr.1) lists every
live note's full id, date, and summary via :func:`lode.notes_read.list_notes`
-- the id source for ``purge`` -- or, with ``--deleted`` (lode-d32.2), lists
only tombstoned notes via the sibling reader
:func:`lode.notes_read.list_deleted_notes`; ``show`` (lode-1gr.5, brought to
CONTENT parity with the TUI inspector modal by lode-ay5.3) prints one note's
head body plus its full derived enrichment -- summary/tags/entities (stale-
flagged), inferred edges (now with reason+confidence, compact -- and, for an
edge that draws down a web link, its external-snapshot introspection
indented beneath, lode-8d2), a three-valued ``enrichment:`` line ({pending,
failed, ready}), and whether it is embedded -- via the shared
:mod:`lode.enrichment_view` seam (lode-ay5.1) also consumed by the TUI, so
on-demand CLI introspection cannot drift from the modal; sharing ``purge``'s
id/prefix resolution, and flagging a tombstoned head with a ``[deleted]``
marker (lode-d32.2) rather than rendering it as if live; ``ask``
(lode-y42.2) runs the cited Q&A loop
(retrieve → synthesize → faithfulness gate → cite or abstain); ``no-egress``
(lode-w0h.7) is the no-egress-tier control surface for a drawn-down external
source -- flips ``externals.no_egress`` via :func:`lode.externals.set_no_egress`
so it stays locally retrievable but is excluded from enrich/Q&A cloud egress
(``docs/externals.md`` "No-egress tier"); ``tui`` (E11,
lode-mkc.1) launches the Textual TUI shell on the instant capture screen;
``models pull`` (lode-og3, rebuilding the bounced lode-6qh) explicitly warms
the local ``fastembed`` weights cache (embedder + reranker/NLI cross-encoder)
so the ~500MB first-run download happens as a deliberate one-time setup step
rather than silently mid-capture, and turns its most likely failure mode --
no network, or a cold cache under ``HF_HUB_OFFLINE=1`` -- into an actionable
message instead of a raw traceback -- see ``docs/onboarding.md`` and
``docs/configuration.md`` ("Models").

The eval harness (``lode.eval.harness.score_golden_set``) is a maintainer/CI
integration test run via ``nox -s eval`` — it is **not** a shipped end-user
command (``docs/decisions.md``, the eval-harness entry, Shape A, lode-5y8.5).
"""

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import ValidationError

from lode import __version__, versions
from lode.config import (
    Settings,
    config_lines,
    config_path,
    default_db_path,
    lance_dir,
    load_settings,
    log_dir,
    model_cache_dir,
)
from lode.enrichment_view import EnrichmentItem, ExternalView, enrichment_view_conn
from lode.ids import SHORT_VERSION_ID_LENGTH, short_version_id
from lode.lock import LockHeld, WorkerLock
from lode.logconfig import configure_logging
from lode.lexical import LexicalCacheBackend
from lode.notes_read import list_deleted_notes, list_notes
from lode.repository import AmbiguousNoteIdError, CompositeCache, Repository
from lode.storage import init_db

if TYPE_CHECKING:
    # Type-only; the runtime imports live inside ``ask`` / ``_retrieve`` so the
    # capture-path commands (``add`` is "instant by design") never pay the cost of
    # loading the Q&A SDK (anthropic) or the vector stack (pyarrow), which the
    # cited Q&A loop pulls in but the rest of the CLI never touches.
    from lode.answer import Support
    from lode.cited_answer import CitedAnswer
    from lode.embedding import Embedder
    from lode.retrieval import ContextItem

app = typer.Typer(
    name="lode",
    help="AI-first personal knowledge base for things you learn at work.",
    no_args_is_help=True,
    add_completion=False,
)


#: Shared ``--debug`` option: raises the log level to DEBUG, which turns on every
#: DEBUG-gated diagnostic (e.g. ``lode.tui.latency_probe``'s event-loop-lag probe,
#: gated on ``log.isEnabledFor(logging.DEBUG)``) -- see main()'s docstring.
_DEBUG_OPTION = typer.Option(
    False,
    "--debug",
    help=(
        "Enable DEBUG-level logging, turning on DEBUG-gated diagnostic "
        "instrumentation (e.g. the event-loop-lag probe). Takes precedence "
        "over LODE_LOG_LEVEL when passed; unset, LODE_LOG_LEVEL (default "
        "INFO) still applies. See docs/configuration.md."
    ),
)


@app.callback()
def main(ctx: typer.Context, debug: bool = _DEBUG_OPTION) -> None:
    """lode — capture and retrieve what you learn at work."""
    # Group callback: keeps lode a multi-command app so ``--help`` lists the
    # subcommands. Configure logging once, here, so every subcommand (and the
    # Anthropic SDK) logs consistently (LODE_LOG_LEVEL / ANTHROPIC_LOG) and lands
    # in $LODE_HOME/logs/ (lode.config.log_dir, docs/configuration.md). ``--debug``
    # (lode-1i8.3) resolves to an explicit DEBUG level, which takes precedence
    # over the LODE_LOG_LEVEL env fallback (configure_logging's ``level=None``
    # path); without it, behavior is unchanged. The resolved flag is stashed on
    # ``ctx.obj`` so ``tui``'s file-only re-configure (lode-1i8.2) can preserve
    # it across that second ``configure_logging`` call.
    level = logging.DEBUG if debug else None
    configure_logging(level=level, log_dir=log_dir())
    ctx.obj = debug


def _open_db(db: Path | None) -> sqlite3.Connection:
    """Open the lode database (creating it if absent) with the schema applied.

    Resolves the path like ``add`` (the ``--db`` flag else the ``$LODE_HOME``
    default), ensures the parent directory exists, and returns an :func:`init_db`
    connection — so the read-out commands always see the ``jobs`` / ``egress_log``
    tables even on a first run.
    """
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return init_db(db_path)


#: Shared ``--db`` option for the db-backed commands — an explicit per-invocation
#: override of just the DB file; the default root is ``$LODE_HOME`` (lode.config).
_DB_OPTION = typer.Option(
    None,
    "--db",
    help="SQLite database path (default: $LODE_HOME/lode.db, i.e. ~/.lode/lode.db).",
)


def _write_draft(db_path: Path, note_id: str, body: str) -> Path:
    """Persist a CAS-rejected capture buffer beside the DB so it is never lost.

    Named uniquely (``mkstemp``) so a retry never clobbers an earlier draft; the
    interactive re-apply/discard surface waits for the TUI (E11). Returns the
    draft's path for the user-facing message.
    """
    fd, name = tempfile.mkstemp(
        prefix=f"{note_id}.", suffix=".draft", dir=db_path.parent
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    return Path(name)


def _resolve_settings() -> Settings:
    """Resolve settings for one command, reporting a bad config file the CLI way.

    :func:`lode.config.load_settings` raises on an unusable
    ``$LODE_HOME/config.toml`` — ``TOMLDecodeError`` for a syntax error,
    pydantic's ``ValidationError`` for an unknown key or an out-of-range value.
    Raising is right for a *library* caller (a test asserts on it), but this
    file is hand-edited by the user, so at the CLI boundary an uncaught raise
    dumps a Python traceback at the terminal over a typo. Convert it to the
    one-line stderr message + exit 1 that every other user-facing CLI failure
    here uses (lode-40g). This is the only place a lode command resolves
    settings; each entry point calls it once and threads the result down.
    """
    try:
        return load_settings()
    except (tomllib.TOMLDecodeError, ValidationError) as err:
        typer.echo(f"invalid config file {config_path()}: {err}", err=True)
        raise typer.Exit(code=1) from None


def _enrich_immediately(
    conn: sqlite3.Connection, db_path: Path, version_id: str, settings: Settings
) -> None:
    """Opportunistically claim + run the enrich job just enqueued for ``version_id``.

    ``Repository.save`` enqueues the ``enrich`` job atomically with the version
    write (same as ``embed``), so it exists as ``pending`` the instant the
    version is visible — there is no gap for reconcile's ``enrich_gap`` step to
    misdetect. This claims that specific job — scoped to ``version_id`` via
    :func:`lode.worker.claim_and_run_one`'s ``target_version`` filter, so a
    backlog of other pending enrich jobs (a burst of prior adds, an idle
    worker) can never cause this note's own job to be skipped in favor of an
    older one (lode-a3x) — using the exact claim/run primitives ``lode work``
    uses, and runs it inline so tags/entities/edges appear without waiting for
    the async worker (lode-npx.2 "interactive now" path).

    If a concurrent ``lode work`` wins the claim race instead, this is a
    harmless no-op: the note is enriched a moment later via the normal worker
    path.  A **transient** run failure is handled entirely by
    :func:`~lode.worker.run_one`'s own attempts/backoff/dead-letter accounting
    — never re-raised here, and never hand-rolled a second time in this
    module. A **permanent, user-actionable** failure
    (:class:`~lode.auth.AuthError`, lode-9yy) is different: ``run_one`` resets
    the job straight back to ``pending`` (uncharged) and re-raises it, but
    capture must stay instant regardless of whether Anthropic credentials are
    configured (``docs/design.md`` §1) — so it is caught and dropped here
    rather than surfaced on every single ``add``. The job is already back at
    ``pending``, uncharged, for the next explicit ``lode work`` to report
    loudly (``docs/storage.md`` "Transient vs. permanent job failures").
    """
    from lode.auth import AuthError
    from lode.worker import claim_and_run_one

    try:
        claim_and_run_one(
            conn, db_path, settings, types=("enrich",), target_version=version_id
        )
    except AuthError:
        logging.getLogger(__name__).debug(
            "immediate-enrich skipped — no Anthropic credentials configured; "
            "note saved, job left pending for a future 'lode work'"
        )


@app.command()
def add(
    text: str | None = typer.Argument(
        None, help="Note body. Omit to read the note verbatim from stdin."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Capture a note, enqueue its derive jobs, and fast-track enrichment.

    The save path (``docs/design.md`` / lode-npx.2):

    1. ``Repository.save`` writes the version and enqueues **both** the
       ``embed`` and ``enrich`` derive jobs atomically — the note is
       keyword-findable the moment the transaction commits (synchronous FTS5,
       lode-xyb), and the enrich job exists as ``pending`` from that same
       instant (no gap for reconcile's ``enrich_gap`` step to misdetect).
    2. :func:`_enrich_immediately` opportunistically claims and runs that
       enrich job inline (:func:`lode.worker.claim_and_run_one`) so the fresh
       note's tags / entities / inferred edges appear without waiting for the
       async worker (lode-npx.2 "interactive now" path). If it loses the claim
       race to a concurrent ``lode work``, or the run fails, the job's normal
       attempts/backoff/dead-letter accounting (:func:`lode.worker.run_one`)
       takes over — no separate re-enqueue path needed.
    3. Embedding runs asynchronously via ``lode work`` (lode-x6r.5) so the CLI
       returns quickly regardless of enrichment latency.

    The body comes from the ``TEXT`` argument or, if omitted, verbatim from
    stdin; an empty / whitespace-only body is refused.
    """
    db_path = db or default_db_path()
    body = text if text is not None else sys.stdin.read()
    if not body.strip():
        typer.echo("refusing to save an empty note", err=True)
        raise typer.Exit(code=1)

    settings = _resolve_settings()

    # A fresh logical id per capture — `add` always creates a new note (no
    # aliasing), so `save` always takes its create path.
    note_id = str(uuid.uuid4())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        # Inject the synchronous model-free cache: LexicalCacheBackend chunks the
        # body and writes passages + passages_fts right after the version commits,
        # so the note is keyword-findable BEFORE any async embedding runs
        # (lode-xyb; embedding stays async via the worker).
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            # settings resolved once for the whole command and threaded into BOTH
            # legs (lode-40g): save() runs redact_before_index() + the drawdown
            # scan off it, so without this the user's own
            # redact_before_index_patterns / url_tracking_param_blocklist would
            # be ignored and a secret they configured would reach the index
            # unredacted.
            result = repo.save(note_id, body, settings=settings)
        except versions.HeadConflictError:
            # A create against an already-present note: never clobber or
            # auto-merge — preserve the buffer as a draft and bail (the
            # interactive re-apply path lands with the TUI, E11).
            draft = _write_draft(db_path, note_id, body)
            typer.echo(f"note changed since opened; draft saved to {draft}", err=True)
            raise typer.Exit(code=1) from None

        # Opportunistic immediate enrichment — claim + run the enrich job
        # save() just enqueued so tags/entities/edges appear right away
        # (lode-npx.2 "interactive now" path). A lost claim race or a run
        # failure is handled by the job's own retry/backoff/dead-letter
        # accounting, not here.
        if not result.deduped:
            _enrich_immediately(conn, db_path, result.version_id, settings)
    finally:
        conn.close()
    typer.echo(note_id)


#: How an abstention reads at the terminal — the honest "no grounded answer"
#: failure mode (``docs/retrieval.md`` the faithfulness gate's abstention path),
#: printed when no claim survives the gate.
_ABSTAIN_LINE = (
    "No grounded answer: your notes don't support a cited claim for this question."
)


@app.command()
def ask(
    question: str = typer.Argument(
        ..., help="Your question, answered from your own notes with citations."
    ),
    think_harder: bool = typer.Option(
        False,
        "--think-harder",
        help="Use the higher-quality 'think harder' Q&A model (Claude Opus).",
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Answer a question from your notes — retrieve, synthesize, gate, then cite.

    Runs the read pipeline (lexical + dense search → RRF fusion → small-to-big
    parent expansion → trust-rank) to build a cited context, hands it to the Q&A
    loop (:func:`lode.cited_answer.ask`, which synthesizes structured claims and
    runs the faithfulness gate **before display**), and prints either the surviving
    cited claims — each with its ``version_id`` / ``snapshot_id`` plus the verbatim
    span it rests on — or an honest abstention when nothing is grounded. Any
    no_egress material that matched is surfaced as "present, withheld from cloud
    synthesis" rather than silently dropped.
    """
    # Imported here, not at module scope: cited_answer / auth pull in the Anthropic
    # SDK, which the instant capture path (``add``) must never load.
    from lode import cited_answer
    from lode.auth import AuthError

    db_path = db or default_db_path()
    # Resolve settings once so gate-tuning knobs (entailment_threshold, etc.) come
    # from a single configured object, not from per-call Settings() defaults buried
    # inside _retrieve and cited_answer.ask. _resolve_settings() (not bare
    # Settings()) so a config-file override actually reaches the pipeline
    # (lode-40g) -- previously this constructed a bare Settings(), so
    # load_settings() had zero production callers and every knob ran at
    # hardcoded defaults.
    settings = _resolve_settings()
    conn = _open_db(db_path)
    try:
        context = _retrieve(
            conn, question, lance_dir=lance_dir(db_path), settings=settings
        )
        answer = cited_answer.ask(
            conn, question, context, think_harder=think_harder, settings=settings
        )
        # Resolve each surviving citation's as-of provenance while conn is still
        # open (docs/externals.md "Every AI claim from an external must cite
        # 'as of fetched_at'") -- a note's is its version's write time, an
        # external's its snapshot's fetch time (:func:`_resolve_as_of`).
        as_of = {
            support.target_id: _resolve_as_of(conn, support)
            for claim in answer.claims
            for support in claim.support
        }
    except AuthError as err:
        # Fail gracefully on missing credentials: a clean, actionable line to the
        # user (no traceback) and the underlying cause to the log for debugging.
        # No exc_info -- the root logger mirrors to stderr, so dumping frames there
        # would re-introduce the very traceback we're suppressing for the user.
        logging.getLogger(__name__).error(
            "ask aborted — could not resolve Anthropic credentials: %s",
            err.__cause__ or err,
        )
        typer.echo(str(err), err=True)
        raise typer.Exit(code=1) from None
    finally:
        conn.close()
    for line in _format_cited_answer(answer, as_of):
        typer.echo(line)


def _retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    lance_dir: str | Path,
    embedder: "Embedder | None" = None,
    settings: Settings | None = None,
) -> "list[ContextItem]":
    """Build the trust-ranked Q&A context for ``question`` — the full read pipeline (E4).

    The full read side (``docs/retrieval.md`` "The v1 retrieval pipeline"): lexical
    search (FTS5/BM25, heads only) and the dense leg (cosine ANN over the LanceDB
    store under ``lance_dir``, the question embedded query-side via
    ``embedder.embed_query``) each capped at ``retrieval_top_k``, fused app-side
    (:func:`~lode.retrieval.reciprocal_rank_fusion`), re-scored by the toggleable
    cross-encoder stage (:func:`~lode.retrieval.rerank`, gated on
    ``Settings.rerank_enabled``), expanded small-to-big to each hit's parent block
    (:func:`~lode.retrieval.expand_parents`), traversed one graph hop from each seed
    note (:func:`~lode.retrieval.graph_expand`, GraphRAG), and finally ordered by the
    trust gradient (:func:`~lode.retrieval.trust_rank`). RRF scores a passage present
    in one leg from that leg alone, so a passage matched only by the dense leg still
    reaches the Q&A context (lode-bkc). A question with no word tokens skips the
    lexical leg, but the dense leg still runs.

    ``embedder`` defaults to the pinned local ONNX model
    (:class:`lode.embedding.FastEmbedEmbedder`); tests inject a stub so the gate
    stays offline.
    """
    # Imported here, not at module scope: retrieval pulls in the vector stack
    # (pyarrow via lode.vectorstore) and the embedder (fastembed), which the instant
    # capture path never loads.
    from lode.embedding import FastEmbedEmbedder
    from lode.retrieval import (
        build_match_query,
        expand_parents,
        graph_expand,
        lexical_search,
        reciprocal_rank_fusion,
        rerank,
        trust_rank,
        vector_search,
    )
    from lode.vectorstore import VectorStore

    settings = settings or Settings()
    match = build_match_query(question)
    lexical = lexical_search(conn, match, k=settings.retrieval_top_k) if match else []

    embedder = embedder or FastEmbedEmbedder(settings)
    query_vector = embedder.embed_query(question)
    store = VectorStore(lance_dir, settings)
    vector = vector_search(store, conn, query_vector, k=settings.retrieval_top_k)

    fused = reciprocal_rank_fusion(lexical, vector, k=settings.rrf_k)
    top = rerank(conn, question, fused[: settings.retrieval_top_k], settings=settings)
    expanded = expand_parents(conn, top)
    graphed = graph_expand(conn, expanded, settings=settings)
    return trust_rank(conn, graphed).context


def _format_cited_answer(
    answer: "CitedAnswer", as_of: dict[str, str | None]
) -> list[str]:
    """Render a gated answer for the terminal: cited claims, or an abstention.

    Each surviving claim prints its text followed by one indented citation line per
    support — its ``version_id`` / ``snapshot_id``, its resolved as-of provenance
    (``as_of``, keyed by :attr:`Support.target_id` — :func:`_resolve_as_of`), and the
    verbatim span. ``docs/externals.md`` ("Every AI claim from an external must cite
    'as of fetched_at'") is why this line is never omitted, note citation or
    external. When the answer abstained (no claim survived the gate) the honest
    abstention line is printed instead. Either way, any no_egress material is
    surfaced as "present, withheld from cloud synthesis" so the user knows relevant
    local content exists.
    """
    lines: list[str] = []
    if answer.abstained:
        lines.append(_ABSTAIN_LINE)
    else:
        for claim in answer.claims:
            lines.append(claim.text)
            lines.extend(
                _format_citation(support, as_of.get(support.target_id))
                for support in claim.support
            )
    for withheld in answer.withheld_citations:
        lines.append(f"  withheld {withheld.target_id}: {withheld.note}")
    return lines


def _format_citation(support: "Support", as_of: str | None) -> str:
    """Render one support as an indented ``<id-kind> <id>, as of <ts>  "<span>"`` line.

    ``as_of`` is ``None`` only for a target the store could not resolve
    (practically unreachable — the faithfulness gate already verified the span
    against the stored body — but rendered as ``"as of unknown"`` rather than
    assumed away).
    """
    if support.version_id is not None:
        target = f"version_id {support.version_id}"
    else:
        target = f"snapshot_id {support.snapshot_id}"
    provenance = f"{target}, as of {as_of}" if as_of else f"{target}, as of unknown"
    return f'  - {provenance}  "{support.quoted_span}"'


def _resolve_as_of(conn: sqlite3.Connection, support: "Support") -> str | None:
    """Resolve one citation's as-of provenance from the store.

    A note ``version_id``'s as-of is its write time (``versions.created``); an
    external ``snapshot_id``'s is its fetch time (``snapshots.fetched_at`` —
    ``docs/externals.md`` "Every AI claim from an external must cite 'as of
    fetched_at'"). Mirrors :func:`lode.tui.ask._resolve_as_of`, which the TUI ask
    screen uses for the same lookup. Returns ``None`` for a target absent from the
    store (practically unreachable — the faithfulness gate already verified the
    span against the stored body — but handled rather than assumed away).
    """
    if support.version_id is not None:
        row = conn.execute(
            "SELECT created FROM versions WHERE version_id = ?",
            (support.version_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT fetched_at FROM snapshots WHERE snapshot_id = ?",
            (support.snapshot_id,),
        ).fetchone()
    return row[0] if row is not None else None


@app.command()
def purge(
    target: str = typer.Argument(
        ..., help="Note id, or an unambiguous prefix of one, to hard-delete."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Hard-delete a note and its derived data (E8 hard delete, ``docs/externals.md``).

    The deliberate immutability break: overwrite every body in the note's version
    chain with a ``[purged YYYY-MM-DD]`` marker, stamp ``purged_at``, drop the
    chain's ``source='ai'`` annotations (keeping ``source='user'`` corrections), and
    cascade-evict the derived cache through the repository's cache seam. Delegates to
    :meth:`lode.repository.Repository.purge` — no half-delete. (The cache is a no-op
    :class:`~lode.repository.NullCache` until the engine wiring lands, lode-1f9.)

    ``target`` may be a full id or an unambiguous prefix of one (lode-1gr.3),
    resolved via :meth:`lode.repository.Repository.resolve_note_prefix` — see
    that method for exactly what a prefix is allowed to match.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn)
        try:
            note_id = repo.resolve_note_prefix(target)
            result = repo.purge(note_id)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            typer.echo(
                f"ambiguous note id prefix {target!r}: matches "
                + ", ".join(exc.candidates),
                err=True,
            )
            raise typer.Exit(code=1) from None
    finally:
        conn.close()
    typer.echo(
        f"purged {result.note_id}: swept {len(result.purged_versions)} version(s); "
        f"body now {result.marker_body}"
    )


@app.command()
def recover(
    target: str = typer.Argument(
        ..., help="Note id, or an unambiguous prefix of one, to recover."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Undo a soft-delete: repoint a tombstoned note's head past the tombstone.

    Reverses :func:`lode.versions.delete` (``lode-d32.3``), the recover leg of
    the epic ``lode-d32``: delete is a one-way trip to ``lode purge`` without
    this command.

    ``target`` may be a full id or an unambiguous prefix of one, resolved via
    :meth:`lode.repository.Repository.resolve_note_prefix` with
    ``include_deleted=True`` — the d32.2 land-review decision (option (a)):
    unlike ``purge``/``show``, which default that flag to ``False`` and so
    stay live-only, ``recover``'s only valid input is a tombstoned note, so a
    prefix here may also resolve one. Ambiguity is judged identically to the
    live-only path regardless of which state(s) match: a prefix matching one
    live and one deleted note is still :class:`~lode.repository.AmbiguousNoteIdError`
    — recover never silently prefers the tombstone — and unknown/ambiguous ids
    error exactly like ``purge``/``show``.

    A resolved note that is not currently tombstoned errors clearly rather
    than silently no-op'ing (there is nothing to recover). The recover target
    is the tombstone's own ``parent_version_id`` — by construction that IS the
    pre-delete head (:func:`lode.versions.delete` writes the tombstone with
    the live head as its parent, versions.py), so this is one column read, not
    a chain walk.

    Delegates to :meth:`lode.repository.Repository.recover`, NOT
    :func:`lode.versions.recover` directly — ``Repository.recover`` is the
    only path that applies :func:`~lode.redact.redact_before_index` before
    re-indexing the recovered head (lode-ibv), so a recovered secret-bearing
    note is not made keyword-findable again via the FTS/lexical cache leg.
    Uses the same write-path cache composite ``add``/capture/edit/reconcile
    already use (``CompositeCache([LexicalCacheBackend(conn)])``) — NOT the
    bare ``Repository(conn)`` -> ``NullCache`` ``purge`` builds, under which
    ``Repository.recover``'s ``cache.index()`` would be a silent no-op and the
    FTS row would never be restored.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn, cache=CompositeCache([LexicalCacheBackend(conn)]))
        try:
            note_id = repo.resolve_note_prefix(target, include_deleted=True)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            typer.echo(
                f"ambiguous note id prefix {target!r}: matches "
                + ", ".join(exc.candidates),
                err=True,
            )
            raise typer.Exit(code=1) from None

        row = conn.execute(
            "SELECT v.op, v.parent_version_id FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            # resolve_note_prefix returns a full id unchanged without checking
            # it exists (purge's/show's own contract) -- an unknown full id
            # lands here.
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1)
        op, parent_version_id = row
        if op != "delete":
            typer.echo(f"note is not deleted: {note_id}", err=True)
            raise typer.Exit(code=1)

        # Threaded for the same reason as `add`'s save (lode-40g): recover()
        # re-indexes the restored body through redact_before_index(), so a bare
        # Settings() here would silently ignore the user's own redaction patterns.
        result = repo.recover(
            note_id, target_version=parent_version_id, settings=_resolve_settings()
        )
    finally:
        conn.close()
    typer.echo(f"recovered {result.note_id}: head now {result.version_id}")


def _short_date(created: str) -> str:
    """Render a ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` timestamp as ``YYYY-MM-DD HH:MM``.

    Just drops the seconds/fractional precision and the ISO ``T``/``Z``
    markers for a shorter, still-sortable read-out; the full adaptive
    (relative) date format is Browse-only scope (lode-1gr.8), not this
    command's.
    """
    return created[:16].replace("T", " ")


@app.command(name="notes")
def notes_(
    deleted: bool = typer.Option(
        False,
        "--deleted",
        help="List only tombstoned (soft-deleted) notes, instead of live ones.",
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List notes -- live by default, tombstoned with ``--deleted``.

    One row per live note, newest first (:func:`lode.notes_read.list_notes`):
    the full ``note_id`` -- copy-pasteable straight into ``lode purge`` -- a
    short date, and its summary (the head's ``kind='summary'`` AI annotation,
    or the note's first line when not yet enriched). Tombstoned notes are
    excluded, same live-heads-only rule ``purge`` and the TUI browse screen
    already use.

    ``--deleted`` (lode-d32.2) flips that: it lists *only* tombstoned notes
    (via the sibling reader :func:`lode.notes_read.list_deleted_notes`) rather
    than overloading this command's live-only contract that browse/purge/
    retrieval/reconcile all depend on. A deleted note vanishes from both
    Browse and plain ``lode notes``, so this full-id listing is the only route
    back to an id a later ``lode show``/``lode recover`` can act on.
    """
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list_deleted_notes(db_path) if deleted else list_notes(db_path)
    if not rows:
        # Scope the empty message to what was actually asked for: a bare
        # "no notes" under --deleted reads as "you have no notes at all",
        # which is false whenever live notes exist (lode-d32.2).
        typer.echo("no deleted notes" if deleted else "no notes")
        return
    for row in rows:
        typer.echo(f"{row.note_id}  {_short_date(row.created)}  {row.summary}")


def _render_item(item: EnrichmentItem) -> str:
    """Render one :class:`~lode.enrichment_view.EnrichmentItem` for the CLI.

    The view-model carries ``stale`` as a bare boolean (lode-0qc); this is
    where the CLI's own ``" [stale]"`` suffix convention gets applied -- the
    TUI modal (lode-ay5.2) is free to style the same bit differently.
    """
    return f"{item.value} [stale]" if item.stale else item.value


def _render_items(items: list[EnrichmentItem]) -> str:
    """Render a list of items as a comma-joined line, or ``(none)`` when empty."""
    return ", ".join(_render_item(item) for item in items) if items else "(none)"


def _render_edge_detail(reason: str | None, confidence: float | None) -> str:
    """Render an edge's optional ``(reason, confidence)`` parenthetical.

    Both fields are nullable (``schema.sql``'s ``edges`` table) -- a
    user-curated (``source='user'``) edge may carry neither. Render whichever
    is present; an empty string when both are missing, so the line degrades to
    today's bare ``-> to_id`` rather than printing an empty ``()``.
    """
    parts = [reason] if reason else []
    if confidence is not None:
        parts.append(f"{confidence:.2f}")
    return f" ({', '.join(parts)})" if parts else ""


def _render_external(external: ExternalView) -> str:
    """Render one edge's :class:`~lode.enrichment_view.ExternalView`, indented (lode-8d2).

    Browse-time introspection for a drawn-down web link, printed directly
    beneath its edge's own ``-> to_id`` line -- the same view-model fields
    the TUI inspector modal (lode-ay5.2) renders, through the ONE seam
    (:mod:`lode.enrichment_view`) so this command holds no second copy of
    what an external's fields mean. ``state`` is always shown explicitly
    (``un-refreshed``/``stale``/``withheld``) rather than suppressed for the
    default case, so all three are equally visible/greppable in the output.
    """
    return (
        f"       {external.source_type} · snapshot "
        f"{short_version_id(external.snapshot_id)} · as of {external.fetched_at} "
        f"[{external.state}]"
    )


@app.command(name="show")
def show_(
    target: str = typer.Argument(
        ..., help="Note id, or an unambiguous prefix of one, to show."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Show a note's head body plus its derived enrichment (on-demand introspection).

    specs/03-tui-features.md item 2 (lode-1gr.5), brought to CONTENT parity
    with the TUI inspector modal (lode-ay5.2) by lode-ay5.3: the CLI surface
    for introspecting what enrichment has (or hasn't) landed on a note,
    without opening the TUI. Prints the head body, then renders
    :func:`~lode.enrichment_view.enrichment_view_conn` -- the ONE seam both
    this command and the TUI modal consume (lode-ay5.1), so neither re-derives
    the stale-display policy or the enrichment-state predicate. Every
    view-model field is surfaced: summary/tags/entities (stale-flagged),
    inferred edges (now WITH reason+confidence, compact -- e.g.
    ``-> to_id (reason, 0.82)[stale]``, a net-new field this command gained
    over the pre-ay5.3 CLI), embed status, and a three-valued
    ``enrichment:`` line ({pending, failed, ready}) that replaces the old
    ambiguous bare ``(none)`` -- an un-enriched note now reads
    ``enrichment: pending``, a dead-lettered one ``enrichment: failed``, both
    distinct from enriched-but-empty (``enrichment: ready``). Per-field
    ``(none)`` is unchanged for a genuinely empty section (content is never
    suppressed by state, lode-ay5.1's pinned predicate) -- ``enrichment:``
    and a field's own ``(none)`` are complementary, not substitutes.

    ``target`` may be a full id or an unambiguous prefix of one, resolved via
    :meth:`lode.repository.Repository.resolve_note_prefix` -- the exact
    resolver ``purge`` uses (lode-1gr.3), so an unknown or ambiguous id errors
    identically.

    A tombstoned note (lode-d32.2) is not filtered out here -- unlike
    ``resolve_note_prefix``, which only ever resolves a *prefix* to a live
    note, a full id still reaches a tombstone unchanged (same "full id always
    works" contract ``purge`` relies on, repository.py). Rather than render it
    as if live, the header carries a visible ``[deleted]`` marker (the same
    ``[stale]``-suffix convention :func:`_render_item` already uses for a
    flagged-not-hidden annotation) while still printing the carried-forward
    body -- useful context for deciding whether to ``lode recover`` it.
    """
    conn = _open_db(db)
    try:
        repo = Repository(conn)
        try:
            note_id = repo.resolve_note_prefix(target)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            typer.echo(
                f"ambiguous note id prefix {target!r}: matches "
                + ", ".join(exc.candidates),
                err=True,
            )
            raise typer.Exit(code=1) from None

        row = conn.execute(
            "SELECT v.created, v.body, v.op FROM notes n "
            "JOIN versions v ON v.version_id = n.head_version_id "
            "WHERE n.note_id = ?",
            (note_id,),
        ).fetchone()
        if row is None:
            # resolve_note_prefix returns a full id unchanged without checking
            # it exists (purge's own contract) -- an unknown full id lands here.
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1)
        created, body, op = row

        # The shared TUI+CLI seam (lode-ay5.1): this command no longer builds
        # its own display.py assembly. `conn` is already open and `note_id`
        # already resolved, so the conn-taking variant avoids a second
        # connection (lode-ay5.1's review note; enrichment_view_conn was
        # promoted public for exactly this caller).
        view = enrichment_view_conn(conn, note_id)
    finally:
        conn.close()
    assert view is not None  # the row fetch above already proved note_id exists

    deleted_marker = " [deleted]" if op == "delete" else ""
    typer.echo(f"note_id: {note_id}{deleted_marker}")
    typer.echo(f"created: {_short_date(created)}")
    typer.echo("")
    typer.echo(body)
    typer.echo("")

    typer.echo(f"enrichment: {view.enrichment_state}")

    summary = _render_item(view.summary) if view.summary else "(none)"
    typer.echo(f"summary: {summary}")

    typer.echo(f"tags: {_render_items(view.tags)}")
    typer.echo(f"entities: {_render_items(view.entities)}")

    if view.edges:
        typer.echo("edges:")
        for edge in view.edges:
            detail = _render_edge_detail(edge.reason, edge.confidence)
            flag = " [stale]" if edge.stale else ""
            typer.echo(f"  -> {edge.to_id}{detail}{flag}")
            if edge.external is not None:
                typer.echo(_render_external(edge.external))
    else:
        typer.echo("edges: (none)")

    embedded = "yes" if view.passage_count else "no"
    typer.echo(f"embedded: {embedded} ({view.passage_count} passage(s))")


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


@app.command()
def status(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show work-queue health: job counts, dead-letters, and an egress summary.

    Reads the ``jobs`` and ``egress_log`` tables (``docs/storage.md`` §8): the
    pending/running/done/failed/dead job counts, the dead-letter (``dead``) jobs
    with their last error, and how much content has left the box, by purpose.

    Status lifecycle: ``pending -> running -> done`` (success);
    ``running -> failed`` (transient error, retried); ``failed -> dead``
    (terminal dead-letter at max-attempts gate).
    """
    conn = _open_db(db)
    try:
        job_counts = dict(
            conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        )
        dead_letters = conn.execute(
            "SELECT id, type, target_version, last_error FROM jobs "
            "WHERE status = 'dead' ORDER BY id"
        ).fetchall()
        egress_counts = conn.execute(
            "SELECT purpose, COUNT(*) FROM egress_log GROUP BY purpose ORDER BY purpose"
        ).fetchall()
    finally:
        conn.close()

    typer.echo(
        "jobs: "
        f"{job_counts.get('pending', 0)} pending, "
        f"{job_counts.get('running', 0)} running, "
        f"{job_counts.get('done', 0)} done, "
        f"{job_counts.get('failed', 0)} failed, "
        f"{job_counts.get('dead', 0)} dead"
    )

    total_egress = sum(n for _, n in egress_counts)
    by_purpose = ", ".join(f"{purpose}: {n}" for purpose, n in egress_counts) or "none"
    typer.echo(f"egress: {total_egress} sends ({by_purpose})")

    typer.echo(f"dead-letters (dead jobs): {len(dead_letters)}")
    for job_id, job_type, target_version, last_error in dead_letters:
        typer.echo(
            f"  job {job_id} ({job_type}) target={_short(target_version)}: "
            f"{last_error or 'no error recorded'}"
        )


@app.command(name="jobs")
def jobs_(
    status: JobStatus | None = typer.Option(
        None, "--status", help="Only list jobs in this status (default: all)."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List the derive jobs on the work queue (``jobs`` table, ``docs/storage.md``).

    One row per job — id, type, status, attempts, target version — newest last;
    a failed job also shows its last error. ``--status`` narrows the list to a
    single queue state.
    """
    conn = _open_db(db)
    try:
        if status is None:
            rows = conn.execute(
                "SELECT id, type, status, attempts, target_version, last_error "
                "FROM jobs ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, type, status, attempts, target_version, last_error "
                "FROM jobs WHERE status = ? ORDER BY id",
                (status.value,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("no jobs")
        return

    for job_id, job_type, job_status, attempts, target_version, last_error in rows:
        line = (
            f"{job_id}  {job_type:<7} {job_status:<8} "
            f"attempts={attempts}  target={_short(target_version)}"
        )
        if last_error:
            line += f"  ! {last_error}"
        typer.echo(line)


@app.command()
def egress(
    purpose: EgressPurpose | None = typer.Option(
        None, "--purpose", help="Only list sends of this purpose (default: all)."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List what content has left the box for the cloud, and when (``egress_log``).

    The audit read-out over ``egress_log`` (``docs/externals.md`` "Egress log") —
    a straight answer to "what of mine has gone to the cloud, and when?". One row
    per cloud send, oldest first: id, ts, purpose, model, the version/passage ids
    sent, and which redactions were applied. ``--purpose`` narrows to ``enrich``
    or ``qa`` sends.
    """
    conn = _open_db(db)
    try:
        if purpose is None:
            rows = conn.execute(
                "SELECT id, ts, purpose, model, sent_targets, redactions "
                "FROM egress_log ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, purpose, model, sent_targets, redactions "
                "FROM egress_log WHERE purpose = ? ORDER BY id",
                (purpose.value,),
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("no egress")
        return

    for log_id, ts, log_purpose, model, sent_targets, redactions in rows:
        typer.echo(
            f"{log_id}  {ts}  {log_purpose:<7} {model:<20}  "
            f"sent: {_format_sent(sent_targets)}  "
            f"redactions: {_format_redactions(redactions)}"
        )


@app.command(name="no-egress")
def no_egress_(
    external_id: str = typer.Argument(
        ..., help="The external source's id (its canonical URL) to mark/clear."
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Clear no_egress instead of setting it (source becomes cloud-eligible again).",
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Mark (or ``--clear``) an external source no_egress (``docs/externals.md``).

    The no-egress-tier control surface (lode-w0h.7): a no_egress external is
    still captured, chunked, embedded, and locally retrievable (keyword +
    vector) — only cloud egress changes. It is excluded from both the
    enrichment send and the Q&A context, and any answer that would have cited
    it surfaces it instead as "present, withheld from cloud synthesis"
    (:data:`lode.egress.WITHHELD_CITATION`). The flag is read generically off
    the ``externals`` row by every send path (:mod:`lode.egress`,
    :mod:`lode.cited_answer`) — this command just flips it.

    ``external_id`` must already exist (e.g. drawn down via a note's pasted
    URL, ``docs/externals.md``); this command does not create sources.
    """
    conn = _open_db(db)
    try:
        from lode.externals import set_no_egress

        existed = set_no_egress(conn, external_id, no_egress=not clear)
    finally:
        conn.close()
    if not existed:
        typer.echo(f"no such external source: {external_id}", err=True)
        raise typer.Exit(code=1)
    state = "cleared" if clear else "marked"
    typer.echo(f"{state} no_egress: {external_id}")


@app.command()
def config(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show the resolved on-disk locations lode uses (``docs/configuration.md``).

    A read-out of the single-root layout under ``$LODE_HOME`` (default ``~/.lode``)
    so you can find, back up, or inspect lode's state: the root, the SQLite DB and
    its sibling lock, the LanceDB vector store, the model-weights cache, the log
    directory, and the optional ``config.toml`` (shown present/absent) — the same
    set ``docs/configuration.md`` "Paths & locations" documents. The rows come
    from the shared row-builder (:func:`lode.config.config_lines`) that the TUI's
    F2 diagnostics screen renders from too, so the two cannot drift (lode-u5gh);
    ``--db`` shifts the displayed DB (and its lock + co-located vector store) to
    an explicit override.
    """
    for line in config_lines(db or default_db_path()):
        typer.echo(line)


@app.command()
def tui(
    ctx: typer.Context,
    db: Path | None = _DB_OPTION,
) -> None:
    """Launch the Textual TUI (E11), starting on the instant capture screen.

    Deferred import — the rest of the CLI never pays Textual's import cost
    (same "heavy dep behind the command that needs it" convention as ``ask``'s
    Anthropic/vector imports). The capture screen's own save path
    (:mod:`lode.tui.capture`) has no AI call in it at all: only the
    synchronous version-write + FTS5 tier runs before it returns.

    Re-configures logging file-only (lode-1i8.2): the group callback already
    ran ``configure_logging(log_dir=...)`` with its default ``console=True``,
    which attaches a stderr handler that would otherwise dump log lines onto
    Textual's alternate-screen display, corrupting it. ``console=False`` here
    removes that stream handler while keeping the file handler, so records
    still land in ``$LODE_HOME/logs/lode.log`` for telemetry — plain commands
    (``ask``/``add``/...) are untouched since only this command passes it.

    Passes ``--debug`` (lode-1i8.3) through to this second ``configure_logging``
    call via ``ctx.obj`` (set by the group callback) so the DEBUG level survives
    the file-only re-configure: in the TUI, ``--debug`` raises verbosity in the
    log FILE only, since ``console=False`` here means the console was never
    reattached in the first place.
    """
    level = logging.DEBUG if ctx.obj else None
    configure_logging(level=level, log_dir=log_dir(), console=False)

    from lode.tui.app import run as run_tui

    # Resolved once here and threaded onto LodeApp (lode-40g) -- every screen
    # then reads it back via self.app.settings (lode.tui.app's single
    # resolve-once-and-share pattern), rather than each screen falling back to
    # its own bare Settings() default independently.
    run_tui(db_path=db or default_db_path(), settings=_resolve_settings())


models_app = typer.Typer(
    help="Manage the local fastembed model-weights cache.",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


#: fastembed's own catch-all failure message -- the final ``raise`` in
#: ``fastembed/common/model_management.py``'s ``download_model``, once every
#: source and retry is exhausted. It is the one stable signature left to key off
#: (see :func:`_warm`'s docstring for why fastembed leaves nothing more specific).
_FASTEMBED_EXHAUSTED_SOURCES = "from any source"


def _hf_hub_offline() -> bool:
    """Mirror fastembed's own ``HF_HUB_OFFLINE`` truthiness check.

    Read directly rather than imported -- fastembed does not expose this as a
    reusable helper (``fastembed/common/model_management.py:398-401`` inlines it)
    -- and must stay the same truthy set fastembed itself checks, or
    :func:`_warm`'s offline/cold-cache branch would misclassify a failure fastembed
    would not actually have treated as offline.
    """
    return os.environ.get("HF_HUB_OFFLINE", "").strip().upper() in {
        "1",
        "TRUE",
        "YES",
        "ON",
    }


def _warm(warm: Callable[[], None], model_id: str) -> None:
    """Run one wrapper's ``warm()``, translating a download failure into an
    actionable ``lode models pull`` message instead of a raw traceback (lode-96t).

    ``lode models pull`` exists precisely to make the first-run network
    dependency explicit, so its own most likely failure path -- no network, or
    ``HF_HUB_OFFLINE=1`` against a cold cache -- must not itself be a stack
    trace. Verified empirically against the installed fastembed/huggingface_hub,
    not just read from source, because fastembed's actual behavior collapses
    more than the source alone suggests:

    - **No network reachable at all** escapes fastembed uncaught, as an
      ``httpx`` transport-level exception (``httpx.TransportError`` --
      ``ConnectError``, ``TimeoutException``, ...): fastembed's retry loop
      (``download_model``) only catches ``(EnvironmentError,
      RepositoryNotFoundError, ValueError)``, none of which an ``httpx``
      transport failure subclasses, so it is never swallowed.
    - **``HF_HUB_OFFLINE=1`` against a cold cache** and a **genuine HTTP error**
      (rate-limited / 5xx) against a reachable network both collapse -- deep
      inside fastembed's retry loop -- into the exact same generic
      ``ValueError("Could not load model {id} from any source.")``: fastembed
      catches ``HfHubHTTPError`` / ``LocalEntryNotFoundError`` /
      ``RepositoryNotFoundError`` internally on every attempt and never
      re-raises or chains the original cause, so by the time this ``ValueError``
      reaches us there is no exception-side signal left to tell the two apart.
      The only reliable signal is one *we* already have before calling in:
      whether ``HF_HUB_OFFLINE`` was set (:func:`_hf_hub_offline`). If it was,
      fastembed forced ``local_files_only=True`` throughout (mirroring the same
      env var itself) and never attempts the network at all, so a failure here
      can only be the cold-cache case; if not, this is a genuine download
      failure after retrying every source. "Every source" is HuggingFace alone
      for lode's default models (their ``sources.url`` is ``None``, so no
      mirror is attempted); but for a config-overridden, GCS-mirrored model id
      (e.g. ``BAAI/bge-base-en-v1.5``) fastembed also falls back to *its own*
      GCS mirror (``storage.googleapis.com/qdrant-fastembed`` -- not a
      HuggingFace host), and swallows that leg's failure just as silently, in a
      bare ``except Exception``. Both legs therefore collapse into this one
      ``ValueError``, carrying no signal for which of them exhausted, so the
      message names both as possible causes rather than blaming HuggingFace
      alone (lode-4hy1).

    Anything else -- a different exception entirely, or a ``ValueError`` that
    doesn't carry fastembed's specific exhausted-sources signature -- propagates
    unchanged: a real defect must never read as a network problem.
    """
    import httpx

    try:
        warm()
    except httpx.TransportError as exc:
        typer.echo(
            f"could not reach HuggingFace to download {model_id}: {exc}\n"
            "No network route to huggingface.co -- connect to the network and "
            "retry 'lode models pull'.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        if _FASTEMBED_EXHAUSTED_SOURCES not in str(exc):
            raise  # not fastembed's download-failure signature -- a real bug
        if _hf_hub_offline():
            typer.echo(
                f"cache is cold for {model_id} and HF_HUB_OFFLINE=1 is set, so "
                "no download was attempted: run 'lode models pull' once without "
                "HF_HUB_OFFLINE to warm the cache, then the offline flag will "
                "work.",
                err=True,
            )
        else:
            typer.echo(
                f"failed to download {model_id} after retrying every "
                f"configured source: {exc}\nHuggingFace (and, for a model "
                "that has one, fastembed's GCS mirror) may be rate-limiting "
                "or unavailable -- check your connection and try again "
                "shortly.",
                err=True,
            )
        raise typer.Exit(code=1) from None


@models_app.command("pull")
def models_pull() -> None:
    """Warm the local model cache: download the resolved weights once, deliberately.

    lode-6qh: on a cold cache, the first embed call otherwise downloads ~500MB of
    ONNX weights from HuggingFace mid-capture -- a surprise phone-home rather than
    a one-time setup cost. This command forces that download now, up front, so a
    later ``lode work`` / ``lode ask`` never hits the network unexpectedly.

    Warms every ``fastembed``-loaded model named by your *resolved* settings
    (:func:`_resolve_settings`, so a ``$LODE_HOME/config.toml`` override of
    ``embedding_model`` / ``rerank_model`` / ``entailment_model`` is honored,
    lode-40g/lode-og3 -- not the pinned :class:`~lode.config.Settings` defaults)
    -- the embedder (``embedding_model``) and the reranker/NLI cross-encoder
    (``rerank_model`` / ``entailment_model``, ``docs/configuration.md`` "Models")
    -- reusing the same lazy-load wrappers the read/gate paths construct
    (:class:`lode.embedding.FastEmbedEmbedder`,
    :class:`lode.retrieval.FastEmbedCrossEncoder`,
    :class:`lode.faithfulness.FastEmbedEntailmentScorer`), so this pulls into
    the exact ``cache_dir`` (:func:`lode.config.model_cache_dir`,
    ``$LODE_HOME/models/``) production reads from -- never ``fastembed``'s own
    ``tempfile.gettempdir()`` default (lode-gmo). ``rerank_model`` and
    ``entailment_model`` default to the same pinned id (``BAAI/bge-reranker-base``,
    lode-txh.6): the second load is a same-model cache hit, so it is skipped
    rather than re-fetched, unless a config override has genuinely split them.

    Once warmed, every subsequent run is fully offline for indexing/retrieval; to
    force that even against a cold miss (an air-gapped run against an
    already-warm cache), set ``HF_HUB_OFFLINE=1`` -- fastembed's own
    ``local_files_only`` escape hatch (not a lode-specific flag).

    A bad ``config.toml`` gives the same clean stderr message + exit 1 every
    other command gives (:func:`_resolve_settings`), not a raw traceback.

    On its most likely failure path -- no network, ``HF_HUB_OFFLINE=1`` against a
    cold cache, or HuggingFace rate-limiting/erroring -- this exits non-zero with
    a clear, actionable message rather than a raw traceback (lode-96t); see
    :func:`_warm` for exactly which exceptions are mapped and why anything else
    still propagates as a real bug.
    """
    from lode.embedding import FastEmbedEmbedder
    from lode.faithfulness import FastEmbedEntailmentScorer
    from lode.retrieval import FastEmbedCrossEncoder

    # _resolve_settings() (not bare Settings()) so a config-file override of
    # embedding_model/rerank_model/entailment_model actually reaches this
    # command (lode-og3) -- otherwise 'models pull' warms the pinned defaults
    # while 'lode work'/'lode ask' (which DO resolve settings) still hit the
    # network mid-capture for the user's actual configured models, exactly the
    # surprise phone-home this command exists to prevent.
    settings = _resolve_settings()
    cache_dir = model_cache_dir()
    typer.echo(f"pulling model weights into {cache_dir} ...")

    typer.echo(f"  embedder: {settings.embedding_model}")
    _warm(FastEmbedEmbedder(settings).warm, settings.embedding_model)

    typer.echo(f"  reranker: {settings.rerank_model}")
    _warm(FastEmbedCrossEncoder(settings).warm, settings.rerank_model)

    # rerank_model and entailment_model default to the same pinned id (lode-txh.6),
    # so the second load would be a pure cache hit -- skip it, but still say so
    # rather than silently omitting the model from the report.
    same_as_reranker = settings.entailment_model == settings.rerank_model
    suffix = " (same model as reranker -- already cached)" if same_as_reranker else ""
    typer.echo(f"  entailment (NLI): {settings.entailment_model}{suffix}")
    if not same_as_reranker:
        _warm(FastEmbedEntailmentScorer(settings).warm, settings.entailment_model)

    typer.echo(f"done: model weights cached at {cache_dir}")


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)


def _outstanding_jobs(conn: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    """List jobs still ``pending``/``running`` -- for ``--wait``'s timeout report.

    Read fresh each poll tick so it reflects the latest drain pass, including
    batch-backed enrich jobs still ``running`` on an in-flight Batches API
    request (they are not a bug -- see ``work``'s ``--wait`` docstring).
    """
    return conn.execute(
        "SELECT id, type, status, target_version FROM jobs "
        "WHERE status IN ('pending', 'running') ORDER BY id"
    ).fetchall()


def _format_outstanding(jobs: list[tuple[int, str, str, str]]) -> str:
    """Render outstanding ``(id, type, status, target_version)`` rows for the CLI."""
    return ", ".join(
        f"{job_id} ({job_type} {status} target={_short(target_version)})"
        for job_id, job_type, status, target_version in jobs
    )


@app.command()
def work(
    db: Path | None = _DB_OPTION,
    loop: bool = typer.Option(
        False,
        "--loop",
        "--watch",
        help="Poll continuously (same as --watch); sleep --interval seconds between passes.",
    ),
    interval: float = typer.Option(
        5.0,
        "--interval",
        help="Polling interval in seconds (--loop / --watch / --wait).",
        min=0.1,
    ),
    wait: bool = typer.Option(
        False,
        "--wait",
        "--until-done",
        help=(
            "Block, polling every --interval seconds, until the queue is fully "
            "drained -- including collected Batches API enrich results -- or "
            "the bounded timeout (Settings.work_wait_timeout_s, "
            "docs/configuration.md) elapses, whichever comes first. On "
            "timeout, exits non-zero naming the still-pending/running jobs. "
            "Suits embed-heavy or small-batch cases; a large async enrich "
            "load can legitimately outlast the timeout (Batches API SLA up "
            "to 24h) -- that is expected, just re-run 'lode work' (or "
            "--wait again) to keep collecting. Mutually exclusive with "
            "--loop/--watch, which never exits on its own."
        ),
    ),
) -> None:
    """Drain the async work queue: claim → run → retry/dead-letter.

    ONE-SHOT by default: acquires the single-instance advisory lock
    (lode-i05.2), resets overdue failed jobs, then claims and runs ready
    pending jobs until none remain and exits.  ``--loop`` / ``--watch`` keeps
    the loop alive forever, sleeping ``--interval`` seconds between passes.
    ``--wait`` / ``--until-done`` instead polls only until the queue is fully
    drained or a bounded timeout fires (see the option help) -- so a caller
    doesn't have to re-run ``work`` by hand to see an async enrich batch land.

    ``embed`` jobs run synchronously in the main claim-run loop.  ``enrich``
    jobs are submitted to the Batches API in a pre-step ahead of that loop and
    collected on a later drain pass (lode-npx.2); ``refresh`` still has no
    handler and accumulates harmlessly until the connectors step arrives
    (lode-i05.3 scope fence).  A second ``lode work`` while one is already
    running is refused.

    Each pass prints a per-job outcome line for what it actually produced
    (lode-1gr.4) -- e.g. ``enriched <short-id>: 4 tags, 2 entities, 3 edges,
    summary set`` for a batch collected this pass, or ``embedded <short-id>: 3
    passages`` for an embed job -- ahead of the existing ``drained N job(s)``
    summary. A one-shot ``work`` right after capture only *submits* the enrich
    batch (nothing to collect yet), so the enrich line appears on a later pass
    (or under ``--wait``); a no-op pass still just prints ``drained 0
    job(s)``.
    """
    if wait and loop:
        typer.echo(
            "--wait and --loop/--watch are mutually exclusive "
            "(--wait already polls until drained or timeout)",
            err=True,
        )
        raise typer.Exit(code=1)

    from lode.auth import AuthError
    from lode.reconcile import reconcile as _reconcile
    from lode.worker import drain as _drain

    # _resolve_settings() (not bare Settings()) so a config-file override -- e.g.
    # refresh_ttl_s -- actually reaches reconcile()'s steps and the drain loop
    # (lode-40g; lode-09n threaded settings through reconcile(), but nothing was
    # flowing through it since this constructed a bare Settings() default).
    settings = _resolve_settings()
    db_path = db or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        try:
            with WorkerLock(db_path):
                try:
                    deadline = (
                        time.monotonic() + settings.work_wait_timeout_s
                        if wait
                        else None
                    )
                    while True:
                        # Reconciliation scan runs at startup (first pass) and
                        # periodically (each poll tick in --loop/--wait mode).
                        # Re-enqueues any head versions missing a fresh embed;
                        # idempotent by the live-job partial unique index
                        # (lode-i05.4). ``settings`` reaches every scan step —
                        # see reconcile.StepFn (lode-09n).
                        gap = _reconcile(conn, settings)
                        if gap:
                            typer.echo(f"reconciled {gap} gap version(s)")
                        # Per-job outcome lines (lode-1gr.4): what this pass's
                        # embed jobs and any enrich batch it collected actually
                        # produced, ahead of the existing job-count summary.
                        outcomes: list[str] = []
                        n = _drain(conn, db_path, settings, outcomes=outcomes)
                        for outcome in outcomes:
                            typer.echo(outcome)
                        typer.echo(f"drained {n} job(s)")

                        if wait:
                            outstanding = _outstanding_jobs(conn)
                            if not outstanding:
                                break
                            if time.monotonic() >= deadline:
                                typer.echo(
                                    "--wait timed out after "
                                    f"{settings.work_wait_timeout_s}s with "
                                    f"{len(outstanding)} job(s) still in "
                                    f"flight: {_format_outstanding(outstanding)}",
                                    err=True,
                                )
                                raise typer.Exit(code=1)
                            time.sleep(interval)
                            continue

                        if not loop:
                            break
                        time.sleep(interval)
                except KeyboardInterrupt:
                    typer.echo("worker interrupted", err=True)
                except AuthError as err:
                    # Permanent, user-actionable failure (lode-9yy): drain()
                    # surfaces it once the offending job is reset to 'pending'
                    # uncharged (docs/storage.md "Transient vs. permanent job
                    # failures"). Rendered exactly as `ask` does above — see that
                    # handler for why there is no exc_info.
                    logging.getLogger(__name__).error(
                        "work aborted — could not resolve Anthropic credentials: %s",
                        err.__cause__ or err,
                    )
                    typer.echo(str(err), err=True)
                    raise typer.Exit(code=1) from None
        except LockHeld as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    app()
