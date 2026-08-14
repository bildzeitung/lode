"""GitHub-compatible heading-to-anchor slug (`lode-fhql.21`).

Reverse-engineered from a real, currently-dead anchor in this repo
(``docs/decisions.md`` -> ``agents-workflow.md#the-landing-loop--build-review-land-planned``,
for the heading that used to read "The landing loop -- build, review, land
(planned)"): strip markdown formatting down to plain text, lowercase it,
delete every character that isn't a word character, hyphen, or space
(deleted, not replaced by a space -- an em dash sitting between two spaces
collapses to a run of TWO adjacent spaces once it's deleted, which is
exactly why that anchor's slug has a double hyphen at "loop--build"), then
convert each remaining space to a hyphen one-for-one -- consecutive hyphens
from a multi-space run are never collapsed to one.

This is a second, independent implementation of the exact same algorithm
``scripts/check_links.py``'s ``github_slug`` uses (``tests/test_check_links.py``
locks both against the identical real-repo regression case in each file's own
docstring) -- not a shared import, deliberately: ``mkdocs.yml``'s
``toc.slugify`` (via ``github_slugify`` below) must generate the same anchor
``id``\\ s locally that GitHub generates when these docs render there -- the
local mkdocs build's default slugify does not, which is the bug this module
exists to close -- and it needs to be importable by dotted module path
(``lode.docs_slug``) from mkdocs's own YAML config loader
(``!!python/name:lode.docs_slug.github_slugify``), which does not run with
the repo root on ``sys.path`` the way pytest does. It lives under
``src/lode/`` rather than ``scripts/`` for exactly that reason: the
installed package is importable from anywhere, ``scripts/`` is not.
"""

from __future__ import annotations

import re

_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def github_slug(heading_text: str) -> str:
    """Reproduce GitHub's heading-to-anchor slug algorithm."""
    text = _LINK_TEXT_RE.sub(r"\1", heading_text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def github_slugify(value: str, separator: str) -> str:
    """Adapter matching ``markdown``'s ``toc`` extension's
    ``slugify(value, separator) -> str`` contract, for ``mkdocs.yml``."""
    slug = github_slug(value)
    return slug.replace("-", separator) if separator != "-" else slug
