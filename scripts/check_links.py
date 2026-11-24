#!/usr/bin/env python3
"""Verify every relative markdown link in every tracked *.md file resolves
(lode-dkdg, widened repo-wide by lode-act5).

docs/ leans heavily on deep cross-document anchors (decisions.md alone points
into stack.md, externals.md, editing.md, agents-workflow.md), but GitHub's
anchor slugs are derived from heading TEXT -- so rewording a heading silently
breaks every inbound link, with nothing failing to report it. This gate walks
every tracked ``*.md`` file in the repo (originally just ``docs/`` and
``.claude/``; widened repo-wide by lode-act5 -- see SCOPE DECISION
(lode-act5) below), resolves each relative markdown link's target file, and
-- for a ``#anchor`` link -- checks the anchor against the target file's
headings, slugged the same way GitHub slugs them.

CONCRETE EVIDENCE this already happens in trunk (found by an ad-hoc slug
check before this gate existed): ``docs/decisions.md`` carried a dead anchor
into ``agents-workflow.md#the-landing-loop--build-review-land-planned`` --
the heading had been reworded to drop "(planned)" and nothing reported it.
That case is captured verbatim in ``tests/test_check_links.py`` as a
regression lock on the slug algorithm itself.

Usage::

    python scripts/check_links.py            # scan every tracked *.md file + every other tracked file
    python scripts/check_links.py --root DIR # scan a different tree (tests)

Exits 1 and prints one ``file:line: reason -> target`` line per broken link
(plus a summary count) on any broken file target or broken anchor; exits 0
and prints a short OK line otherwise. A tracked file the gate would read that
is missing from the working tree (deleted, the deletion never staged) is
reported in the same shape at line 0, with the two git commands that fix it in
place of a target (lode-h85o). A markdown file that is there but cannot be
read (a dangling symlink, a directory in its place, no read permission) is
reported at line 0 too, with the system's reason in place of a target
(lode-l8coy).

Scope, deliberately: relative markdown links only -- external links
(``http(s)://``, ``mailto:``, etc.) are never resolvable against this repo
and are skipped; image links (``![alt](...)``) are out of scope (the
acceptance criteria is about cross-document *links*); an ``#anchor`` is only
checked against a target's headings when the target is itself a ``.md``
file -- a link into a non-markdown file (a script, a config) has no heading
model to check the anchor against, so only its file-existence is verified.

SCOPE DECISION (lode-v10i): a ``docs/`` anchor is cited from plenty of files
that are neither under ``docs/`` nor ``.claude/`` -- a bare-text pointer like
``docs/release.md#ci-workflow-trigger-scope-push-and-pull_request`` inside a
``# comment`` in ``.github/workflows/*.yml`` or ``scripts/*.sh``, with no
markdown ``[text](...)`` brackets at all. The general form was chosen over
special-casing ``.github/workflows/``: EVERY tracked file outside
``BARE_CITATION_EXCLUDE_DIRS`` is scanned for a bare ``docs/<path>.md#<anchor>``
text reference (``_bare_doc_anchor_refs`` / ``_tracked_other_files`` below),
not just workflow YAML -- ``scripts/``, ``noxfile.py`` and ``src/`` all cite
docs/ anchors the identical way, so special-casing one directory would have
left the others silently ungated again.

This second pass is deliberately narrower than the full bracketed-link walk:
it recognizes only a literal, root-relative ``docs/<path>.md#<anchor>``
substring -- the shape every real instance in this repo is written in,
regardless of the citing file's own directory depth.

SCOPE DECISION (lode-act5): what is now ``BARE_CITATION_EXCLUDE_DIRS``
originally bounded BOTH what got the full bracketed-link walk (``docs/`` +
``.claude/`` only) AND, inversely, what got the bare-citation pass above
(everything else). That meant a bracketed relative link --
``[text](../CLAUDE.md)``, say -- written in a tracked markdown file outside
that pair of directories (a top-level ``README.md``, ``tests/README.md``,
``AGENTS.md``, ...) was never resolved at all: it isn't a bare ``docs/...``
citation (so the second pass's regex doesn't match it), and the file wasn't
under ``docs/``/``.claude/`` (so the first pass never walked it). Found
concretely reviewing lode-s9xe.7: ``tests/README.md``'s
``[`CLAUDE.md`](../CLAUDE.md)`` link was verified by hand, not by this gate.

Two ways to close it were on the table: widen the full walk to every tracked
``*.md`` file, or hand-maintain a named allowlist of top-level READMEs. The
general form won, consistent with the lode-v10i precedent above -- an
allowlist needs a human to remember to add every new top-level README (and
this repo already has several: the root, ``tests/``, ``.beads/``, plus
``specs/*.md`` and ``src/lode/eval/corpus/*.md``, all of which carry real
relative links today and are exactly the shape this gate exists to check),
where the general form costs nothing extra to maintain and catches the next
one automatically. ``_tracked_markdown_files`` below now returns every
tracked ``*.md`` file, repo-wide, for the full walk; ``_tracked_other_files``
(the bare-citation pass) is UNCHANGED -- still every tracked file outside
``BARE_CITATION_EXCLUDE_DIRS`` -- so a markdown file outside those two
directories now gets BOTH passes. That is deliberate, not an oversight: the
bare pass alone still catches a citation with no markdown brackets at all
(backtick-quoted prose, e.g. tests/README.md's `` `docs/conventions.md` ``
mentions), a shape the bracket walk cannot see. Where the two passes report
the same break -- a link into a ``docs/`` target whose text repeats its
destination (the destination's own path is read by the bracket walk alone,
see LIMIT below) -- ``check`` de-duplicates its result list, so one real
break is still reported exactly once.

RETIRED (lode-6e9c): once the full bracketed-link walk widened repo-wide
(lode-act5, above), the old ``SCAN_DIRS`` name stopped bounding any scan at
all -- its only surviving job was excluding ``docs/``/``.claude/`` from the
bare-citation pass, the opposite of what "scan dirs" suggests. Renamed to
``BARE_CITATION_EXCLUDE_DIRS`` to say what it actually does; nothing about
its VALUE or behavior changed, only the name. This also collapsed
``check()`` to a single ``git ls-files`` fork per call (both file sets are
now derived in memory from one fetch) and to reading each markdown file from
disk at most once per call, via the ``_cached_text`` cache all three markdown
reads now go through: the bracketed walk's source read, the bare pass's
source read for a ``*.md`` file outside ``BARE_CITATION_EXCLUDE_DIRS``, and
``_slugs_for_file``'s read of a link TARGET -- the last being the one that
actually dominated, since nearly every anchor target is a ``docs/`` page the
walk had already read. Non-markdown files in the bare pass are deliberately
NOT cached: they are read exactly once, so retaining them would cost
megabytes of resident text for no saved read.

LIMIT (lode-905hd): a bare citation of a page whose path holds a space is
NOT supported, in any spelling. ``_DOC_ANCHOR_REF_RE``'s path class holds no
space, and a bare citation has no delimiter, so admitting one would swallow
the prose after it. What the pattern does with such a citation:

- In two shapes it matches a SHORTER path and checks that in the page's
  place. One: the text before the space already ends in ``.md`` (a page named
  ``a.md notes.md`` is read as ``a.md``). Two: the text after the space
  starts at the docs directory again (a directory named ``my docs``, say,
  inside it), and the citation is read from there on. The shorter path is
  reported missing when it does not exist, although the cited page does; when
  it exists the citation passes, although the cited page may not.
- Otherwise it matches nothing, and a dead citation of the page is not
  reported.

A ``%20`` for the space is not read either (``%`` is outside the class):
shape one is cut the same, and shape two then matches nothing.

Cite such a page with a markdown link instead, which reads its own delimiters
(angle brackets or percent-encoding, lode-tgr6). In a markdown file outside
``BARE_CITATION_EXCLUDE_DIRS`` the bare pass blanks out, before matching, the
path of each inline link destination that the markdown walk resolves (not a
URL, not one quoted in a code span), so that walk alone checks it, whole and
relative to its file (lode-wfy6i, lode-q14mi). The bare pattern then reads
nothing that starts in that path: no shorter path cut at a space, a tab, a
parenthesis, a comma or any other character outside its classes, no anchor
cut at a dot, and no link written below the repo root as a path from the
root. It still reads the link's text, its title, its fragment and link syntax
quoted in a code span. Still cut as above: such a page named by an image or a
reference definition. NEITHER walk resolves those, so blanking their
destinations would remove the only check the text gets. For those, write the
character percent-encoded and begin the destination with ``./``: the pattern
then matches nothing in it. No tracked path holds a space today.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, unquote_to_bytes

import typer

app = typer.Typer(add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories excluded from the bare-citation pass (``_bare_doc_anchor_refs``
#: / ``_tracked_other_files`` below) -- NOT a scan boundary any more
#: (lode-6e9c). Before lode-act5 this also bounded the full bracketed-link
#: walk; that job is gone now (the walk is repo-wide), leaving this constant
#: with exactly one surviving job: keeping ``docs/`` and ``.claude/`` (both
#: already fully covered by the bracketed-link walk) out of the bare-citation
#: pass, so a bare same-directory self-reference written IN docs/ prose
#: isn't double-checked by both passes. (No contiguous literal ``docs/*.md``
#: example here on purpose -- this file is itself scanned by
#: ``_DOC_ANCHOR_REF_RE`` as a tracked file outside this exclusion list; see
#: the identical concern noted near that regex's definition below.)
BARE_CITATION_EXCLUDE_DIRS = ("docs", ".claude")

# A markdown inline link, never an image (`![...]`): `[text](destination)`,
# with an optional title after the destination, on one line. Group 1 is an
# angle-bracket destination (`<sp ace.md>`), which may hold spaces and
# parentheses and ends at a `>` that whitespace or the closing `)` follows
# (CommonMark: `<a.md>x` is not one). Group 2 is any other destination, which
# ends at the first whitespace or `)`. Not recognised: a backslash escape
# (`\>`, `\)`), and a `)` in a destination without angle brackets (`%29`
# spells it there).
_LINK_RE = re.compile(
    r"(?<!!)\[[^\]]*\]\(\s*(?:<([^<>]*)>(?=[\s)])|([^\s)]+))(?:\s[^)]*)?\)"
)
# Inline code spans -- stripped before link and anchor-tag scanning so a
# *literal* markdown example inside backticks (docs/editing.md has several:
# `` `[text](url)` ``) is never mistaken for a real link or anchor. A span is
# an opening backtick run, the shortest text, and a closing run of exactly
# the same length (CommonMark), matched on ONE line. Not recognised: a span
# wrapped across lines and a backslash-escaped backtick, so whatever sits
# between two such backtick runs of the same length on a line is stripped.
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
# A backtick run followed by another backtick on the line is an inline code
# span, not a fence (CommonMark: a backtick fence's info string holds no
# backtick); tilde fences have no such restriction.
_FENCE_RE = re.compile(r"^\s*(`{3,}(?!.*`)|~{3,})")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# An explicit HTML anchor (`<a id="foo"></a>` or the older `name=`) is a
# second, independent way to make a target addressable in GFM -- not derived
# from any heading. .claude/skills/code/SKILL.md#reclaim is a real, working
# instance of exactly this: a step is cross-referenced from two places, and
# the anchor was hand-placed rather than relying on that step's own heading
# (which doesn't exist -- it's a bullet inside a numbered step, not a
# heading). These ids are literal, never slugified.
_HTML_ANCHOR_RE = re.compile(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', re.IGNORECASE)
# A bare (no markdown brackets) `docs/<path>.md` or `docs/<path>.md#<anchor>`
# text reference -- what a `.github/workflows/*.yml` or `scripts/*.sh` comment
# actually writes, AND what a `help=` docs/ footnote in `src/lode/cli/` writes
# when it points at a whole page rather than a specific heading (e.g. "See
# docs/how-to/maintenance-commands.md.", lode-6lvu). The `#<anchor>` suffix is
# optional, so both shapes are recognized by one regex; a reference lacking
# it still gets its FILE existence verified by `_resolve_error` (anchor
# checking simply doesn't apply when there is no anchor to check, same as an
# anchor-less markdown link). The anchor takes whole percent-escapes
# (`%C3%A9`) beside word characters and hyphens: cut at the `%`, an encoded
# anchor would be looked up as its first few characters. The path takes none.
# The `(?<![\w./-])` lookbehind makes "root-relative" mechanical: it refuses
# any `docs/` preceded by a path character, so a URL into ANOTHER repo's docs
# (`https://github.com/org/repo/blob/main/docs/release.md#anchor`) is never
# resolved against this tree. Without it, one upstream URL in a README turns
# this blocking gate red on a target that was never ours (verified: it did).
# The trailing `(?![\w-])` stops the match from swallowing a following word
# character or hyphen that would make the matched text not actually a plain
# `<page>.md` reference -- e.g. a hypothetical `<page>.mdx` or `<page>.md`
# immediately followed by a hyphenated suffix. (Written here without a
# contiguous literal `docs/*.md` example on purpose: this file is itself
# scanned by this same regex, as a tracked file outside
# BARE_CITATION_EXCLUDE_DIRS -- see tests/test_check_links.py's `_DOCS` split
# for the identical concern.)
_DOC_ANCHOR_REF_RE = re.compile(
    r"(?<![\w./-])docs/[\w./-]+\.md(?:#(?:[\w-]|%[0-9A-Fa-f]{2})+)?(?![\w-])"
)
# Extensions skipped when walking tracked files OUTSIDE
# BARE_CITATION_EXCLUDE_DIRS for a bare docs/ anchor reference:
# machine-generated data/lock formats, whose contents
# nobody edits by hand. `.jsonl` is the load-bearing entry, NOT dead weight --
# `.beads/issues.jsonl` is bd's passive, regenerated export of issue HISTORY,
# and closed tickets' free-text descriptions really do cite docs/ anchors
# (`docs/configuration.md#paths--locations` is in there today). A citation
# written into a ticket that closed months ago is not a pointer anyone
# maintains, and it cannot be repaired if its heading is later reworded --
# the file is regenerated from Dolt, so "fix the source" means editing closed
# history. Scanning it would let a legitimate heading rename wedge this gate
# red with no fix available.
_OTHER_SKIP_EXTENSIONS = {
    ".lock",
    ".jsonl",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
}


@dataclass(frozen=True)
class LinkError:
    source: Path
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: {self.reason} -> {self.target}"


def _missing_tracked_error(source: Path) -> LinkError | None:
    """The report for a tracked file that is gone from the working tree (its
    deletion was never staged), or ``None`` when the path is there.

    Gone means the path itself: nothing at it, or a directory above it
    replaced by a file -- the two cases git lists as deleted. A dangling
    symlink is still there, so it is not reported here: it is a file that
    cannot be read (``_unreadable_error``). A file a sparse checkout leaves
    out IS reported, although git does not call it deleted: the index is not
    consulted.

    The path is shell-quoted so either command can be pasted as printed."""
    if os.path.lexists(source):
        return None
    path = shlex.quote(str(source))
    return LinkError(
        source,
        0,
        target=f"git rm {path} (stage the deletion) or git restore {path}",
        reason="tracked file is missing from the working tree",
    )


def _unreadable_error(path: Path, exc: OSError) -> LinkError:
    """The report for a markdown file that is there but cannot be read: a
    dangling symlink, a directory in its place, no read permission. There is
    no single remedy, so the line gives the system's reason.

    Built from the file alone -- never from the link that led to it, and
    without ``str(exc)``, which repeats the path as the caller spelled it --
    so every failed read of one file is the identical entry, and ``check``'s
    de-duplication leaves one line per spelling of its path. A walk source is
    ``root / <tracked rel>`` and a link target is resolved, so the spellings
    differ only under a symlink. A link through a dangling symlink never gets
    here: its target does not exist, and the link is reported as such."""
    return LinkError(path, 0, exc.strerror or str(exc), "file cannot be read")


def _is_external(target: str) -> bool:
    """True for anything not resolvable as a path in this repo: a URL with a
    scheme (``http:``, ``mailto:``, ...) or a protocol-relative ``//...``."""
    return target.startswith("//") or bool(_SCHEME_RE.match(target))


def _tracked_paths(root: Path) -> list[Path]:
    """Every repo-relative path git tracks. ``check()`` calls this exactly
    once and passes the result to both filters below (lode-6e9c) -- before
    lode-act5 widened the markdown walk repo-wide the two filters issued
    genuinely different pathspec-scoped queries; once both became the
    identical unscoped one, forking git twice bought nothing.

    ``-z``: NUL-separated and never C-quoted, so a path holding a space or a
    non-ASCII byte reaches ``Path`` as the real name. The output is read as
    bytes and each name goes through ``os.fsdecode``: text mode would raise on
    a name that is not valid in the locale encoding and would turn a carriage
    return in a name into a newline."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    return [Path(os.fsdecode(p)) for p in out.split(b"\0") if p]


def _tracked_markdown_files(root: Path, tracked: list[Path]) -> list[Path]:
    """Every ``*.md`` file git tracks, repo-wide (lode-act5) -- not limited to
    ``docs/`` and ``.claude/``. Widened from the original two-directory scan
    so a bracketed relative link written in ANY tracked markdown file (a
    top-level ``README.md``, ``tests/README.md``, ...) gets the full
    link+anchor walk, not just the narrower bare-citation check. See the
    module docstring's SCOPE DECISION (lode-act5) for why the general form
    was chosen over a named allowlist of top-level READMEs.

    A pure filter over ``tracked`` (``_tracked_paths``' output): required, not
    optional, so ``check()`` stays the single owner of the ``git ls-files``
    fork and no caller can silently reintroduce a second one."""
    return sorted(root / rel for rel in tracked if rel.suffix == ".md")


def _tracked_other_files(root: Path, tracked: list[Path]) -> list[Path]:
    """Every tracked file OUTSIDE ``BARE_CITATION_EXCLUDE_DIRS`` -- the
    general form the scope decision in the module docstring calls for. Any
    of these can cite a ``docs/`` anchor in a bare-text comment (CI workflow
    YAML, a shell script, a ``README.md`` prose sentence with no markdown
    brackets, ...); ``_OTHER_SKIP_EXTENSIONS`` is subtracted.

    Deliberately NOT narrowed to non-markdown files even though
    ``_tracked_markdown_files`` above now ALSO walks every tracked ``*.md``
    file for the full bracketed-link check (lode-act5): a markdown file
    outside ``BARE_CITATION_EXCLUDE_DIRS`` can carry a *bare* citation with
    no brackets at all (e.g. tests/README.md's `` `docs/conventions.md` ``
    inline-code mentions), and that shape is only ever caught by this
    bare-citation pass, never by the bracket walk. A markdown file INSIDE
    ``BARE_CITATION_EXCLUDE_DIRS`` still never gets this pass -- unchanged
    from before this ticket, and out of its scope. The resulting two-pass
    overlap (impossible before lode-act5, since no file was ever in both
    walks' input sets) is handled by ``_mask_link_destinations`` for a link
    destination and by ``check``'s de-duplication for the rest, not by
    narrowing this set.

    ``tracked``: see ``_tracked_markdown_files`` above -- identical role."""
    return sorted(
        root / rel
        for rel in tracked
        if not (rel.parts and rel.parts[0] in BARE_CITATION_EXCLUDE_DIRS)
        and rel.suffix not in _OTHER_SKIP_EXTENSIONS
    )


def _numbered_lines(text: str) -> Iterator[tuple[int, str]]:
    """``(line_number, line)`` for every line of ``text``, numbered from 1 --
    the single home of where a line ends in this gate: at ``"\\n"`` and
    nowhere else. Every file the gate reads comes through ``read_text``, which
    has already turned ``\\r\\n`` and a bare ``\\r`` into ``"\\n"``.

    Never ``str.splitlines()``: it also breaks at a form feed, U+2028 and six
    more characters (``\\x0b``, ``\\x1c``-``\\x1e``, ``\\x85``, U+2029), so text
    after one would be a line of its own here -- a heading, a fence marker --
    while ``docs_slug.anchor_slugs``, which must return the same anchor set,
    splits on ``"\\n"`` and sees none.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # a final "\n" ends the last line, it does not start another
    return enumerate(lines, start=1)


def _mask_link_destinations(line: str) -> str:
    """``line`` with the path blanked out of each link destination that the
    markdown walk resolves (each one that is not external), so that walk
    alone reads it. The module docstring's LIMIT paragraph lists what the bare
    pattern would read out of such a path (lode-wfy6i, lode-q14mi).

    The fragment stays in place: ``_resolve_error`` does not look at one after
    a path that is not ``.md``, so the bare pass is the only check that text
    gets.

    A link is found as ``_links_in_file`` finds it, with inline code spans
    taken out first."""
    in_span = {i for s in _INLINE_CODE_RE.finditer(line) for i in range(*s.span())}
    # kept[i]: where the i-th character outside a code span sits in `line`.
    kept = [i for i in range(len(line)) if i not in in_span]
    chars = list(line)
    for m in _LINK_RE.finditer("".join(line[i] for i in kept)):
        group = 1 if m.group(1) is not None else 2
        dest = m.group(group)
        if not _is_external(dest):
            path_end = m.start(group) + len(dest.partition("#")[0])
            for i in kept[m.start(group) : path_end]:
                chars[i] = " "
    return "".join(chars)


def _bare_doc_anchor_refs(text: str, *, skip_fences: bool) -> list[tuple[int, str]]:
    """``(line_number, target)`` for every bare ``docs/<path>.md#<anchor>``
    text reference in a tracked file -- no markdown link brackets required.

    ``skip_fences`` is on for a markdown source (``README.md``, ``AGENTS.md``,
    ...), reusing ``_content_lines``: an anchor inside a ```` ``` ```` block
    is an example of the syntax, not a citation. It is off everywhere else,
    where a fence-shaped line carries no such meaning -- see
    ``tests/test_check_links.py`` for why applying it there would silently
    under-check.

    A markdown source is also the only kind the markdown walk reads, so the
    same flag turns on ``_mask_link_destinations``.
    """
    lines = _content_lines(text) if skip_fences else _numbered_lines(text)
    refs = []
    for line_no, line in lines:
        if skip_fences:
            line = _mask_link_destinations(line)
        for m in _DOC_ANCHOR_REF_RE.finditer(line):
            refs.append((line_no, m.group(0)))
    return refs


def github_slug(heading_text: str) -> str:
    """Reproduce GitHub's heading-to-anchor slug algorithm.

    Reverse-engineered from a real, currently-dead anchor in this repo
    (``docs/decisions.md`` -> ``agents-workflow.md#the-landing-loop--build-review-land-planned``,
    for the heading that used to read "The landing loop -- build, review,
    land (planned)"): strip markdown formatting down to plain text, lowercase
    it, delete every character that isn't a word character, hyphen, or space
    (deleted, not replaced by a space -- an em dash sitting between two
    spaces collapses to a run of TWO adjacent spaces once it's deleted, which
    is exactly why that anchor's slug has a double hyphen at "loop--build"),
    then convert each remaining space to a hyphen one-for-one -- consecutive
    hyphens from a multi-space run are never collapsed to one.
    """
    # Unwrap link text (`[text](url)` -> `text`) so only the visible text
    # feeds the slug -- the sole markdown construct needing a dedicated pass,
    # because its `(url)` must be dropped while the text is kept. Every other
    # formatting marker (backticks, `*` emphasis) is punctuation that the
    # char-class deletion below strips in place, so no per-construct regex is
    # needed for those.
    text = _LINK_TEXT_RE.sub(r"\1", heading_text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _content_lines(text: str) -> Iterator[tuple[int, str]]:
    """``(line_number, line)`` for every line OUTSIDE a fenced code block --
    the single home of the fence rule, shared by every scanner that skips code
    so they can never disagree about what counts as code (a shell comment like
    ``# run tests`` inside a ```bash``` fence must never read as a heading, nor
    a literal ``[text](url)`` example inside a fence as a real link).

    A SEPARATE single home from ``tests/conftest.py``'s ``fence_scan``, not a
    competing claim: this is production code and cannot import anything under
    ``tests/`` (lode-jm4a). The two do NOT agree on the rule, and are not meant
    to -- this one toggles on ANY fence marker (so a ``~~~`` line closes a
    ```-opened block) and does not strip blockquote markers; ``fence_scan``
    does both differently. Do not read either as documentation for the other.

    NOT a consumer of ``src/lode/fence_parsing.py`` (lode-ee7b), the ONE
    importable home of the CommonMark same-marker/at-least-as-long-close fence
    rule, even though this module *can* import from ``src/``: this function's
    simpler ANY-marker-closes rule is a deliberate, documented divergence, not
    an oversight -- unifying it would change behavior here (a real
    ``~~~``-inside-``` `` example in these docs would newly toggle where it
    doesn't today), which is exactly the kind of no-behaviour-change bar this
    ticket held everywhere else.

    One rule here is the same as ``fence_parsing``'s, as a local copy in
    ``_FENCE_RE`` and not an import: a backtick run with another backtick later
    on the line (an inline code span written with three backticks) is not a
    fence marker (lode-7jwe). ``_FENCE_RE`` decides opens and closes alike, so
    such a line inside an open fence is content and does not close it -- what
    ``fence_parsing`` reports there too.
    """
    in_fence = False
    for line_no, line in _numbered_lines(text):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line_no, line


def _headings(text: str) -> list[str]:
    """Every ATX heading's rendered text, in document order (fenced code
    blocks skipped by ``_content_lines``)."""
    headings = []
    for _, line in _content_lines(text):
        m = _ATX_HEADING_RE.match(line)
        if m:
            headings.append(m.group(1))
    return headings


def _cached_text(path: Path, cache: dict[Path, str]) -> str:
    """Read ``path`` once per ``check()`` call and remember it -- the single
    home of every markdown read in this gate (lode-6e9c). Keyed by the
    RESOLVED path, because the same file reaches this function under two
    spellings: a walk source is ``root / <tracked rel>`` while a link target
    has already been ``.resolve()``d, and any symlink above the repo root
    would otherwise make those two keys miss each other and read twice.

    Raises ``OSError`` exactly as a plain ``.read_text()`` would on the first
    read of an unreadable path; a cached hit can never raise, since an entry
    only exists once a read has already succeeded."""
    key = path.resolve()
    if key not in cache:
        cache[key] = path.read_text(encoding="utf-8", errors="replace")
    return cache[key]


def _slugs_for_file(path: Path, text_cache: dict[Path, str]) -> set[str]:
    """All valid anchor slugs for a markdown file: every heading's slug
    (including GitHub's disambiguating ``-1``, ``-2``, ... suffixes for
    repeated headings) plus every literal id from an explicit
    ``<a id="...">``/``<a name="...">`` anchor tag outside a fenced code
    block and outside an inline code span, matched one line at a time (as
    ``docs_slug.anchor_slugs`` does), so a tag split across lines offers no
    anchor.

    An inline code span here is exactly what ``_INLINE_CODE_RE`` strips, so
    its limits are this function's: a real tag between two backtick runs of
    the same length that do not delimit one span (a wrapped span's close and
    the next one's open, or escaped backticks) is lost. Indented code blocks
    are not tracked at all, so a tag in one still counts.

    Reads through ``text_cache`` (lode-6e9c): a link *target* is almost
    always some ``docs/*.md`` page the bracketed walk already read as a
    *source*, so without the shared cache this re-read the bulk of the
    repo's markdown a second time -- by far the larger of the two duplicate
    reads that ticket set out to remove."""
    text = _cached_text(path, text_cache)
    slugs: set[str] = set()
    seen_counts: dict[str, int] = {}
    for heading in _headings(text):
        base = github_slug(heading)
        count = seen_counts.get(base, 0)
        slug = base if count == 0 else f"{base}-{count}"
        seen_counts[base] = count + 1
        slugs.add(slug)
    for _, line in _content_lines(text):
        slugs.update(_HTML_ANCHOR_RE.findall(_INLINE_CODE_RE.sub("", line)))
    return slugs


def _links_in_file(text: str) -> list[tuple[int, str]]:
    """``(line_number, target)`` for every real inline link outside a fenced
    code block, with inline code spans stripped first so a literal
    markdown-syntax example inside backticks is never matched as a real link.

    ``_mask_link_destinations`` must find the same links: what it
    blanks, the bare pass no longer reads, so a link only it found would be
    checked by neither walk."""
    links = []
    for line_no, line in _content_lines(text):
        scrubbed = _INLINE_CODE_RE.sub("", line)
        for m in _LINK_RE.finditer(scrubbed):
            angle, plain = m.groups()
            links.append((line_no, plain if angle is None else angle))
    return links


#: The reason reported for a target that names no file, wherever it is found.
_NO_SUCH_FILE = "target file does not exist"


def _resolve_error(
    root: Path,
    source: Path,
    line_no: int,
    target: str,
    target_path: Path,
    anchor: str,
    slug_cache: dict[Path, set[str]],
    text_cache: dict[Path, str],
) -> LinkError | None:
    """The verdict on one already-resolved target: missing file, missing
    anchor, a file that cannot be read for its anchors, or fine. Shared by
    both walks in ``check`` so a broken link and a broken bare citation always
    report in the same words -- the two passes had already drifted to
    different wordings for the identical failure.

    ``anchor`` is the fragment as the source wrote it. It names an anchor as
    written or, failing that, percent-decoded as UTF-8 (``caf%C3%A9`` is the
    ``café`` slug) -- the two lookups a browser makes, in that order, so an
    explicit ``<a id="a%20b">`` is still reached by ``#a%20b``. An escape that
    is not UTF-8 decodes to U+FFFD, which is what ``_cached_text`` makes of
    such a byte in the target. The report quotes the anchor as written: a
    decoded one may hold a newline."""
    if not target_path.exists():
        return LinkError(source, line_no, target, _NO_SUCH_FILE)
    if not anchor or target_path.suffix != ".md":
        return None
    if target_path not in slug_cache:
        try:
            slug_cache[target_path] = _slugs_for_file(target_path, text_cache)
        except OSError as exc:
            return _unreadable_error(target_path, exc)
    slugs = slug_cache[target_path]
    if anchor in slugs or unquote(anchor) in slugs:
        return None
    try:
        display = target_path.relative_to(root)
    except ValueError:
        display = target_path
    return LinkError(
        source, line_no, target, f"no heading slug '#{anchor}' in {display}"
    )


def check(root: Path) -> list[LinkError]:
    errors: list[LinkError] = []
    slug_cache: dict[Path, set[str]] = {}
    text_cache: dict[Path, str] = {}
    tracked = _tracked_paths(root)  # single 'git ls-files' fork for this call

    for source in _tracked_markdown_files(root, tracked):
        missing = _missing_tracked_error(source)
        if missing:
            errors.append(missing)
            continue
        try:
            source_text = _cached_text(source, text_cache)
        except OSError as exc:
            errors.append(_unreadable_error(source, exc))
            continue
        for line_no, target in _links_in_file(source_text):
            if not target or _is_external(target):
                continue
            file_part, _, anchor = target.partition("#")
            # The path of a link is percent-encoded (`sp%20ace.md`). Decoded
            # as bytes, then as `_tracked_paths` decodes a name, so an escape
            # that is not UTF-8 (`%E9`) names the file whose name holds it.
            # The anchor stays as written: `_resolve_error` decodes it.
            file_part = os.fsdecode(unquote_to_bytes(file_part))
            if "\0" in file_part:
                # No file name holds a NUL, and resolve() raises on one.
                errors.append(LinkError(source, line_no, target, _NO_SUCH_FILE))
                continue
            # A markdown link is relative to the file it is written in.
            target_path = (
                (source.parent / file_part).resolve() if file_part else source.resolve()
            )
            error = _resolve_error(
                root,
                source,
                line_no,
                target,
                target_path,
                anchor,
                slug_cache,
                text_cache,
            )
            if error:
                errors.append(error)

    for source in _tracked_other_files(root, tracked):
        missing = _missing_tracked_error(source)
        if missing:
            errors.append(missing)
            continue
        try:
            # Only markdown is worth caching here: a `.md` source outside
            # BARE_CITATION_EXCLUDE_DIRS was already read by the walk above
            # (and may yet be read again as a link target), while the rest of
            # this set -- src/, tests/, workflow YAML, shell scripts -- is
            # read exactly once, so caching it would retain megabytes of text
            # for the whole call in exchange for nothing.
            source_text = (
                _cached_text(source, text_cache)
                if source.suffix == ".md"
                else source.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        for line_no, target in _bare_doc_anchor_refs(
            source_text, skip_fences=source.suffix == ".md"
        ):
            file_part, _, anchor = target.partition("#")
            # A bare citation is always root-relative; the regex enforces it.
            error = _resolve_error(
                root,
                source,
                line_no,
                target,
                (root / file_part).resolve(),
                anchor,
                slug_cache,
                text_cache,
            )
            if error:
                errors.append(error)
    # Since lode-act5 a markdown file outside BARE_CITATION_EXCLUDE_DIRS goes
    # through BOTH walks, so a link whose text repeats its `docs/` destination,
    # or one missing file, can produce the identical LinkError twice; so can each
    # link into a file that cannot be read (`_unreadable_error`). De-duplicate
    # (order-preserving; LinkError is a frozen dataclass, so hashable) --
    # reporting one break twice would also double-count it in the summary line.
    return list(dict.fromkeys(errors))


@app.command()
def main(
    root: Annotated[
        Path | None,
        typer.Option(
            "--root", help="Repo root to scan (defaults to this checkout's root)."
        ),
    ] = None,
) -> None:
    """Fail if any relative markdown link in any tracked *.md file is broken,
    or any docs/ anchor cited elsewhere in the tree (e.g. .github/workflows/,
    scripts/) does not resolve."""
    target_root = (root or REPO_ROOT).resolve()
    errors = check(target_root)
    if errors:
        for error in errors:
            print(str(error), file=sys.stderr)
        print(f"\n{len(errors)} broken markdown link(s) found", file=sys.stderr)
        raise typer.Exit(1)
    print(
        "OK: every relative markdown link in every tracked *.md file resolves, and "
        "every docs/ anchor citation elsewhere in the tree resolves"
    )


if __name__ == "__main__":
    app()
