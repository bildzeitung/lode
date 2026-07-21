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

    python scripts/check_links.py            # scan this repo's docs/ + .claude/
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
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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


def _tracked_markdown_files(root: Path) -> list[Path]:
    """Every ``*.md`` file git tracks under ``docs/`` and ``.claude/`` -- scoped
    to tracked files (mirroring the ``shellcheck`` nox session's own
    ``git ls-files`` scoping) so scratch or gitignored markdown never enters
    the gate."""
    existing_dirs = [d for d in SCAN_DIRS if (root / d).is_dir()]
    if not existing_dirs:
        return []
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", *existing_dirs],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(root / p for p in out.split() if p.endswith(".md"))


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
    a literal ``[text](url)`` example inside a fence as a real link)."""
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


def check(root: Path) -> list[LinkError]:
    errors: list[LinkError] = []
    slug_cache: dict[Path, set[str]] = {}
    for source in _tracked_markdown_files(root):
        source_text = source.read_text(encoding="utf-8", errors="replace")
        for line_no, target in _links_in_file(source_text):
            if not target or _is_external(target):
                continue
            file_part, _, anchor = target.partition("#")
            target_path = (
                (source.parent / file_part).resolve() if file_part else source.resolve()
            )
            if not target_path.exists():
                errors.append(
                    LinkError(source, line_no, target, "target file does not exist")
                )
                continue
            if anchor and target_path.suffix == ".md":
                if target_path not in slug_cache:
                    slug_cache[target_path] = _slugs_for_file(target_path)
                if anchor not in slug_cache[target_path]:
                    try:
                        display = target_path.relative_to(root)
                    except ValueError:
                        display = target_path
                    errors.append(
                        LinkError(
                            source,
                            line_no,
                            target,
                            f"no heading slug '#{anchor}' in {display}",
                        )
                    )
    return errors


@app.command()
def main(
    root: Path = typer.Option(
        None,
        "--root",
        help="Repo root to scan (defaults to this checkout's root).",
    ),
) -> None:
    """Fail if any relative markdown link in docs/ (and .claude/) is broken."""
    target_root = (root or REPO_ROOT).resolve()
    errors = check(target_root)
    if errors:
        for error in errors:
            print(str(error), file=sys.stderr)
        print(f"\n{len(errors)} broken markdown link(s) found", file=sys.stderr)
        raise typer.Exit(1)
    print("OK: every relative markdown link in docs/ and .claude/ resolves")


if __name__ == "__main__":
    app()
