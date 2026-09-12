"""Drift gate for the docs-site's derived reference pages (lode-fhql.15).

`scripts/generate_derived_docs.py` derives `docs/keymap.md` from `docs/keybindings.md`'s "Current
keymap" tables, and `docs/settings.md` from `docs/configuration.md`'s `runtime`-kind rows -- see
`docs/stack.md`'s "Derived reference pages" section for the full contract. Generation, not
hand-copying, is what keeps the derived page from silently disagreeing with its source; this test is
what keeps a source edit that skips regeneration from shipping unnoticed, by actually running the
generator's `--check` mode -- the same one a human/CI would run -- against the real repo tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package, so load by file path (same helper tests/test_check_links.py
# uses).
generate_derived_docs = load_module_from_path(
    "generate_derived_docs", REPO_ROOT / "scripts" / "generate_derived_docs.py"
)
# tests/test_check_links.py loads the same module, and `load_module_from_path` deliberately refuses
# to replace a live `sys.modules` entry -- so reuse whichever module object got there first.
check_links = sys.modules.get("check_links") or load_module_from_path(
    "check_links", REPO_ROOT / "scripts" / "check_links.py"
)


def test_derived_docs_are_up_to_date() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_derived_docs.py"),
            "--check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "docs/keymap.md and/or docs/settings.md are stale relative to their source docs "
        "(docs/keybindings.md / docs/configuration.md) -- re-run "
        "`scripts/generate_derived_docs.py` and commit the result.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _run_check_in_scratch_repo(
    tmp_path: Path, sources: dict[str, str], derived: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the real generator's `--check` against a scratch mirror of the repo, so a test can
    sabotage a source doc or a derived page without touching the working tree. `sources`/`derived`
    override a `docs/` file's text; any source not overridden is copied from the repo verbatim.

    The generator resolves `docs/` relative to its own file, so the script is copied in too.
    """
    scratch_docs = tmp_path / "docs"
    scratch_scripts = tmp_path / "scripts"
    scratch_docs.mkdir()
    scratch_scripts.mkdir()
    for name in ("keybindings.md", "configuration.md"):
        text = sources.get(
            name, (REPO_ROOT / "docs" / name).read_text(encoding="utf-8")
        )
        (scratch_docs / name).write_text(text, encoding="utf-8")
    for name, text in derived.items():
        (scratch_docs / name).write_text(text, encoding="utf-8")
    script = scratch_scripts / "generate_derived_docs.py"
    script.write_text(
        (REPO_ROOT / "scripts" / "generate_derived_docs.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_catches_a_stale_derived_page(tmp_path: Path) -> None:
    """Sabotage check: if `docs/keymap.md` (or `settings.md`) is not what the generator would
    currently produce, `--check` must actually fail, not silently pass -- proving the check's own
    generate-and-diff logic, not just that today's committed pages happen to match."""
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={},
        # A deliberately-stale pair (a real generated page would never be this).
        derived={"keymap.md": "stale\n", "settings.md": "stale\n"},
    )
    assert result.returncode == 1
    assert "keymap.md" in result.stdout + result.stderr
    assert "settings.md" in result.stdout + result.stderr


def test_a_renamed_source_heading_fails_loud(tmp_path: Path) -> None:
    """A source doc reshaped past what the generator can read must raise, not quietly emit a page
    missing the rows it could no longer find -- `--check` would then say only "stale, regenerate",
    and the truncated page would get committed."""
    keybindings = (REPO_ROOT / "docs" / "keybindings.md").read_text(encoding="utf-8")
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={
            "keybindings.md": keybindings.replace(
                "## Current keymap", "## The keymap as it stands"
            )
        },
        derived={},
    )
    assert result.returncode != 0
    assert "SourceDocChanged" in result.stderr


@pytest.mark.parametrize(
    "renamed_header",
    [
        # Renaming a column the generator only READS: a positional read would survive this
        # silently, emitting one column's content under another's header.
        "| Knob | Kind | Value | Notes |",
        # Renaming a column the generator also RECOGNIZES knob tables BY. This is the rename that
        # could skip a whole table rather than misread it -- emitting a page quietly missing a
        # section -- so it must raise like every other reshape, not fall through as "not a table".
        "| Setting | Kind | Default | Notes |",
        "| Knob | Category | Default | Notes |",
    ],
)
def test_a_renamed_source_column_fails_loud(
    tmp_path: Path, renamed_header: str
) -> None:
    """Columns are read by NAME, and a knob table is recognized by those same names, so any rename
    of one raises rather than degrading the page."""
    configuration = (REPO_ROOT / "docs" / "configuration.md").read_text(
        encoding="utf-8"
    )
    result = _run_check_in_scratch_repo(
        tmp_path,
        sources={
            "configuration.md": configuration.replace(
                "| Knob | Kind | Default | Notes |", renamed_header
            )
        },
        derived={},
    )
    assert result.returncode != 0
    assert "SourceDocChanged" in result.stderr


def _headings(markdown: str) -> set[str]:
    """The set of heading texts (with the leading `#`s stripped) present in a derived page --
    what a `See "X" below` reference in that same page must name to be resolvable in place."""
    return {
        line.lstrip("#").strip()
        for line in markdown.splitlines()
        if line.startswith("#")
    }


def _dangling_see_below_rows(markdown: str) -> list[str]:
    """Rows of a derived table (`| ... |` lines) that cite prose the page itself never carries --
    either a `See "X" below` pointer where X is not a heading on this same page, or a bare
    `(see below)` / `see below` cross-reference with no named destination at all (lode-0teo). A
    derived page has table rows only, so any such row is a dangling pointer at the section a
    source-doc prose block lived in, which regeneration never carries across."""
    headings = _headings(markdown)
    violations = []
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        quoted_matches = list(re.finditer(r'See "([^"]+)" below', line))
        for match in quoted_matches:
            if match.group(1) not in headings:
                violations.append(line)
        # A bare "(see below)" / "see below" carries no destination at all -- always dangling.
        # Skip a line already flagged via the quoted form above, so it isn't double-counted.
        if not quoted_matches and re.search(
            r"\(see below\)|\bsee below\b", line, re.IGNORECASE
        ):
            violations.append(line)
    return violations


def test_settings_md_has_no_dangling_see_below_rows() -> None:
    """Regression gate for lode-0teo: docs/settings.md is derived from docs/configuration.md's
    knob TABLES ONLY -- prose sections (headings, paragraphs) never reach it, so a source row that
    says 'See "X" below' or a bare '(see below)' is pointing at text the derived page doesn't have."""
    settings_md = (REPO_ROOT / "docs" / "settings.md").read_text(encoding="utf-8")
    assert _dangling_see_below_rows(settings_md) == []


def test_dangling_see_below_scan_catches_the_pre_fix_rows() -> None:
    """Sabotage check: the scan must actually flag the exact pre-fix row shapes lode-0teo fixed --
    proving the scan's own logic, not just that today's settings.md happens to pass it."""
    quoted_row = (
        '| `jira_projects` | `[]` (empty) | ... See "Bare JIRA issue keys" below. |'
    )
    paren_row = "| `jira_token` | unset | ... never the value (see below). |"
    bare_row = "| `no_egress_scopes` | `[]` | Declarative no_egress SCOPE rules -- see below. |"
    markdown = (
        f"# lode -- settings you can change\n\n{quoted_row}\n{paren_row}\n{bare_row}"
    )
    violations = _dangling_see_below_rows(markdown)
    assert quoted_row in violations
    assert paren_row in violations
    assert bare_row in violations

    # A quoted reference to a heading that IS present on the page is not dangling.
    resolvable = '| `jira_projects` | `[]` | ... See "Bare JIRA issue keys" below. |'
    resolvable_markdown = (
        "# lode -- settings you can change\n\n## Bare JIRA issue keys\n\n" + resolvable
    )
    assert _dangling_see_below_rows(resolvable_markdown) == []


def test_emitted_anchor_ids_match_the_link_gates_github_slug() -> None:
    """The generated `<a id=...>` must be the anchor GitHub itself derives from the same heading, so
    one `#link` resolves under both GitHub and the site's own (differently-slugged) renderer."""
    for heading in (
        "Paths & locations",
        "TUI — passive connection surfacing (E11)",
        "Retrieval and ranking",
        "Privacy & egress",
        # Empty link text (`[]( url )`) -- the authority strips the whole construct via `[^\]]*`
        # (zero-or-more); a `[^\]]+` (one-or-more) copy fails to match on empty text and leaves
        # the url's own text sitting in the slug instead (lode-d3k0).
        "See [](/x.md) here",
    ):
        assert generate_derived_docs._github_slug(heading) == check_links.github_slug(
            heading
        )
