#!/usr/bin/env python3
"""Verify every relative markdown link in docs/ and .claude/ resolves (lode-dkdg).

docs/ leans heavily on deep cross-document anchors (decisions.md alone points
into stack.md, externals.md, editing.md, agents-workflow.md), but GitHub's
anchor slugs are derived from heading TEXT -- so rewording a heading silently
breaks every inbound link, with nothing failing to report it. This gate walks
every tracked ``*.md`` file under ``docs/`` and ``.claude/``, resolves each
relative markdown link's target file, and -- for a ``#anchor`` link -- checks
the anchor against the target file's headings, slugged the same way GitHub
slugs them.

CONCRETE EVIDENCE this already happens in trunk (found by an ad-hoc slug
check before this gate existed): ``docs/decisions.md`` carried a dead anchor
into ``agents-workflow.md#the-landing-loop--build-review-land-planned`` --
the heading had been reworded to drop "(planned)" and nothing reported it.
That case is captured verbatim in ``tests/test_check_links.py`` as a
regression lock on the slug algorithm itself.

Usage::

    python scripts/check_links.py            # scan this repo's docs/ + .claude/ + every other tracked file
    python scripts/check_links.py --root DIR # scan a different tree (tests)

Exits 1 and prints one ``file:line: reason -> target`` line per broken link
(plus a summary count) on any broken file target or broken anchor; exits 0
and prints a short OK line otherwise.

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
``SCAN_DIRS`` is scanned for a bare ``docs/<path>.md#<anchor>`` text
reference (``_bare_doc_anchor_refs`` / ``_tracked_other_files`` below), not
just workflow YAML -- ``scripts/``, ``noxfile.py`` and ``src/`` all cite
docs/ anchors the identical way, so special-casing one directory would have
left the others silently ungated again.

This second pass is deliberately narrower than the SCAN_DIRS walk: it
recognizes only a literal, root-relative ``docs/<path>.md#<anchor>``
substring -- the shape every real instance in this repo is written in,
regardless of the citing file's own directory depth.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(add_completion=False)

REPO_ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("docs", ".claude")

# A markdown inline link: `[text](target)`, never an image (`![...]`).
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
# Inline code spans -- stripped before link scanning so a *literal* markdown
# example inside backticks (docs/editing.md has several: `` `[text](url)` ``)
# is never mistaken for a real link.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# An explicit HTML anchor (`<a id="foo"></a>` or the older `name=`) is a
# second, independent way to make a target addressable in GFM -- not derived
# from any heading. .claude/skills/code/SKILL.md#reclaim is a real, working
# instance of exactly this: a step is cross-referenced from two places, and
# the anchor was hand-placed rather than relying on that step's own heading
# (which doesn't exist -- it's a bullet inside a numbered step, not a
# heading). These ids are literal, never slugified.
_HTML_ANCHOR_RE = re.compile(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', re.IGNORECASE)
# A bare (no markdown brackets) `docs/<path>.md#<anchor>` text reference --
# what a `.github/workflows/*.yml` or `scripts/*.sh` comment actually writes.
# The `(?<![\w./-])` lookbehind makes "root-relative" mechanical: it refuses
# any `docs/` preceded by a path character, so a URL into ANOTHER repo's docs
# (`https://github.com/org/repo/blob/main/docs/release.md#anchor`) is never
# resolved against this tree. Without it, one upstream URL in a README turns
# this blocking gate red on a target that was never ours (verified: it did).
_DOC_ANCHOR_REF_RE = re.compile(r"(?<![\w./-])docs/[\w./-]+\.md#[\w-]+")
# Extensions skipped when walking tracked files OUTSIDE SCAN_DIRS for a bare
# docs/ anchor reference: machine-generated data/lock formats, whose contents
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


def _is_external(target: str) -> bool:
    """True for anything not resolvable as a path in this repo: a URL with a
    scheme (``http:``, ``mailto:``, ...) or a protocol-relative ``//...``."""
    return target.startswith("//") or bool(_SCHEME_RE.match(target))


def _tracked_paths(root: Path, *pathspecs: str) -> list[Path]:
    """Repo-relative paths git tracks, optionally narrowed by pathspec -- the
    single home of this gate's ``git ls-files`` scoping (mirroring the
    ``shellcheck`` nox session's) so scratch or gitignored files never enter
    it, and so both walks below parse that output exactly one way."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", *pathspecs],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [Path(p) for p in out.split()]


def _tracked_markdown_files(root: Path) -> list[Path]:
    """Every ``*.md`` file git tracks under ``docs/`` and ``.claude/``."""
    existing_dirs = [d for d in SCAN_DIRS if (root / d).is_dir()]
    if not existing_dirs:
        return []
    return sorted(
        root / rel
        for rel in _tracked_paths(root, *existing_dirs)
        if rel.suffix == ".md"
    )


def _tracked_other_files(root: Path) -> list[Path]:
    """Every tracked file OUTSIDE ``SCAN_DIRS`` -- the general form the scope
    decision in the module docstring calls for. Any of these can cite a
    ``docs/`` anchor in a bare-text comment (CI workflow YAML, a shell
    script, ...); ``_OTHER_SKIP_EXTENSIONS`` is subtracted."""
    return sorted(
        root / rel
        for rel in _tracked_paths(root)
        if not (rel.parts and rel.parts[0] in SCAN_DIRS)
        and rel.suffix not in _OTHER_SKIP_EXTENSIONS
    )


def _bare_doc_anchor_refs(text: str, *, skip_fences: bool) -> list[tuple[int, str]]:
    """``(line_number, target)`` for every bare ``docs/<path>.md#<anchor>``
    text reference in a file outside ``SCAN_DIRS`` -- no markdown link
    brackets required.

    ``skip_fences`` is on for a markdown source (``README.md``, ``AGENTS.md``,
    ...), reusing ``_content_lines``: an anchor inside a ```` ``` ```` block
    is an example of the syntax, not a citation. It is off everywhere else,
    where a fence-shaped line carries no such meaning -- see
    ``tests/test_check_links.py`` for why applying it there would silently
    under-check.
    """
    lines = _content_lines(text) if skip_fences else enumerate(text.splitlines(), 1)
    refs = []
    for line_no, line in lines:
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
    the single home of the fence rule, shared by the heading and link scanners
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
    """
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
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


def _slugs_for_file(path: Path) -> set[str]:
    """All valid anchor slugs for a markdown file: every heading's slug
    (including GitHub's disambiguating ``-1``, ``-2``, ... suffixes for
    repeated headings) plus every literal id from an explicit
    ``<a id="...">``/``<a name="...">`` anchor tag."""
    text = path.read_text(encoding="utf-8", errors="replace")
    slugs: set[str] = set()
    seen_counts: dict[str, int] = {}
    for heading in _headings(text):
        base = github_slug(heading)
        count = seen_counts.get(base, 0)
        slug = base if count == 0 else f"{base}-{count}"
        seen_counts[base] = count + 1
        slugs.add(slug)
    slugs.update(_HTML_ANCHOR_RE.findall(text))
    return slugs


def _links_in_file(text: str) -> list[tuple[int, str]]:
    """``(line_number, target)`` for every real inline link outside a fenced
    code block, with inline code spans stripped first so a literal
    markdown-syntax example inside backticks is never matched as a real link."""
    links = []
    for line_no, line in _content_lines(text):
        scrubbed = _INLINE_CODE_RE.sub("", line)
        for m in _LINK_RE.finditer(scrubbed):
            raw_target = m.group(1).strip()
            # Strip an optional `"title"` after the URL, and a wrapping <>.
            target = raw_target.split(None, 1)[0].strip("<>")
            links.append((line_no, target))
    return links


def _resolve_error(
    root: Path,
    source: Path,
    line_no: int,
    target: str,
    target_path: Path,
    anchor: str,
    slug_cache: dict[Path, set[str]],
) -> LinkError | None:
    """The verdict on one already-resolved target: missing file, missing
    anchor, or fine. Shared by both walks in ``check`` so a broken link and a
    broken bare citation always report in the same words -- the two passes had
    already drifted to different wordings for the identical failure."""
    if not target_path.exists():
        return LinkError(source, line_no, target, "target file does not exist")
    if not anchor or target_path.suffix != ".md":
        return None
    if target_path not in slug_cache:
        slug_cache[target_path] = _slugs_for_file(target_path)
    if anchor in slug_cache[target_path]:
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
    for source in _tracked_markdown_files(root):
        source_text = source.read_text(encoding="utf-8", errors="replace")
        for line_no, target in _links_in_file(source_text):
            if not target or _is_external(target):
                continue
            file_part, _, anchor = target.partition("#")
            # A markdown link is relative to the file it is written in.
            target_path = (
                (source.parent / file_part).resolve() if file_part else source.resolve()
            )
            error = _resolve_error(
                root, source, line_no, target, target_path, anchor, slug_cache
            )
            if error:
                errors.append(error)

    for source in _tracked_other_files(root):
        try:
            source_text = source.read_text(encoding="utf-8", errors="replace")
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
            )
            if error:
                errors.append(error)
    return errors


@app.command()
def main(
    root: Annotated[
        Path | None,
        typer.Option(
            "--root", help="Repo root to scan (defaults to this checkout's root)."
        ),
    ] = None,
) -> None:
    """Fail if any relative markdown link in docs/ (and .claude/) is broken, or
    any docs/ anchor cited elsewhere in the tree (e.g. .github/workflows/,
    scripts/) does not resolve."""
    target_root = (root or REPO_ROOT).resolve()
    errors = check(target_root)
    if errors:
        for error in errors:
            print(str(error), file=sys.stderr)
        print(f"\n{len(errors)} broken markdown link(s) found", file=sys.stderr)
        raise typer.Exit(1)
    print(
        "OK: every relative markdown link in docs/ and .claude/ resolves, and every "
        "docs/ anchor citation elsewhere in the tree resolves"
    )


if __name__ == "__main__":
    app()
