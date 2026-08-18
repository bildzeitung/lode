#!/usr/bin/env python3
"""Stage the published docs subset for the MkDocs-Material site build (lode-fhql.9).

This is the "wiring" .9 owns, built against the architecture .8 decided in
docs/stack.md: MkDocs-Material never renders Mermaid at build time on its own
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

Usage:
    scripts/build_docs_site.py <output-dir>

Exits non-zero (with a message on stderr) on any render or I/O failure --
never a silent partial build.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer

# src/ on the path so the fence rule below comes from lode.fence_parsing --
# the ONE importable home of it (lode-ee7b) -- without this CI job having to
# `pip install` the package. Same approach as scripts/check_docstring_refs.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lode.fence_parsing import fence_flags

# The pinned mermaid-cli image. Deliberately NOT `:latest` (scripts/validate-
# mermaid.sh's tag) -- lode-fhql.9's own acceptance requires the toolchain
# pinned, not floating; this is a build-output artifact (embedded in every
# page the site ships) where an unannounced upstream change is a worse
# surprise than in a pass/fail validation gate.
#
# docs/stack.md mandates ONE image shared with validate-mermaid.sh, "not a
# second, independently-versioned copy" -- which the tag split above is, until
# validate-mermaid.sh + update-images.sh move onto this same pin. Converging
# them touches a repo-wide merge gate and is deliberately NOT folded into this
# ticket; it is filed as lode-3ld8. Bump the two together once that lands.
# tests/test_build_docs_site.py pins .github/workflows/docs.yml to this value.
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
    # lode-fhql.15's derived reference pages, wired in by lode-7uze. Each
    # links back to its maintainer source (keybindings.md / configuration.md)
    # by GitHub URL for whoever needs the full doc -- see DERIVED_PAGE_ALIASES
    # below for the reverse: citations of the maintainer doc elsewhere in the
    # published set resolve to these derived pages instead of falling through
    # to a GitHub blob URL (docs/stack.md, lode-fhql.8/.9 "derived pages take
    # precedence once they exist").
    "keymap.md",
    "settings.md",
]
PUBLISHED_DIRS = ["how-to"]

# docs/stack.md ("`lode-fhql.15`'s derived pages take precedence once they
# exist"): a link elsewhere in the published set that cites one of these
# maintainer docs resolves to its derived, published counterpart instead of
# falling through to the one GITHUB_BASE rewrite rule. Keyed and valued by
# root-relative (repo-root, POSIX) path, matching _rewrite_target's
# root_rel_str.
DERIVED_PAGE_ALIASES = {
    "docs/keybindings.md": "docs/keymap.md",
    "docs/configuration.md": "docs/settings.md",
}

# Static assets the theme (mkdocs.yml: theme.logo/favicon, docs/overrides/
# main.html's OG tags) references by a docs_dir-relative path -- these are
# not markdown and carry no mermaid/link processing, just a verbatim copy
# into the staged tree so mkdocs can find them (lode-fhql.9/.10 mkdocs.yml
# merge, 2026-08-14).
ASSETS_DIR = "assets"

GITHUB_BASE = "https://github.com/bildzeitung/lode/blob/trunk"

_MERMAID_FENCE_RE = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
# An inline code span. Same one-line rule as scripts/check_links.py's
# _INLINE_CODE_RE, and for the same reason: a literal `[text](foo.md)` written
# as an EXAMPLE inside backticks (or inside a fenced block) is documentation,
# not a link, and rewriting it would corrupt the published page.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


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
    mode exists so `nox -s docs` can stage + `mkdocs build --strict` without
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
    if link_target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    path_part, _, fragment = link_target.partition("#")
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

    alias = DERIVED_PAGE_ALIASES.get(root_rel_str)
    if alias is not None and alias[len("docs/") :] in published:
        # A citation of the maintainer doc resolves to its derived, published
        # counterpart instead of falling through to GitHub -- compute a fresh
        # relative link from the current page's directory, since the alias
        # target's filename differs from what the source markdown wrote.
        current_dir = posixpath.dirname(current_rel)
        rel_to_alias = posixpath.relpath(alias[len("docs/") :], start=current_dir or ".")
        return f"{rel_to_alias}#{fragment}" if fragment else rel_to_alias

    # Everything else -- an unpublished docs/ page, a repo-root file, a source
    # file -- gets the one rewrite rule: its GitHub blob URL, fragment verbatim.
    url = f"{GITHUB_BASE}/{root_rel_str}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def _process_links(text: str, rel_path: str, published: set[str]) -> str:
    """Apply the rewrite rule to every real link, skipping code.

    Fenced blocks and inline code spans are left untouched -- a published page
    that documents markdown syntax must ship that example verbatim, not a
    rewritten GitHub URL.
    """

    def _sub(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        rewritten = _rewrite_target(rel_path, target, published)
        if rewritten is None:
            return match.group(0)
        return f"[{label}]({rewritten})"

    def _sub_unless_protected(match: re.Match[str]) -> str:
        # Keyed on where the link STARTS, so a link whose label is itself
        # backticked -- [`configuration.md`](configuration.md), the dominant
        # form in these docs -- is still rewritten; only a link that starts
        # inside code is left alone.
        if any(lo <= match.start() < hi for lo, hi in protected):
            return match.group(0)
        return _sub(match)

    protected: list[tuple[int, int]] = []
    offset = 0
    lines = text.split("\n")
    for line, fenced in zip(lines, fence_flags(lines), strict=True):
        if fenced:
            protected.append((offset, offset + len(line)))
        else:
            protected += [
                (offset + m.start(), offset + m.end())
                for m in _INLINE_CODE_RE.finditer(line)
            ]
        offset += len(line) + 1  # +1 for the "\n" that split() removed

    # Substituted over the WHOLE text, never line by line: a link's label
    # routinely wraps across lines in these docs, and _LINK_RE has to see it
    # whole or it silently stops matching.
    return _LINK_RE.sub(_sub_unless_protected, text)


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
        print(f"staged {rel}")

    # lode-fhql.10 (concurrent sibling) owns the real landing page. Until it
    # lands, MkDocs has no index.md/README.md in the published set and would
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
        "Stage the published docs subset into OUTPUT_DIR for `mkdocs build`, "
        "pre-rendering every Mermaid diagram to SVG.\n\n"
        "Run this before `mkdocs build` -- mkdocs.yml's docs_dir points at the "
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
