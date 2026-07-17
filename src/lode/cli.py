"""lode command-line entry point.

A Typer app wired to the lode console-script (lode --help lists the
subcommand surface). See docs/design.md for the save path, docs/storage.md
for the async work queue, docs/retrieval.md for the cited Q&A pipeline, and
docs/externals.md for external sources, no-egress, and hard delete.

The eval harness (lode.eval.harness.score_golden_set) is a maintainer/CI
integration test run via "nox -s eval" -- it is not a shipped end-user
command (see docs/decisions.md).
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
from typing import TYPE_CHECKING, NoReturn

import typer
from pydantic import ValidationError
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from lode import __version__, versions
from lode.config import (
    Settings,
    config_path,
    config_rows,
    default_db_path,
    knob_rows,
    lance_dir,
    load_settings,
    log_dir,
    model_cache_dir,
    model_cache_identity,
)
from lode.enrichment_view import (
    EnrichmentItem,
    EnrichmentView,
    ExternalView,
    enrichment_view_conn,
)
from lode.ids import SHORT_VERSION_ID_LENGTH, short_version_id
from lode.lock import LockHeld, WorkerLock
from lode.logconfig import configure_logging
from lode.lexical import LexicalCacheBackend
from lode.notes_read import (
    candidate_rows_conn,
    list_deleted_notes,
    list_notes,
    list_notes_conn,
)
from lode.repository import AmbiguousNoteIdError, CompositeCache, Repository
from lode.storage import init_db
from lode.timestamps import parse_stamp

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

#: The one shared rich Theme for the whole CLI (lode-l38d.11) — style names
#: are SEMANTIC, not colours, so a command writes e.g. ``style="note_id"``
#: and never a colour literal. Covers exactly what the four colour/table
#: consumer tickets need and no more:
#:   * ``note_id`` / ``date`` — lode-l38d.5 ("lode notes"'s id + date
#:     columns), matched by lode-l38d.10's ambiguous-prefix candidate
#:     listing so the two look like the same format.
#:   * ``warn`` / ``danger`` / ``ok`` — lode-l38d.6 ("lode status": a
#:     dead-letter count > 0 renders ``danger``, action hints render
#:     ``warn``, the explicit all-clear line renders ``ok``).
#:   * ``table.header`` — lode-l38d.4 ("lode config"'s rich ``Table``
#:     column headers).
#: This ticket does not restyle any command — nothing prints through these
#: names yet, so defining the theme is a no-op at the user-visible level;
#: the sibling tickets are what actually consume it.
#:
#: Declared as a plain dict, and NOT inlined into the ``Theme(...)`` call
#: below, because ``Theme.__init__`` DESTROYS the declaration: it does
#: ``self.styles = DEFAULT_STYLES.copy()`` (``inherit=True`` is the default)
#: and then ``.update()``s these on top. Any name whose value equals rich's
#: own default is therefore indistinguishable, on the constructed ``Theme``,
#: from a name that was never declared at all — see ``table.header`` below.
#: Keeping the declaration reachable is what lets tests/test_cli_theme.py
#: assert this palette rather than rich's.
CLI_STYLES: dict[str, str] = {
    "note_id": "cyan",
    "date": "dim",
    "warn": "yellow",
    "danger": "bold red",
    "ok": "bold green",
    # NOTE: a deliberate RESTATEMENT of rich's own default —
    # rich.default_styles.DEFAULT_STYLES["table.header"] is already "bold",
    # and rich's Table already defaults header_style="table.header", so
    # lode-l38d.4 would render bold headers even if this line were deleted.
    # Declared anyway so the palette has ONE source of truth: lode-l38d.4's
    # builder works in an isolated parallel worktree and cannot ask what the
    # header style is — it reads this dict. Were the name absent here it would
    # invent its own literal, which is the exact coordination failure this
    # ticket was split out of lode-l38d.1 to prevent. The cost of the
    # redundancy: no assertion against the constructed Theme/Console can prove
    # this entry exists (the inherited value is identical), so
    # tests/test_cli_theme.py pins it against CLI_STYLES — the reason this
    # dict is named rather than inlined above.
    "table.header": "bold",
}

#: The shared ``Theme`` built from :data:`CLI_STYLES` — ``inherit=True`` (the
#: default) is deliberate: rich's own ~150 defaults (``repr.*``, ``progress.*``,
#: traceback and pretty-printing styles) must keep working underneath ours.
CLI_THEME = Theme(CLI_STYLES)

#: The one shared rich Console for the whole CLI (lode-l38d.1) — every
#: colour/width-aware command renders through this, never a per-command
#: ``Console()``, so colour is decided once per process rather than
#: hand-rolled per command. Deliberately **no test seam** (no
#: ``force_terminal``, no accessor to monkeypatch) — see docs/stack.md.
#:
#: BEWARE, if you are writing the sibling colour tickets' tests: ``Console()``
#: freezes BOTH its TTY check and its ``NO_COLOR`` read at CONSTRUCTION, which
#: at module scope means **import** time. That is correct for real use (piping
#: ``lode notes | cat`` replaces stdout before this module is imported), but it
#: has two non-obvious consequences under test:
#:
#: * Colour is off under ``CliRunner`` because *pytest's default capture* had
#:   already replaced stdout by import time — NOT because CliRunner's output is
#:   not a TTY. Swapping stdout afterwards cannot change the frozen decision.
#:   Under ``pytest -s`` from a real terminal the decision freezes the other
#:   way and ANSI leaks into captured output, failing such assertions.
#: * ``monkeypatch.setenv("NO_COLOR", "1")`` after import is a **no-op** — it
#:   is read too late, so the assertion passes without exercising anything.
#:   Assert the ``NO_COLOR`` path in a **subprocess** with ``NO_COLOR=1`` in
#:   its env, which re-imports and so re-detects (verified in lode-l38d.1).
#:
#: Attached to a shared ``Theme`` (lode-l38d.11) so every colour-rendering
#: command below references a semantic style NAME (e.g. ``style="note_id"``)
#: rather than a colour literal. Split out from lode-l38d.1 because
#: lode-l38d.4/.5/.6/.10 all depend only on that ticket and so reach the
#: ready frontier together — /code fans them out as four PARALLEL producers
#: in isolated worktrees that cannot coordinate a palette with each other.
#: Deciding it once, here, removes the need for that coordination (see the
#: lode-l38d epic's /challenge finding). See ``CLI_STYLES`` above for the
#: style names and what each sibling ticket uses them for.
#:
#: ``highlight=False`` (lode-re0s) is process-wide colour POLICY, hoisted here
#: rather than left per-call-site: rich's Console applies its ReprHighlighter
#: to every plain string BY DEFAULT, injecting ``repr.*`` styles that are NOT
#: in ``CLI_STYLES`` and so bypass the theme entirely. Verified against rich
#: 15.0.0 (lode-l38d.5's technical review): a rendered date like
#: "2026-07-16 14:32" gets shredded into bold-cyan numerals + dim dashes + a
#: bold-GREEN time -- neither uniformly ``date``-styled nor distinct from
#: ``note_id``'s cyan -- and any number/IP/UUID/True/None inside a user's own
#: note text gets silently recoloured too. Every current consumer
#: (``notes_``) wants the highlighter off; none wants it on, and rich Tables
#: never run it regardless (verified -- ``lode config``'s Table is
#: unaffected), so there is no blast radius from centralising this. rich
#: still honours a per-call ``highlight=True`` if a future renderer ever
#: genuinely wants the highlighter, so nothing is foreclosed.
#:
#: IF A SECOND ``Console`` IS EVER ADDED to this module (e.g. a stderr twin
#: for error-path rendering) -- it MUST also pass ``highlight=False``. This is
#: process-wide policy, not a property of this one instance; a second Console
#: constructed without it silently reopens the exact defect this hoist closes.
#: rich exposes no public accessor for this flag -- only the private
#: ``Console._highlight`` -- so an assertion pinning it must use that
#: attribute (see ``tests/test_cli_console.py``'s ``test_console_highlight_is_disabled``),
#: the same way ``tests/test_cli_theme.py`` pins ``CLI_STYLES`` against the
#: private declaration rather than the merged-with-defaults ``Theme``.
#:
#: ``soft_wrap`` is NOT hoisted alongside this -- it is genuinely per-renderer
#: (``notes_`` wants no wrap; ``lode config``'s Table wants width-aware
#: wrapping), so it stays a per-call-site kwarg.
console = Console(theme=CLI_THEME, highlight=False)

#: A STDERR twin of ``console`` above (lode-l810) -- same theme, same
#: colour/width auto-detection rules, but writing to stderr rather than
#: stdout. Exists because :func:`_report_ambiguous_prefix` has a
#: stderr + exit-1 contract that every one of its four call sites
#: (``purge``/``recover``/``show``/``dump-html``) already depends on
#: (lode-l38d.10); reusing the stdout ``console`` there would silently move
#: that output onto stdout while colouring it. ``Console.file`` re-resolves
#: ``sys.stderr`` on every ``print()`` call (it is a property, not frozen at
#: construction) rather than the TTY/``NO_COLOR`` detection above it, so this
#: still captures correctly under ``CliRunner``'s per-invocation stderr
#: redirection, the same way ``typer.echo(err=True)`` already does.
err_console = Console(theme=CLI_THEME, stderr=True)


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

    The save path (see docs/design.md):

    1. The note is saved and both its embed and enrich jobs are enqueued
       atomically -- it is keyword-findable the moment the save commits.
    2. The enrich job is opportunistically run immediately, so the fresh
       note's tags, entities, and inferred edges usually appear right away.
       If that immediate attempt loses to a concurrent "lode work" or
       fails outright, the job's normal retry/backoff accounting picks it
       up later -- no separate re-enqueue path needed.
    3. Embedding always runs asynchronously via "lode work", so this
       command returns quickly regardless of enrichment latency.

    The body comes from the TEXT argument, or -- if omitted -- is read
    verbatim from stdin. An empty or whitespace-only body is refused.
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
    """Answer a question from your notes -- retrieve, synthesize, gate, then cite.

    Runs the read pipeline (lexical + dense search, fused and re-ranked) to
    build a cited context, synthesizes structured claims from it, and checks
    each one against your notes before showing anything. Prints either the
    surviving cited claims -- each with its source and the verbatim span it
    rests on -- or an honest abstention when nothing is grounded. Any
    no_egress material that matched is surfaced as "present, withheld from
    cloud synthesis" rather than silently dropped.
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
    """Hard-delete a note and its derived data (see docs/externals.md).

    The deliberate immutability break: every body in the note's version
    chain is overwritten with a "\\[purged YYYY-MM-DD]" marker, the purge is
    stamped, AI-generated annotations are dropped (your own corrections are
    kept), and derived cache entries are evicted. There is no half-delete.

    TARGET may be a full note id or an unambiguous prefix of one.
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
            _report_ambiguous_prefix(conn, target, exc)
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

    A soft-deleted note is otherwise a one-way trip to "lode purge".

    TARGET may be a full id or an unambiguous prefix of one. Unlike
    "purge"/"show", which only ever resolve a prefix to a live note, a
    prefix here may also resolve to a tombstoned note, since that is the
    only valid input. A prefix matching more than one note (live or
    deleted) is still ambiguous, and unknown ids error the same way
    "purge"/"show" do.

    A resolved note that is not currently tombstoned errors clearly rather
    than silently doing nothing.
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
            _report_ambiguous_prefix(conn, target, exc)

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
    """Render a stored UTC ``created`` timestamp as ``YYYY-MM-DD HH:MM`` local time.

    ``created`` is parsed via the shared :func:`lode.timestamps.parse_stamp`
    (:mod:`lode.worker`/:mod:`lode.versions` write the UTC stamp it expects),
    then converted to system local time with ``.astimezone()`` (no argument,
    lode-olmi.5) before dropping the seconds/fractional precision --
    stored/parsed values stay UTC, only the human-facing render changes. The
    full adaptive (relative) date format is Browse-only scope (lode-1gr.8),
    not this command's.
    """
    return parse_stamp(created).astimezone().strftime("%Y-%m-%d %H:%M")


def _report_ambiguous_prefix(
    conn: sqlite3.Connection, target: str, exc: AmbiguousNoteIdError
) -> NoReturn:
    """Render an ambiguous note-id prefix's candidates, then exit 1 (lode-l38d.10).

    The one shared body for the four call sites that resolve a note-id prefix
    (``purge``/``recover``/``show``/``dump-html``) and can raise
    :class:`AmbiguousNoteIdError`: each candidate gets a full listing row --
    id, date, summary, same columns as ``lode notes`` (:func:`notes_`) -- so
    the error is self-sufficient, no second command needed to tell the
    candidates apart.

    ``recover``'s ``include_deleted=True`` resolution can raise this across a
    live AND a tombstoned candidate together (repository.py) -- the tombstoned
    one is flagged `` [deleted]`` (the same trailing-marker convention
    ``show`` uses for a tombstoned head) rather than left to look identical to
    a live match, since for ``recover`` specifically the tombstoned candidate
    is the one the user actually wants.

    Still stderr, still exit code 1 -- the contract every call site already
    had; only the rendering gained the extra columns.

    Rows render through ``err_console`` -- a stderr twin of the shared
    ``console`` (lode-l810) -- with the same theme style NAMES, escaping,
    and ``highlight=False``/``soft_wrap=True`` flags ``notes_`` uses, so the
    two listings' shared columns (id, date) now look identical rather than
    one being coloured and the other bare ``typer.echo``.
    """
    typer.echo(
        f"ambiguous note id prefix {target!r}: {len(exc.candidates)} matches",
        err=True,
    )
    for row in candidate_rows_conn(conn, exc.candidates):
        marker = " [deleted]" if row.deleted else ""
        # Deliberately the same rendering path as notes_ (lode-l38d.5,
        # lode-l810): the shared theme's note_id/date style NAMES (never a
        # colour literal -- CLI_STYLES stays the one source of truth), the
        # summary escape()d, and the same two rendering flags. The rationale
        # for each flag lives at notes_'s loop and is deliberately NOT
        # restated here -- both pin rich-version-specific behaviour, and two
        # copies would drift apart (the same call notes_'s own tests make).
        #
        # The " [deleted]" tombstone marker (lode-l38d.10) stays a literal,
        # uncoloured suffix -- but it must be escape()d ALONG WITH the
        # summary, not appended after it. "[deleted]" is otherwise parsed as
        # a style tag by rich's markup engine: "deleted" is not a valid style
        # (Style.parse raises StyleSyntaxError on it), yet Console.print does
        # NOT raise -- it resolves the unknown tag to a null style and
        # CONSUMES it, so the marker renders as nothing at all and the
        # tombstone silently vanishes, which is precisely what this ticket's
        # acceptance forbids. Only the tag itself is swallowed; text after it
        # survives unharmed (verified against rich 15.0.0). Guarded, not just
        # asserted by eye: tests/test_cli.py's recover-ambiguous cases fail
        # if this marker is ever left unescaped.
        err_console.print(
            f"  [note_id]{row.note_id}[/note_id]  "
            f"[date]{_short_date(row.created)}[/date]  "
            f"{escape(row.summary + marker)}",
            soft_wrap=True,
            highlight=False,
        )
    raise typer.Exit(code=1) from None


@app.command(name="notes")
def notes_(
    deleted: bool = typer.Option(
        False,
        "--deleted",
        help="List only tombstoned (soft-deleted) notes, instead of live ones.",
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List notes -- live by default, tombstoned with --deleted.

    One row per note, newest first: its full id (copy-pasteable straight
    into "lode purge"), a short date, and its summary (the AI-generated one
    once available, otherwise the note's first line). The full id is never
    shortened here -- this is the copy-pasteable, greppable listing that
    Browse and "lode show" deliberately aren't. A blank line separates each
    note from the next.

    --deleted flips that: it lists only tombstoned notes instead of live
    ones. A deleted note vanishes from a plain listing, so this is the only
    route back to an id a later "lode show" or "lode recover" can act on.
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
    for i, row in enumerate(rows):
        if i:
            console.print()
        # ``row.summary`` is unescaped user/AI text and may itself contain
        # "[...]" (markdown links, code, etc.) -- escape it so it can never
        # be mistaken for markup and corrupt the row or the styles around it.
        # ``soft_wrap=True`` -- rich's Console otherwise word-wraps to its
        # detected width (80 columns when not a terminal), which would
        # silently break a long summary across lines; the prior
        # ``typer.echo`` never did that ("no truncation, no width clamp" is
        # this ticket's own description of the behaviour being preserved).
        # This is genuinely per-renderer (unlike ``highlight``, hoisted onto
        # the shared ``console`` itself, lode-re0s) -- ``lode config``'s
        # Table wants width-aware wrapping instead.
        #
        # The shared ``console`` is constructed with ``highlight=False``
        # (see its docstring above) precisely so this row never needs the
        # flag here: rich's ReprHighlighter would otherwise shred the date
        # and recolour numbers/IPs/etc. inside the user's own summary text.
        # The theme styles are the ONLY colour this row should carry.
        console.print(
            f"[note_id]{row.note_id}[/note_id]  "
            f"[date]{_short_date(row.created)}[/date]  {escape(row.summary)}",
            soft_wrap=True,
        )


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

    Prints the head body, then every enrichment field: summary, tags, and
    entities (flagged if stale), inferred edges with their reason and
    confidence (e.g. "-> to_id (reason, 0.82) \\[stale]"), whether the note
    is embedded, and an overall enrichment status of pending, failed, or
    ready. A field that is genuinely empty still shows "(none)" -- that is
    independent of the overall status line.

    TARGET may be a full id or an unambiguous prefix of one, resolved the
    same way "purge" resolves one, so an unknown or ambiguous id errors
    identically.

    A tombstoned note is not filtered out here -- unlike a prefix, which
    never resolves to one, a full id still reaches it. Rather than render it
    as if live, the header carries a visible "\\[deleted]" marker while
    still printing the carried-forward body -- useful context for deciding
    whether to "lode recover" it.
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
            _report_ambiguous_prefix(conn, target, exc)

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
        repo_id=hf_source, filename=model_file, cache_dir=str(model_cache_dir())
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


@app.command()
def status(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show work-queue health: job counts, dead-letters, an egress summary, and what needs your attention.

    Counts of jobs in each status (pending, running, done, failed, dead --
    the dead count highlighted when nonzero), the dead-letter jobs with
    their last error, and how much content has left the box, by purpose.

    Status lifecycle: pending -> running -> done (success); running ->
    failed (transient error, retried); failed -> dead (terminal, once
    retries are exhausted).

    Below that, an action-hint footer tells you what to do next: run
    "lode work" if anything is pending or still-retryable, run
    "lode models pull" if the local model cache is cold, or an explicit
    "No action needed." if neither applies. Dead-letter jobs get no hint --
    they are already listed above with their errors, and won't be retried.
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
    table = Table()
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("Pending", str(job_counts.get("pending", 0)))
    table.add_row("Running", str(job_counts.get("running", 0)))
    table.add_row("Done", str(job_counts.get("done", 0)))
    table.add_row("Failed", str(job_counts.get("failed", 0)))
    table.add_row("Dead", str(len(dead_letters)), style=dead_style)
    console.print(table)

    total_egress = sum(n for _, n in egress_counts)
    by_purpose = ", ".join(f"{purpose}: {n}" for purpose, n in egress_counts) or "none"
    # Two console.print defaults must be turned OFF on every prose line below --
    # each is a behaviour change the typer.echo -> rich switch would otherwise
    # smuggle in, since the typer.echo these replaced printed plain, verbatim
    # text:
    #
    # markup=False -- console.print parses "[...]" as rich markup, so a job's
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
    #   Console (cli.py:~202) is lode-re0s's call to make once .4/.6/.10 land,
    #   and taking it here would conflict with sibling branches live on this
    #   file. Table cells need none of this: rich runs no highlighter over them.
    console.print(
        f"egress: {total_egress} sends ({by_purpose})", markup=False, highlight=False
    )

    console.print(
        f"dead-letters (dead jobs): {len(dead_letters)}",
        style=dead_style,
        markup=False,
        highlight=False,
    )
    for job_id, job_type, target_version, last_error in dead_letters:
        console.print(
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
        settings = _resolve_settings()
    except Exception:
        settings = None
    cache_cold = False if settings is None else _cold_model_cache(settings)
    console.print()
    # markup stays ON here -- these strings are author-written, not DB-derived,
    # so the [warn]/[ok] tags are the point. highlight stays OFF for the same
    # reason as above: the job count and the quoted 'lode work' would otherwise
    # pick up rich's undeclared repr.* colours mid-sentence.
    if pending_or_failed > 0:
        console.print(
            f"[warn]Action needed:[/warn] {pending_or_failed} job(s) pending or "
            "failed -- run 'lode work' to drain the queue.",
            highlight=False,
        )
    if cache_cold:
        console.print(
            "[warn]Action needed:[/warn] the local model cache is cold -- run "
            "'lode models pull' to warm it.",
            highlight=False,
        )
    if pending_or_failed == 0 and not cache_cold:
        console.print("[ok]No action needed.[/ok]", highlight=False)


@app.command(name="jobs")
def jobs_(
    status: JobStatus | None = typer.Option(
        None, "--status", help="Only list jobs in this status (default: all)."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """List the derive jobs on the work queue (see docs/storage.md).

    One row per job -- id, type, status, attempts, target version -- newest
    last; a failed job also shows its last error. --status narrows the list
    to a single queue state.
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
    """List what content has left the box for the cloud, and when.

    A straight answer to "what of mine has gone to the cloud, and when?"
    (see docs/externals.md "Egress log"). One row per cloud send, oldest
    first: id, timestamp, purpose, model, the ids of what was sent, and
    which redactions were applied. --purpose narrows to enrich or qa sends.
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
    """Mark (or --clear) an external source no_egress (see docs/externals.md).

    A no_egress external stays captured, chunked, embedded, and locally
    retrievable (keyword + vector) -- only cloud egress changes. It is
    excluded from both the enrichment send and the Q&A context, and any
    answer that would have cited it surfaces it instead as "present,
    withheld from cloud synthesis".

    EXTERNAL_ID must already exist (e.g. drawn down via a note's pasted
    URL) -- this command does not create sources.
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


def _render_external_choice(index: int, external: ExternalView) -> str:
    """Render one numbered listing line for ``dump-html``'s disambiguation prompt.

    Same fields :func:`_render_external` shows beneath a ``show`` edge line
    (source_type, short snapshot id, fetched_at, state), fronted by the
    1-based ``index`` this command also accepts as a selector -- so what's
    printed here is exactly what a subsequent selector argument can
    reference back.
    """
    return (
        f"  {index}) {external.external_id}  "
        f"{external.source_type} · snapshot {short_version_id(external.snapshot_id)} "
        f"· as of {external.fetched_at} [{external.state}]"
    )


def _select_external(
    externals: list[ExternalView], selector: str
) -> tuple[int, ExternalView] | None:
    """Resolve ``selector`` against ``externals`` -- a 1-based index or an exact id.

    ``selector`` is either the 1-based position :func:`_render_external_choice`
    printed, or the external's own id (its canonical URL) verbatim -- no
    prefix matching, unlike note-id resolution, since ``external_id`` values
    are typically full URLs a caller would paste rather than abbreviate.
    Returns ``None`` on no match; the caller decides how to report that.

    Returns the match's 1-based listing position ALONGSIDE it, because the
    position is what ``--file`` names the output file with
    (``<note-id>-NNNN.dmp``) and only this function knows it: recovering it
    afterwards with ``externals.index(chosen)`` would find the first
    *equal* entry instead of the selected one. :class:`ExternalView` is a
    frozen dataclass, so two edges pointing at the same external compare
    equal -- reachable, since ``edges`` has no ``(from_id, to_id)`` unique
    constraint and ``enrich`` inserts an ``ai`` edge without dedup against an
    existing one (``lode.enrich``) -- and selecting the later duplicate would
    then write the earlier one's filename.
    """
    if selector.isdigit():
        index = int(selector)
        if 1 <= index <= len(externals):
            return index, externals[index - 1]
        return None
    matches = [
        (index, external)
        for index, external in enumerate(externals, start=1)
        if external.external_id == selector
    ]
    return matches[0] if len(matches) == 1 else None


def _externals_from_view(view: EnrichmentView) -> list[ExternalView]:
    """Filter an enrichment view's edges to the ones that are real externals.

    The single definition of "which of a note's edges ``dump-html`` can
    address": exactly the edges that resolve to a real ``externals`` row.
    Shared by ``dump_html``'s single-target path (which already holds the
    view, having needed it to tell "unknown note" from "no externals") and by
    :func:`_note_externals` on the ``--all`` path, so the two cannot drift
    onto different rules about what counts as dumpable.
    """
    return [edge.external for edge in view.edges if edge.external is not None]


def _note_externals(conn: sqlite3.Connection, note_id: str) -> list[ExternalView]:
    """Return a note's dumpable externals -- the addressable set for ``dump-html``.

    ``dump_html``'s ``--all`` path only: looks the note's view up and applies
    :func:`_externals_from_view` (the shared dumpable-edge rule). Returns
    ``[]`` for an unknown note id, same as a note with no such edges, which
    is why the single-target path does NOT call this: it must distinguish
    "unknown note" from "no externals" (two different errors), so it checks
    :func:`~lode.enrichment_view.enrichment_view_conn` itself and passes the
    view it already holds to :func:`_externals_from_view` directly rather
    than re-querying it here.
    """
    view = enrichment_view_conn(conn, note_id)
    if view is None:
        return []
    return _externals_from_view(view)


def _raw_payload(conn: sqlite3.Connection, snapshot_id: str) -> str | None:
    """Fetch one snapshot's raw HTML payload, or ``None`` if absent (nullable, schema.sql)."""
    row = conn.execute(
        "SELECT raw_payload FROM snapshots WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchone()
    return row[0] if row else None


def _dump_path(out_dir: Path, note_id: str, index: int) -> Path:
    """Where ``--file`` writes one dump: ``<out_dir>/<note-id>-NNNN.dmp``.

    The single definition of the output naming both write paths promise to
    share -- ``--all``'s per-external sweep and the single-target path's one
    resolved dump -- so a change to the suffix width or the extension cannot
    land on one and miss the other. ``index`` is the external's 1-based
    position in the note's dumpable-external listing, 0-padded to four digits
    UNCONDITIONALLY (lode-l38d.8), even when the note has only one external.
    """
    return out_dir / f"{note_id}-{index:04d}.dmp"


def _dump_all_notes(
    conn: sqlite3.Connection,
    *,
    write_files: bool,
    out_dir: Path,
) -> None:
    """Implement ``dump-html --all``: every live note's dumpable external(s).

    Iterates :func:`~lode.notes_read.list_notes_conn` (newest-first, the same
    listing plain ``lode notes`` shows) and, per note, ALL of
    :func:`_note_externals`' externals -- not just one, unlike the
    single-target path's selector-driven single choice (lode-l38d.8: "a note
    with multiple externals should dump ALL of them, that is what the
    0-padded suffix scheme is for"). A note with no externals, or an
    external with no captured raw HTML (tombstoned or simply never
    captured), is silently skipped -- ``--all`` is a best-effort bulk sweep
    ("if there is something to dump"), not the single-target path's targeted
    request, so it never errors on the merely "nothing to dump here" case
    (only the single-target path still does that).

    ``write_files=False`` prints the delimited stdout concatenation (the
    ``head``/``tail`` multi-file convention): a ``==> NOTE-ID  EXTERNAL-URL
    <==`` header per dump, with a blank line between dumps. Raw
    un-delimited concatenation was explicitly rejected -- you cannot tell
    where one note's HTML ends.

    ``write_files=True`` writes each external to its own file under
    ``out_dir`` (created if absent), named with an UNCONDITIONAL 0-padded
    suffix -- ``<note-id>-0001.dmp`` -- even when the note has only one
    external; an existing file of the same name is overwritten.
    """
    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)

    dumped = 0
    for note in list_notes_conn(conn):
        for index, external in enumerate(_note_externals(conn, note.note_id), start=1):
            raw_payload = _raw_payload(conn, external.snapshot_id)
            if not raw_payload:
                continue
            if write_files:
                out_path = _dump_path(out_dir, note.note_id, index)
                out_path.write_text(raw_payload, encoding="utf-8")
            else:
                if dumped:
                    typer.echo("")
                typer.echo(f"==> {note.note_id}  {external.external_id} <==")
                typer.echo(raw_payload)
            dumped += 1

    if write_files:
        typer.echo(f"wrote {dumped} file(s) to {out_dir}")
    elif not dumped:
        typer.echo("no external HTML captured for any note")


@app.command(name="dump-html")
def dump_html(
    target: str | None = typer.Argument(
        None,
        help="Note id, or an unambiguous prefix of one, to dump an external "
        "for. Required unless --all is given; conflicts with --all.",
    ),
    selector: str | None = typer.Argument(
        None,
        help="Which external to dump when the note has more than one: its "
        "1-based listing index, or its external id (URL) verbatim. "
        "Conflicts with --all.",
    ),
    all_notes: bool = typer.Option(
        False,
        "--all",
        help="Dump every live note's dumpable external(s) instead of one "
        "target. Conflicts with an explicit target/selector.",
    ),
    file: bool = typer.Option(
        False,
        "--file",
        help="Write dump(s) to per-note file(s) (named <note-id>-NNNN.dmp, "
        "see --dir) instead of printing to stdout. Valid with or without "
        "--all.",
    ),
    dir_: Path | None = typer.Option(
        None,
        "--dir",
        help="Directory to write files into with --file (created if "
        "absent). Default: the current directory. Only valid with --file.",
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Print a note's drawn-down external's raw HTML (its captured snapshot).

    TARGET resolves the same way as "show"/"purge": a full id or an
    unambiguous prefix. A note reaches an external via one of its
    enrichment edges; only edges that resolve to a real external count.

    With exactly one such external, no SELECTOR is needed. With more than
    one and no SELECTOR given, the command lists them (index, id, source
    type, snapshot, state) instead of guessing; SELECTOR then picks one by
    that listing's 1-based index or by the external's id (URL) verbatim.

    A tombstoned snapshot, or one with no captured raw HTML, reports
    cleanly to stderr and exits non-zero rather than printing an empty
    line.

    --all switches to bulk mode: every live note's dumpable external(s)
    instead of one target -- TARGET/SELECTOR must then be omitted. A note
    or external with nothing captured is silently skipped rather than
    erroring. Without --file, output is printed to stdout, each dump
    preceded by an "==> id url <==" header; with --file (written into
    --dir, default the current directory), one <note-id>-NNNN.dmp file is
    written per external instead, 0-padded and numbered by listing
    position.

    --file also works with a single TARGET (no --all needed): it writes
    that one resolved external's dump to a file instead of stdout, using
    the same naming and --dir handling. The single-target "nothing to
    dump" errors still apply and take priority over writing a file.
    """
    if all_notes and (target is not None or selector is not None):
        typer.echo(
            "--all cannot be combined with an explicit target/selector", err=True
        )
        raise typer.Exit(code=1)
    if not all_notes and target is None:
        typer.echo("target is required unless --all is given", err=True)
        raise typer.Exit(code=1)
    if dir_ is not None and not file:
        typer.echo("--dir requires --file", err=True)
        raise typer.Exit(code=1)

    # Resolved once, for both write paths: --dir defaults to None (NOT Path("."))
    # so the check above can tell "given" from "absent"; the cwd fallback is this
    # command's output-location rule and belongs in one place, like _dump_path's
    # naming rule. Harmless without --file -- nothing is created until a mkdir.
    out_dir = dir_ or Path(".")

    conn = _open_db(db)
    try:
        if all_notes:
            _dump_all_notes(conn, write_files=file, out_dir=out_dir)
            return

        assert target is not None  # validated above: required unless --all
        repo = Repository(conn)
        try:
            note_id = repo.resolve_note_prefix(target)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
        except AmbiguousNoteIdError as exc:
            _report_ambiguous_prefix(conn, target, exc)

        view = enrichment_view_conn(conn, note_id)
        if view is None:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1)

        externals = _externals_from_view(view)
        if not externals:
            typer.echo(f"no external sources for note {note_id}", err=True)
            raise typer.Exit(code=1)

        if len(externals) == 1:
            chosen_index, chosen = 1, externals[0]
        elif selector is None:
            typer.echo(f"note {note_id} has {len(externals)} external sources:")
            for index, external in enumerate(externals, start=1):
                typer.echo(_render_external_choice(index, external))
            return
        else:
            selected = _select_external(externals, selector)
            if selected is None:
                typer.echo(
                    f"no external source matching {selector!r} for note "
                    f"{note_id}; options:",
                    err=True,
                )
                for index, external in enumerate(externals, start=1):
                    typer.echo(_render_external_choice(index, external), err=True)
                raise typer.Exit(code=1)
            chosen_index, chosen = selected

        raw_payload = _raw_payload(conn, chosen.snapshot_id)
    finally:
        conn.close()

    if not raw_payload:
        reason = (
            "fetch failed (tombstone)"
            if chosen.status == "tombstone"
            else "no HTML was captured for this snapshot"
        )
        typer.echo(
            f"no stored HTML for {chosen.external_id} "
            f"(snapshot {short_version_id(chosen.snapshot_id)}): {reason}",
            err=True,
        )
        raise typer.Exit(code=1)

    if file:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = _dump_path(out_dir, note_id, chosen_index)
        out_path.write_text(raw_payload, encoding="utf-8")
        typer.echo(f"wrote {out_path}")
        return

    typer.echo(raw_payload)


def _config_path_table(rows: list[tuple[str, str, str]]) -> Table:
    """Render :func:`~lode.config.config_rows`' output as a terminal-width-aware
    rich ``Table`` (lode-l38d.4). No header -- this block is a labelled dump,
    not a column-semantic listing, so its look is unchanged from before this
    ticket. The parenthetical annotation (``($LODE_HOME)``, ``(present)``/
    ``(absent)``) lands in a real ``Note`` column instead of being
    string-baked into ``Value`` (the TUI's Ctrl+O screen still bakes it in --
    it renders :func:`lode.config.config_lines` untouched).

    ``overflow="fold"`` on ``Value``/``Note``: rich's ``Column`` default,
    ``overflow="ellipsis"``, DROPS characters off any unbreakable single-token
    string (e.g. a long absolute path) wider than its column instead of
    wrapping it -- verified against the installed rich (15.0.0). Unacceptable
    here, since this command's entire point is showing exact paths; nothing
    may ever be silently truncated. ``"fold"`` hard-breaks mid-word when it
    must, which is ugly but lossless.

    Every cell is wrapped in :class:`rich.text.Text` rather than passed as a
    bare ``str`` -- a bare string renders through rich's markup parser (the
    shared ``console``'s default), which reads a literal ``[...]`` in a path
    (or, for the knob table below, a regex character class) as a markup tag
    and SILENTLY DROPS it. Verified against the installed rich: an unwrapped
    ``"gh[pousr]_..."`` cell rendered as ``"gh_..."``, quietly losing
    ``[pousr]``. ``Text(...)`` bypasses markup parsing entirely, so arbitrary
    path/value content round-trips byte-for-byte.
    """
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Label")
    table.add_column("Value", overflow="fold")
    table.add_column("Note", overflow="fold")
    for label, value, note in rows:
        # Parenthesized to match the pre-lode-l38d.4 baked-in text
        # (f"{value}  ({note})") -- moving it to its own column fixes the
        # actual bug (that text used to distort the Value column's computed
        # width), not the visual convention of wrapping it in parens.
        table.add_row(Text(label), Text(value), Text(f"({note})" if note else ""))
    return table


def _config_knob_table(rows: list[tuple[str, str, str]]) -> Table:
    """Render :func:`~lode.config.knob_rows`' output as a terminal-width-aware
    rich ``Table`` (lode-l38d.4), with a header + separator rule
    (``box.SIMPLE_HEAD``, closing the ticket's "no header separator rule"
    gap) but no side borders, keeping the previous plain-list look. The TUI
    renders the same row data into a ``DataTable`` widget instead
    (:mod:`lode.tui.screens.config`); only the row DATA is shared, not this
    formatting.

    A list-valued knob (comma+space-joined by :func:`~lode.config.knob_rows`)
    wraps at the space boundaries "for free" under ``overflow="fold"`` -- no
    need to un-join it or render it specially. This, plus terminal-width-aware
    column sizing instead of padding every row to the single widest value in
    the table, is what removes the original bug.

    Every cell is wrapped in :class:`rich.text.Text`, not passed as a bare
    ``str`` -- several knob values here are regex character classes
    (``redact_before_egress_patterns`` et al: ``gh[pousr]_...``,
    ``xox[baprs]-...``), and a bare string renders through rich's markup
    parser, which reads a literal ``[...]`` as a markup tag and silently
    drops it. See :func:`_config_path_table` for the verification.
    """
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)
    table.add_column("Knob")
    table.add_column("Value", overflow="fold")
    table.add_column("Kind")
    for name, value, kind in rows:
        table.add_row(Text(name), Text(value), Text(kind))
    return table


@app.command()
def config(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show the resolved on-disk locations and every runtime/tune knob.

    A read-out of the single-root layout under $LODE_HOME (default
    ~/.lode) so you can find, back up, or inspect lode's state: the root,
    the SQLite DB and its lock, the vector store, the model-weights cache,
    the log directory, and whether a config.toml is present (see
    docs/configuration.md). --db shifts the displayed DB (and its lock and
    co-located vector store) to an explicit override.

    Below the paths, a knob table lists every runtime/tune setting with its
    currently resolved value (defaults, then config.toml, then overrides),
    even with no config.toml present. Both tables adapt to the terminal
    width, wrapping long values within their column instead of running off
    the edge.
    """
    console.print(_config_path_table(config_rows(db or default_db_path())))
    console.print()
    console.print(_config_knob_table(knob_rows(_resolve_settings())))


@app.command()
def tui(
    ctx: typer.Context,
    db: Path | None = _DB_OPTION,
) -> None:
    """Launch the Textual TUI, starting on the instant capture screen.

    Logs go to $LODE_HOME/logs/lode.log instead of the terminal, since the
    TUI takes over the screen. The top-level --debug flag still raises the
    log file's verbosity.
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

    On a cold cache, the first embed call otherwise downloads several
    hundred MB of model weights from HuggingFace mid-capture -- a surprise
    phone-home rather than a one-time setup cost. This forces that download
    now, up front, so a later "lode work" / "lode ask" never hits the
    network unexpectedly.

    Warms every model named by your resolved settings (a config.toml
    override of the embedding, reranker, or entailment model is honored):
    the embedder and the reranker/NLI cross-encoder (see
    docs/configuration.md "Models"). The reranker and entailment models
    default to the same id, so when they match, the second load is skipped
    as a cache hit rather than re-fetched.

    Once warmed, every subsequent run is fully offline for indexing and
    retrieval. To force that even against a cold cache miss, set
    HF_HUB_OFFLINE=1 -- fastembed's own offline flag, not lode-specific.

    A bad config.toml gives the same clean stderr message and exit 1 every
    other command gives, not a raw traceback. On its most likely failure
    path -- no network, HF_HUB_OFFLINE=1 against a cold cache, or a
    HuggingFace rate limit or error -- this exits non-zero with a clear,
    actionable message instead.
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
            "Block, polling every --interval seconds, until the queue is "
            "fully drained or a bounded timeout elapses (see "
            "docs/configuration.md), whichever comes first. On timeout, "
            "exits non-zero naming the still-pending/running jobs -- just "
            "re-run 'lode work' (or --wait again) to keep collecting; a "
            "large enrich batch can legitimately take a long time to land. "
            "Mutually exclusive with --loop/--watch, which never exits on "
            "its own."
        ),
    ),
) -> None:
    """Drain the async work queue: claim, run, retry, or dead-letter each job.

    One-shot by default: acquires the single-instance lock, resets overdue
    failed jobs, then claims and runs ready pending jobs until none remain
    and exits. --loop / --watch keeps the loop alive forever instead,
    sleeping --interval seconds between passes. --wait / --until-done polls
    only until the queue is fully drained or a bounded timeout fires (see
    its own help), so you don't have to re-run this by hand to see an async
    enrich batch land.

    embed jobs run synchronously in the main loop. enrich jobs are
    submitted to a batch API ahead of that loop and collected on a later
    pass; refresh jobs have no handler yet and simply accumulate
    harmlessly. A second "lode work" while one is already running is
    refused.

    Each pass prints a per-job outcome line for what it actually produced
    (e.g. "enriched <short-id>: 4 tags, 2 entities, 3 edges, summary set",
    or "embedded <short-id>: 3 passages"), followed by a "drained N job(s)"
    summary. A one-shot run right after capture only submits the enrich
    batch (nothing to collect yet), so its outcome line appears on a later
    pass instead.

    Without --wait, if jobs are still pending or running once the pass
    ends, that is reported too -- naming each one -- so a single pass over
    a thrashing head stays visible instead of a bare "drained 0 job(s)"
    that looks like nothing happened.
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

                        # Logging parity with --wait (lode-olmi.13): --wait
                        # already surfaces outstanding jobs via the timeout
                        # message above (or by polling again next tick), so a
                        # thrashing head is visible across its passes. A
                        # one-shot pass has no "next tick" -- if reconcile()
                        # just re-enqueued jobs that this pass's drain() never
                        # got to (or can't -- e.g. no handler registered yet),
                        # the one-shot exits right after a bare "drained 0
                        # job(s)" with no sign anything is left, which is what
                        # hid the reconcile re-enqueue loop (lode-olmi.11) and
                        # the one-shot hang (lode-olmi.12) from a plain
                        # 'lode work'. Report the same outstanding-jobs detail
                        # --wait's own timeout path names, every pass, so a
                        # one-shot (or --loop) run is never silent about it.
                        outstanding = _outstanding_jobs(conn)
                        if outstanding:
                            typer.echo(
                                f"{len(outstanding)} job(s) still outstanding "
                                f"after this pass: {_format_outstanding(outstanding)}"
                            )

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
