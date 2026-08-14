#!/usr/bin/env python3
"""Generate the docs-site's user-facing reference pages from their maintainer sources
(lode-fhql.15).

docs/stack.md's "Published / excluded page sets" (lode-fhql.8) excludes docs/keybindings.md and
docs/configuration.md from the site because they're addressed to whoever *builds* lode next, not
whoever *uses* it -- but each holds genuinely user-facing content trapped inside maintainer prose:
docs/keybindings.md's "Current keymap" tables, and docs/configuration.md's `runtime`-kind rows.
This script derives docs/keymap.md and docs/settings.md from those tables mechanically, so the two
copies cannot silently diverge -- regenerating is the sync mechanism, not hand-copying. See
docs/stack.md's "Derived reference pages" section for the full contract, and
tests/test_generate_derived_docs.py for the drift gate that regenerates and diffs on every test run.

Usage:
    scripts/generate_derived_docs.py           # regenerate docs/keymap.md + docs/settings.md in place
    scripts/generate_derived_docs.py --check   # regenerate to memory and diff against the committed
                                               # files; exit 1 (naming the drifted file) on any diff

If a source doc's headings or table shape change enough that a page can no longer be derived, this
raises `SourceDocChanged` rather than emitting a thinner page -- `--check` would otherwise report
only "stale, regenerate", inviting a maintainer to commit the truncated result.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Annotated

import typer

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

app = typer.Typer(add_completion=False)


class SourceDocChanged(Exception):
    """A source doc no longer has the structure this generator reads.

    Raised rather than silently emitting a thinner page: a renamed heading or a reshaped table
    would otherwise drop rows quietly, and `--check` would then report only "stale, regenerate" --
    inviting a maintainer to commit the truncated page and lose the content for good.
    """


# Friendlier, reader-facing names for the TUI's Screen classes -- someone using lode doesn't think
# in terms of Python class names. Keys are matched against the source table's Screen cell normalized
# by `_screen_key` -- backticks dropped and any trailing parenthetical aside ("(default screen)",
# "(focusable widget, not a Screen)") cut -- so they are bare class names here, and rewording one of
# those asides in the source doc cannot silently stop a label from matching. Any screen name found
# in the source table but missing here falls back to a de-camel-cased version of the class name
# (see `_fallback_label`), so a new screen never goes unlisted -- it just gets a slightly less
# polished label until this map is extended.
SCREEN_LABELS = {
    "VersionHistoryScreen": "Version history",
    "VersionViewScreen": "Viewing an old version",
    "EnrichmentModalScreen": "Enrichment inspector",
    "YesNoConfirmScreen": "Yes/No confirmation dialogs",
    "BrowseScreen": "Browse",
    "ExternalPickerScreen": "External-source picker",
    "TagsScreen": "Tags",
    "SnapshotViewerScreen": "Viewing a saved web snapshot",
    "EditScreen": "Editing a note",
    "DiscardConfirmScreen": "Discard-and-quit confirmation",
    "CaptureScreen": "Capture (the screen you land on)",
    "AskScreen": "Ask",
    "ConfigScreen": "Config",
    "ReconcileScreen": "Reconcile",
    "RelatedNotesPanel": "The related-notes panel",
    "RelatedNoteModalScreen": "Viewing a related note",
    "HelpScreen": "The keybinding help overlay",
}


def _screen_key(screen: str) -> str:
    """The source table's Screen cell, normalized for `SCREEN_LABELS` lookup: the cell wraps the
    class name in backticks and sometimes trails a parenthetical aside, neither of which the map's
    bare-class-name keys carry. `_fallback_label` normalizes the same way, so an unmapped screen and
    a mapped one are keyed off identical text."""
    return screen.replace("`", "").split(" (")[0].strip()


def _fallback_label(screen: str) -> str:
    base = screen.split(" (")[0]
    words = re.findall(r"[A-Z][a-z0-9]*", base)
    return " ".join(words) if words else base


def _split_row(line: str) -> list[str] | None:
    """Split one `| a | b | c |` markdown table row into cells, honoring `\\|` as a literal pipe."""
    line = line.strip()
    if not line.startswith("|"):
        return None
    # Split on an unescaped pipe only. `\|` (an escaped, literal pipe inside a cell) is left
    # untouched -- cell text is re-emitted verbatim into a new table below, where it still needs
    # to be escaped the same way or it would end the cell early.
    raw_cells = re.split(r"(?<!\\)\|", line)
    # First/last elements are the empty strings outside the leading/trailing pipes.
    cells = [c.strip() for c in raw_cells[1:-1]]
    return cells


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-+:?", c) for c in cells)


# What makes a parenthetical maintainer-only: a bd ticket id, the Textual footer-visibility flag,
# or a source-file path (`screens/browse.py`) -- each names something only someone editing lode can
# act on. Anything else in parentheses is prose a reader wants and is left alone.
_DEV_ASIDE_MARKER_RE = re.compile(r"lode-[a-z0-9][a-z0-9.]*|show=False|[\w/]+\.py\b")


def _github_slug(heading_text: str) -> str:
    """GitHub's heading-to-anchor slug, so an emitted `<a id=...>` matches the anchor GitHub derives
    from the same heading and one link works in both renderers (GitHub's auto anchor; the explicit
    tag under MkDocs, whose own slug algorithm differs on punctuation).

    `scripts/check_links.py::github_slug` is the authority for this algorithm. Copied rather than
    imported because `scripts/` is not a package and this file is executed three different ways (as
    a script, as a pytest subprocess, and from a scratch copy in `tmp_path`), so an
    `import check_links` would resolve in some of them and not others;
    `tests/test_generate_derived_docs.py` asserts the copy still agrees with the authority.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading_text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _balanced_paren_spans(text: str) -> list[tuple[int, int]]:
    """Top-level `(...)` spans in `text`, honoring nesting (e.g. a `(see [x](url))` citation) so a
    span is never split in the middle of a markdown link's own parens."""
    spans = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    spans.append((start, i + 1))
    return spans


def _strip_dev_asides(text: str) -> str:
    """Drop TOP-LEVEL parenthetical asides that cite a bd ticket id or `show=False` --
    maintainer-facing provenance/footer-visibility trivia, not something someone using lode needs
    to know. Nesting-aware, so a citation like `(see [stack.md](stack.md#lode-123))` is dropped as
    one whole span rather than leaving a dangling `(see [stack.md])` behind. Leaves any other
    parenthetical untouched. One right-to-left pass suffices: dropping a top-level span removes its
    nested spans with it, so no new top-level span is ever exposed by a removal."""
    for start, end in reversed(_balanced_paren_spans(text)):
        if _DEV_ASIDE_MARKER_RE.search(text[start:end]):
            text = text[:start].rstrip() + text[end:]
    # A markdown link whose `(url)` half was just dropped above (the url pointed at a bd-id
    # anchor) leaves an orphaned `[label]` with no following `(...)` -- no longer a link, just
    # bracket noise. Unwrap it to plain text rather than shipping dead-looking brackets.
    text = re.sub(r"\[([^\]]+)\](?!\()", r"\1", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Keymap (docs/keybindings.md -> docs/keymap.md)
# ---------------------------------------------------------------------------


def _column(header: list[str], name: str, source: str) -> int:
    """The index of the `name` column in a source table's header row.

    Read by NAME, never by position: an inserted or reordered column is the likeliest future edit to
    either source doc, and a positional read would survive it silently -- emitting, say, the File
    column under a "Key" heading. A missing name raises instead.
    """
    try:
        return header.index(name)
    except ValueError:
        raise SourceDocChanged(
            f"{source} no longer has a {name!r} column -- its table header now reads "
            f"{header!r}. Re-point scripts/generate_derived_docs.py at the new columns rather "
            "than shipping a page built from the wrong ones."
        ) from None


def _extract_tables(
    lines: list[str], section_heading: str
) -> list[tuple[str, list[str], list[list[str]]]]:
    """Within the `## <section_heading>` section, return every (subheading, header, rows) markdown
    table.

    `subheading` is the nearest `###`/`##` heading text above the table (used to label App-level vs
    Screen-level below); `header` is the table's header cells; `rows` excludes the header and
    separator rows.
    """
    in_section = False
    current_subheading = ""
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    pending: tuple[list[str], list[list[str]]] | None = None

    def close() -> None:
        nonlocal pending
        if pending is not None:
            tables.append((current_subheading, pending[0], pending[1]))
        pending = None

    for line in lines:
        if line.startswith("## "):
            if line[3:].strip() == section_heading:
                in_section = True
                pending = None
                continue
            if in_section:
                break  # next top-level section ends this one
            continue
        if not in_section:
            continue
        if line.startswith("### "):
            current_subheading = line[4:].strip()
            pending = None
            continue
        cells = _split_row(line)
        if cells is None:
            close()
            continue
        if pending is None:
            pending = (cells, [])
            continue
        if _is_separator_row(cells):
            continue
        pending[1].append(cells)

    close()
    return tables


def _forward_fill(rows: list[list[str]], columns: int) -> list[list[str]]:
    """A markdown table that visually groups repeated cells leaves later rows' leading columns
    blank (just an empty cell). Carry the last non-empty value forward."""
    last: list[str] = [""] * columns
    filled = []
    for row in rows:
        new_row = list(row)
        for i in range(columns):
            if new_row[i]:
                last[i] = new_row[i]
            else:
                new_row[i] = last[i]
        filled.append(new_row)
    return filled


def generate_keymap() -> str:
    src = (DOCS_DIR / "keybindings.md").read_text(encoding="utf-8")
    lines = src.splitlines()
    tables = _extract_tables(lines, "Current keymap")

    src_name = "docs/keybindings.md"
    app_rows: list[list[str]] = []
    app_cols: dict[str, int] = {}
    screen_rows: list[list[str]] = []
    screen_cols: dict[str, int] = {}
    for subheading, header, rows in tables:
        if subheading.startswith("App-level"):
            app_rows = rows
            app_cols = {n: _column(header, n, src_name) for n in ("Key", "Action")}
        elif subheading == "Screen-level":
            screen_rows = _forward_fill(rows, len(header))
            screen_cols = {
                n: _column(header, n, src_name) for n in ("Screen", "Key", "Action")
            }
    if not app_rows or not screen_rows:
        raise SourceDocChanged(
            "docs/keybindings.md no longer yields the tables this page is built from: expected a "
            '"## Current keymap" section holding an "### App-level ..." table and an '
            f'"### Screen-level" table (found {len(app_rows)} app-level and {len(screen_rows)} '
            "screen-level rows). Re-point scripts/generate_derived_docs.py at the new headings "
            "rather than shipping a page that silently lost them."
        )

    out: list[str] = []
    out.append("# lode — keyboard shortcuts")
    out.append("")
    out.append(
        "A reference for every key lode's TUI responds to, for someone **using** lode day to "
        "day -- not for someone adding or rebinding a key (that's "
        "[keybindings.md](https://github.com/bildzeitung/lode/blob/trunk/docs/keybindings.md), "
        "the maintainer doc this page is generated from)."
    )
    out.append("")
    out.append(
        "**The live, always-current list is one keypress away.** Press `Ctrl+_` (or `?` on most "
        "screens) inside lode any time to open the in-app help overlay -- it lists every binding "
        "on the screen you're on, including a few kept off the footer to save space. This page is "
        "a convenience for browsing outside the app; the overlay is the definitive source at "
        "runtime."
    )
    out.append("")
    out.append("## Keys that work everywhere")
    out.append("")
    out.append("| Key | Action |")
    out.append("|---|---|")
    for row in app_rows:
        key = _strip_dev_asides(row[app_cols["Key"]])
        action = _strip_dev_asides(row[app_cols["Action"]])
        out.append(f"| {key} | {action} |")
    out.append("")
    out.append("## Keys by screen")
    out.append("")
    out.append(
        "Each screen also has its own keys, active only while that screen is showing. A key "
        "listed here can differ from what the same key does elsewhere in the app -- lode "
        "resolves whichever binding belongs to the screen you're currently on first."
    )
    out.append("")

    # dict preserves insertion order, so this is also the order the screens appear in the source.
    grouped: dict[str, list[list[str]]] = {}
    for row in screen_rows:
        grouped.setdefault(_screen_key(row[screen_cols["Screen"]]), []).append(row)

    for screen, rows in grouped.items():
        out.append(f"### {SCREEN_LABELS.get(screen, _fallback_label(screen))}")
        out.append("")
        out.append("| Key | Action |")
        out.append("|---|---|")
        for row in rows:
            key = _strip_dev_asides(row[screen_cols["Key"]])
            action = _strip_dev_asides(row[screen_cols["Action"]])
            out.append(f"| {key} | {action} |")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Settings (docs/configuration.md -> docs/settings.md, runtime rows only)
# ---------------------------------------------------------------------------

# The columns every `| Knob | Kind | Default | Notes |` table in docs/configuration.md declares.
# Doubles as the table SNIFFER (any one of them present marks a header row) and as the list read
# by name below -- one concept, so a renamed column can never make a table go unrecognized instead
# of raising.
_KNOB_TABLE_COLUMN_NAMES = ("Knob", "Kind", "Default", "Notes")


def generate_settings() -> str:
    src = (DOCS_DIR / "configuration.md").read_text(encoding="utf-8")
    lines = src.splitlines()

    out: list[str] = []
    out.append("# lode — settings you can change")
    out.append("")
    out.append(
        "Every knob lode reads that you can change **while running it** -- an environment "
        "variable, a `config.toml` value, or a CLI flag. This is a filtered view of "
        "[configuration.md](https://github.com/bildzeitung/lode/blob/trunk/docs/configuration.md), "
        "the maintainer doc this page is generated from: that doc catalogues every tunable "
        "*and* every build-time knob together; this page keeps only the rows marked `runtime` "
        "there, since a build-time knob isn't something you can act on."
    )
    out.append("")
    out.append(
        "See [Paths & locations](#paths--locations) below for where `config.toml` lives and its "
        "format; a knob not listed there defaults, and there is no requirement to have a "
        "`config.toml` at all."
    )
    out.append("")

    current_heading = ""
    cols: dict[str, int] | None = None
    pending_rows: list[list[str]] = []
    emitted = 0

    def flush() -> None:
        nonlocal pending_rows, emitted
        rows, pending_rows = pending_rows, []
        if cols is None:
            return
        runtime_rows = [r for r in rows if r[cols["Kind"]].strip() == "runtime"]
        if not runtime_rows:
            return
        heading = _strip_dev_asides(current_heading)
        out.append(f'<a id="{_github_slug(heading)}"></a>')
        out.append(f"## {heading}")
        out.append("")
        out.append("| Setting | Default | Notes |")
        out.append("|---|---|---|")
        for row in runtime_rows:
            out.append(
                f"| {row[cols['Knob']]} | {row[cols['Default']]} "
                f"| {_strip_dev_asides(row[cols['Notes']])} |"
            )
        out.append("")
        emitted += len(runtime_rows)

    in_table = False
    for line in lines:
        if line.startswith("## "):
            # A section's tables are emitted together, under that section's heading.
            flush()
            current_heading = line[3:].strip()
            cols = None
            in_table = False
            continue
        cells = _split_row(line)
        if cells is None:
            in_table = False
            continue
        if set(_KNOB_TABLE_COLUMN_NAMES) & set(cells):
            # A knob table is anything declaring ANY of the four column names -- not `Knob` alone.
            # Recognizing it by one column would make renaming THAT column skip the whole table
            # silently, emitting a page quietly missing a section: exactly the thinner-page failure
            # `_column` exists to raise on. Declaring some of them means a reshaped knob table, and
            # falls through to `_column` below, which raises.
            # Read by name from here on, so an inserted or reordered column raises rather than
            # silently shifting (e.g. emitting the Kind column as the Default).
            header_cols = {
                n: _column(cells, n, "docs/configuration.md")
                for n in _KNOB_TABLE_COLUMN_NAMES
            }
            if cols is not None and header_cols != cols:
                raise SourceDocChanged(
                    f"docs/configuration.md's {current_heading!r} section holds two knob tables "
                    f"with different column orders ({cols!r} then {header_cols!r}); their rows are "
                    "emitted as one table and can no longer be read the same way."
                )
            cols = header_cols
            in_table = True
            continue
        if not in_table:
            continue
        if _is_separator_row(cells):
            continue
        pending_rows.append(cells)

    flush()
    if not emitted:
        raise SourceDocChanged(
            "docs/configuration.md yielded no `runtime`-kind rows: this page is built from every "
            "`| Knob | Kind | Default | Notes |` table there whose Kind cell is `runtime`, so a "
            "renamed header cell or a reshaped Kind column empties it. Re-point "
            "scripts/generate_derived_docs.py at the new shape rather than shipping an empty page."
        )

    return "\n".join(out).rstrip() + "\n"


@app.command(
    help="Regenerate the docs site's derived reference pages (docs/keymap.md, docs/settings.md) "
    "from docs/keybindings.md and docs/configuration.md.\n\n"
    "Run it after editing either source doc. Pass --check to verify instead of write: it exits 1, "
    "naming the stale file, when a committed page is not what the sources would produce now."
)
def main(
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Diff the regenerated pages against the committed ones instead of writing them.",
        ),
    ] = False,
) -> None:
    pages = {
        DOCS_DIR / "keymap.md": generate_keymap(),
        DOCS_DIR / "settings.md": generate_settings(),
    }

    if check:
        drift = [
            str(path)
            for path, want in pages.items()
            if not path.exists() or path.read_text(encoding="utf-8") != want
        ]
        if drift:
            print(
                "docs-site derived pages are stale (lode-fhql.15) -- re-run "
                "scripts/generate_derived_docs.py and commit the result. Stale file(s): "
                + ", ".join(drift),
                file=sys.stderr,
            )
            raise typer.Exit(1)
        print("docs/keymap.md and docs/settings.md are up to date.")
        return

    for path, want in pages.items():
        path.write_text(want, encoding="utf-8")
    print("wrote docs/keymap.md and docs/settings.md")


if __name__ == "__main__":
    app()
