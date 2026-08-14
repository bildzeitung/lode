"""Tests for scripts/check_links.py, the markdown link/anchor gate (lode-dkdg).

Exercises the two units the gate is built from -- ``github_slug`` (the
heading-to-anchor algorithm) and ``check`` (the link-resolution walk) --
directly against synthetic fixture trees, plus one end-to-end regression
locking the exact real-world case that motivated the gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package, so load by file path via the shared
# helper (tests/conftest.py) -- check_links.py's frozen @dataclass is the
# case its sys.modules registration exists for; see its docstring.
check_links = load_module_from_path(
    "check_links", REPO_ROOT / "scripts" / "check_links.py"
)
check = check_links.check
github_slug = check_links.github_slug

#: Split so this module's own source never spells out a bare, matchable
#: `docs/<path>.md` (anchored or not, lode-6lvu) reference -- the real-repo
#: self-test below (``test_real_repo_passes_the_gate``) walks tests/ as a
#: tracked file outside SCAN_DIRS, so a contiguous literal here would make
#: this file cite itself, and its fabricated fixture paths would fail the
#: gate. Used throughout this module's `docs/`-shaped fixture paths, not just
#: the ones with an explicit anchor -- since lode-6lvu, an anchor-LESS bare
#: reference is gated too, so every such fixture needs the same treatment.
_DOCS = "doc" + "s"


def _git_init(root: Path) -> None:
    """A real git repo is required -- the gate scopes to ``git ls-files``, not
    a bare directory walk, so scratch/gitignored markdown never enters it."""
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestGithubSlug:
    def test_plain_words_become_hyphenated_lowercase(self):
        assert github_slug("Hello World") == "hello-world"

    def test_regression_real_dead_anchor_double_hyphen_from_em_dash(self):
        """Locks the exact real case that motivated this gate: an em dash
        sitting between two spaces DELETES to a double hyphen, not a single
        one -- ``docs/decisions.md`` linked
        ``agents-workflow.md#the-landing-loop--build-review-land-planned``
        for a heading that read "The landing loop -- build, review, land
        (planned)" (real em dash, not two hyphens). Collapsing multi-space
        runs to a single hyphen (a plausible but wrong reading of GitHub's
        algorithm) would silently pass this exact broken-anchor shape."""
        heading = "The landing loop — build, review, land (planned)"
        assert github_slug(heading) == "the-landing-loop--build-review-land-planned"

    def test_inline_code_span_contributes_bare_text(self):
        assert github_slug("Fix `lode-o7pf` now") == "fix-lode-o7pf-now"

    def test_punctuation_is_deleted_not_replaced_with_space(self):
        # A single deleted char between two words with no surrounding space
        # change must not introduce an extra hyphen.
        assert github_slug("not built (lode-o7pf)") == "not-built-lode-o7pf"

    def test_bold_and_italic_markers_stripped(self):
        assert github_slug("**Bold** and *italic*") == "bold-and-italic"

    def test_link_text_extracted_from_heading(self):
        assert github_slug("See [the doc](other.md) for more") == "see-the-doc-for-more"


class TestCheck:
    def test_clean_tree_has_no_errors(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "# A\n\nSee [B](b.md#section-one).\n")
        _write(tmp_path, f"{_DOCS}/b.md", "# B\n\n## Section One\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_broken_file_target_is_reported(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "See [missing](nope.md).\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "nope.md"
        assert "does not exist" in errors[0].reason

    def test_broken_anchor_is_reported(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "See [B](b.md#no-such-heading).\n")
        _write(tmp_path, f"{_DOCS}/b.md", "# B\n\n## Real Heading\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert "no-such-heading" in errors[0].target
        assert "no heading slug" in errors[0].reason

    def test_same_file_anchor_checked_against_own_headings(self, tmp_path):
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "# A\n\n## Real\n\n[back](#real)\n\n[bad](#fake)\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "#fake"

    def test_explicit_html_anchor_tag_is_a_valid_target(self, tmp_path):
        """A real, live case: .claude/skills/code/SKILL.md#reclaim is a hand-
        placed `<a id="reclaim"></a>` anchoring a bullet inside a numbered
        step, not a heading -- GFM honors these independently of headings."""
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            '# A\n\n<a id="reclaim"></a>\n**Step.** Do the thing.\n\n[see](#reclaim)\n',
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_duplicate_headings_get_dash_n_suffix(self, tmp_path):
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "# A\n\n## Note\n\n## Note\n\n[first](#note) [second](#note-1)\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_external_links_are_skipped(self, tmp_path):
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "[web](https://example.com/nope#frag) [mail](mailto:a@b.com)\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_link_inside_inline_code_span_is_not_a_real_link(self, tmp_path):
        """docs/editing.md has real prose like `` `[text](url)` `` showing
        markdown syntax -- that must never be scanned as an actual link."""
        _write(tmp_path, f"{_DOCS}/a.md", "Example: `[text](nope.md)` shows a link.\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_heading_inside_fenced_code_block_is_not_a_real_heading(self, tmp_path):
        """A shell comment like ``# run tests`` inside a fenced code block
        must never be read as an ATX heading target for an anchor check."""
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "# A\n\n```bash\n# not a heading\n```\n\n[link](#not-a-heading)\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "#not-a-heading"

    def test_anchor_into_non_markdown_target_only_checks_file_existence(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "[script](../scripts/foo.sh#anything)\n")
        _write(tmp_path, "scripts/foo.sh", "#!/bin/bash\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_files_outside_scan_dirs_are_valid_link_targets(self, tmp_path):
        """A markdown file outside SCAN_DIRS is itself a valid link *target*
        for a link written inside a SCAN_DIRS document (it also gets the
        full markdown-link walk in its own right since lode-act5 -- see
        TestBareDocAnchorRefs below for the bare docs/-anchor citation check
        it gets on top of that)."""
        _write(tmp_path, f"{_DOCS}/a.md", "[readme](../README.md)\n")
        _write(tmp_path, "README.md", "# Readme\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []


@pytest.mark.parametrize("scan_dir", ["docs", ".claude"])
def test_gate_scans_both_docs_and_dot_claude(tmp_path, scan_dir):
    _write(tmp_path, f"{scan_dir}/a.md", "[missing](nope.md)\n")
    _git_init(tmp_path)

    errors = check(tmp_path)

    assert len(errors) == 1


class TestScanScopeWidenedRepoWide:
    """lode-act5: the full bracketed-link walk is no longer bounded to
    SCAN_DIRS -- it now covers every tracked ``*.md`` file, closing the gap
    where a bracketed relative link written in a top-level ``README.md`` (or
    any other markdown file outside ``docs/``/``.claude/``) was never
    resolved at all: not by the full walk (file outside SCAN_DIRS) and not
    by the bare-citation pass (a real ``[text](target)`` link isn't a bare
    root-relative doc-page text reference unless its target happens to start
    with ``docs/``)."""

    def test_broken_bracketed_link_in_top_level_readme_is_reported(self, tmp_path):
        _write(tmp_path, "README.md", "[missing](nope.md)\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "nope.md"

    def test_broken_bracketed_link_in_nested_readme_is_reported(self, tmp_path):
        """The concrete case found reviewing lode-s9xe.7: a bracketed link
        in tests/README.md, verified by hand because nothing gated it."""
        _write(tmp_path, "tests/README.md", "[claude](../CLAUDE.md)\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "../CLAUDE.md"

    def test_working_bracketed_link_in_top_level_readme_passes(self, tmp_path):
        _write(tmp_path, "README.md", "[claude](CLAUDE.md)\n")
        _write(tmp_path, "CLAUDE.md", "# Claude\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_broken_anchor_in_top_level_readme_link_is_reported(self, tmp_path):
        _write(tmp_path, "README.md", "[claude](CLAUDE.md#no-such-anchor)\n")
        _write(tmp_path, "CLAUDE.md", "# Claude\n\n## Real Heading\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert "no heading slug" in errors[0].reason


class TestBareDocAnchorRefs:
    """lode-v10i: a bare-text `docs/<path>.md#<anchor>` reference -- no
    markdown brackets -- cited from any tracked file OUTSIDE SCAN_DIRS (a
    .github/workflows/*.yml comment, a scripts/*.sh comment, ...) must also
    be gated. This is the exact shape that was silently ungated before this
    ticket: the anchor is referenced exclusively from files check_links.py
    never scanned."""

    def test_bare_reference_in_workflow_yaml_to_valid_anchor_passes(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/release.md", "# Release\n\n## CI Trigger Scope\n")
        _write(
            tmp_path,
            ".github/workflows/build.yml",
            f"# See {_DOCS}/release.md#ci-trigger-scope for details.\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_bare_reference_in_workflow_yaml_to_broken_anchor_is_reported(
        self, tmp_path
    ):
        _write(tmp_path, f"{_DOCS}/release.md", "# Release\n\n## CI Trigger Scope\n")
        _write(
            tmp_path,
            ".github/workflows/build.yml",
            f"# See {_DOCS}/release.md#no-such-anchor for details.\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == f"{_DOCS}/release.md#no-such-anchor"
        assert "no heading slug" in errors[0].reason

    def test_bare_reference_to_missing_file_is_reported(self, tmp_path):
        _write(
            tmp_path,
            "scripts/foo.sh",
            f"# {_DOCS}/does-not-exist.md#anything\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert "does not exist" in errors[0].reason

    def test_bare_reference_is_root_relative_regardless_of_citing_files_depth(
        self, tmp_path
    ):
        """A scripts/*.sh comment writes a bare docs/x.md-style reference --
        meant relative to the repo root, NOT to scripts/'s own directory
        (which would look for scripts/docs/x.md and never find it)."""
        _write(tmp_path, f"{_DOCS}/x.md", "# X\n\n## Anchor\n")
        _write(tmp_path, "scripts/foo.sh", f"# {_DOCS}/x.md#anchor\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_anchor_less_bare_reference_to_existing_file_passes(self, tmp_path):
        """lode-6lvu: a bare `docs/<page>.md` reference with NO `#anchor` --
        the exact shape a `help=` footnote in `src/lode/cli/` writes ("See
        docs/how-to/maintenance-commands.md.") -- must resolve when the file
        exists, with nothing to check beyond file existence (there is no
        anchor to validate)."""
        _write(tmp_path, f"{_DOCS}/how-to/x.md", "# X\n")
        _write(
            tmp_path,
            "src/lode/cli/reembed.py",
            f'"""See {_DOCS}/how-to/x.md."""\n',
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_anchor_less_bare_reference_to_missing_file_is_reported(self, tmp_path):
        """lode-6lvu's core acceptance case: an anchor-less citation to a
        `docs/<page>.md` that does NOT exist must be flagged -- this is the
        exact hole that let a page rename/move silently break these
        footnotes with nothing failing."""
        _write(
            tmp_path,
            "src/lode/cli/reenrich.py",
            f'"""See {_DOCS}/how-to/does-not-exist.md."""\n',
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == f"{_DOCS}/how-to/does-not-exist.md"
        assert "does not exist" in errors[0].reason

    def test_anchor_less_and_anchored_bare_references_both_reported_once_each(
        self, tmp_path
    ):
        """An anchored reference to a broken anchor, and an anchor-less
        reference on another line, are independent -- fixing one must not
        hide or duplicate the other."""
        _write(tmp_path, f"{_DOCS}/a.md", "# A\n\n## Real\n")
        _write(
            tmp_path,
            "scripts/foo.sh",
            f"# See {_DOCS}/a.md#no-such-anchor\n# See {_DOCS}/does-not-exist.md\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 2
        targets = {e.target for e in errors}
        assert targets == {
            f"{_DOCS}/a.md#no-such-anchor",
            f"{_DOCS}/does-not-exist.md",
        }

    def test_anchor_less_reference_does_not_swallow_trailing_word_char(self, tmp_path):
        """A `<page>.mdx`-shaped token must not be matched as if it were the
        plain `<page>.md` reference with a stray trailing 'x' -- the trailing
        `(?![\\w-])` guard is what prevents that. Nothing in this fixture is a
        citation, so the gate must report NOTHING: without the guard the regex
        matches a truncated `<page>.md`, which does not exist and so surfaces
        as an error here. (Written without a contiguous literal `docs/*.md`
        example -- see the `_DOCS` split's comment above.)"""
        _write(
            tmp_path,
            "scripts/foo.sh",
            f"# not a citation: {_DOCS}/foo.mdx\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_scan_dirs_files_are_excluded_from_the_bare_pass(self, tmp_path):
        """The two passes must not both claim the same file. Asserted on the
        pass boundary itself rather than on `check() == []`, which would hold
        under any implementation (a plain-prose mention matches no `_LINK_RE`
        either) and so could not detect double-scanning."""
        _write(tmp_path, f"{_DOCS}/a.md", f"# A\n\nSee {_DOCS}/a.md#no-such-anchor.\n")
        _write(tmp_path, "README.md", "# R\n")
        _git_init(tmp_path)

        walked = check_links._tracked_other_files(tmp_path)

        assert tmp_path / f"{_DOCS}/a.md" not in walked
        assert tmp_path / "README.md" in walked
        assert check(tmp_path) == []

    def test_url_into_another_repos_docs_is_not_resolved_locally(self, tmp_path):
        """A URL into ANOTHER repo's docs/ contains the literal
        `docs/<file>.md#<anchor>` substring but is not a citation into OUR
        docs/; the regex's root-relative lookbehind is what rejects it."""
        _write(tmp_path, f"{_DOCS}/release.md", "# R\n\n## Real\n")
        _write(
            tmp_path,
            "README.md",
            f"See https://github.com/other/repo/blob/main/{_DOCS}"
            "/release.md#upstream-only for background.\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_anchor_inside_a_fenced_block_in_a_non_scan_dir_markdown_is_example(
        self, tmp_path
    ):
        """README.md lives outside SCAN_DIRS, so the bare pass walks it -- but
        an anchor inside a ``` fence there is an example, not a citation. The
        identical text OUTSIDE the fence still fails, which is what keeps this
        test from passing vacuously."""
        _write(tmp_path, f"{_DOCS}/release.md", "# R\n\n## Real\n")
        fenced = f"# Example\n\n```\n{_DOCS}/release.md#not-a-real-anchor\n```\n"
        _write(tmp_path, "README.md", fenced)
        _git_init(tmp_path)

        assert check(tmp_path) == []

        _write(
            tmp_path, "README.md", fenced + f"\n{_DOCS}/release.md#not-a-real-anchor\n"
        )
        _git_init(tmp_path)

        assert len(check(tmp_path)) == 1

    def test_fence_rule_is_markdown_only_so_a_py_docstring_fence_hides_nothing(
        self, tmp_path
    ):
        """The fence rule must NOT leak to non-markdown sources. A Python
        docstring can legitimately open a ``` block (a fence-shaped line at
        the start of a line, which is what _FENCE_RE actually matches) and
        still cite a doc anchor after it -- noxfile.py and src/lode/cli/
        (e.g. reembed.py, reenrich.py, status.py) both carry real docstring
        citations today. Skipping fenced lines
        there would silently DROP a real citation -- and worse, an ODD
        number of fence-shaped lines in a non-markdown file would leave
        the walker "inside a fence" for the whole remainder of the file,
        disabling the gate for everything after it with nothing to
        report. Silent under-checking is the exact failure this gate
        exists to prevent."""
        _write(tmp_path, f"{_DOCS}/release.md", "# R\n\n## Real\n")
        _write(
            tmp_path,
            "noxfile.py",
            f'"""Doc.\n\n```\nSee {_DOCS}/release.md#no-such-anchor.\n```\n"""\n',
        )
        _git_init(tmp_path)

        assert len(check(tmp_path)) == 1


def test_real_repo_passes_the_gate():
    """The acceptance criterion, for both passes at once: every relative
    markdown link in every tracked *.md file (lode-act5: repo-wide, not just
    docs/ and .claude/) resolves, AND every bare docs/ anchor citation from
    anywhere else in the tree (.github/workflows/, scripts/, src/,
    noxfile.py, README.md, ...) resolves."""
    errors = check(REPO_ROOT)

    assert errors == [], "broken link(s):\n" + "\n".join(str(e) for e in errors)


def test_cli_exits_nonzero_and_names_source_line_and_target_on_breakage(tmp_path):
    _write(tmp_path, f"{_DOCS}/a.md", "line one\nSee [missing](nope.md) here.\n")
    _git_init(tmp_path)

    result = subprocess.run(
        [
            "python",
            str(REPO_ROOT / "scripts" / "check_links.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert f"{tmp_path / 'docs' / 'a.md'}:2:" in result.stderr
    assert "nope.md" in result.stderr


def test_cli_exits_zero_on_clean_tree(tmp_path):
    _write(tmp_path, f"{_DOCS}/a.md", "# A\n\nNo links here.\n")
    _git_init(tmp_path)

    result = subprocess.run(
        [
            "python",
            str(REPO_ROOT / "scripts" / "check_links.py"),
            "--root",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
