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

WHY THIS MODULE IS HERE, AND WHY IT IS A COPY. ``mkdocs.yml``'s
``toc.slugify`` (via ``github_slugify`` below) must generate the same anchor
``id``\\ s locally that GitHub generates when these docs render there -- the
``toc`` extension's default slugify does not (it collapses that em-dash run to
a SINGLE hyphen), which is the bug this module exists to close. mkdocs reaches
a slugifier only by DOTTED MODULE PATH, through its YAML loader
(``!!python/name:lode.docs_slug.github_slugify``), and it does not put the repo
root on ``sys.path`` -- so ``scripts/`` is unreachable from there and the
installed package is not. Hence ``src/lode/``.

The obvious cleanup -- delete this copy and have ``scripts/check_links.py``
import it, now that an importable home exists -- was considered and REJECTED,
with a measurement: ``check_links.py`` must stay runnable under an ARBITRARY
interpreter that has never installed this project, and
``tests/test_check_links.py`` exercises exactly that by invoking it as a bare
``["python", "scripts/check_links.py", ...]`` subprocess -- which on a normal
dev box resolves to a pyenv shim where ``import lode`` raises
``ModuleNotFoundError``. Importing from ``lode`` would make the repo's own link
gate depend on the package being installed. The cost of the copy is owned, not
hidden: a docs-tooling module ships inside the runtime wheel, and one algorithm
lives in two files (three, counting ``scripts/generate_derived_docs.py``'s
``_github_slug``, which the same reasoning already produced).

The copies MUST agree, so ``tests/test_check_links.py`` asserts this module and
the gate's ``github_slug`` return the identical slug for every heading in every
``docs/*.md`` file -- the same copy-plus-equivalence-test shape
``tests/test_generate_derived_docs.py`` already uses for the third copy. A
drift turns ``nox -s tests`` red rather than silently republishing the site
with anchors the link gate believes are fine.
"""

from __future__ import annotations

import re

from lode.fence_parsing import fence_flags

# Character-for-character `scripts/check_links.py`'s own `_LINK_TEXT_RE`,
# `[^\]]*` included: the two differ only on a heading whose link text is EMPTY
# (`[](url)`), the one input the docs/-corpus equivalence test cannot cover
# because no heading in docs/ has one. `tests/test_check_links.py`'s
# `test_agrees_on_an_empty_link_text` covers it explicitly instead.
_LINK_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def github_slug(heading_text: str) -> str:
    """Reproduce GitHub's heading-to-anchor slug algorithm."""
    text = _LINK_TEXT_RE.sub(r"\1", heading_text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


# Character-for-character `scripts/check_links.py`'s `_ATX_HEADING_RE` and
# `_HTML_ANCHOR_RE`. Same copy-plus-equivalence-test discipline as
# `_LINK_TEXT_RE` above: that gate cannot import this module (see the module
# docstring), so `tests/test_check_links.py` asserts the two implementations
# return the identical anchor set for every `docs/*.md` file instead.
_ATX_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_HTML_ANCHOR_RE = re.compile(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', re.IGNORECASE)


def anchor_slugs(text: str) -> set[str]:
    """Every anchor a markdown document offers, as GitHub addresses them.

    Each heading's slug -- including GitHub's disambiguating ``-1``, ``-2``
    suffixes when a heading repeats -- plus the literal id of every explicit
    ``<a id="...">`` / ``<a name="...">`` anchor. Headings inside fenced code
    blocks are not headings and are skipped; an explicit anchor tag inside one
    is skipped for the same reason.

    ``scripts/check_links.py::_slugs_for_file`` is the authority for this
    algorithm -- this is the importable copy of it, for callers that can
    ``import lode``.
    """
    lines = text.split("\n")
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    for line, fenced in zip(lines, fence_flags(lines), strict=True):
        if fenced:
            continue
        heading = _ATX_HEADING_RE.match(line)
        if heading:
            base = github_slug(heading.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            slugs.add(base if count == 0 else f"{base}-{count}")
        slugs.update(_HTML_ANCHOR_RE.findall(line))
    return slugs


def github_slugify(value: str, separator: str) -> str:
    """Adapter matching ``markdown``'s ``toc`` extension's
    ``slugify(value, separator) -> str`` contract, for ``mkdocs.yml``.

    ``separator`` is accepted and ignored: GitHub's algorithm always joins
    with a hyphen, and matching GitHub is the entire point of installing this
    slugifier -- honouring some other separator would reintroduce exactly the
    mismatch this module exists to close.
    """
    return github_slug(value)
