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


def _git_init(root: Path) -> None:
    """A real git repo is required -- the gate scopes to ``git ls-files``, not
    a bare directory walk, so scratch/gitignored markdown never enters it."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"],
        cwd=root,
        check=True,
    )


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
        _write(tmp_path, "docs/a.md", "# A\n\nSee [B](b.md#section-one).\n")
        _write(tmp_path, "docs/b.md", "# B\n\n## Section One\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_broken_file_target_is_reported(self, tmp_path):
        _write(tmp_path, "docs/a.md", "See [missing](nope.md).\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "nope.md"
        assert "does not exist" in errors[0].reason

    def test_broken_anchor_is_reported(self, tmp_path):
        _write(tmp_path, "docs/a.md", "See [B](b.md#no-such-heading).\n")
        _write(tmp_path, "docs/b.md", "# B\n\n## Real Heading\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert "no-such-heading" in errors[0].target
        assert "no heading slug" in errors[0].reason

    def test_same_file_anchor_checked_against_own_headings(self, tmp_path):
        _write(
            tmp_path, "docs/a.md", "# A\n\n## Real\n\n[back](#real)\n\n[bad](#fake)\n"
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
            "docs/a.md",
            '# A\n\n<a id="reclaim"></a>\n**Step.** Do the thing.\n\n[see](#reclaim)\n',
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_duplicate_headings_get_dash_n_suffix(self, tmp_path):
        _write(
            tmp_path,
            "docs/a.md",
            "# A\n\n## Note\n\n## Note\n\n[first](#note) [second](#note-1)\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_external_links_are_skipped(self, tmp_path):
        _write(
            tmp_path,
            "docs/a.md",
            "[web](https://example.com/nope#frag) [mail](mailto:a@b.com)\n",
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_link_inside_inline_code_span_is_not_a_real_link(self, tmp_path):
        """docs/editing.md has real prose like `` `[text](url)` `` showing
        markdown syntax -- that must never be scanned as an actual link."""
        _write(tmp_path, "docs/a.md", "Example: `[text](nope.md)` shows a link.\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_heading_inside_fenced_code_block_is_not_a_real_heading(self, tmp_path):
        """A shell comment like ``# run tests`` inside a fenced code block
        must never be read as an ATX heading target for an anchor check."""
        _write(
            tmp_path,
            "docs/a.md",
            "# A\n\n```bash\n# not a heading\n```\n\n[link](#not-a-heading)\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].target == "#not-a-heading"

    def test_anchor_into_non_markdown_target_only_checks_file_existence(self, tmp_path):
        _write(tmp_path, "docs/a.md", "[script](../scripts/foo.sh#anything)\n")
        _write(tmp_path, "scripts/foo.sh", "#!/bin/bash\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_files_outside_scan_dirs_are_not_walked_but_are_valid_targets(
        self, tmp_path
    ):
        _write(tmp_path, "docs/a.md", "[readme](../README.md)\n")
        _write(tmp_path, "README.md", "# Readme\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []


@pytest.mark.parametrize("scan_dir", ["docs", ".claude"])
def test_gate_scans_both_docs_and_dot_claude(tmp_path, scan_dir):
    _write(tmp_path, f"{scan_dir}/a.md", "[missing](nope.md)\n")
    _git_init(tmp_path)

    errors = check(tmp_path)

    assert len(errors) == 1


def test_real_repo_docs_and_dot_claude_pass_the_gate():
    """The acceptance criterion: the gate is green against docs/ and .claude/
    as they stand once this ticket lands."""
    errors = check(REPO_ROOT)

    assert errors == [], "broken link(s):\n" + "\n".join(str(e) for e in errors)


def test_cli_exits_nonzero_and_names_source_line_and_target_on_breakage(tmp_path):
    _write(tmp_path, "docs/a.md", "line one\nSee [missing](nope.md) here.\n")
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
    _write(tmp_path, "docs/a.md", "# A\n\nNo links here.\n")
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
