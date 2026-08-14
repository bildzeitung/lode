"""Tests for src/lode/docs_slug.py, the mkdocs-side GitHub slugifier (lode-fhql.21).

Behaviour of this module in isolation only. The load-bearing test -- that this
module and ``scripts/check_links.py``'s ``github_slug`` never diverge, since one
is a deliberate copy of the other -- lives in ``tests/test_check_links.py``,
which already loads that script; loading it a second time here would collide in
``sys.modules`` (``conftest.load_module_from_path`` refuses it).
"""

from __future__ import annotations

from lode.docs_slug import github_slug, github_slugify


def test_em_dash_between_spaces_yields_a_double_hyphen() -> None:
    """The regression case the whole module exists for: the ``toc``
    extension's default slugify collapses this to a single hyphen, GitHub does
    not, and every intra-doc anchor in docs/ is written GitHub's way."""
    heading = "The landing loop — build, review, land (planned)"
    assert github_slug(heading) == "the-landing-loop--build-review-land-planned"


def test_slugify_adapter_ignores_the_separator() -> None:
    """GitHub always joins with a hyphen, so the ``toc`` contract's
    ``separator`` argument is accepted and deliberately ignored -- honouring it
    would reintroduce the very mismatch this module closes."""
    assert github_slugify("Paths & locations", "_") == "paths--locations"
