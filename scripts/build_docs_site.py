#!/usr/bin/env python3
"""Stage the published docs subset for the Zensical site build (lode-fhql.9).

This is the "wiring" .9 owns, built against the architecture .8 decided in
docs/stack.md: the site generator never renders Mermaid at build time on its own
(neither does Sphinx) -- both hand a live ``mermaid.js`` require to the
visitor's browser, so a broken diagram would ship as a silently-empty box.
This script is the *new* renderer docs/stack.md calls for: it walks every
```mermaid fenced block in the PUBLISHED subset of docs/, renders each to SVG
through the same mermaid-cli Docker image scripts/validate-mermaid.sh gates
with -- at a PINNED tag rather than that script's floating ``:latest``, see
MERMAID_IMAGE below -- and embeds the SVG in place of the fence; the built
site never ships a live client-side Mermaid require. Any render failure aborts
the whole build (a machine/content distinction, unlike validate-mermaid.sh's gate
contract, is not needed here -- this is a build step, not a merge gate; any
nonzero from `docker run` fails the build loudly, full stop).

It also applies docs/stack.md's ONE link-rewrite rule: a relative link inside
a published page that targets a file NOT in the published set is rewritten to
that file's GitHub blob URL (fragment preserved verbatim) instead of shipping
a broken relative link on the site. Links between published pages are left
alone.

Last, it refuses to stage a page whose markdown image points at a file the
staged tree does not have: the site generator's link validation resolves
links to pages only, so nothing downstream would report it.

Usage:
    scripts/build_docs_site.py <output-dir>

Exits non-zero (with a message on stderr) on any render or I/O failure --
never a silent partial build.
"""

from __future__ import annotations

import functools
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_from_bytes, unquote, unquote_to_bytes

import typer

# src/ on the path so the fence rule below comes from lode.fence_parsing --
# the ONE importable home of it (lode-ee7b) -- without this CI job having to
# `pip install` the package. Same approach as scripts/check_docstring_refs.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from lode.docs_slug import anchor_slugs
from lode.fence_parsing import fence_flags

# The pinned mermaid-cli image. Deliberately NOT `:latest` -- lode-fhql.9's
# own acceptance requires the toolchain pinned, not floating; this is a
# build-output artifact (embedded in every page the site ships) where an
# unannounced upstream change is a worse surprise than in a pass/fail
# validation gate.
#
# docs/stack.md mandates ONE image shared with validate-mermaid.sh, "not a
# second, independently-versioned copy" -- converged by lode-3ld8:
# scripts/validate-mermaid.sh and scripts/update-images.sh now pin the same
# tag as this constant. tests/test_build_docs_site.py's
# test_workflow_pins_match_their_sources pins .github/workflows/docs.yml to
# this value, and test_validate_mermaid_and_update_images_pin_match_build_docs_site
# pins those two scripts to it too -- bump all three alongside this constant.
MERMAID_IMAGE = "minlag/mermaid-cli:10.9.1"

# docs/stack.md "Published / excluded page sets" (lode-fhql.8, current as of
# 2026-08-13). PUBLISHED is authoritative; docs/how-to/ is published as a
# DIRECTORY (every file in it, not a frozen list) -- see that section.
# "index.md" is the landing page (lode-fhql.10, postdates the 2026-08-12
# PUBLISHED call recorded in stack.md) -- staging it here is what lets the
# real landing page ship instead of build()'s placeholder fallback below.
PUBLISHED_TOP_LEVEL = [
    "index.md",
    "design.md",
    "retrieval.md",
    "storage.md",
    "externals.md",
    "brand.md",
    # lode-fhql.15's derived reference pages, wired in by lode-7uze.
    # See DERIVED_PAGE_ALIASES below for the link precedence they get.
    "keymap.md",
    "settings.md",
]
PUBLISHED_DIRS = ["how-to"]

# docs/stack.md ("`lode-fhql.15`'s derived pages take precedence once they
# exist"): a link elsewhere in the published set that cites one of these
# maintainer docs resolves to its derived, published counterpart instead of
# falling through to the one GITHUB_BASE rewrite rule. Keyed by repo-root-
# relative POSIX path (matching _rewrite_target's root_rel_str), valued
# docs-relative (matching the published set).
DERIVED_PAGE_ALIASES = {
    "docs/keybindings.md": "keymap.md",
    "docs/configuration.md": "settings.md",
}

# Static assets the theme (mkdocs.yml: theme.logo/favicon, docs/overrides/
# main.html's OG tags) references by a docs_dir-relative path -- these are
# not markdown and carry no mermaid/link processing, just a verbatim copy
# into the staged tree so the generator can find them (lode-fhql.9/.10 mkdocs.yml
# merge, 2026-08-14).
ASSETS_DIR = "assets"

GITHUB_BASE = "https://github.com/bildzeitung/lode/blob/trunk"

_MERMAID_FENCE_RE = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
# A markdown inline link, never an image. The destination grammar is
# scripts/check_links.py's _LINK_RE (group 2: angle-bracket destination, group
# 3: any other; an optional title follows), differing only in capturing the
# label as group 1. tests/test_build_docs_site.py holds the two equal.
_LINK_RE = re.compile(
    r"(?<!!)\[([^\]]*)\]\(\s*(?:<([^<>]*)>(?=[\s)])|([^\s)]+))(?:\s[^)]*)?\)"
)
# An inline code span. Same one-line, run-aware rule as scripts/check_links.py's
# _INLINE_CODE_RE, and for the same reason: a literal `[text](foo.md)` written
# as an EXAMPLE inside backticks (or inside a fenced block) is documentation,
# not a link, and rewriting it would corrupt the published page.
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
# A markdown image, with or without a quoted title after the target.
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")


@functools.cache
def _alias_anchors(rel: str) -> frozenset[str]:
    """Anchors the derived page `rel` (docs-relative) actually offers.

    Read from `docs/` -- the tree `build()` stages FROM, so the staged copy
    carries the same anchors. Cached: `DERIVED_PAGE_ALIASES` has two values,
    so this reads at most two files per run.

    Used to decide whether an aliased link's fragment survives the rewrite:
    mkdocs.yml sets `validation.invalid_link_anchors`, which `zensical build
    --strict` turns into a build failure, so pointing a link at an anchor the
    derived page does not have is worse than leaving it on GitHub.
    """
    path = _REPO_ROOT / "docs" / rel
    if not path.is_file():
        return frozenset()
    return frozenset(anchor_slugs(path.read_text(encoding="utf-8")))


def _published_set(docs_dir: Path) -> set[str]:
    """Relative (POSIX, to docs/) paths of every published file."""
    published = set(PUBLISHED_TOP_LEVEL)
    for d in PUBLISHED_DIRS:
        # rglob, not glob: docs/stack.md publishes how-to as a DIRECTORY, so a
        # guide filed under a subdirectory later is published by default too.
        published |= {
            p.relative_to(docs_dir).as_posix() for p in (docs_dir / d).rglob("*.md")
        }
    return published


def _render_mermaid_svg(code: str, out_svg: Path) -> None:
    """Render one Mermaid block to `out_svg` via the pinned Docker image.

    Mirrors scripts/validate-mermaid.sh's puppeteer/chromium setup. Any
    nonzero exit from `docker run` raises -- this is a build step, not a
    gate, so there is no machine-fault/content split to preserve; every
    failure here must fail the site build loudly, which a raised exception
    propagating out of main() already does.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "input.mmd").write_text(code, encoding="utf-8")
        cfg = tmp / "puppeteer.json"
        cfg.write_text(
            '{"executablePath":"/usr/bin/chromium-browser",'
            '"args":["--no-sandbox","--disable-setuid-sandbox"]}',
            encoding="utf-8",
        )
        # The container's non-root user needs read+traverse+WRITE access to
        # the bind-mounted dir (mmdc writes out.svg into it) and read access
        # to the input files (tempfile.mkdtemp creates the dir 0700,
        # group/other-inaccessible) -- 0777 rather than validate-mermaid.sh's
        # 755 because, unlike that read-only validation mount, this one must
        # accept a write-back from the container.
        tmp.chmod(0o777)
        (tmp / "input.mmd").chmod(0o644)
        cfg.chmod(0o644)
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{tmp}:/data",
                "-w",
                "/data",
                MERMAID_IMAGE,
                "-p",
                "/data/puppeteer.json",
                "-i",
                "/data/input.mmd",
                "-o",
                "/data/out.svg",
                "-b",
                "transparent",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,  # we inspect returncode ourselves below
        )
        if result.returncode != 0:
            raise SystemExit(
                "mermaid render FAILED (docker exit "
                f"{result.returncode}) while rendering a diagram for {out_svg}:\n"
                f"{result.stdout}\n{result.stderr}\n"
                "This fails the docs-site build -- a broken diagram must never "
                "ship as a silently-empty box (docs/stack.md, lode-fhql.8)."
            )
        out_svg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp / "out.svg", out_svg)


def _process_mermaid(
    text: str,
    rel_path: str,
    assets_dir: Path,
    out_dir: Path,
    render_mermaid: bool = True,
) -> str:
    """Replace every ```mermaid fence in `text` with an embedded SVG image.

    ``render_mermaid=False`` (the ``--no-mermaid`` / copy-only mode, lode-
    fhql.9's HUMAN DECISION 2026-08-14) skips the Docker render entirely and
    leaves every fence byte-for-byte as-is -- no substitution at all. That
    mode exists so `nox -s docs` can stage + `zensical build --strict` without
    Docker, keeping the default `nox` set offline; the real, Docker-backed
    pre-render stays exclusive to `.github/workflows/docs.yml`, which never
    passes this flag.
    """
    if not render_mermaid:
        return text
    page_slug = rel_path.replace("/", "-").removesuffix(".md")
    counter = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        code = match.group(1)
        svg_name = f"{page_slug}-{counter}.svg"
        svg_path = assets_dir / svg_name
        print(
            f"  rendering mermaid diagram {counter} of {rel_path} -> assets/mermaid/{svg_name}"
        )
        _render_mermaid_svg(code, svg_path)
        # Path relative to the page's own directory once staged.
        page_dir = (out_dir / rel_path).parent
        rel_svg = Path(os.path.relpath(svg_path, start=page_dir)).as_posix()
        return f"![Diagram]({rel_svg})"

    return _MERMAID_FENCE_RE.sub(_sub, text)


def _rewrite_target(
    current_rel: str, link_target: str, published: set[str]
) -> str | None:
    """Return a rewritten link target, or None if the link is left alone."""
    if not link_target or link_target.startswith(
        ("http://", "https://", "mailto:", "#")
    ):
        return None
    path_part, _, fragment = link_target.partition("#")
    # The path of a link is percent-encoded; the published set and the alias
    # table hold plain names. Decoded as the link gate decodes it.
    path_part = os.fsdecode(unquote_to_bytes(path_part))
    # Resolve relative to docs/, allowing `..` to escape it (a published page
    # linking ../README.md or ../src/lode/config.py resolves repo-root-relative).
    root_rel_str = posixpath.normpath(
        posixpath.join("docs", posixpath.dirname(current_rel), path_part)
    )
    if root_rel_str.startswith(".."):
        # Escapes the repo entirely -- no GitHub blob URL can express it, so
        # leave it verbatim rather than emit a confidently-wrong link.
        return None

    if root_rel_str.startswith("docs/") and root_rel_str[len("docs/") :] in published:
        return None  # stays a plain relative link between published pages

    # A citation of a maintainer doc resolves to its derived, published
    # counterpart -- but a fragment only survives if that page HAS the anchor
    # (the derived pages are curated subsets; see _alias_anchors). Anything
    # else falls through to the GitHub blob URL below, which still resolves to
    # exactly what was cited.
    alias = DERIVED_PAGE_ALIASES.get(root_rel_str)
    # The fragment is looked up as written, then percent-decoded -- the order
    # the link gate uses -- and kept as written in the rewritten link.
    if alias in published and (
        not fragment
        or fragment in _alias_anchors(alias)
        or unquote(fragment) in _alias_anchors(alias)
    ):
        # A fresh relative link from the current page's directory: the alias
        # target's filename differs from what the source markdown wrote.
        current_dir = posixpath.dirname(current_rel)
        rel_to_alias = posixpath.relpath(alias, start=current_dir or ".")
        return f"{rel_to_alias}#{fragment}" if fragment else rel_to_alias

    # Everything else -- an unpublished docs/ page, a repo-root file, a source
    # file -- gets the one rewrite rule: its GitHub blob URL, fragment verbatim.
    url = f"{GITHUB_BASE}/{quote_from_bytes(os.fsencode(root_rel_str), safe='/')}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Character ranges of `text` that are code: fenced lines and inline spans."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    lines = text.split("\n")
    for line, fenced in zip(lines, fence_flags(lines), strict=True):
        if fenced:
            ranges.append((offset, offset + len(line)))
        else:
            ranges += [
                (offset + m.start(), offset + m.end())
                for m in _INLINE_CODE_RE.finditer(line)
            ]
        offset += len(line) + 1  # +1 for the "\n" that split() removed
    return ranges


def _process_links(text: str, rel_path: str, published: set[str]) -> str:
    """Apply the rewrite rule to every real link, skipping code.

    Fenced blocks and inline code spans are left untouched -- a published page
    that documents markdown syntax must ship that example verbatim, not a
    rewritten GitHub URL.
    """
    protected = _code_ranges(text)
    out: list[str] = []
    pos = 0
    # Searched over the WHOLE text, never line by line: a link's label
    # routinely wraps across lines in these docs, and _LINK_RE has to see it
    # whole or it silently stops matching.
    while match := _LINK_RE.search(text, pos):
        start, end = match.span()
        label_end = match.end(1)
        if "\n" in text[label_end:end]:
            # The gate reads a destination and its title on ONE line, so a
            # match that runs past the line its label ends on is prose holding
            # an unclosed `](`, not a link. Look again from just past the
            # label: a real link further on is still read.
            end = label_end
        elif not any(lo <= start < hi for lo, hi in protected):
            # Keyed on where the link STARTS, so a link whose label is itself
            # backticked -- [`configuration.md`](configuration.md), the
            # dominant form in these docs -- is still rewritten; only a link
            # that starts inside code is left alone.
            group = 2 if match.group(2) is not None else 3
            rewritten = _rewrite_target(rel_path, match.group(group), published)
            if rewritten is not None:
                # Spliced over the destination text alone, so its angle
                # brackets, a title and the spacing survive.
                dest_start, dest_end = match.span(group)
                out += [text[pos:dest_start], rewritten]
                pos = dest_end
        out.append(text[pos:end])
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _missing_image_targets(text: str, page: Path) -> list[str]:
    """Relative image targets in staged `text` with no file beside `page`.

    Zensical's ``invalid_links`` check resolves links to PAGES only: an image
    whose file is missing builds clean under ``--strict`` and ships broken
    (measured on 0.1.3). The staged tree is the site's whole input, so a
    target absent from it is absent from the site. Images inside code are
    examples, not images, and are skipped like links are.
    """
    protected = _code_ranges(text)
    missing = []
    for match in _IMAGE_RE.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "data:")):
            continue
        if any(lo <= match.start() < hi for lo, hi in protected):
            continue
        path_part = target.partition("#")[0].partition("?")[0]
        if not (page.parent / path_part).is_file():
            missing.append(target)
    return missing


def build(repo_root: Path, out_dir: Path, render_mermaid: bool = True) -> None:
    docs_dir = repo_root / "docs"
    published = _published_set(docs_dir)
    # `out_dir` is wiped below, so refuse anything that isn't a disposable
    # staging directory INSIDE the repo. A mistyped argument otherwise turns
    # this into `rm -rf` on whatever it names (the CI invocation passes a
    # relative path, so a stray leading `/` is one keystroke away).
    if repo_root not in out_dir.parents or out_dir.is_relative_to(docs_dir):
        raise SystemExit(
            f"refusing to wipe {out_dir}: the staging dir must be inside "
            f"{repo_root} and outside {docs_dir}"
        )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    assets_dir = out_dir / "assets" / "mermaid"

    # Static assets (theme.logo/favicon, the OG-card image) -- a verbatim
    # copy, no mermaid/link processing. Copied before any mermaid render so
    # assets_dir.mkdir(parents=True) below never races a directory this
    # copytree already created.
    src_assets_dir = docs_dir / ASSETS_DIR
    if src_assets_dir.is_dir():
        shutil.copytree(src_assets_dir, out_dir / ASSETS_DIR, dirs_exist_ok=True)

    missing_images: list[str] = []
    for rel in sorted(published):
        src = docs_dir / rel
        if not src.is_file():
            raise SystemExit(
                f"published doc {rel!r} listed in build_docs_site.py but missing on disk: {src}"
            )
        text = src.read_text(encoding="utf-8")
        text = _process_mermaid(
            text, rel, assets_dir, out_dir, render_mermaid=render_mermaid
        )
        text = _process_links(text, rel, published)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        missing_images += [
            f"{rel}: {target}" for target in _missing_image_targets(text, dest)
        ]
        print(f"staged {rel}")
    if missing_images:
        raise SystemExit(
            "published page(s) reference an image the staged site does not have "
            "(page: target):\n  " + "\n  ".join(missing_images)
        )

    # lode-fhql.10 (concurrent sibling) owns the real landing page. Until it
    # lands, the generator has no index.md/README.md in the published set and would
    # otherwise ship a homepage-less site. Synthesize a minimal placeholder
    # ONLY if the published set doesn't already provide one -- once .10 adds
    # its own index.md (or README.md) under docs/ to the published set, this
    # branch stops firing and the real page wins.
    if "index.md" not in published and "README.md" not in published:
        (out_dir / "index.md").write_text(
            "# lode\n\n"
            "This is a placeholder landing page. See "
            "[design.md](design.md) for the project overview; the real "
            "landing page is tracked as lode-fhql.10.\n",
            encoding="utf-8",
        )
        print("staged placeholder index.md (lode-fhql.10 not yet landed)")


app = typer.Typer(add_completion=False)


@app.command(
    help=(
        "Stage the published docs subset into OUTPUT_DIR for `zensical build`, "
        "pre-rendering every Mermaid diagram to SVG.\n\n"
        "Run this before `zensical build` -- mkdocs.yml's docs_dir points at the "
        "staged output, never at docs/ directly, so the site can only ever ship "
        "the published set. OUTPUT_DIR is wiped and recreated on every run, and "
        "must live inside the repository."
    )
)
def main(
    output_dir: Annotated[
        Path,
        typer.Argument(help="Staging directory to (re)create. Wiped on every run."),
    ],
    no_mermaid: Annotated[
        bool,
        typer.Option(
            "--no-mermaid",
            help=(
                "Skip the Docker Mermaid render; copy fences through as-is. "
                "Used by `nox -s docs` so the default gate stays offline."
            ),
        ),
    ] = False,
) -> None:
    """Entry point. See the module docstring for the architecture this implements."""
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = output_dir.resolve()
    build(repo_root, out_dir, render_mermaid=not no_mermaid)
    print(f"docs site staged at {out_dir}")


if __name__ == "__main__":
    app()
