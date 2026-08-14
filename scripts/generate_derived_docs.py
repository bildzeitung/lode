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
    scripts/generate_derived_docs.py            # regenerate docs/keymap.md + docs/settings.md in place
    scripts/generate_derived_docs.py --check     # regenerate to memory and diff against the committed
                                                  # files; exit 1 (naming the drifted file) on any diff
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# Friendlier, reader-facing names for the TUI's Screen classes -- someone using lode doesn't think
# in terms of Python class names. Any screen name found in the source table but missing here falls
# back to a de-camel-cased version of the class name (see `_fallback_label`), so a new screen never
# goes unlisted -- it just gets a slightly less polished label until this map is extended.
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
    "CaptureScreen (default screen)": "Capture (the screen you land on)",
    "AskScreen": "Ask",
    "ConfigScreen": "Config",
    "ReconcileScreen": "Reconcile",
    "RelatedNotesPanel (focusable widget, not a Screen)": "The related-notes panel",
    "RelatedNoteModalScreen": "Viewing a related note",
    "HelpScreen": "The keybinding help overlay",
}


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


_DEV_ASIDE_MARKER_RE = re.compile(r"lode-[a-z0-9][a-z0-9.]*|show=False")


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
    parenthetical untouched."""
    prev = None
    while prev != text:
        prev = text
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


def _extract_tables(
    lines: list[str], section_heading: str
) -> list[tuple[str, list[list[str]]]]:
    """Within the `## <section_heading>` section, return every (subheading, rows) markdown table.

    `subheading` is the nearest `###`/`##` heading text above the table (used to label App-level vs
    Screen-level below); `rows` excludes the header and separator rows.
    """
    in_section = False
    current_subheading = ""
    tables: list[tuple[str, list[list[str]]]] = []
    pending_rows: list[list[str]] | None = None
    header_seen = False

    for line in lines:
        if line.startswith("## "):
            if line[3:].strip() == section_heading:
                in_section = True
                pending_rows = None
                header_seen = False
                continue
            if in_section:
                break  # next top-level section ends this one
            continue
        if not in_section:
            continue
        if line.startswith("### "):
            current_subheading = line[4:].strip()
            pending_rows = None
            header_seen = False
            continue
        cells = _split_row(line)
        if cells is None:
            if pending_rows is not None:
                tables.append((current_subheading, pending_rows))
            pending_rows = None
            header_seen = False
            continue
        if not header_seen:
            header_seen = True
            pending_rows = []
            continue
        if _is_separator_row(cells):
            continue
        assert pending_rows is not None
        pending_rows.append(cells)

    if pending_rows is not None:
        tables.append((current_subheading, pending_rows))
    return tables


def _forward_fill(rows: list[list[str]], columns: int) -> list[list[str]]:
    """A markdown table that visually groups repeated cells leaves later rows' leading columns
    blank ('' or '&nbsp;'-free -- just an empty cell). Carry the last non-empty value forward."""
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

    app_rows: list[list[str]] = []
    screen_rows: list[list[str]] = []
    for subheading, rows in tables:
        if subheading.startswith("App-level"):
            app_rows = rows
        elif subheading == "Screen-level":
            screen_rows = _forward_fill(
                rows, 5
            )  # Screen | File | Key | Action | Body TextArea

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
        key, action = row[0], _strip_dev_asides(row[1])
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

    seen_screens: list[str] = []
    grouped: dict[str, list[list[str]]] = {}
    for row in screen_rows:
        screen = row[0]
        grouped.setdefault(screen, []).append(row)
        if screen not in seen_screens:
            seen_screens.append(screen)

    for screen in seen_screens:
        label = SCREEN_LABELS.get(screen, _fallback_label(screen))
        out.append(f"### {label}")
        out.append("")
        out.append("| Key | Action |")
        out.append("|---|---|")
        for row in grouped[screen]:
            key, action = row[2], _strip_dev_asides(row[3])
            out.append(f"| {key} | {action} |")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Settings (docs/configuration.md -> docs/settings.md, runtime rows only)
# ---------------------------------------------------------------------------


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
    header_seen = False
    pending_rows: list[list[str]] = []

    def flush() -> None:
        nonlocal pending_rows
        runtime_rows = [
            r for r in pending_rows if len(r) >= 3 and r[1].strip() == "runtime"
        ]
        if runtime_rows:
            slug = re.sub(r"[^a-z0-9]+", "-", current_heading.lower()).strip("-")
            out.append(f'<a id="{slug}"></a>')
            out.append(f"## {current_heading}")
            out.append("")
            out.append("| Setting | Default | Notes |")
            out.append("|---|---|---|")
            for row in runtime_rows:
                knob, _kind, default, notes = (
                    row[0],
                    row[1],
                    row[2],
                    _strip_dev_asides(row[3]),
                )
                out.append(f"| {knob} | {default} | {notes} |")
            out.append("")
        pending_rows = []

    for line in lines:
        if line.startswith("## "):
            flush()
            current_heading = line[3:].strip()
            header_seen = False
            continue
        cells = _split_row(line)
        if cells is None:
            header_seen = False
            continue
        if cells[:1] == ["Knob"]:
            header_seen = True
            continue
        if not header_seen:
            continue
        if _is_separator_row(cells):
            continue
        pending_rows.append(cells)

    flush()

    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    keymap = generate_keymap()
    settings = generate_settings()

    if check:
        drift = []
        keymap_path = DOCS_DIR / "keymap.md"
        settings_path = DOCS_DIR / "settings.md"
        if (
            not keymap_path.exists()
            or keymap_path.read_text(encoding="utf-8") != keymap
        ):
            drift.append(str(keymap_path))
        if (
            not settings_path.exists()
            or settings_path.read_text(encoding="utf-8") != settings
        ):
            drift.append(str(settings_path))
        if drift:
            print(
                "docs-site derived pages are stale (lode-fhql.15) -- re-run "
                "scripts/generate_derived_docs.py and commit the result. Stale file(s): "
                + ", ".join(drift),
                file=sys.stderr,
            )
            return 1
        print("docs/keymap.md and docs/settings.md are up to date.")
        return 0

    (DOCS_DIR / "keymap.md").write_text(keymap, encoding="utf-8")
    (DOCS_DIR / "settings.md").write_text(settings, encoding="utf-8")
    print("wrote docs/keymap.md and docs/settings.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
