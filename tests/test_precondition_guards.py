"""The precondition-guard family's shared contract, pinned (lode-t6ni).

``scripts/isolation-guard.sh``, ``scripts/recycled-worktree-guard.sh`` and
``scripts/assert-main-checkout.sh`` share ONE 0/1/2 exit contract, stated once
in ``docs/agents-workflow.md`` and pointed at from each script's header rather
than re-derived in all three (which is the drift lode-t6ni was filed to close).

WHY THIS MODULE EXISTS -- nothing else gates those pointers. ``nox -s
linkcheck`` (``scripts/check_links.py``) is the repo's anchor-rot gate, but it
walks bracketed markdown links only in git-tracked ``*.md`` files (repo-wide
since lode-act5), so a markdown link living in a ``*.sh`` comment is invisible
to it -- its second, bare-citation pass over non-markdown files recognizes only
a literal ``docs/<page>.md#<anchor>`` text reference, never a bracketed link to
anything else. The target heading is stamped with a ticket id -- "Precondition guards
(the 0/1/2 family) (lode-t6ni)" -- and dropping a ticket id from a heading that
describes a general contract is exactly the kind of tidy-up a later reader
reasonably makes. Today that would silently break all three headers' only route
to the contract, with every gate green. These tests are that missing gate, and
they reuse ``check_links.py``'s own slug algorithm rather than re-rolling it, so
"resolves" means here precisely what it means to the real gate.

IT IS ALSO THE FAMILY'S AUTHORITATIVE ROSTER. Per ``tests/test_gate_lib.py``'s
lesson -- "a test that hard-codes the list IS that list", learned when
``land-merge-one.sh`` spent its entire life a stranded inline copy because
nothing enumerated the set -- the roster is written down in exactly one place
that fails loudly rather than restated in each header, where a fourth guard
could silently miss one. ``docs/agents-workflow.md``'s prose roster is for
humans; ``GUARDS`` below is the one a gate reads. Add a guard to both.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
CONTRACT_DOC = REPO_ROOT / "docs" / "agents-workflow.md"

# The heading whose slug every guard header links to. Hard-coded as TEXT, not
# as a slug: the slug is derived below, so a rename of the heading alone turns
# `test_the_contract_anchor_resolves_in_the_target_doc` red, and a rename that
# updates this constant but not the headers turns `..._links_to_...` red.
CONTRACT_HEADING = "Precondition guards (the 0/1/2 family) (lode-t6ni)"

# The roster -- see the module docstring. This list IS the family.
GUARDS = (
    "isolation-guard.sh",
    "recycled-worktree-guard.sh",
    "assert-main-checkout.sh",
)

# Loaded under a name of its own: tests/test_check_links.py already registers
# "check_links", and load_module_from_path deliberately refuses a second
# registration of the same name.
_check_links = load_module_from_path(
    "check_links_for_guard_contract", SCRIPTS / "check_links.py"
)
ANCHOR = _check_links.github_slug(CONTRACT_HEADING)


def _header(script: str) -> str:
    """A guard's comment header: everything above its ``set -euo pipefail``.

    Bounded deliberately rather than reading the whole file, so a link that
    appeared in executable code (a diagnostic string, say) could not satisfy
    the header-pointer assertion below.
    """
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    head, sep, _ = text.partition("\nset -euo pipefail")
    assert sep, f"{script}: no `set -euo pipefail` line to bound the header"
    return head


def test_the_roster_is_not_vacuous() -> None:
    """Every named guard exists and is executable -- a renamed or deleted
    script must not quietly empty the sweeps below (the vacuity trap
    ``tests/test_gate_lib.py::test_the_consumer_sweep_discovers_something``
    exists for, applied to a hard-coded roster instead of a discovered one)."""
    assert GUARDS, "the guard roster is empty"
    for script in GUARDS:
        path = SCRIPTS / script
        assert path.is_file(), f"{script} is in the roster but not on disk"
        assert path.stat().st_mode & 0o111, f"{script} is not executable"


def test_the_contract_anchor_resolves_in_the_target_doc() -> None:
    """The anchor all three headers point at is a real heading in
    ``docs/agents-workflow.md``.

    This is the assertion ``nox -s linkcheck`` structurally cannot make: it
    never opens ``scripts/*.sh``. Slugs come from ``check_links.py`` itself, so
    a heading rename fails here for the same reason and by the same algorithm
    it would fail there.
    """
    # `{}` is a throwaway per-call text cache -- see `_cached_text`; this
    # test reads one file once, so it has nothing to share.
    slugs = _check_links._slugs_for_file(CONTRACT_DOC, {})
    assert ANCHOR in slugs, (
        f"'{CONTRACT_HEADING}' no longer resolves to #{ANCHOR} in "
        f"{CONTRACT_DOC.name} -- the heading was renamed or removed, and the "
        f"{len(GUARDS)} guard headers pointing at it are now dead links that "
        "no other gate checks."
    )


@pytest.mark.parametrize("script", GUARDS)
def test_every_guard_header_links_to_the_shared_contract(script: str) -> None:
    """Each guard defers to the one contract instead of re-deriving it.

    The pointer is what makes the single source real: a header that carries no
    link leaves its reader with whatever that header happens to say, which is
    the three-way drift lode-t6ni closed.
    """
    header = _header(script)
    assert f"agents-workflow.md#{ANCHOR}" in header, (
        f"scripts/{script}'s header does not link to the shared "
        f"precondition-guard contract (#{ANCHOR}). Point at "
        f"docs/agents-workflow.md rather than restating the contract inline."
    )
