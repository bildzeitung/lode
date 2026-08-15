"""Gate: no PUBLISHED doc may contain two headings that slug to the same
GitHub anchor (lode-rmsf).

mkdocs' ``toc`` extension dedups a repeated heading slug as ``<slug>_1``,
GitHub (and this repo's own link gate) as ``<slug>-1``; ``toc`` slugifies
BEFORE dedup, so the custom slugify installed in ``mkdocs.yml``
(``src/lode/docs_slug.py``, lode-fhql.21) cannot influence the suffix at all.
The only remaining lever is to keep duplicate heading slugs out of the
published set -- which is what this gate does. Why gate rather than patch
``toc``, and why the scope is the published set only, is stated once in
docs/stack.md ("Heading-anchor slugs") -- not restated here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import load_module_from_path
from test_docs_site_index import PUBLISHED_DIRS, PUBLISHED_TOP_LEVEL

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package -- load by file path via the shared
# helper, same as tests/test_check_links.py. Reuses check_links.py's own
# heading scan and GitHub-slug algorithm unmodified; this test adds a new,
# independent check on top of them rather than changing either.
check_links = load_module_from_path(
    "check_links_dup_heading_gate", REPO_ROOT / "scripts" / "check_links.py"
)


def _published_doc_paths() -> list[Path]:
    """Every published markdown file, derived from
    ``test_docs_site_index.py``'s ``PUBLISHED_TOP_LEVEL``/``PUBLISHED_DIRS``
    (the authoritative PUBLISHED set, docs/stack.md, lode-fhql.8) rather than
    hand-listed here, so this gate cannot drift from what the site actually
    publishes as that set grows.

    Checked against docs/ (the SOURCE tree), not the staged
    ``scripts/build_docs_site.py`` output: the staged tree is git-ignored and
    only exists after a build runs, not a fixture this test can assume. Every
    PUBLISHED page stages under the identical relative path it has in docs/,
    so checking the source is equivalent (same rationale as
    ``test_docs_site_index.test_every_nav_target_exists``, lode-fhql.9/.10
    mkdocs.yml merge, 2026-08-14).
    """
    docs_dir = REPO_ROOT / "docs"
    paths: list[Path] = []
    for stem in sorted(PUBLISHED_TOP_LEVEL):
        hit = docs_dir / f"{stem}.md"
        if hit.is_file():
            paths.append(hit)
    for directory in sorted(PUBLISHED_DIRS):
        paths.extend(sorted((docs_dir / directory).rglob("*.md")))
    assert paths, "no published .md files resolved -- PUBLISHED set parsing broke"
    return paths


def _duplicate_slugs(text: str) -> list[str]:
    """Every heading slug that occurs more than once in ``text``, in the order
    its first duplicate appears. Split out from the corpus scan below so the
    detector itself is directly testable -- the corpus is clean today and is
    expected to stay clean, so without a synthetic test nothing would ever
    exercise the positive branch and a broken detector would pass silently."""
    seen: set[str] = set()
    dupes: list[str] = []
    for heading in check_links._headings(text):
        base = check_links.github_slug(heading)
        if base in seen and base not in dupes:
            dupes.append(base)
        seen.add(base)
    return dupes


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Alpha\n\n# Beta\n", []),
        ("# Alpha\n\n# Alpha\n", ["alpha"]),
        # Different levels, same text -> same slug on both surfaces.
        ("# Alpha\n\n## Alpha\n", ["alpha"]),
        # Different text, same slug once punctuation is stripped.
        ("# Alpha!\n\n# Alpha?\n", ["alpha"]),
        # Three occurrences report the slug once, not twice.
        ("# Alpha\n\n# Alpha\n\n# Alpha\n", ["alpha"]),
        # A `#` inside a fenced code block is not a heading.
        ("# Alpha\n\n```sh\n# Alpha\n```\n", []),
    ],
)
def test_duplicate_slug_detector(markdown: str, expected: list[str]) -> None:
    assert _duplicate_slugs(markdown) == expected


def test_no_published_doc_has_a_duplicate_heading_slug() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _published_doc_paths():
        dupes = _duplicate_slugs(path.read_text(encoding="utf-8"))
        if dupes:
            offenders[str(path.relative_to(REPO_ROOT))] = dupes

    assert not offenders, (
        "Published doc(s) with a repeated heading slug -- mkdocs' toc "
        "extension will dedup these as '<slug>_1' while GitHub (and this "
        "repo's own link gate) dedups as '<slug>-1', a mismatch no slugify "
        "override can fix (docs/stack.md, 'Heading-anchor slugs', lode-rmsf). "
        f"Rename the repeated heading(s) to be unique: {offenders}"
    )
