"""Gates for the docs-site landing page (lode-fhql.10).

Checks the things that would otherwise drift silently: that the landing page
carries the required content (per its acceptance criteria), that it is
reused verbatim from README.md rather than forked into separate marketing
copy, and that mkdocs.yml's hand-restricted ``nav`` only lists pages from
the PUBLISHED set decided in lode-fhql.8 (docs/stack.md) -- never one of the
EXCLUDED maintainer docs.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "docs" / "index.md"
README = ROOT / "README.md"
MKDOCS_YML = ROOT / "mkdocs.yml"

# The PUBLISHED set from docs/stack.md (lode-fhql.8) -- kept as a literal
# list here rather than parsed out of the doc, since this is a small,
# deliberately-enumerated set that changes rarely and by explicit decision.
EXCLUDED_STEMS = {
    "decisions",
    "agents-workflow",
    "stack",
    "conventions",
    "release",
    "test-suite-audit",
    "onboarding",
    "keybindings",
    "tui",
    "editing",
    "configuration",
}


def _index_text() -> str:
    return INDEX.read_text()


def test_index_exists_and_has_front_matter_title() -> None:
    text = _index_text()
    assert text.startswith("---\n"), "docs/index.md should open with YAML front matter"
    assert "title: lode" in text.splitlines()[1]


def test_index_carries_the_required_content() -> None:
    text = _index_text()
    # The lockup (lode-fhql.4).
    assert "assets/lockup.svg" in text
    # The install command that actually works (lode-fhql.2 / README parity).
    assert "pip install lode-*.whl" in text
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


def test_mkdocs_yml_exists_with_og_meta_override() -> None:
    assert MKDOCS_YML.exists()
    config = yaml.safe_load(MKDOCS_YML.read_text())
    assert config["docs_dir"] == "docs"
    assert config["theme"]["custom_dir"] == "docs/overrides"

    overrides = ROOT / "docs" / "overrides" / "main.html"
    assert overrides.exists()
    html = overrides.read_text()
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


def test_nav_only_lists_published_pages() -> None:
    config = yaml.safe_load(MKDOCS_YML.read_text())
    pages = _nav_leaf_values(config["nav"])
    assert pages, "mkdocs.yml's nav is empty"
    for page in pages:
        stem = Path(page).stem
        # how-to/* pages aren't in EXCLUDED_STEMS at all -- they're PUBLISHED
        # as a directory. Only guard against an EXCLUDED maintainer doc
        # slipping into nav.
        assert stem not in EXCLUDED_STEMS, (
            f"mkdocs.yml nav references {page!r}, which is on the EXCLUDED list "
            "in docs/stack.md (lode-fhql.8) -- it must not be published."
        )
