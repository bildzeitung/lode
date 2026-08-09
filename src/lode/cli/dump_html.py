"""``lode dump-html`` -- print a note's drawn-down external's raw captured HTML."""

import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from lode.cli import _DbOption, _open_db, _report_ambiguous_prefix, app
from lode.enrichment_view import (
    EnrichmentView,
    ExternalView,
    enrichment_view_conn,
    raw_snapshot_payload,
)
from lode.ids import short_version_id
from lode.notes_read import list_notes_conn
from lode.repository import AmbiguousNoteIdError, Repository


def _render_external_choice(index: int, external: ExternalView) -> str:
    """Render one numbered listing line for ``dump-html``'s disambiguation prompt.

    Same fields :func:`~lode.cli.show._render_external` shows beneath a
    ``show`` edge line (source_type, short snapshot id, fetched_at, state),
    fronted by the 1-based ``index`` this command also accepts as a selector
    -- so what's printed here is exactly what a subsequent selector argument
    can reference back.
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
            raw_payload = raw_snapshot_payload(conn, external.snapshot_id)
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


@app.command(
    name="dump-html",
    help=(
        "Print a note's captured external HTML (or write it to a file).\n\n"
        "With one external, prints it immediately; with more than one, "
        "lists them for SELECTOR to pick by index or URL. --all dumps every "
        "note's external(s) instead of one TARGET; --file writes to disk "
        "(see --dir) instead of stdout."
    ),
)
def dump_html(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Note id, or an unambiguous prefix of one. Required unless "
            "--all is given; conflicts with --all."
        ),
    ] = None,
    selector: Annotated[
        str | None,
        typer.Argument(
            help="1-based listing index or external id (URL), to disambiguate "
            "a note with more than one external. Conflicts with --all."
        ),
    ] = None,
    all_notes: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Bulk mode: every live note's external(s). Conflicts with an "
            "explicit target/selector.",
        ),
    ] = False,
    file: Annotated[
        bool,
        typer.Option(
            "--file",
            help="Write to per-note file(s) named <note-id>-NNNN.dmp (see "
            "--dir) instead of stdout. Valid with or without --all.",
        ),
    ] = False,
    dir_: Annotated[
        Path | None,
        typer.Option(
            "--dir",
            help="Output directory for --file (created if absent). Default: "
            "the current directory. Only valid with --file.",
        ),
    ] = None,
    db: _DbOption = None,
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

        raw_payload = raw_snapshot_payload(conn, chosen.snapshot_id)
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
