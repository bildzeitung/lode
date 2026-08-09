"""Gate: every read site that loads `land_head`/`review_head` shape-checks it
before comparing it to a branch tip (lode-xdg3).

`tests/test_validate_sha40.py` pins what `scripts/validate-sha40.sh` DOES.
Nothing there pins that anyone CALLS it -- and the two callers are fenced bash
inside markdown agent instructions, which no other gate parses. Without this
file, deleting the two-line check from `.claude/skills/land/SKILL.md` or
`.claude/agents/code-reviewer.md` leaves the whole suite green while the
pipeline silently returns to the exact lode-r9z0 behaviour the ticket exists to
prevent: a truncated `land_head` misread as drift, which in Section 2a bounces
an already-correct branch (superseding its ticket and deleting the branch)
instead of escalating it for a human to re-derive the field (lode-xdg3).

This is the second half of the split `scripts/assert-main-checkout.sh` already
uses -- script-behaviour tests plus a markdown coverage gate
(`tests/test_land_skill_guard_coverage.py`, lode-2thl) -- applied to the guard
this ticket added. It is also the ticket's own option (c), which the design
note argued neither for nor against.

Within each rostered file the gate is shaped around the *hazard*, not around
the current text: it finds any fenced bash block that reads one of these
metadata fields AND compares that value against a real branch tip, and
requires the validator call in THAT SAME BLOCK. Same-block is not a style
preference -- shell state does not survive between fenced blocks (lode-sfnb),
so a check one block later would be reading an unset variable.

The file roster itself is no longer hand-maintained (lode-rby4): it is a
hazard-keyed scan over the same corpus every other markdown gate in this repo
globs -- ``tests/test_skill_bash_state.py``,
``tests/test_no_hand_derived_skill_md_path.py``,
``tests/test_bd_list_limit_gate.py``, ``tests/test_isolation_guard.py`` all use
``sorted(SKILLS_DIR.glob('*/SKILL.md')) + sorted(AGENTS_DIR.glob('*.md'))``.
The read/compare predicate below (:data:`TIP_COMPARISON_MARKERS`) is what
keeps that widening from producing a false positive: it excludes
``.claude/skills/code/SKILL.md``'s own ``metadata.review_head`` read, which is
only a non-emptiness check, never a tip comparison.

What that trades, honestly: the hand-maintained *file roster* is gone, but
:data:`DRIFT_FIELDS` and :data:`TIP_COMPARISON_MARKERS` are both still hand-
maintained. The win is not "nothing is hand-maintained" -- it is that what
remains is keyed on the hazard (which fields drift, which verbs compare a tip)
rather than on which files happen to contain it today, so a read site added to
a *new* file is caught without editing this test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import markdown_corpus_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = REPO_ROOT / ".claude"

VALIDATOR = "scripts/validate-sha40.sh"

#: The metadata fields whose value is compared against a real branch tip to
#: detect drift -- exactly the comparison a malformed value corrupts. Read as
#: `.[0].metadata.<field>` out of `bd show --json` at every site. Hand-
#: maintained, as is :data:`TIP_COMPARISON_MARKERS`; the *file roster* is not.
DRIFT_FIELDS = ("land_head", "review_head")

#: Markers that, co-occurring with a `metadata.<field>` read in the SAME
#: fenced block, mean that value is about to be compared against a real
#: branch tip -- the actual hazard this gate polices, not the field read by
#: itself. `.claude/skills/code/SKILL.md`'s `metadata.review_head` read is a
#: non-emptiness check only and carries none of these, so it is correctly
#: excluded rather than demanding a spurious validator call there.
#:
#: `git merge-base` is listed because it is the comparison `code-reviewer.md`
#: actually performs on `review_head`. Without it that block matched only on an
#: incidental `git rev-parse --abbrev-ref HEAD` line that exists for an
#: unrelated branch-name check -- so deleting that line would have silently
#: dropped a genuine call site out of the gate. Prefer over-inclusion here: a
#: spurious marker match only demands a validator call that is harmless to add,
#: whereas a missing one un-guards a real read.
TIP_COMPARISON_MARKERS = (
    "git rev-parse",
    "git ls-remote",
    "git merge-base",
    "origin/land",
)


def _read_sites(blocks: list[str], field: str) -> list[str]:
    """Blocks in ``blocks`` that pull ``metadata.<field>`` out of
    `bd show --json` AND compare that value against a branch tip in the same
    block -- see :data:`TIP_COMPARISON_MARKERS`."""
    return [
        b
        for b in blocks
        if f"metadata.{field}" in b
        and "jq" in b
        and any(marker in b for marker in TIP_COMPARISON_MARKERS)
    ]


#: (relative path, fenced-bash-blocks) for every file in the corpus -- the
#: hazard-keyed replacement for the old hand-maintained roster. SKILL.md docs
#: then agent defs, the same order the sibling gates use -- conftest's cached
#: :func:`markdown_corpus_blocks` already reads and fence-parses the whole
#: corpus once per session (lode-2evf), so this module no longer re-reads and
#: re-fence-parses the ~174KB ``land/SKILL.md``/``code-reviewer.md`` a second
#: time on top of the ``LAND_SKILL_BLOCKS``/``CODE_REVIEWER_AGENT_BLOCKS``
#: reads conftest already does for other consumers.
CALL_SITES: list[tuple[str, list[str]]] = [
    (str(path.relative_to(REPO_ROOT)), list(blocks))
    for path, blocks in markdown_corpus_blocks()
]


@pytest.mark.parametrize("field", DRIFT_FIELDS)
def test_the_expected_read_site_still_exists(field: str) -> None:
    """Non-vacuity: if the read itself is renamed away, this whole gate would
    pass by finding nothing to check. Pins that each field is still read (and
    compared to a tip) at least once somewhere in the corpus.

    Deliberately "at least one", not "exactly one file" (lode-rby4): a strict
    single-file assertion would forbid a legitimate second guarded read from
    ever existing. No signal is lost: "exactly one" existed for non-vacuity,
    which "at least one" still provides, and its incidental coverage job
    (noticing a new read site) is now done properly by
    :func:`test_every_drift_field_read_is_shape_checked_in_the_same_block`,
    which guards the whole corpus rather than merely reporting on it.
    """
    found = [path for path, blocks in CALL_SITES if _read_sites(blocks, field)]
    assert found, (
        f"expected at least one fenced bash block reading metadata.{field} "
        "AND comparing it to a branch tip, found none -- if the read site "
        "was renamed, moved, or removed, extend or update this gate rather "
        "than deleting it"
    )


def test_every_drift_field_read_is_shape_checked_in_the_same_block() -> None:
    """The gate proper. A block that reads `land_head`/`review_head` and
    compares it to a branch tip must call the validator on it first."""
    unguarded: list[str] = []
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for block in _read_sites(blocks, field):
                if VALIDATOR not in block:
                    unguarded.append(f"{path}: block reading metadata.{field}")
    assert not unguarded, (
        "these fenced bash blocks read a drift-detection SHA out of bd "
        f"metadata and compare it to a branch tip without calling {VALIDATOR} "
        f"on it in the same block (lode-xdg3): {unguarded}. A malformed value "
        "never equals a real branch tip, so it reads as drift and bounces a "
        "correct branch. Shell state does not survive between blocks "
        "(lode-sfnb), so the check must be in the SAME block as the read."
    )


def test_validator_is_called_on_the_value_that_was_read() -> None:
    """A call passing the wrong variable would satisfy the gate above while
    validating nothing. Pins that the field name and the shell variable the
    read assigned both appear on the validator's own line."""
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for block in _read_sites(blocks, field):
                var = field.upper()
                lines = [ln for ln in block.splitlines() if VALIDATOR in ln]
                assert len(lines) == 1, f"{path}: expected one {VALIDATOR} call"
                assert field in lines[0], f"{path}: {VALIDATOR} call omits {field}"
                assert f'"${var}"' in lines[0], (
                    f"{path}: {VALIDATOR} is called on something other than "
                    f'"${var}", the variable the same block assigned from '
                    f"metadata.{field}"
                )


def test_validator_script_is_executable() -> None:
    """Both call sites invoke it as a bare path, not via `bash <path>`."""
    script = REPO_ROOT / VALIDATOR
    assert script.exists(), f"{VALIDATOR} is missing"
    assert os.access(script, os.X_OK), f"{VALIDATOR} is not executable"
