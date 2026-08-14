"""Gates for the docs-site landing page (lode-fhql.10).

Checks the things that would otherwise drift silently: that the landing page
carries the required content (per its acceptance criteria), that it is
reused verbatim from README.md rather than forked into separate marketing
copy, and that mkdocs.yml's ``nav`` only lists pages from the PUBLISHED set
decided in lode-fhql.8.

What is actually BUILT is decided by scripts/build_docs_site.py's staging
step (lode-fhql.9) -- mkdocs.yml's ``docs_dir`` points at that staged output,
never at docs/ directly, so nothing build_docs_site.py doesn't stage can ship,
``nav`` or no ``nav``. This supersedes the ``exclude_docs``-allowlist
mechanism lode-fhql.10 originally scaffolded (2026-08-14 mkdocs.yml merge
decision, see lode-fhql.9's notes) -- ``exclude_docs`` no longer appears in
mkdocs.yml, so there is no second mechanism to gate here.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "docs" / "index.md"
README = REPO_ROOT / "README.md"
STACK = REPO_ROOT / "docs" / "stack.md"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"

# The PUBLISHED set from docs/stack.md (lode-fhql.8), as an ALLOWLIST.
#
# Held as a literal rather than derived from mkdocs.yml on purpose: deriving
# it would make the two mkdocs.yml checks below tautological. It is instead
# tied back to stack.md -- the authoritative statement -- by
# ``test_published_set_matches_stack_md``.
PUBLISHED_TOP_LEVEL = {
    "index",  # the landing page itself (lode-fhql.10; postdates the 2026-08-12 call)
    "design",
    "storage",
    "retrieval",
    "externals",
    "brand",
}
# docs/how-to/ is published as a DIRECTORY, not a frozen file list -- a guide
# added there later is published by default (docs/stack.md).
PUBLISHED_DIRS = {"how-to"}


def _index_text() -> str:
    return INDEX.read_text()


def _mkdocs_config() -> dict:
    return yaml.safe_load(MKDOCS_YML.read_text())


def test_index_exists_and_has_front_matter_title() -> None:
    text = _index_text()
    assert text.startswith("---\n"), "docs/index.md should open with YAML front matter"
    assert "title: lode" in text.splitlines()[1]


def test_index_carries_the_required_content() -> None:
    text = _index_text()
    # The lockup (lode-fhql.4).
    assert "assets/lockup.svg" in text
    # The install command that actually works (lode-fhql.2 / README parity).
    assert "pip install lode_kb-*.whl" in text
    assert "./scripts/python-init.sh" in text
    # The two-line demo.
    assert "$ lode add" in text
    assert '$ lode ask "what did we decide about auth?"' in text
    # Entry points into the docs.
    for target in ("design.md", "retrieval.md", "storage.md", "externals.md"):
        assert f"]({target})" in text, f"missing entry-point link to {target}"


def test_idea_in_one_breath_is_reused_verbatim_from_readme() -> None:
    """The ticket text is explicit: reuse the README argument, don't rewrite it."""
    readme = README.read_text()
    m = re.search(
        r"## The idea in one breath\n\n(.+?)\n\n##",
        readme,
        re.DOTALL,
    )
    assert m, "README.md's 'The idea in one breath' section moved or was renamed"
    readme_paragraph = m.group(1).strip()
    assert readme_paragraph in _index_text(), (
        "docs/index.md's 'idea in one breath' copy has drifted from README.md's -- "
        "it must be reused verbatim, not rewritten (lode-fhql.10)."
    )


def test_index_documents_its_relationship_to_readme() -> None:
    text = _index_text()
    assert "README.md" in text
    assert "canonical" in text.lower()


def test_og_meta_override_is_wired_and_populated() -> None:
    """The OG tags only reach the site if ``custom_dir`` points at them."""
    custom_dir = REPO_ROOT / _mkdocs_config()["theme"]["custom_dir"]
    html = (custom_dir / "main.html").read_text()
    assert 'property="og:image"' in html
    assert "assets/og-card.png" in html


def _nav_leaf_values(node) -> list[str]:
    """Flatten mkdocs.yml's nav tree (list of dicts / nested lists) to page paths."""
    values: list[str] = []
    if isinstance(node, list):
        for item in node:
            values.extend(_nav_leaf_values(item))
    elif isinstance(node, dict):
        for v in node.values():
            values.extend(_nav_leaf_values(v))
    elif isinstance(node, str):
        values.append(node)
    return values


def _is_published(page: str) -> bool:
    path = Path(page)
    if len(path.parts) == 1:
        return path.stem in PUBLISHED_TOP_LEVEL
    return path.parts[0] in PUBLISHED_DIRS


def test_nav_only_lists_published_pages() -> None:
    config = _mkdocs_config()
    pages = _nav_leaf_values(config["nav"])
    assert pages, "mkdocs.yml's nav is empty"
    for page in pages:
        assert _is_published(page), (
            f"mkdocs.yml nav references {page!r}, which is not in the PUBLISHED set "
            "decided in docs/stack.md (lode-fhql.8) -- everything not on that closed "
            "list is unpublished."
        )


def test_every_nav_target_exists() -> None:
    """A nav entry pointing at a missing file fails ``mkdocs build --strict``.

    Checked against docs/ (the SOURCE tree), not ``config["docs_dir"]``: that
    now points at scripts/build_docs_site.py's staged output
    (``.docs-site-src``), which is git-ignored and only exists after a build
    runs -- not a fixture this test can assume. Every PUBLISHED page stages
    under the identical relative path it has in docs/, so checking the
    source is equivalent (lode-fhql.9/.10 mkdocs.yml merge, 2026-08-14).
    """
    config = _mkdocs_config()
    docs_dir = REPO_ROOT / "docs"
    for page in _nav_leaf_values(config["nav"]):
        assert (docs_dir / page).is_file(), (
            f"mkdocs.yml nav references {page!r}, which does not exist under "
            f"docs/ -- this breaks the docs build."
        )


def test_every_how_to_guide_is_in_nav() -> None:
    """how-to/ is published as a directory, so a new guide must reach the nav."""
    config = _mkdocs_config()
    pages = set(_nav_leaf_values(config["nav"]))
    for guide in sorted((REPO_ROOT / "docs" / "how-to").glob("*.md")):
        rel = f"how-to/{guide.name}"
        assert rel in pages, (
            f"docs/{rel} exists but is not listed in mkdocs.yml's nav. docs/stack.md "
            "publishes docs/how-to/ as a directory, not a frozen file list -- add it."
        )


def test_published_set_matches_stack_md() -> None:
    """docs/stack.md is the authoritative statement -- anchor the literal to it.

    mkdocs.yml's two mechanisms and this module's literal already cross-check
    each other, but all three could drift away from the prose decision
    together without this.
    """
    m = re.search(
        r"- \*\*PUBLISHED\*\*: (.+?)- \*\*EXCLUDED\*\*",
        STACK.read_text(),
        re.DOTALL,
    )
    assert m, "docs/stack.md's '**PUBLISHED**:' bullet moved or was renamed"
    bullet = m.group(1)
    for stem in PUBLISHED_TOP_LEVEL - {"index"}:  # index.md postdates the bullet
        assert f"`{stem}.md`" in bullet, (
            f"docs/{stem}.md is in this module's PUBLISHED literal but docs/stack.md's "
            "PUBLISHED bullet no longer names it -- reconcile the two."
        )
    for directory in PUBLISHED_DIRS:
        assert f"`docs/{directory}/`" in bullet, (
            f"docs/{directory}/ is in this module's PUBLISHED literal but docs/stack.md's "
            "PUBLISHED bullet no longer names it -- reconcile the two."
        )


def test_mkdocs_yml_has_no_exclude_docs() -> None:
    """The staged-build docs_dir supersedes the exclude_docs mechanism.

    mkdocs.yml's docs_dir points at scripts/build_docs_site.py's staged
    output (lode-fhql.9), which contains only the PUBLISHED set by
    construction -- there is nothing left for exclude_docs to filter, and a
    reintroduced copy would silently reference docs_dir=docs semantics that
    no longer apply (2026-08-14 mkdocs.yml merge decision).
    """
    config = _mkdocs_config()
    assert "exclude_docs" not in config, (
        "mkdocs.yml carries exclude_docs again -- the staged docs_dir "
        "(scripts/build_docs_site.py) is the sole publish mechanism now; "
        "see lode-fhql.9's notes for why the two were merged this way."
    )
