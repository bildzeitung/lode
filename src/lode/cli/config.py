"""``lode config`` -- show the resolved on-disk locations and every runtime/tune knob."""

from lode import cli
from lode.cli import SafeTable, _DbOption, _tabular_table, app, console
from lode.config import config_rows, default_db_path, knob_rows


def _config_path_table(rows: list[tuple[str, str, str]]) -> SafeTable:
    """Render :func:`~lode.config.config_rows`' output as a terminal-width-aware
    ``SafeTable`` (lode-l38d.4). No header, no box -- this block is a labelled
    dump, not a column-semantic listing, so it explicitly opts out of
    :func:`~lode.cli._tabular_table`'s house style rather than using it; its
    look is unchanged from before this ticket. The parenthetical annotation
    (``($LODE_HOME)``, ``(present)``/``(absent)``) lands in a real ``Note``
    column instead of being string-baked into ``Value`` (the TUI's Ctrl+O
    screen still bakes it in -- it renders :func:`lode.config.config_lines`
    untouched).

    ``overflow="fold"`` on ``Value``/``Note``: rich's ``Column`` default,
    ``overflow="ellipsis"``, DROPS characters off any unbreakable single-token
    string (e.g. a long absolute path) wider than its column instead of
    wrapping it -- verified against the installed rich (15.0.0). Unacceptable
    here, since this command's entire point is showing exact paths; nothing
    may ever be silently truncated. ``"fold"`` hard-breaks mid-word when it
    must, which is ugly but lossless.

    Cells are passed as bare ``str`` -- ``SafeTable.add_row`` wraps each in
    :class:`rich.text.Text` structurally (lode-9tmd), so a literal ``[...]``
    in a path (or, for the knob table below, a regex character class) can
    never be silently dropped by rich's markup parser. See ``SafeTable``'s
    docstring for the verification.
    """
    table = SafeTable(show_header=False, box=None, pad_edge=False)
    table.add_column("Label")
    table.add_column("Value", overflow="fold")
    table.add_column("Note", overflow="fold")
    for label, value, note in rows:
        # Parenthesized to match the pre-lode-l38d.4 baked-in text
        # (f"{value}  ({note})") -- moving it to its own column fixes the
        # actual bug (that text used to distort the Value column's computed
        # width), not the visual convention of wrapping it in parens.
        table.add_row(label, value, f"({note})" if note else "")
    return table


def _config_knob_table(rows: list[tuple[str, str, str]]) -> SafeTable:
    """Render :func:`~lode.config.knob_rows`' output as a terminal-width-aware
    ``SafeTable`` (lode-l38d.4) built via :func:`~lode.cli._tabular_table`'s
    house style -- header + separator rule (``box.SIMPLE_HEAD``, closing the
    ticket's "no header separator rule" gap), no side borders, keeping the
    previous plain-list look. The TUI renders the same row data into a
    ``DataTable`` widget instead (:mod:`lode.tui.screens.config`); only the
    row DATA is shared, not this formatting.

    A list-valued knob (comma+space-joined by :func:`~lode.config.knob_rows`)
    wraps at the space boundaries "for free" under ``overflow="fold"`` -- no
    need to un-join it or render it specially. This, plus terminal-width-aware
    column sizing instead of padding every row to the single widest value in
    the table, is what removes the original bug.

    Cells are passed as bare ``str`` -- several knob values here are regex
    character classes (``redact_before_egress_patterns`` et al:
    ``gh[pousr]_...``, ``xox[baprs]-...``), and ``SafeTable.add_row`` wraps
    each cell in :class:`rich.text.Text` structurally (lode-9tmd) before it
    ever reaches rich's markup parser, so a literal ``[...]`` can never be
    silently dropped. See ``SafeTable``'s docstring for the verification.
    """
    table = _tabular_table()
    table.add_column("Knob")
    table.add_column("Value", overflow="fold")
    table.add_column("Kind")
    for name, value, kind in rows:
        table.add_row(name, value, kind)
    return table


@app.command(
    help=(
        "Show the resolved on-disk locations and every runtime/tune knob.\n\n"
        "A read-out of the single-root layout under $LODE_HOME (default "
        "~/.lode): the root, the SQLite DB and its lock, the vector store, "
        "the model-weights cache, the log directory, and whether a "
        "config.toml is present. --db shifts the displayed DB (and its "
        "lock and co-located vector store) to an explicit override.\n\n"
        "Below the paths, a knob table lists every runtime/tune setting "
        "with its currently resolved value."
    )
)
def config(db: _DbOption = None) -> None:
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
    console.print(_config_knob_table(knob_rows(cli._resolve_settings())))
