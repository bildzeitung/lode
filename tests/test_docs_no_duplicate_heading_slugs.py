"""Gate: no PUBLISHED doc may contain two headings that slug to the same
GitHub anchor (lode-rmsf).

Why this can't be fixed by patching ``mkdocs.yml``'s slugify: GitHub dedups a
repeated heading text with a ``-1``/``-2``/... suffix (``scripts/
check_links.py``'s ``github_slug`` plus its own per-file dedup counter
already implement that side). ``mkdocs``'s ``toc`` extension dedups with its
own ``unique()`` helper, which appends ``_1``/``_2``/... instead -- and,
crucially, ``toc`` calls ``slugify`` *before* dedup, so a custom slugify
(``src/lode/docs_slug.py``, lode-fhql.21) cannot influence the suffix at all.
A published doc that ever gains a repeated heading would render ``#foo_1`` on
the built site while every other citation of that anchor (GitHub itself,
this repo's own link gate) resolves ``#foo-1`` -- a broken-anchor bug that
would only show up by inspection, not by an existing gate.

The docs set has zero duplicate heading slugs today (verified during the
lode-fhql.21 review). Rather than patch ``toc``'s internals to match GitHub's
suffix -- which would mean vendoring/monkeypatching a python-markdown
extension for a case that has never actually occurred -- this test fails
loudly the moment one does occur, so the mismatch is caught here instead of
being rediscovered as a second broken-anchor bug on the live site. Decision
recorded in ``docs/stack.md`` (mkdocs.yml scaffold section).
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent

# scripts/ isn't an installed package -- load by file path via the shared
# helper, same as tests/test_check_links.py. Reuses check_links.py's own
# heading scan and GitHub-slug algorithm unmodified; this test adds a new,
# independent check on top of them rather than changing either.
check_links = load_module_from_path(
    "check_links_dup_heading_gate", REPO_ROOT / "scripts" / "check_links.py"
)


def _published_doc_globs() -> list[str]:
    """The re-included globs from ``mkdocs.yml``'s ``exclude_docs`` allowlist
    (the ``!pattern`` lines) -- read out of the YAML source text directly
    rather than hand-copied here, so this test cannot silently drift from
    what the site actually publishes as the allowlist grows.

    ``exclude_docs`` is a literal YAML block scalar (``exclude_docs: |``), so
    a plain line scan is enough -- no YAML parser needed, and none of the
    surrounding file's custom ``!!python/name:`` tag (which needs
    ``lode.docs_slug`` importable) has to be touched to read it.
    """
    text = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    m = re.search(r"^exclude_docs: \|\n((?:^ {2}.*\n)+)", text, re.MULTILINE)
    assert m, "mkdocs.yml's exclude_docs block scalar has moved or changed shape"
    return [
        line.strip()[1:]
        for line in m.group(1).splitlines()
        if line.strip().startswith("!")
    ]


def _published_doc_paths() -> list[Path]:
    docs_dir = REPO_ROOT / "docs"
    paths: list[Path] = []
    for glob in _published_doc_globs():
        if not glob.endswith(".md"):
            continue  # e.g. `!assets/*` -- not a markdown source
        paths.extend(sorted(docs_dir.glob(glob)))
    assert paths, "no published .md files resolved -- exclude_docs parsing broke"
    return paths


def test_no_published_doc_has_a_duplicate_heading_slug() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _published_doc_paths():
        text = path.read_text(encoding="utf-8")
        seen: dict[str, int] = {}
        dupes: list[str] = []
        for heading in check_links._headings(text):
            base = check_links.github_slug(heading)
            seen[base] = seen.get(base, 0) + 1
            if seen[base] == 2:  # second occurrence is the first duplicate
                dupes.append(base)
        if dupes:
            offenders[str(path.relative_to(REPO_ROOT))] = dupes

    assert not offenders, (
        "Published doc(s) with a repeated heading slug -- mkdocs' toc "
        "extension will dedup these as '<slug>_1' while GitHub (and this "
        "repo's own link gate) dedups as '<slug>-1', a mismatch no slugify "
        "override can fix (see this test's module docstring, lode-rmsf). "
        f"Rename the repeated heading(s) to be unique: {offenders}"
    )
