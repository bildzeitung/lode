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
cross-command helper (``_open_db``, ``SafeTable``, ...) is imported
normally, since nothing patches those by name **on this package**. That
qualifier is the whole distinction: a name a test rebinds on the SUBMODULE
that consumes it (as ``tests/test_cli.py`` does for ``lode.cli.status``'s
``console``, lode-nftw) needs no package indirection at all -- the plain
import is already the patch target.

**``time`` and ``uuid`` are patched DIFFERENTLY from each other, and the
difference decides the call-site form -- do not unify them.**

* ``time`` is REBOUND AS A NAME on this package:
  ``monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=..., sleep=...))``
  (``tests/test_cli.py``'s ``_patch_cli_clock_past_deadline``, deliberately
  narrowed to this namespace in lode-e8lo so no other module observes the
  fake). That is the same live-binding problem as the six names above, so
  ``lode.cli.work`` must call ``cli.time.monotonic()`` / ``cli.time.sleep()``;
  a plain ``import time`` there would bind the real module and the fake clock
  would never be seen -- ``work --wait``'s timeout tests would spin instead of
  trip.
* ``uuid`` is patched as an ATTRIBUTE ON THE SHARED MODULE OBJECT:
  ``monkeypatch.setattr(cli.uuid, "uuid4", ...)`` (``tests/test_cli.py``).
  That mutates the stdlib module every importer already shares, so
  ``lode.cli.add``'s plain ``uuid.uuid4()`` sees it and needs no indirection.

Both ``import`` lines below are load-bearing regardless: they are what make
``cli.time`` / ``cli.uuid`` resolve for those patches to target at all.
"""

import logging
import sqlite3
import tempfile
import time  # noqa: F401 -- rebound by name in tests; call sites use `cli.time` (see module docstring)
import tomllib
import uuid  # noqa: F401 -- re-exported so `cli.uuid` resolves for tests (see module docstring)
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any, NoReturn

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

#: ``ctx.meta`` key ``_HelpAwareGroup.resolve_command()`` stashes its answer
#: under -- see that class's docstring and ``_help_requested()`` below
#: (lode-rtcx).
_META_SUBCOMMAND_HELP_REQUESTED = "lode.cli.subcommand_help_requested"


class _HelpAwareGroup(typer.core.TyperGroup):
    """``TyperGroup`` that records, in ``ctx.meta``, whether the subcommand
    about to be dispatched will itself end up printing ``--help`` output --
    e.g. ``lode notes --help`` (lode-rtcx, deepening lode-moq7's process-
    global ``sys.argv`` sniff into something read off the actual invocation).

    Click resolves a subcommand's own residual args (e.g. the ``--help`` in
    ``lode notes --help``) here, in ``resolve_command()``, BEFORE the group
    callback (``main()``, below) is ever invoked -- verified against
    ``typer.core.TyperGroup.resolve_command`` in typer 0.27.1 (which
    delegates to its OWN vendored fork of Click 8.3.1's ``Group
    .resolve_command`` -- ``typer/_click/`` is a bundled copy, not the
    separately-installed ``click`` 8.4.2 package; behaviorally identical
    here, confirmed by reading both): ``args[1:]`` (the subcommand's own
    remaining args) is returned as this method's third tuple element and is
    not otherwise exposed on ``ctx`` -- see ``_help_requested()``'s
    docstring for why that means there is no OTHER ``ctx``-based signal to
    read once ``main()`` runs. Matches against ``ctx.help_option_names`` --
    Click's actual configured help flags (``['--help']`` by default) --
    rather than a hardcoded ``'--help'`` literal, so a future ``-h`` alias
    (or any ``context_settings`` override) stays covered without a code
    change here.
    """

    # ``Any`` on purpose: the true types are Click's ``Context``/``Command``,
    # nameable only from typer's PRIVATE vendored ``typer._click`` (and a
    # direct ``click`` import would trip tests/test_deps_declared.py). The
    # public typer names would be WRONG, not merely loose -- ctx is the
    # vendored ``Context``, not ``typer.Context``, and for a sub-``Typer``
    # (``lode models``) the resolved command is a ``TyperGroup``, which is not
    # a ``TyperCommand``.
    def resolve_command(
        self, ctx: Any, args: list[str]
    ) -> tuple[str | None, Any, list[str]]:
        cmd_name, cmd, remaining_args = super().resolve_command(ctx, args)
        ctx.meta[_META_SUBCOMMAND_HELP_REQUESTED] = any(
            arg in ctx.help_option_names for arg in remaining_args
        )
        return cmd_name, cmd, remaining_args


app = typer.Typer(
    name="lode",
    help="AI-first personal knowledge base for things you learn at work.",
    no_args_is_help=True,
    add_completion=False,
    cls=_HelpAwareGroup,
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
#: mechanism this relies on. That is a statement about THIS object: a test
#: needing a substitute Console rebinds the ``console`` NAME on the single
#: command submodule it exercises (``lode.cli.status``, lode-nftw), so no
#: seam on the shared object is required.
#:
#: ``highlight=False`` (lode-re0s) is process-wide colour POLICY: rich's
#: default ``ReprHighlighter`` runs over every plain string rendered
#: through a ``Console``, injecting ``repr.*`` styles absent from
#: :data:`CLI_STYLES` -- a rendered date like ``2026-07-16 14:32`` gets
#: shredded into bold-cyan ``repr.number`` numerals and a bold-green
#: ``repr.ipv6`` time (yes, the clock reads as an IPv6 address), with the
#: separators left unstyled between them (verified against rich 15.0.0 --
#: ``ReprHighlighter()("2026-07-16 14:32").spans``). ``Table`` rendering
#: never runs this highlighter over cell text, so there is no blast
#: radius there, and a per-call
#: ``highlight=True`` still works where wanted -- nothing is foreclosed.
#: So this Console hoists ``highlight=False`` once at construction instead
#: of leaving it a per-call-site kwarg, and every command below relies on
#: that. IF A SECOND Console IS EVER ADDED to this module (as
#: ``err_console`` below already has been -- lode-l810), it MUST also pass
#: ``highlight=False``, for the same reason.
console = Console(theme=CLI_THEME, highlight=False)

#: A STDERR twin of ``console`` above (lode-l810) -- same theme, same
#: colour/width auto-detection rules, but writing to stderr rather than
#: stdout. ``highlight=False`` is a constructor kwarg here too (lode-9jmv),
#: per the hoisted-highlight convention ``console`` above documents.
err_console = Console(theme=CLI_THEME, stderr=True, highlight=False)

#: ``[cli.theme.styles]`` key -> the semantic ``CLI_STYLES`` name it
#: overrides (the ``_``-for-``.`` mapping so ``table.header`` becomes
#: ``table_header``, since TOML cannot key a table with a literal ``.``
#: anyway). Derived from :data:`CLI_STYLES` so the two key sets can never
#: disagree; ``tests/test_cli_theme_config.py`` pins this mapping's key set
#: equal to ``lode.config.CliThemeStyles``'s declared fields.
CLI_STYLE_KEY_TO_NAME: dict[str, str] = {
    name.replace(".", "_"): name for name in CLI_STYLES
}


def resolve_cli_styles(settings: Settings) -> dict[str, str]:
    """The fully-resolved ``[cli.theme]`` semantic style map: :data:`CLI_STYLES`
    with any ``[cli.theme.styles]`` overrides in ``settings`` applied on top.

    Keyed by SEMANTIC name (e.g. ``"table.header"``) -- the same shape as
    :data:`CLI_STYLES` -- so the result can be handed straight to
    ``rich.theme.Theme``. Always a fresh dict -- never :data:`CLI_STYLES`
    itself, so a caller can never mutate the shared defaults -- and equal to
    :data:`CLI_STYLES` when ``[cli.theme]`` is absent (the "absent section
    leaves defaults unchanged" acceptance criterion).
    """
    resolved = dict(CLI_STYLES)
    theme_cfg = settings.cli.theme
    if theme_cfg is None:
        return resolved
    for key, name in CLI_STYLE_KEY_TO_NAME.items():
        value = getattr(theme_cfg.styles, key)
        if value is not None:
            resolved[name] = value
    return resolved


#: Tracks whether ``main()`` currently has a ``[cli.theme]`` override layer
#: pushed onto ``console``/``err_console``'s theme stacks, so a later
#: invocation in the SAME process (a test's ``CliRunner``, or the TUI's own
#: re-invocation path) pops the previous layer before pushing a new one
#: instead of growing the stack unboundedly one layer per invocation.
_cli_theme_pushed = False


def _apply_cli_theme(settings: Settings | None) -> None:
    """Push (or clear) the effective ``[cli.theme]`` override layer onto the
    shared ``console``/``err_console``, called once per CLI invocation from
    ``main()``.

    ``settings is None`` (the ``lode status`` config-resolution-failed case)
    clears any previously-pushed override and leaves the two Consoles on
    their :data:`CLI_THEME` base -- never raises.
    """
    global _cli_theme_pushed
    if _cli_theme_pushed:
        console.pop_theme()
        err_console.pop_theme()
        _cli_theme_pushed = False
    if settings is None:
        return
    resolved = resolve_cli_styles(settings)
    # Equality, not identity: a present-but-empty ``[cli.theme]`` (a bare
    # section header, or a pasted ``lode theme export`` block edited back to
    # the defaults) resolves to the default map too, and pushing a layer
    # identical to the base buys nothing.
    if resolved == CLI_STYLES:
        return
    theme = Theme(resolved)
    console.push_theme(theme)
    err_console.push_theme(theme)
    _cli_theme_pushed = True


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


@dataclass
class CliObj:
    """``ctx.obj`` payload threaded from ``main()`` down to every subcommand.

    ``debug`` is the resolved ``--debug`` flag, preserved so ``tui``'s
    file-only re-configure (lode-1i8.2) can carry it across that second
    ``configure_logging`` call. ``settings`` is the ``Settings`` ``main()``
    already resolved once for this invocation (via the ``[cli.theme]``
    wiring below, lode-mk9j) -- threaded down so a subcommand that needs the
    identical resolution (e.g. ``lode theme export``) reads it back instead
    of resolving ``config.toml`` a second time (lode-9otn). ``None`` only
    when ``main()`` never got to that resolution at all: no subcommand is
    about to run (bare ``lode`` / ``--help`` under ``no_args_is_help``), or
    ``lode status``'s swallow-on-failure path.
    """

    debug: bool
    settings: Settings | None = None


def _help_requested(ctx: typer.Context) -> bool:
    """True when the subcommand this invocation is about to dispatch to will
    itself end up printing ``--help`` output (e.g. ``lode notes --help``),
    and so has nothing to restyle or fail loudly over (lode-moq7, deepened
    by lode-rtcx).

    Top-level ``lode --help`` never reaches here at all: Click's own
    ``--help`` option is eager and prints/exits during argument parsing,
    before the group callback (``main()``, this function's only caller) is
    ever invoked -- so this only needs to cover the SUBcommand-``--help``
    case. Reads the answer ``_HelpAwareGroup.resolve_command()`` already
    computed off Click's own parse state and stashed on ``ctx.meta`` --
    Click resolves a subcommand's own residual args before ``main()`` ever
    runs, so there is no OTHER ``ctx``-based signal available at this point
    (see ``_HelpAwareGroup``'s docstring). Defaults to ``False`` when the
    key is absent (e.g. no subcommand was invoked at all).
    """
    return bool(ctx.meta.get(_META_SUBCOMMAND_HELP_REQUESTED, False))


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
    # ``ctx.obj`` (a :class:`CliObj`) so ``tui``'s file-only re-configure
    # (lode-1i8.2) can preserve it across that second ``configure_logging``
    # call.
    level = logging.DEBUG if debug else None
    configure_logging(level=level, log_dir=log_dir())
    obj = CliObj(debug=debug)
    ctx.obj = obj

    # Reset the per-invocation _resolve_settings() cache (lode-bsga) -- this
    # is the group callback, so it runs exactly once per invocation, before
    # any subcommand body. See _resolve_settings()'s docstring for why that
    # reset is what makes a module-level cache safe here.
    global _settings_cache
    _settings_cache = None

    # [cli.theme] resolution + application (lode-mk9j) -- global, here in
    # main(), so it covers every subcommand, including one like ``lode
    # notes`` that never otherwise calls ``_resolve_settings()``. Skipped
    # when no subcommand is actually about to run (bare ``lode`` / ``--help``
    # under ``no_args_is_help``), since there's nothing to restyle for --
    # and skipped too when a SUBcommand's own ``--help`` was requested
    # (``lode notes --help``, lode-moq7): the group callback runs before
    # Click parses the subcommand's own args, so an unrelated malformed
    # config.toml previously took even a pure ``--help`` invocation down.
    # ``--help`` never reads config, so there is nothing to restyle or fail
    # loudly over either way -- see docs/decisions.md's lode-moq7 entry
    # (and its lode-rtcx correction: the detection below reads Click's own
    # parse state via ``_HelpAwareGroup``, not process-global ``sys.argv``).
    #
    # ``lode status`` ALONE swallows a failed resolution, keeping its
    # pre-existing lode-l38d.6 survival contract; every OTHER command lets
    # the failure propagate, so ANY config error now fails loudly even on a
    # command that never read config before. Both halves are the maintainer's
    # 2026-08-18 placement decision -- the rationale and the rejected
    # alternatives live in docs/decisions.md's lode-mk9j entry, not here.
    # ``except Exception``, not ``except typer.Exit``, for the same reason
    # status.py's own guard uses it: an unreadable config.toml raises a bare
    # ``OSError`` straight through ``_resolve_settings``, above the
    # ``TOMLDecodeError``/``ValidationError`` it converts to ``typer.Exit``.
    #
    # The resolved ``settings`` is also stashed on ``obj.settings`` (lode-9otn)
    # -- ``lode theme export`` reads it back from there instead of calling
    # ``_resolve_settings()`` a second time.
    if ctx.invoked_subcommand is not None and not _help_requested(ctx):
        settings: Settings | None
        if ctx.invoked_subcommand == "status":
            try:
                settings = _resolve_settings()
            except Exception:
                log.debug("main: _resolve_settings failed for status", exc_info=True)
                settings = None
        else:
            settings = _resolve_settings()
        obj.settings = settings
        _apply_cli_theme(settings)


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
        help="SQLite database path (default: $LODE_HOME/lode.db).",
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


#: This invocation's resolved settings, or ``None`` for "not resolved yet"
#: (lode-bsga). ``None`` is unambiguous here precisely because a *failed*
#: resolution is never cached -- see :func:`_resolve_settings`, which is the
#: only writer, and only ever on success.
_settings_cache: Settings | None = None


def _resolve_settings() -> Settings:
    """Resolve settings for one command, reporting a bad config file the CLI way.

    :func:`lode.config.load_settings` raises on an unusable
    ``$LODE_HOME/config.toml`` — ``TOMLDecodeError`` for a syntax error,
    pydantic's ``ValidationError`` for an unknown key or an out-of-range value.
    Raising is right for a *library* caller (a test asserts on it), but this
    file is hand-edited by the user, so at the CLI boundary an uncaught raise
    dumps a Python traceback at the terminal over a typo. Convert it to the
    one-line stderr message + exit 1 that every other user-facing CLI failure
    here uses (lode-40g).

    ``main()`` already resolves settings once per invocation (for the
    ``[cli.theme]`` wiring, lode-mk9j) and threads the result down via
    ``ctx.obj`` (a :class:`CliObj`, lode-9otn); a command that only needs
    that same resolution should read ``ctx.obj.settings`` instead of calling
    this again. Today ``lode theme export`` is the only one that does --
    every other command still calls this a second time, a pre-existing
    duplication tracked for migration in lode-47he, not a documented
    exemption.

    **Caches the first successful resolution for the rest of this
    invocation** (lode-bsga): ``main()`` — this Typer app's group callback —
    already resolves settings once per invocation for ``[cli.theme]``
    application (lode-mk9j); without a cache, the subcommand body then
    resolved a second, redundant ``load_settings()``. ``main()`` resets the
    cache to ``None`` at the top of every invocation, so this never leaks a
    resolution across separate CLI invocations (or across a CliRunner test
    suite's repeated in-process calls, which drive ``app`` many times in one
    process).

    A *failed* resolution is deliberately NOT cached, and that is
    **load-bearing rather than merely harmless**: ``lode status``'s
    ``lode-l38d.6`` survival contract is two independent swallowed attempts
    — ``main()`` swallows one, ``status``'s own body re-attempts and swallows
    again. Caching the failure would collapse those into a single attempt and
    quietly change that contract, so the second real ``load_settings()`` call
    on a broken ``config.toml`` is the point, not an oversight.
    ``tests/test_cli_settings_cache.py`` pins it.

    Not :func:`functools.cache`, despite the shape fitting (no arguments, and
    ``lru_cache`` likewise declines to cache exceptions): the reset would have
    to be ``_resolve_settings.cache_clear()``, and tests monkeypatch
    ``lode.cli._resolve_settings`` wholesale with a plain function (see
    ``tests/test_cli.py``'s unreadable-config case), which has no
    ``cache_clear`` — ``main()`` would then die with ``AttributeError`` on
    exactly the error path that seam exists to exercise.

    Defined here, on the package itself, rather than in a command submodule
    (lode-35nu.9): nearly every command calls this, and it is independently
    monkeypatched as ``lode.cli._resolve_settings`` — every command therefore
    reaches it via ``cli._resolve_settings()`` (see this module's own
    docstring for why), which only works if the name actually lives on the
    package.
    """
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    try:
        settings = load_settings()
    except (tomllib.TOMLDecodeError, ValidationError) as err:
        typer.echo(f"invalid config file {config_path()}: {err}", err=True)
        raise typer.Exit(code=1) from None
    _settings_cache = settings
    return settings


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


# --- command module registration ---------------------------------------------
#: The command modules, in the order their commands must REGISTER (lode-35nu.9).
#:
#: Order is load-bearing and user-visible: Typer lists subcommands in
#: registration order, so this tuple IS the order of ``lode --help``'s command
#: table -- which the split must preserve byte-for-byte, exactly as it
#: preserves each command's name and flags. It reproduces the order the
#: commands were defined in when ``lode.cli`` was one flat module.
#:
#: Registration is driven by this explicit tuple rather than by a block of
#: twenty ``from lode.cli import <module>`` statements because import order is
#: the wrong place to encode it twice over: the isort rules ruff enforces would
#: re-sort such a block alphabetically, AND a module pulled in transitively by
#: an earlier sibling (``egress``/``jobs``/``work`` all import
#: ``lode.cli.status``) registers at the point of the *transitive* import, not
#: at its own line -- so the statement order and the real order silently
#: disagree. Naming the order once, here, is immune to both.
#:
#: ``models`` and ``theme`` attach their own sub-``Typer`` instead of a flat
#: ``@app.command``. click renders every sub-``Typer`` AFTER every flat
#: command, in sub-``Typer`` registration order -- so their position here fixes
#: only their order relative to each other, never their absolute position in
#: the help table. Keep such modules trailing in this tuple so its order does
#: not lie about the rendered one; the rendered order itself is asserted by
#: tests/test_cli.py's ``HELP_COMMAND_ORDER``.
_COMMAND_MODULES = (
    "add",
    "ask",
    "purge",
    "recover",
    "notes",
    "show",
    "status",
    "stats",
    "reembed",
    "reindex_lexical",
    "reenrich",
    "jobs",
    "egress",  # also registers `no-egress`
    "dump_html",
    "config",
    "verify",
    "tui",
    "version",
    "work",
    "backfill",
    "theme",
    "models",
)

for _name in _COMMAND_MODULES:
    import_module(f"lode.cli.{_name}")
del _name

# --- backward-compatible re-exports ------------------------------------------
# A handful of names tests still reach as `lode.cli.<name>`. Plain re-exports
# (unlike the call-through-the-package names documented in this module's own
# docstring, which are never imported anywhere -- every INTERNAL call site
# reaches them via `cli.<name>` instead).
#
# `_retrieve` used to be re-exported here; it now lives in `lode.retrieval`
# (lode-z3es), which every caller imports directly.
from lode.cli.ask import (  # noqa: F401
    _ABSTAIN_LINE,
    _format_citation,
    _format_cited_answer,
)
from lode.cli.models import _FASTEMBED_EXHAUSTED_SOURCES, _warm  # noqa: F401
from lode.cli.status import (  # noqa: F401
    EgressPurpose,
    JobStatus,
    _cache_hit,
    _cold_model_cache,
    _enrichment_model_stale,
    _lexical_gap_count,
    _model_cache_probe,
    _model_revision_status,
    _short,
)
from lode.cli.verify import _default_verify_fetcher  # noqa: F401
from lode.enrichment_view import (
    stale_enrichment_heads as _stale_enrichment_heads,  # noqa: F401
)

if __name__ == "__main__":  # pragma: no cover
    app()
