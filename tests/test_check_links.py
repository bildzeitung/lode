"""Tests for scripts/check_links.py, the markdown link/anchor gate (lode-dkdg).

Exercises the two units the gate is built from -- ``github_slug`` (the
heading-to-anchor algorithm) and ``check`` (the link-resolution walk) --
directly against synthetic fixture trees, plus one end-to-end regression
locking the exact real-world case that motivated the gate.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _gitrepo import _git
from conftest import load_module_from_path

from lode import docs_slug

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
#: tracked file outside ``BARE_CITATION_EXCLUDE_DIRS``, so a contiguous
#: literal here would make
#: this file cite itself, and its fabricated fixture paths would fail the
#: gate. Used throughout this module's `docs/`-shaped fixture paths, not just
#: the ones with an explicit anchor -- since lode-6lvu, an anchor-LESS bare
#: reference is gated too, so every such fixture needs the same treatment.
_DOCS = "doc" + "s"

#: Every character ``str.splitlines()`` breaks a line at besides ``\n`` and
#: ``\r``. None of them ends a line for the gate.
_SPLITLINES_ONLY_BREAKS = (
    "\x0b",
    "\x0c",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)


def _git_init(root: Path) -> None:
    """A real git repo is required -- the gate scopes to ``git ls-files``, not
    a bare directory walk, so scratch/gitignored markdown never enters it."""
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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


class TestDocsSlugCopyAgrees:
    """``src/lode/docs_slug.py`` is a deliberate copy of ``github_slug`` above,
    installed as ``mkdocs.yml``'s ``toc.slugify`` so the built site's heading
    ``id``\\ s are the anchors this gate resolves against (lode-fhql.21; that
    module's docstring owns the why-a-copy). A divergence would republish the
    site with anchors the gate believes are fine -- the exact silent breakage
    lode-fhql.21 was filed for -- so it is asserted here, in the module that
    already owns the authority implementation."""

    def test_agrees_on_every_heading_in_docs(self):
        headings = [
            heading
            for path in sorted((REPO_ROOT / "docs").rglob("*.md"))
            for heading in check_links._headings(path.read_text(encoding="utf-8"))
        ]
        assert headings, (
            "no headings found under docs/ -- the corpus scan is not running"
        )
        for heading in headings:
            assert docs_slug.github_slug(heading) == github_slug(heading)

    def test_agrees_on_an_empty_link_text(self):
        """The one shape the docs/ corpus above cannot cover: no heading in
        docs/ has an empty link text, so only the `[^\\]]*`-vs-`[^\\]]+`
        spelling of the two copies' link-text regex decides it."""
        heading = "See [](other.md) for more"
        assert docs_slug.github_slug(heading) == github_slug(heading)

    def test_anchor_slugs_agrees_on_every_docs_file(self):
        """``docs_slug.anchor_slugs`` is the importable copy of this gate's
        ``_slugs_for_file`` (scripts/build_docs_site.py consults it to decide
        whether an aliased link's fragment survives, lode-7uze). Same
        copy-plus-equivalence discipline as the slug copy above: a drift would
        let the site builder rewrite a link onto an anchor this gate does not
        believe exists, or vice versa."""
        paths = sorted((REPO_ROOT / "docs").rglob("*.md"))
        assert paths, "no docs/*.md found -- the corpus scan is not running"
        for path in paths:
            expected = check_links._slugs_for_file(path, {})
            assert (
                docs_slug.anchor_slugs(path.read_text(encoding="utf-8")) == expected
            ), f"anchor-set copies disagree on {path}"

    def test_three_backtick_inline_span_is_prose_not_a_fence(self, tmp_path):
        """A line starting with a three-backtick inline code span must not
        toggle the fence (lode-7jwe); both anchor-set copies agree."""
        text = (
            "```inline``` code starts this line.\n\n## Heading\n\nSee [x](/nope.md).\n"
        )
        assert [n for n, _ in check_links._content_lines(text)] == [1, 2, 3, 4, 5]
        assert check_links._headings(text) == ["Heading"]
        path = _write(tmp_path, "a.md", text)
        assert docs_slug.anchor_slugs(text) == check_links._slugs_for_file(path, {})
        assert "heading" in docs_slug.anchor_slugs(text)

    def test_three_backtick_inline_span_inside_a_fence_does_not_close_it(
        self, tmp_path
    ):
        """One regex decides opens and closes, so the same line inside an open
        fence is content, not a close (lode-7jwe); both anchor-set copies
        agree."""
        text = "```text\n```a``` b\n## Not a heading\n```\n\n## Heading\n"
        assert [n for n, _ in check_links._content_lines(text)] == [5, 6]
        path = _write(tmp_path, "a.md", text)
        assert docs_slug.anchor_slugs(text) == check_links._slugs_for_file(path, {})
        assert docs_slug.anchor_slugs(text) == {"heading"}

    @pytest.mark.parametrize("sep", _SPLITLINES_ONLY_BREAKS)
    def test_non_newline_line_break_chars_do_not_split_lines(self, tmp_path, sep):
        """Only ``\\n`` ends a line: a form feed, U+2028 or any other character
        ``str.splitlines()`` breaks at neither makes a heading out of text after
        it nor opens a fence, and is not counted in a line number; both
        anchor-set copies agree (lode-bdyx)."""
        assert f"a{sep}b".splitlines() == ["a", "b"]
        for text, expected in (
            (f"a{sep}## H\n", set()),
            (f'a{sep}```\n<a id="ff"></a>\n', {"ff"}),
        ):
            path = _write(tmp_path, "a.md", text)
            assert check_links._slugs_for_file(path, {}) == expected
            assert docs_slug.anchor_slugs(text) == expected
        assert list(check_links._content_lines(f"a{sep}b\nc\n")) == [
            (1, f"a{sep}b"),
            (2, "c"),
        ]

    def test_crlf_and_bare_cr_are_newlines_to_both_copies(self, tmp_path):
        """The gate and the site builder both open a file with newline
        translation, so ``\\r\\n`` and a bare ``\\r`` are already ``\\n`` when
        either copy splits lines."""
        path = tmp_path / "a.md"
        path.write_bytes(
            b'## One\r\n```\r\n## Not\r\n```\r## Two\r\n<a id="x"></a>\r\n'
        )
        got = check_links._slugs_for_file(path, {})
        assert got == {"one", "two", "x"}
        assert docs_slug.anchor_slugs(path.read_text(encoding="utf-8")) == got

    def test_anchor_tag_inside_a_fence_is_not_an_anchor(self, tmp_path):
        """An explicit anchor tag inside a fenced block is literal code, so
        neither anchor-set copy counts it (lode-ho02)."""
        text = '```\n<a id="infence"></a>\n```\n## Out\n<a id="real"></a>\n'
        path = _write(tmp_path, "a.md", text)
        got = check_links._slugs_for_file(path, {})
        assert got == {"out", "real"}
        assert docs_slug.anchor_slugs(text) == got

    @pytest.mark.parametrize("marker", ["```", "~~~", "  ```"])
    def test_anchor_tag_in_a_fence_info_string_is_not_an_anchor(self, tmp_path, marker):
        """A fence opener's info string is not content, so a tag there offers
        no anchor to either copy (lode-wo3w)."""
        text = f'{marker}<a id="x"></a>\ncode\n{marker.strip()}\n## Out\n'
        path = _write(tmp_path, "a.md", text)
        got = check_links._slugs_for_file(path, {})
        assert got == {"out"}
        assert docs_slug.anchor_slugs(text) == got

    def test_anchor_tag_after_a_three_backtick_span_is_an_anchor(self, tmp_path):
        """A line starting with a three-backtick inline span opens no fence
        (lode-7jwe), so it has no info string: a tag after the span is an
        anchor to both copies."""
        text = '```span``` <a id="kept"></a>\n## Out\n'
        path = _write(tmp_path, "a.md", text)
        got = check_links._slugs_for_file(path, {})
        assert got == {"kept", "out"}
        assert docs_slug.anchor_slugs(text) == got

    def test_anchor_tag_inside_an_inline_code_span_is_not_an_anchor(self, tmp_path):
        """An explicit anchor tag inside an inline code span is literal code,
        so neither anchor-set copy counts it (lode-8raj)."""
        text = 'write `<a id="lit"></a>` above\n## H\n<a id="real"></a>\n'
        path = _write(tmp_path, "a.md", text)
        got = check_links._slugs_for_file(path, {})
        assert got == {"h", "real"}
        assert docs_slug.anchor_slugs(text) == got

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                '`x` <a id="kept"></a> `y`\n', {"kept"}, id="tag-between-two-spans"
            ),
            pytest.param('``<a id="double"></a>``\n', set(), id="double-backtick-span"),
            pytest.param(
                '`` ` `` <a id="kept"></a> `` ` ``\n',
                {"kept"},
                id="tag-between-literal-backtick-double-spans",
            ),
            pytest.param(
                'para\n\n    <a id="indented"></a>\n',
                {"indented"},
                id="indented-code-block",
            ),
            pytest.param(
                'ends` <a id="lost"></a> and `starts\n',
                set(),
                id="tag-between-two-wrapped-spans",
            ),
            pytest.param(
                'ends`` <a id="lost"></a> and ``starts\n',
                set(),
                id="tag-between-two-wrapped-double-spans",
            ),
            pytest.param(
                'ends` <a id="kept"></a> and ``starts\n',
                {"kept"},
                id="tag-between-wrapped-runs-of-different-length",
            ),
        ],
    )
    def test_anchor_tag_code_span_limits(self, tmp_path, text, expected):
        """Where the one-line, run-aware span strip stops, in both
        anchor-set copies alike: a tag between two separate spans is kept
        (double-backtick spans showing a literal backtick included); a tag in
        a double-backtick span is stripped; a tag in an indented code block
        still counts (not recognised as code); and a real tag between the
        close of one wrapped span and the open of the next is lost when the
        two backtick runs are the same length, kept when they differ."""
        path = _write(tmp_path, "a.md", text)
        assert check_links._slugs_for_file(path, {}) == expected
        assert docs_slug.anchor_slugs(text) == expected

    def test_inline_code_pattern_is_the_gate_copy(self):
        """``tests/test_build_docs_site.py`` holds the site builder's copy to
        this one, so the three cannot drift apart."""
        assert docs_slug._INLINE_CODE_RE.pattern == check_links._INLINE_CODE_RE.pattern

    def test_anchor_tag_split_across_lines_is_not_an_anchor(self, tmp_path):
        """Both anchor-set copies match the tag one line at a time, so a tag
        whose ``<a`` and ``id=`` sit on different lines offers no anchor to
        either."""
        text = '<a\nid="wrap"></a>\n## H\n'
        path = _write(tmp_path, "a.md", text)
        got = check_links._slugs_for_file(path, {})
        assert got == {"h"}
        assert docs_slug.anchor_slugs(text) == got

    def test_any_fence_marker_still_closes_a_backtick_fence(self):
        """The any-marker close is this gate's recorded divergence from
        ``fence_parsing`` (see ``_content_lines``), so it is asserted on the
        gate alone: a ``~~~`` line, or a backtick run carrying an info string,
        closes a backtick-opened block."""
        for closer in ("~~~", "```bash"):
            text = f"```\n## Not a heading\n{closer}\n## Heading\n"
            assert check_links._headings(text) == ["Heading"], closer


class TestCheck:
    def test_clean_tree_has_no_errors(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "# A\n\nSee [B](b.md#section-one).\n")
        _write(tmp_path, f"{_DOCS}/b.md", "# B\n\n## Section One\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_link_to_spaced_file_name_resolves(self, tmp_path):
        """An angle-bracket destination is read whole and a percent-encoded one
        is decoded, each with or without a trailing title."""
        _write(tmp_path, f"{_DOCS}/sp ace.md", "# Spaced\n\n## Part\n")
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            '[1](<sp ace.md>) [2](sp%20ace.md) [3](<sp ace.md> "t") '
            '[4](sp%20ace.md "t") [5](<sp ace.md#part>) [6](sp%20ace.md#part)\n',
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_link_to_missing_spaced_file_name_is_reported(self, tmp_path):
        _write(tmp_path, f"{_DOCS}/a.md", "[1](<no such.md>) [2](no%20such.md)\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert [e.target for e in errors] == ["no such.md", "no%20such.md"]
        assert all("does not exist" in e.reason for e in errors)

    def test_link_path_percent_escapes_are_decoded(self, tmp_path):
        """``%23`` is a ``#`` in the file name, never the start of the anchor,
        and an escape that is not UTF-8 is that byte of the name. A ``)`` in a
        name is written ``%29``, or as itself inside angle brackets."""
        _write(tmp_path, f"{_DOCS}/a#b.md", "## Part\n")
        _write(tmp_path, f"{_DOCS}/" + os.fsdecode(b"caf\xe9.md"), "# C\n")
        _write(tmp_path, f"{_DOCS}/a (b).md", "## Part\n")
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "[1](a%23b.md#part) [2](caf%E9.md) [3](a%20%28b%29.md) "
            '[4](<a (b).md>) [5](<a (b).md#part> "t")\n',
        )
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_percent_encoded_anchor_matches_the_decoded_slug(self, tmp_path):
        """``#caf%C3%A9`` matches the ``café`` heading, in another file and in
        the link's own; an encoded anchor that names no heading is reported as
        written; ``%23`` in the path stays in the file name."""
        _write(tmp_path, f"{_DOCS}/t.md", "## café\n")
        _write(tmp_path, f"{_DOCS}/a#b.md", "## Part\n")
        _write(
            tmp_path,
            f"{_DOCS}/a.md",
            "## café\n\n[1](t.md#caf%C3%A9) [2](t.md#caf%C3%A8) [3](a%23b.md#part) "
            "[4](#caf%C3%A9)\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert [e.target for e in errors] == ["t.md#caf%C3%A8"]
        assert errors[0].reason == f"no heading slug '#caf%C3%A8' in {_DOCS}/t.md"

    def test_anchor_is_looked_up_as_written_before_it_is_decoded(self, tmp_path):
        """An explicit id is literal, so ``#a%20b`` reaches ``<a id="a%20b">``."""
        _write(tmp_path, f"{_DOCS}/t.md", '<a id="a%20b"></a>\n')
        _write(tmp_path, f"{_DOCS}/a.md", "[1](t.md#a%20b)\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    def test_link_destination_that_names_no_file(self, tmp_path):
        """A decoded NUL is reported, not a ``ValueError`` out of ``resolve()``.
        ``<t.md>`` with no whitespace before the title is not an angle-bracket
        destination (CommonMark), so ``t.md``, which is there, is not reached.
        A blank destination is no link, and not an ``IndexError``."""
        _write(tmp_path, f"{_DOCS}/t.md", "# T\n")
        _write(
            tmp_path, f"{_DOCS}/a.md", '[1](x%00.md)\n[2](<t.md>"t")\n[3]( ) [4](<>)\n'
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert [(e.line, e.target) for e in errors] == [
            (1, "x%00.md"),
            (2, '<t.md>"t"'),
        ]
        assert all("does not exist" in e.reason for e in errors)

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

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("``[x](a.md)``\n", [], id="link-in-double-backtick-span"),
            pytest.param(
                "`` ` `` [x](a.md) `` ` ``\n",
                [(1, "a.md")],
                id="link-between-literal-backtick-double-spans",
            ),
            pytest.param(
                "ends`` [x](a.md) and ``starts\n",
                [],
                id="link-between-two-wrapped-double-spans",
            ),
        ],
    )
    def test_link_code_span_runs(self, text, expected):
        """A link in a double-backtick span is code; a real link between two
        double-backtick spans that each show a literal backtick is kept; a
        real link between the close of one wrapped span and the open of the
        next, both runs the same length, is lost."""
        assert check_links._links_in_file(text) == expected

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

    def test_files_outside_docs_and_dot_claude_are_valid_link_targets(self, tmp_path):
        """A markdown file outside ``docs/``/``.claude/`` is itself a valid
        link *target* for a link written inside a ``docs/`` document (it also
        gets the full markdown-link walk in its own right since lode-act5 --
        see TestBareDocAnchorRefs below for the bare docs/-anchor citation
        check it gets on top of that)."""
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
    ``docs/``/``.claude/`` -- it now covers every tracked ``*.md`` file,
    closing the gap where a bracketed relative link written in a top-level
    ``README.md`` (or any other markdown file outside those two directories)
    was never resolved at all: not by the full walk (file out of scope) and not
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

    def test_link_caught_by_both_passes_is_reported_once(self, tmp_path):
        """A bracketed link into a `docs/` target, written in a markdown file
        outside ``BARE_CITATION_EXCLUDE_DIRS``, matches BOTH the widened
        bracket walk and the
        bare-citation regex. `check` de-duplicates so one real break is
        reported once, not twice (which would also double the summary
        count)."""
        _write(tmp_path, "README.md", f"[foo]({_DOCS}/foo.md#no-such-anchor)\n")
        _write(tmp_path, f"{_DOCS}/foo.md", "# Real Heading\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1


class TestBareDocAnchorRefs:
    """lode-v10i: a bare-text `docs/<path>.md#<anchor>` reference -- no
    markdown brackets -- cited from any tracked file OUTSIDE
    ``BARE_CITATION_EXCLUDE_DIRS`` (a
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

    def test_bare_reference_with_a_percent_encoded_anchor_is_read_whole(self, tmp_path):
        """The anchor is not cut at its first ``%`` (cut there, every anchor
        below would be the live ``#caf``): an encoded anchor is decoded like a
        link's, and a dead one is reported as written. ``README.md`` is in
        both walks, and its dead link is still one entry. ``%2`` is no escape,
        so that anchor does end at the ``%``."""
        _write(tmp_path, f"{_DOCS}/t.md", "## café\n\n## caf\n")
        readme = _write(
            tmp_path,
            "README.md",
            f"[1]({_DOCS}/t.md#caf%C3%A9) [2]({_DOCS}/t.md#caf%C3%A8)\n",
        )
        script = _write(
            tmp_path,
            "scripts/x.sh",
            f"# {_DOCS}/t.md#caf%C3%A9 {_DOCS}/t.md#caf%C3%A8 {_DOCS}/t.md#caf%2\n",
        )
        _git_init(tmp_path)

        errors = check(tmp_path)

        dead = f"{_DOCS}/t.md#caf%C3%A8"
        assert [(e.source, e.target) for e in errors] == [
            (readme, dead),
            (script, dead),
        ]

    @pytest.mark.parametrize(
        "name",
        ["a b.sh", "café.sh", os.fsdecode(b"bad\xff.sh"), "a\rb.sh"],
        ids=["space", "non-ascii", "not-utf-8", "carriage-return"],
    )
    def test_bare_reference_in_oddly_named_file_is_reported(self, tmp_path, name):
        """Each name is lost by a simpler listing: a whitespace split cuts the
        first, plain ``git ls-files`` C-quotes the other three, and ``-z``
        output read in text mode raises on the third and newline-translates
        the fourth."""
        source = _write(tmp_path, f"scripts/{name}", f"# {_DOCS}/does-not-exist.md\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert len(errors) == 1
        assert errors[0].source == source

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

    def test_excluded_dir_files_are_excluded_from_the_bare_pass(self, tmp_path):
        """A file under ``BARE_CITATION_EXCLUDE_DIRS`` is never claimed by the
        bare pass (a markdown file OUTSIDE it is claimed by both since
        lode-act5 -- see TestScanScopeWidenedRepoWide). Asserted on the pass
        boundary itself rather than on `check() == []`, which would hold
        under any implementation (a plain-prose mention matches no `_LINK_RE`
        either) and so could not detect double-scanning."""
        _write(tmp_path, f"{_DOCS}/a.md", f"# A\n\nSee {_DOCS}/a.md#no-such-anchor.\n")
        _write(tmp_path, "README.md", "# R\n")
        _git_init(tmp_path)

        walked = check_links._tracked_other_files(
            tmp_path, check_links._tracked_paths(tmp_path)
        )

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

    def test_anchor_inside_a_fenced_block_in_a_non_excluded_markdown_is_example(
        self, tmp_path
    ):
        """README.md lives outside ``BARE_CITATION_EXCLUDE_DIRS``, so the bare
        pass walks it -- but
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

    @pytest.mark.parametrize("sep", _SPLITLINES_ONLY_BREAKS)
    def test_reported_line_of_a_non_markdown_citation_counts_newlines_only(self, sep):
        """A non-markdown source is numbered like a markdown one: a form feed
        (a page break in a source file) above a citation does not shift the
        line the gate reports for it."""
        ref = f"{_DOCS}/x.md#anchor"
        refs = check_links._bare_doc_anchor_refs(
            f"a{sep}b\n# {ref}\n", skip_fences=False
        )
        assert refs == [(2, ref)]

    @pytest.mark.parametrize(
        ("written", "matched"),
        [
            # Before the space no `.md`, after it no docs directory: no match.
            (f"{_DOCS}/my notes.md", []),
            (f"{_DOCS}/my%20notes.md", []),
            # `.md` already before the space: cut there, the anchor lost.
            (f"{_DOCS}/a.md notes.md#x", [f"{_DOCS}/a.md"]),
            (f"{_DOCS}/a.md%20notes.md", [f"{_DOCS}/a.md"]),
            # The docs directory again after the space: read from there on.
            (f"{_DOCS}/my {_DOCS}/page.md#a", [f"{_DOCS}/page.md#a"]),
            # The spelling the LIMIT gives for a destination that is still
            # cut: `%20` undoes the second shape, a leading `./` the first.
            (f"{_DOCS}/my%20{_DOCS}/page.md", []),
            (f"./{_DOCS}/a.md%20notes.md", []),
        ],
    )
    def test_citation_of_a_path_holding_a_space_is_never_read_whole(
        self, written, matched
    ):
        """lode-905hd: the module docstring's LIMIT, shape by shape."""
        refs = check_links._bare_doc_anchor_refs(
            f"see {written} here\n", skip_fences=False
        )
        assert [target for _, target in refs] == matched

    #: Destinations of pages whose path holds a space, one per LIMIT shape and
    #: spelling. The shorter path the bare pattern reads out of each
    #: (``page.md`` in the docs directory, ``a.md``) is never written by the
    #: tests that use these.
    _SPACED_DESTS = (
        f"<{_DOCS}/my {_DOCS}/page.md>",
        f"<{_DOCS}/a.md notes.md>",
        f"{_DOCS}/a.md%20notes.md",
    )

    @pytest.mark.parametrize("dest", _SPACED_DESTS)
    def test_live_spaced_link_outside_docs_is_not_reported(self, tmp_path, dest):
        """lode-wfy6i: the bare pass does not cut a shorter path out of a link
        destination the markdown walk resolves whole."""
        _write(tmp_path, f"{_DOCS}/my {_DOCS}/page.md", "# P\n")
        _write(tmp_path, f"{_DOCS}/a.md notes.md", "# N\n")
        _write(tmp_path, "README.md", f"[x]({dest})\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    @pytest.mark.parametrize("dest", _SPACED_DESTS)
    def test_dead_spaced_link_outside_docs_is_reported_once(self, tmp_path, dest):
        """Once, as written, by the markdown walk -- and not a second time as
        the shorter path."""
        _write(tmp_path, "README.md", f"[x]({dest})\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert [(e.line, e.target) for e in errors] == [(1, dest.strip("<>"))]

    @pytest.mark.parametrize(
        ("line", "matched"),
        [
            # Blanked: what the markdown walk resolves, however it is dressed.
            (f"[x](<{_DOCS}/a.md notes.md>)", []),
            (f'[x](<{_DOCS}/a.md notes.md> "title")', []),
            # A code span is taken out before the link is looked for, as in
            # the markdown walk: a `]` or link-shaped text in one hides no link.
            (f"[`a]b`](<{_DOCS}/a.md notes.md>)", []),
            (f"`[a](b` and [x](<{_DOCS}/a.md notes.md>)", []),
            # Not blanked: the markdown walk resolves none of these.
            (f"`[x]({_DOCS}/t.md#a%20b)`", [f"{_DOCS}/t.md#a%20b"]),
            (f"[x](<mailto:a?subject=see {_DOCS}/gone.md>)", [f"{_DOCS}/gone.md"]),
            (
                f"[x](https://example.com/a%20b?next={_DOCS}/gone.md)",
                [f"{_DOCS}/gone.md"],
            ),
            # Only the destination goes: the text, a title and the rest of the
            # line are read as before, and so is a destination without a space.
            (
                f"[see {_DOCS}/b.md](<{_DOCS}/a.md notes.md>) and {_DOCS}/c.md",
                [f"{_DOCS}/b.md", f"{_DOCS}/c.md"],
            ),
            (
                f'[x]({_DOCS}/a.md "see {_DOCS}/b.md")',
                [f"{_DOCS}/a.md", f"{_DOCS}/b.md"],
            ),
        ],
    )
    def test_only_a_spaced_destination_the_markdown_walk_resolves_is_masked(
        self, line, matched
    ):
        refs = check_links._bare_doc_anchor_refs(f"{line}\n", skip_fences=True)
        assert [target for _, target in refs] == matched

    #: Destinations cut by the bare pattern at a character outside its path or
    #: anchor class (lode-q14mi), with the page each one names.
    _CUT_DESTS = (
        (f"<{_DOCS}/a.md(old).md>", f"{_DOCS}/a.md(old).md"),
        (f"{_DOCS}/a.md%28old%29.md", f"{_DOCS}/a.md(old).md"),
        (f"<{_DOCS}/a.md\tnotes.md>", f"{_DOCS}/a.md\tnotes.md"),
        (f"{_DOCS}/a.md,b.md", f"{_DOCS}/a.md,b.md"),
        (f"{_DOCS}/a.md~b.md", f"{_DOCS}/a.md~b.md"),
        (f"{_DOCS}/a.md+x.md", f"{_DOCS}/a.md+x.md"),
    )

    @pytest.mark.parametrize(("dest", "page"), _CUT_DESTS)
    def test_live_link_cut_at_another_character_is_not_reported(
        self, tmp_path, dest, page
    ):
        _write(tmp_path, page, "# P\n")
        _write(tmp_path, "README.md", f"[x]({dest})\n")
        _git_init(tmp_path)

        assert check(tmp_path) == []

    @pytest.mark.parametrize(("dest", "page"), _CUT_DESTS)
    def test_dead_link_cut_at_another_character_is_reported_once(
        self, tmp_path, dest, page
    ):
        _write(tmp_path, "README.md", f"[x]({dest})\n")
        _git_init(tmp_path)

        errors = check(tmp_path)

        assert [(e.line, e.target) for e in errors] == [(1, dest.strip("<>"))]

    @pytest.mark.parametrize(
        "line",
        [
            f"![x](<{_DOCS}/a.md notes.md>)",
            f"[ref]: <{_DOCS}/a.md notes.md>",
        ],
    )
    def test_image_and_reference_definition_destinations_are_still_read(self, line):
        """Neither walk resolves them, so masking would remove their only check
        (the LIMIT paragraph says so)."""
        refs = check_links._bare_doc_anchor_refs(f"{line}\n", skip_fences=True)
        assert [target for _, target in refs] == [f"{_DOCS}/a.md"]

    def test_link_syntax_in_a_non_markdown_file_is_not_masked(self):
        """The markdown walk never reads such a file, so the bare pass is the
        only check its text gets."""
        refs = check_links._bare_doc_anchor_refs(
            f"# [x](<{_DOCS}/a.md gone.md>)\n", skip_fences=False
        )
        assert refs == [(1, f"{_DOCS}/a.md")]


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
            sys.executable,
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
            sys.executable,
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


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param(f"{_DOCS}/gone.md", id="markdown-walk-only"),
        pytest.param("scripts/gone.sh", id="bare-citation-walk-only"),
        pytest.param("notes/gone.md", id="both-walks"),
    ],
)
def test_cli_reports_tracked_file_deleted_but_unstaged_without_traceback(tmp_path, rel):
    """One report per missing file, whichever walk reads it (a ``.md`` outside
    ``docs/`` and ``.claude/`` is read by both). The root holds a space, so
    the printed commands only name the file if the path is shell-quoted."""
    root = tmp_path / "work tree"
    _write(root, f"{_DOCS}/a.md", "# A\n")
    gone = _write(root, rel, "x\n")
    _git_init(root)
    gone.unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_links.py"),
            "--root",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    quoted = f"'{gone}'"
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.count("missing from the working tree") == 1
    assert f"{gone}:0: tracked file is missing from the working tree" in result.stderr
    assert f"git rm {quoted} " in result.stderr
    assert f"git restore {quoted}\n" in result.stderr


def test_tracked_file_under_a_directory_replaced_by_a_file_is_reported_missing(
    tmp_path,
):
    """git lists such a file as deleted too, and reading it fails with
    ``NotADirectoryError``, which is not a ``FileNotFoundError``."""
    _write(tmp_path, f"{_DOCS}/a.md", "# A\n")
    gone = _write(tmp_path, f"{_DOCS}/guide/intro.md", "# I\n")
    _git_init(tmp_path)
    gone.unlink()
    gone.parent.rmdir()
    gone.parent.write_text("x\n", encoding="utf-8")

    errors = check(tmp_path)

    assert [(e.source, e.line) for e in errors] == [(gone, 0)]
    assert "missing from the working tree" in errors[0].reason


def test_dangling_tracked_symlink_is_not_reported_as_missing(tmp_path):
    """A tracked symlink whose target is absent is still in the working tree
    (git does not list it as deleted, and ``git restore`` would change
    nothing), so the bare-citation walk skips it like any unreadable file."""
    _write(tmp_path, f"{_DOCS}/a.md", "# A\n")
    (tmp_path / "current").symlink_to("build/out")
    _git_init(tmp_path)

    assert check(tmp_path) == []


def _make_unreadable(path: Path, state: str) -> str:
    """Leave ``path`` in place but unreadable; return the system's reason."""
    if state == "dangling-symlink":
        path.unlink()
        path.symlink_to("nowhere.md")
        return os.strerror(errno.ENOENT)
    if state == "directory":
        path.unlink()
        path.mkdir()
        return os.strerror(errno.EISDIR)
    path.chmod(0)
    return os.strerror(errno.EACCES)


@pytest.mark.parametrize(
    "state",
    [
        "dangling-symlink",
        "directory",
        pytest.param(
            "no-permission",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root reads files regardless of mode"
            ),
        ),
    ],
)
def test_cli_reports_tracked_markdown_that_cannot_be_read_without_traceback(
    tmp_path, state
):
    """The path is there (so not the missing-file report) but reading it
    raises; the gate names the file and the reason, once each, and stays
    red."""
    _write(tmp_path, f"{_DOCS}/a.md", "# A\n")
    bad = _write(tmp_path, f"{_DOCS}/x.md", "# X\n")
    _git_init(tmp_path)
    why = _make_unreadable(bad, state)

    result = subprocess.run(
        [
            sys.executable,
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
    assert "Traceback" not in result.stderr
    assert result.stderr.count("cannot be read") == 1
    assert f"{bad}:0: file cannot be read -> {why}\n" in result.stderr


def test_unreadable_markdown_is_one_error_however_many_links_reach_it(tmp_path):
    """A link or a bare citation with an anchor reads its target for the
    anchors. That read fails as the walk's own read of the file does, and
    each failure is the same entry, so a file is reported once. ``NOTES.md``
    is a source in both walks; the bare-citation walk skips it."""
    _write(tmp_path, "README.md", "[n](NOTES.md#n)\n")
    _write(tmp_path, f"{_DOCS}/a.md", "[x](x.md#x) and [again](x.md#x)\n")
    _write(tmp_path, "scripts/s.sh", f"# see {_DOCS}/x.md#x\n")
    bad = [
        _write(tmp_path, "NOTES.md", "# N\n"),
        _write(tmp_path, f"{_DOCS}/x.md", "# X\n"),
    ]
    _git_init(tmp_path)
    why = [_make_unreadable(path, "directory") for path in bad]

    assert check(tmp_path) == [
        check_links.LinkError(path, 0, reason, "file cannot be read")
        for path, reason in zip(bad, why, strict=True)
    ]
