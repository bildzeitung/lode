"""lode command-line entry point.

A Typer app wired to the lode console-script (lode --help lists the
subcommand surface). See docs/design.md for the save path, docs/storage.md
for the async work queue, docs/retrieval.md for the cited Q&A pipeline, and
docs/externals.md for external sources, no-egress, and hard delete.

The eval harness (lode.eval.harness.score_golden_set) is a maintainer/CI
integration test run via "nox -s eval" -- it is not a shipped end-user
command (see docs/decisions.md).

**Package layout (lode-35nu.9).** This module is the CLI's dispatch/plumbing
layer: the shared Typer ``app``, the top-level ``main`` callback, the shared
rich Console/theme, and the handful of cross-command helpers
(``_resolve_settings``, ``_open_db``, ...) every command needs. Each command
itself lives in its own sibling module (``lode.cli.add``, ``lode.cli.ask``,
...) -- dispatch plus argument wiring only, no bare SQL; every SQL-touching
helper a command used to carry was relocated to the module that owns the
concern (``lode.notes_read``, ``lode.enrichment_view``, ...). Every command
module is imported below (for its ``@app.command()`` registration side
effect), then the handful of names a test or another package still reaches
via ``lode.cli.<name>`` are re-exported at the bottom.

**Why some genuinely command-specific helpers stay resolvable through the
PACKAGE rather than their own submodule.** A few helpers
(``_resolve_settings``, ``model_cache_dir``, ``provider_identity``,
``_default_verify_fetcher``, ``_cold_model_cache``, ``configure_logging``)
are monkeypatched by tests as ``lode.cli.<name>`` (or the equivalent
``monkeypatch.setattr(cli, "<name>", ...)``) rather than by string path into
their owning submodule. Python attribute-patching only affects a NAME
LOOKUP, not the object underneath, so a submodule that imported one of these
via a plain ``from lode.cli.status import _cold_model_cache`` would bind its
own frozen reference at import time -- a later ``lode.cli._cold_model_cache``
patch would then silently miss it. Every call site of one of these six names
therefore does ``from lode import cli`` and calls ``cli.<name>(...)`` --
looking the name up through the package's OWN namespace at call time, the
same live-binding indirection this single flat module gave every call for
free before the split. This is a deliberate, narrow exception; every other
cross-command helper (``_open_db``, ``console``, ``SafeTable``, ...) is
imported normally, since nothing patches those by name.
"""

import logging
import sqlite3
import tempfile
import time  # noqa: F401 -- re-exported so `cli.time` resolves for tests (see module docstring)
import tomllib
import uuid  # noqa: F401 -- re-exported so `cli.uuid` resolves for tests (see module docstring)
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError
from rich import box
from rich.console import Console, RenderableType
from rich.style import Style
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# Registers the "jira" connector into lode.backfill's registry on import
# (lode-gpzn.10) -- deliberately a MODULE-LEVEL import here, not the lazy,
# inside-the-command-function style `backfill`/`reconcile`/`work` otherwise
# use to keep CLI startup light. Registration must happen exactly once, at
# collection/process-start time, before any test's own registry-isolation
# fixture runs -- a lazy import inside the `backfill` command races
# tests/test_cli_backfill.py's fake-handler injection under pytest-xdist
# (parallel worker processes make "which test calls `backfill()` first"
# non-deterministic run to run).
import lode.jira_backfill  # noqa: F401
from lode.config import Settings, config_path, default_db_path, load_settings, log_dir
from lode.config import model_cache_dir as model_cache_dir
from lode.llm_provider import LLMProviderError, provider_identity  # noqa: F401
from lode.logconfig import configure_logging
from lode.repository import AmbiguousNoteIdError
from lode.storage import init_db

log = logging.getLogger(__name__)

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
#: Declared as a plain dict, and NOT inlined into the ``Theme(...)`` call
#: below, because ``Theme.__init__`` DESTROYS the declaration: it does
#: ``self.styles = DEFAULT_STYLES.copy()`` (``inherit=True`` is the default)
#: and then ``.update()``s these on top. Keeping the declaration reachable is
#: what lets tests/test_cli_theme.py assert this palette rather than rich's.
CLI_STYLES: dict[str, str] = {
    "note_id": "cyan",
    "date": "dim",
    "warn": "yellow",
    "danger": "bold red",
    "ok": "bold green",
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
#: ``force_terminal``, no accessor to monkeypatch) — see docs/stack.md and
#: ``tests/test_cli_console.py``'s module docstring for the freeze-vs-live
#: mechanism this relies on.
#:
#: ``highlight=False`` (lode-re0s) is process-wide colour POLICY -- see
#: ``tests/test_cli_console.py`` for the full rationale; every command below
#: relies on it rather than passing the flag per call site.
console = Console(theme=CLI_THEME, highlight=False)

#: A STDERR twin of ``console`` above (lode-l810) -- same theme, same
#: colour/width auto-detection rules, but writing to stderr rather than
#: stdout. ``highlight=False`` is a constructor kwarg here too (lode-9jmv),
#: per the hoisted-highlight convention ``console`` above documents.
err_console = Console(theme=CLI_THEME, stderr=True, highlight=False)


class SafeTable(Table):
    """``rich.table.Table`` with the bare-str markup-injection guard built
    into ``add_row`` itself (lode-9tmd) -- the ONE shared seam every CLI
    table in this package must construct through, instead of ``rich.table
    .Table`` directly.

    THE INVARIANT, verified against the installed rich 15.0.0: a bare
    ``str`` cell passed to ``Table.add_row`` is parsed as rich MARKUP, so a
    literal ``[...]`` substring -- a redaction regex character class like
    ``gh[pousr]_[0-9a-zA-Z]{36}``, an absolute path, anything -- is read as
    a tag and SILENTLY DROPPED (``"gh[pousr]_..."`` renders as
    ``"gh_..."``). ``SafeTable.add_row`` wraps every bare ``str`` cell in
    ``Text(...)`` before delegating to ``Table.add_row``, structurally,
    once, here -- a call site building a ``SafeTable`` needs no per-cell
    wrapping and cannot forget it.
    """

    def add_row(
        self,
        *renderables: RenderableType | None,
        style: str | Style | None = None,
        end_section: bool = False,
    ) -> None:
        safe = tuple(
            Text(cell) if isinstance(cell, str) else cell for cell in renderables
        )
        super().add_row(*safe, style=style, end_section=end_section)


def _tabular_table() -> SafeTable:
    """Construct a ``SafeTable`` in lode's house style for a real columnar
    listing -- header + separator rule, no side borders, no cell padding at
    the table's own edges (``box.SIMPLE_HEAD, show_edge=False,
    pad_edge=False``). The one style every CLI table with column-semantic
    headers uses (lode-9tmd). The sanctioned exception is a label:value dump
    with no column semantics -- see ``lode.cli.config._config_path_table``,
    which explicitly opts out instead of using this helper.
    """
    return SafeTable(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False)


#: Shared ``--debug`` option: raises the log level to DEBUG, which turns on every
#: DEBUG-gated diagnostic (e.g. ``lode.tui.latency_probe``'s event-loop-lag probe,
#: gated on ``log.isEnabledFor(logging.DEBUG)``) -- see main()'s docstring.
_DebugOption = Annotated[
    bool,
    typer.Option(
        "--debug",
        help=(
            "Enable DEBUG-level logging, turning on DEBUG-gated diagnostic "
            "instrumentation (e.g. the event-loop-lag probe). Takes precedence "
            "over LODE_LOG_LEVEL when passed; unset, LODE_LOG_LEVEL (default "
            "INFO) still applies. See docs/configuration.md."
        ),
    ),
]


@app.callback()
def main(ctx: typer.Context, debug: _DebugOption = False) -> None:
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
_DbOption = Annotated[
    Path | None,
    typer.Option(
        "--db",
        help="SQLite database path (default: $LODE_HOME/lode.db, i.e. ~/.lode/lode.db).",
    ),
]


def _write_draft(db_path: Path, note_id: str, body: str) -> Path:
    """Persist a CAS-rejected capture buffer beside the DB so it is never lost.

    Named uniquely (``mkstemp``) so a retry never clobbers an earlier draft; the
    interactive re-apply/discard surface waits for the TUI (E11). Returns the
    draft's path for the user-facing message.
    """
    import os

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

    Defined here, on the package itself, rather than in a command submodule
    (lode-35nu.9): nearly every command calls this, and it is independently
    monkeypatched as ``lode.cli._resolve_settings`` — every command therefore
    reaches it via ``cli._resolve_settings()`` (see this module's own
    docstring for why), which only works if the name actually lives on the
    package.
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
    (:class:`~lode.auth.AuthError`, lode-9yy, or any other
    :class:`~lode.llm_provider.LLMProviderError` — lode-s08c, mirroring
    lode-yx1c's identical fix to ``ask``/``work``) is different: ``run_one``
    resets the job straight back to ``pending`` (uncharged) and re-raises it,
    but capture must stay instant regardless of whether the **active
    provider's** credentials are configured (``docs/design.md`` §1) — so it is
    caught and dropped here rather than surfaced on every single ``add``,
    unlike ``ask``/``work``, which abort. The job is already back at
    ``pending``, uncharged, for the next explicit ``lode work`` to report
    loudly. ``docs/storage.md`` "Transient vs. permanent job failures" owns
    *which* errors reach here — and why a non-auth ``LLMProviderError`` raised
    by a job handler never does.
    """
    from lode.auth import AuthError
    from lode.worker import claim_and_run_one

    try:
        claim_and_run_one(
            conn, db_path, settings, types=("enrich",), target_version=version_id
        )
    except (AuthError, LLMProviderError) as err:
        # `err`, not a hardcoded cause: this arm is no longer Anthropic-only.
        # Same forked-message trap `_abort_on_provider_error` was extracted to
        # close -- see its docstring (lode-yx1c).
        log.debug(
            "immediate-enrich skipped — %s; note saved, job left pending for a "
            "future 'lode work'",
            err.__cause__ or err,
        )


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
    from lode.timestamps import parse_stamp

    return parse_stamp(created).astimezone().strftime("%Y-%m-%d %H:%M")


def _report_ambiguous_prefix(
    conn: sqlite3.Connection, target: str, exc: AmbiguousNoteIdError
) -> NoReturn:
    """Render an ambiguous note-id prefix's candidates, then exit 1 (lode-l38d.10).

    The one shared body for the four call sites that resolve a note-id prefix
    (``purge``/``recover``/``show``/``dump-html``) and can raise
    :class:`~lode.repository.AmbiguousNoteIdError`: each candidate gets a full
    listing row -- id, date, summary, same columns as ``lode notes``
    (:func:`lode.cli.notes.notes_`) -- so the error is self-sufficient, no
    second command needed to tell the candidates apart.

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
    and ``soft_wrap=True`` flag ``notes_`` uses, so the two listings' shared
    columns (id, date) now look identical rather than one being coloured and
    the other bare ``typer.echo``.
    """
    from rich.markup import escape

    from lode.notes_read import candidate_rows_conn

    typer.echo(
        f"ambiguous note id prefix {target!r}: {len(exc.candidates)} matches",
        err=True,
    )
    for row in candidate_rows_conn(conn, exc.candidates):
        marker = " [deleted]" if row.deleted else ""
        # Deliberately the same rendering path as notes_ (lode-l38d.5,
        # lode-l810): the shared theme's note_id/date style NAMES (never a
        # colour literal), the summary escape()d, and the same soft_wrap
        # flag. The " [deleted]" tombstone marker (lode-l38d.10) stays a
        # literal, uncoloured suffix -- but it must be escape()d ALONG WITH
        # the summary, not appended after it (a bare "[deleted]" is
        # otherwise parsed -- and silently eaten -- as rich markup; verified
        # against rich 15.0.0).
        err_console.print(
            f"  [note_id]{row.note_id}[/note_id]  "
            f"[date]{_short_date(row.created)}[/date]  "
            f"{escape(row.summary + marker)}",
            soft_wrap=True,
        )
    raise typer.Exit(code=1) from None


def _abort_on_provider_error(command: str, err: BaseException) -> NoReturn:
    """Render a cloud-LLM provider failure as one actionable line, then exit 1.

    The one shared body for ``ask``'s and ``work``'s
    ``except (AuthError, LLMProviderError)`` arms (lode-yx1c). Shared rather
    than copied because the copies had already forked once: each hardcoded
    ``"could not resolve Anthropic credentials"``, which became wrong the
    moment a non-Anthropic provider (lode-568v.3) or a non-credential failure
    could reach them. Every provider exception this repo raises already carries
    a self-describing message -- ``auth.MISSING_CREDENTIALS_MESSAGE`` names
    every resolution path; ``LLMProviderError``'s embeds the underlying SDK
    error -- so ``str(err)`` alone is the actionable line, with no per-command
    framing to keep in sync.

    No ``exc_info``: the root logger mirrors to stderr, so dumping frames there
    would re-introduce the very traceback being suppressed for the user. The
    cause is logged instead, since the user-facing message deliberately does
    not carry it.

    ``docs/storage.md`` "Transient vs. permanent job failures" owns *which*
    errors reach here, and why a non-auth ``LLMProviderError`` raised by a job
    handler never does.
    """
    log.error("%s aborted — %s", command, err.__cause__ or err)
    typer.echo(str(err), err=True)
    raise typer.Exit(code=1) from None


# --- command module registration --------------------------------------------
# Each import below triggers its module's own @app.command()/@models_app.command()
# decorators. Order among these is immaterial (Typer collects commands, it
# doesn't dispatch at import time) except that `models` must be imported
# before anything expects `models_app` to already be attached to `app` --
# each module attaches itself, so there is no ordering hazard here either.
from lode.cli import add as _add  # noqa: F401
from lode.cli import ask as _ask  # noqa: F401
from lode.cli import backfill as _backfill  # noqa: F401
from lode.cli import config as _config  # noqa: F401
from lode.cli import dump_html as _dump_html  # noqa: F401
from lode.cli import egress as _egress  # noqa: F401
from lode.cli import jobs as _jobs  # noqa: F401
from lode.cli import models as _models  # noqa: F401
from lode.cli import notes as _notes  # noqa: F401
from lode.cli import purge as _purge  # noqa: F401
from lode.cli import recover as _recover  # noqa: F401
from lode.cli import reembed as _reembed  # noqa: F401
from lode.cli import reenrich as _reenrich  # noqa: F401
from lode.cli import reindex_lexical as _reindex_lexical  # noqa: F401
from lode.cli import show as _show  # noqa: F401
from lode.cli import status as _status  # noqa: F401
from lode.cli import tui as _tui  # noqa: F401
from lode.cli import verify as _verify  # noqa: F401
from lode.cli import version as _version  # noqa: F401
from lode.cli import work as _work  # noqa: F401

# --- backward-compatible re-exports ------------------------------------------
# A handful of names other packages (lode.tui.services.ask's deferred
# `from lode.cli import _retrieve`) or tests still reach as `lode.cli.<name>`.
# Plain re-exports (unlike the six call-through-the-package names documented
# in this module's own docstring, which are never imported anywhere -- every
# INTERNAL call site reaches them via `cli.<name>` instead).
from lode.cli.ask import (  # noqa: F401
    _ABSTAIN_LINE,
    _format_citation,
    _format_cited_answer,
    _retrieve,
)
from lode.cli.dump_html import _raw_payload  # noqa: F401
from lode.cli.models import _FASTEMBED_EXHAUSTED_SOURCES, _warm  # noqa: F401
from lode.cli.status import (  # noqa: F401
    EgressPurpose,
    JobStatus,
    _cache_hit,
    _cold_model_cache,
    _enrichment_model_stale,
    _model_cache_probe,
    _model_revision_status,
)
from lode.cli.verify import _default_verify_fetcher  # noqa: F401
from lode.cli.work import _short  # noqa: F401
from lode.enrichment_view import (
    stale_enrichment_heads as _stale_enrichment_heads,  # noqa: F401
)

if __name__ == "__main__":  # pragma: no cover
    app()
