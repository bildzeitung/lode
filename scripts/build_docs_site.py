#!/usr/bin/env python3
"""Stage the published docs subset for the MkDocs-Material site build (lode-fhql.9).

This is the "wiring" .9 owns, built against the architecture .8 decided in
docs/stack.md: MkDocs-Material never renders Mermaid at build time on its own
(neither does Sphinx) -- both hand a live ``mermaid.js`` require to the
visitor's browser, so a broken diagram would ship as a silently-empty box.
This script is the *new* renderer docs/stack.md calls for: it walks every
```mermaid fenced block in the PUBLISHED subset of docs/, renders each to SVG
through the SAME pinned mermaid-cli Docker image scripts/validate-mermaid.sh
already uses, and embeds the SVG in place of the fence -- the built site never
ships a live client-side Mermaid require. Any render failure aborts the whole
build (a machine/content distinction, unlike validate-mermaid.sh's gate
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
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The pinned mermaid-cli image. Deliberately NOT `:latest` (scripts/validate-
# mermaid.sh's tag) -- lode-fhql.9's own acceptance requires the toolchain
# pinned, not floating; this is a build-output artifact (embedded in every
# page the site ships) where an unannounced upstream change is a worse
# surprise than in a pass/fail validation gate. Bump deliberately, matching
# validate-mermaid.sh's IMAGE if the two are ever reconciled.
MERMAID_IMAGE = "minlag/mermaid-cli:10.9.1"

# docs/stack.md "Published / excluded page sets" (lode-fhql.8, current as of
# 2026-08-13). PUBLISHED is authoritative; docs/how-to/ is published as a
# DIRECTORY (every file in it, not a frozen list) -- see that section.
PUBLISHED_TOP_LEVEL = ["design.md", "retrieval.md", "storage.md", "externals.md", "brand.md"]
PUBLISHED_DIRS = ["how-to"]

GITHUB_BASE = "https://github.com/bildzeitung/lode/blob/trunk"

_MERMAID_FENCE_RE = re.compile(r"```mermaid\n(.*?)\n```", re.DOTALL)
_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")


def _published_set(docs_dir: Path) -> set[str]:
    """Relative (POSIX, to docs/) paths of every published file."""
    published = set(PUBLISHED_TOP_LEVEL)
    for d in PUBLISHED_DIRS:
        for f in sorted((docs_dir / d).glob("*.md")):
            published.add(f"{d}/{f.name}")
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


def _process_mermaid(text: str, rel_path: str, assets_dir: Path, out_dir: Path) -> str:
    """Replace every ```mermaid fence in `text` with an embedded SVG image."""
    page_slug = rel_path.replace("/", "-").removesuffix(".md")
    counter = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        code = match.group(1)
        svg_name = f"{page_slug}-{counter}.svg"
        svg_path = assets_dir / svg_name
        print(f"  rendering mermaid diagram {counter} of {rel_path} -> assets/mermaid/{svg_name}")
        _render_mermaid_svg(code, svg_path)
        # Path relative to the page's own directory once staged.
        page_dir = (out_dir / rel_path).parent
        rel_svg = Path(os.path.relpath(svg_path, start=page_dir)).as_posix()
        return f"![Diagram]({rel_svg})"

    return _MERMAID_FENCE_RE.sub(_sub, text)


def _rewrite_target(current_rel: str, link_target: str, published: set[str]) -> str | None:
    """Return a rewritten link target, or None if the link is left alone."""
    if link_target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    path_part, _, fragment = link_target.partition("#")
    if not path_part:
        return None  # pure same-page fragment, already excluded above
    current_dir = str(Path(current_rel).parent)
    combined = Path(("" if current_dir == "." else current_dir + "/") + path_part)
    # Resolve relative to docs/, allowing `..` to escape it.
    root_rel = Path("docs") / combined
    parts: list[str] = []
    for part in root_rel.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    root_rel_str = "/".join(parts)

    if root_rel_str.startswith("docs/"):
        docs_rel = root_rel_str[len("docs/") :]
        if docs_rel in published:
            return None  # stays a plain relative link between published pages
        url = f"{GITHUB_BASE}/{root_rel_str}"
    else:
        url = f"{GITHUB_BASE}/{root_rel_str}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def _process_links(text: str, rel_path: str, published: set[str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        rewritten = _rewrite_target(rel_path, target, published)
        if rewritten is None:
            return match.group(0)
        return f"[{label}]({rewritten})"

    return _LINK_RE.sub(_sub, text)


def build(repo_root: Path, out_dir: Path) -> None:
    docs_dir = repo_root / "docs"
    published = _published_set(docs_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    assets_dir = out_dir / "assets" / "mermaid"

    for rel in sorted(published):
        src = docs_dir / rel
        if not src.is_file():
            raise SystemExit(f"published doc {rel!r} listed in build_docs_site.py but missing on disk: {src}")
        text = src.read_text(encoding="utf-8")
        text = _process_mermaid(text, rel, assets_dir, out_dir)
        text = _process_links(text, rel, published)
        dest = out_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        print(f"staged {rel}")

    # lode-fhql.10 (concurrent sibling) owns the real landing page. Until it
    # lands, MkDocs has no index.md/README.md in the published set and would
    # otherwise ship a homepage-less site. Synthesize a minimal placeholder
    # ONLY if the published set doesn't already provide one -- once .10 adds
    # its own docs/index.md (or README.md) to the published set, this branch
    # stops firing and the real page wins.
    if "index.md" not in published and "README.md" not in published:
        (out_dir / "index.md").write_text(
            "# lode\n\n"
            "This is a placeholder landing page. See "
            "[design.md](design.md) for the project overview; the real "
            "landing page is tracked as lode-fhql.10.\n",
            encoding="utf-8",
        )
        print("staged placeholder index.md (lode-fhql.10 not yet landed)")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <output-dir>", file=sys.stderr)
        return 2
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = Path(argv[1]).resolve()
    build(repo_root, out_dir)
    print(f"docs site staged at {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
