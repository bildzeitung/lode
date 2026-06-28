"""lode command-line entry point.

A Typer app wired to the ``lode`` console-script (``lode --help`` lists the
subcommand surface). ``add`` (capture + save, lode-y42.1), the operational
``status`` / ``jobs`` read-outs (lode-y42.3), and the ``egress`` audit read-out
(E8, lode-fk8.3) are real; ``purge`` (E8, lode-7cx) hard-deletes a note via
:meth:`lode.repository.Repository.purge`; ``ask`` (lode-y42.2) runs the cited Q&A
loop (retrieve → synthesize → faithfulness gate → cite or abstain).

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
import uuid
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from lode import __version__, jobs, versions
from lode.config import (
    LODE_HOME_ENV,
    Settings,
    config_path,
    default_db_path,
    lance_dir,
    lode_home,
    log_dir,
)
from lode.logconfig import configure_logging
from lode.repository import Repository
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


@app.callback()
def main() -> None:
    """lode — capture and retrieve what you learn at work."""
    # Group callback: keeps lode a multi-command app so ``--help`` lists the
    # subcommands. Configure logging once, here, so every subcommand (and the
    # Anthropic SDK) logs consistently (LODE_LOG_LEVEL / ANTHROPIC_LOG) and lands
    # in $LODE_HOME/logs/ (lode.config.log_dir, docs/configuration.md).
    configure_logging(log_dir=log_dir())


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


def _embed_inline(
    conn: sqlite3.Connection,
    version_id: str,
    body: str,
    db_path: Path,
) -> None:
    """Run chunk+embed+FTS inline after capture (skeleton stand-in for the E2 worker).

    Makes the just-captured note immediately findable by both the vector leg
    (LanceDB cosine ANN) and the lexical leg (FTS5 BM25), so a ``lode ask``
    immediately after ``lode add`` retrieves it. This is the lode-x6r.2 intent
    — synchronous inline embed on save — for the Phase-A walking skeleton. The
    async move to the E2 work queue is the follow-up (lode-x6r.5, after the
    async worker lode-i05.3 lands). The durable ``embed`` job enqueued by
    :func:`lode.jobs.enqueue_derive_jobs` remains as the E2 seam.

    Imports are deferred to avoid paying the vector-stack cost (pyarrow, fastembed)
    on CLI commands that never embed.
    """
    from lode.chunking import chunk
    from lode.embedding import embed
    from lode.lexical import LexicalIndex

    settings = Settings()
    # Vector leg: chunk + embed + persist passage rows + store vectors in LanceDB.
    embed(conn, version_id, lance_dir=lance_dir(db_path), settings=settings)
    # Lexical leg: populate passages_fts so BM25 keyword search finds the note.
    passages = chunk(body, version_id, settings=settings)
    LexicalIndex(conn).replace_passages(version_id, passages)


@app.command()
def add(
    text: str | None = typer.Argument(
        None, help="Note body. Omit to read the note verbatim from stdin."
    ),
    db: Path | None = _DB_OPTION,
) -> None:
    """Capture a note into lode and enqueue its derive jobs.

    The save path (``docs/design.md``): writes the version (``versions.save``),
    enqueues the embed/enrich derive jobs for the E2 async worker (lode-i05.3),
    and — for the Phase-A walking skeleton — also runs chunk+embed+FTS inline
    so ``lode ask`` can find the note immediately (lode-x6r.2 intent; the
    inline→async move is deferred to lode-x6r.5). **No AI in the capture path**
    — the embedder is a local ONNX model, no network. The body comes from the
    ``TEXT`` argument or, if omitted, verbatim from stdin; an empty /
    whitespace-only body is refused.
    """
    db_path = db or default_db_path()
    body = text if text is not None else sys.stdin.read()
    if not body.strip():
        typer.echo("refusing to save an empty note", err=True)
        raise typer.Exit(code=1)

    # A fresh logical id per capture — `add` always creates a new note (no
    # aliasing), so `save` always takes its create path.
    note_id = str(uuid.uuid4())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    try:
        try:
            result = versions.save(conn, note_id, body)
        except versions.HeadConflictError:
            # A create against an already-present note: never clobber or
            # auto-merge — preserve the buffer as a draft and bail (the
            # interactive re-apply path lands with the TUI, E11).
            draft = _write_draft(db_path, note_id, body)
            typer.echo(f"note changed since opened; draft saved to {draft}", err=True)
            raise typer.Exit(code=1) from None
        jobs.enqueue_derive_jobs(conn, result.version_id)
        # Skeleton: run embed inline so the note is findable immediately.
        # The async worker (E2, lode-i05.3) will drain these jobs later; the
        # inline step here is the skeleton stand-in until then (lode-x6r.5).
        _embed_inline(conn, result.version_id, body, db_path)
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
    # inside _retrieve and cited_answer.ask.  The eval path already does this;
    # mirroring it here closes the gap so the interactive ask path honours config.
    settings = Settings()
    conn = _open_db(db_path)
    try:
        context = _retrieve(
            conn, question, lance_dir=lance_dir(db_path), settings=settings
        )
        answer = cited_answer.ask(
            conn, question, context, think_harder=think_harder, settings=settings
        )
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
    for line in _format_cited_answer(answer):
        typer.echo(line)


def _retrieve(
    conn: sqlite3.Connection,
    question: str,
    *,
    lance_dir: str | Path,
    embedder: "Embedder | None" = None,
    settings: Settings | None = None,
) -> "list[ContextItem]":
    """Build the trust-ranked Q&A context for ``question`` — both legs fused (E4).

    The full read side: lexical search (FTS5/BM25, heads only) and the dense leg
    (cosine ANN over the LanceDB store under ``lance_dir``, the question embedded
    query-side via ``embedder.embed_query``) each capped at ``retrieval_top_k``,
    fused app-side (:func:`~lode.retrieval.reciprocal_rank_fusion`), the top fused
    passages expanded small-to-big (:func:`~lode.retrieval.expand_parents`) and
    ordered by the trust gradient (:func:`~lode.retrieval.trust_rank`). RRF scores
    a passage present in one leg from that leg alone, so a passage matched only by
    the dense leg still reaches the Q&A context (lode-bkc). A question with no word
    tokens skips the lexical leg, but the dense leg still runs.

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
        lexical_search,
        reciprocal_rank_fusion,
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
    expanded = expand_parents(conn, fused[: settings.retrieval_top_k])
    return trust_rank(conn, expanded).context


def _format_cited_answer(answer: "CitedAnswer") -> list[str]:
    """Render a gated answer for the terminal: cited claims, or an abstention.

    Each surviving claim prints its text followed by one indented citation line per
    support — its ``version_id`` / ``snapshot_id`` and the verbatim span. When the
    answer abstained (no claim survived the gate) the honest abstention line is
    printed instead. Either way, any no_egress material is surfaced as "present,
    withheld from cloud synthesis" so the user knows relevant local content exists.
    """
    lines: list[str] = []
    if answer.abstained:
        lines.append(_ABSTAIN_LINE)
    else:
        for claim in answer.claims:
            lines.append(claim.text)
            lines.extend(_format_citation(support) for support in claim.support)
    for withheld in answer.withheld_citations:
        lines.append(f"  withheld {withheld.target_id}: {withheld.note}")
    return lines


def _format_citation(support: "Support") -> str:
    """Render one support as an indented ``<id-kind> <id>  "<span>"`` citation."""
    if support.version_id is not None:
        target = f"version_id {support.version_id}"
    else:
        target = f"snapshot_id {support.snapshot_id}"
    return f'  - {target}  "{support.quoted_span}"'


@app.command()
def purge(
    target: str = typer.Argument(..., help="Note id to hard-delete."),
    db: Path | None = _DB_OPTION,
) -> None:
    """Hard-delete a note and its derived data (E8 hard delete, ``docs/externals.md``).

    The deliberate immutability break: overwrite every body in the note's version
    chain with a ``[purged YYYY-MM-DD]`` marker, stamp ``purged_at``, drop the
    chain's ``source='ai'`` annotations (keeping ``source='user'`` corrections), and
    cascade-evict the derived cache through the repository's cache seam. Delegates to
    :meth:`lode.repository.Repository.purge` — no half-delete. (The cache is a no-op
    :class:`~lode.repository.NullCache` until the engine wiring lands, lode-1f9.)
    """
    conn = _open_db(db)
    try:
        try:
            result = Repository(conn).purge(target)
        except KeyError:
            typer.echo(f"no such note: {target}", err=True)
            raise typer.Exit(code=1) from None
    finally:
        conn.close()
    typer.echo(
        f"purged {result.note_id}: swept {len(result.purged_versions)} version(s); "
        f"body now {result.marker_body}"
    )


class JobStatus(str, Enum):
    """The ``jobs.status`` enum from ``schema.sql`` — accepted by ``--status``."""

    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class EgressPurpose(str, Enum):
    """The ``egress_log.purpose`` enum from ``schema.sql`` — accepted by ``--purpose``."""

    enrich = "enrich"
    qa = "qa"


def _short(target_version: str) -> str:
    """Abbreviate a version-id digest for a one-line listing (full id is a hash)."""
    return target_version if len(target_version) <= 12 else f"{target_version[:12]}…"


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
    pending/running/done/failed job counts, the dead-letter (failed) jobs with
    their last error, and how much content has left the box, by purpose.
    """
    conn = _open_db(db)
    try:
        job_counts = dict(
            conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        )
        dead_letters = conn.execute(
            "SELECT id, type, target_version, last_error FROM jobs "
            "WHERE status = 'failed' ORDER BY id"
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
        f"{job_counts.get('failed', 0)} failed"
    )

    total_egress = sum(n for _, n in egress_counts)
    by_purpose = ", ".join(f"{purpose}: {n}" for purpose, n in egress_counts) or "none"
    typer.echo(f"egress: {total_egress} sends ({by_purpose})")

    typer.echo(f"dead-letters (failed jobs): {len(dead_letters)}")
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


def _config_lines(db: Path | None) -> list[str]:
    """Render the resolved on-disk locations as aligned ``label  path`` lines.

    The root, log dir, and ``config.toml`` come from ``$LODE_HOME`` (lode.config);
    the DB, its sibling lock, and the vector store reflect a ``--db`` override when
    given — the lock and store are derived beside the chosen DB, matching the
    "co-locate beside the DB" layout (``docs/configuration.md``). Whether
    ``$LODE_HOME`` is set in the environment (vs the ``~/.lode`` default) and
    whether the optional ``config.toml`` is present are surfaced inline.
    """
    db_path = db or default_db_path()
    lock_path = db_path.with_name(db_path.name + ".lock")
    cfg = config_path()
    home_source = "$LODE_HOME" if os.environ.get(LODE_HOME_ENV) else "default"
    config_state = "present" if cfg.exists() else "absent"
    rows = [
        ("LODE_HOME", f"{lode_home()}  ({home_source})"),
        ("database", str(db_path)),
        ("db lock", str(lock_path)),
        ("vector store", str(lance_dir(db_path))),
        ("logs", str(log_dir())),
        ("config", f"{cfg}  ({config_state})"),
    ]
    width = max(len(label) for label, _ in rows)
    return [f"{label:<{width}}  {value}" for label, value in rows]


@app.command()
def config(
    db: Path | None = _DB_OPTION,
) -> None:
    """Show the resolved on-disk locations lode uses (``docs/configuration.md``).

    A read-out of the single-root layout under ``$LODE_HOME`` (default ``~/.lode``)
    so you can find, back up, or inspect lode's state: the root, the SQLite DB and
    its sibling lock, the LanceDB vector store, the log directory, and the optional
    ``config.toml`` (shown present/absent). Reads the resolved paths from
    :mod:`lode.config` rather than re-deriving them; ``--db`` shifts the displayed
    DB (and its lock + co-located vector store) to an explicit override.
    """
    for line in _config_lines(db):
        typer.echo(line)


@app.command()
def version() -> None:
    """Print the installed lode version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    app()
