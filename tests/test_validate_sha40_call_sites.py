"""Gate: every read site that loads `land_head`/`review_head` shape-checks it
before comparing it to a branch tip (lode-xdg3).

`tests/test_validate_sha40.py` pins what `scripts/validate-sha40.sh` DOES.
Nothing there pins that anyone CALLS it -- and the two callers are fenced bash
inside markdown agent instructions, which no other gate parses. Without this
file, deleting the two-line check from `.claude/skills/land/SKILL.md` or
`.claude/agents/code-reviewer.md` leaves the whole suite green while the
pipeline silently returns to the exact lode-r9z0 behaviour the ticket exists to
prevent: a truncated `land_head` misread as drift, bouncing an already-correct
branch to `needs-rebase`.

This is the second half of the split `scripts/assert-main-checkout.sh` already
uses -- script-behaviour tests plus a markdown coverage gate
(`tests/test_land_skill_guard_coverage.py`, lode-2thl) -- applied to the guard
this ticket added. It is also the ticket's own option (c), which the design
note argued neither for nor against.

The gate is deliberately shaped around the *hazard*, not around the current
text: it finds any fenced bash block that reads one of these metadata fields
and requires the validator call in THAT SAME BLOCK. Same-block is not a style
preference -- shell state does not survive between fenced blocks (lode-sfnb),
so a check one block later would be reading an unset variable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CODE_REVIEWER_AGENT_BLOCKS, LAND_SKILL_BLOCKS

REPO_ROOT = Path(__file__).resolve().parent.parent

VALIDATOR = "scripts/validate-sha40.sh"

#: The metadata fields whose value is compared against a real branch tip to
#: detect drift -- exactly the comparison a malformed value corrupts. Read as
#: `.[0].metadata.<field>` out of `bd show --json` at every site.
DRIFT_FIELDS = ("land_head", "review_head")


def _read_sites(blocks: list[str], field: str) -> list[str]:
    """Blocks that pull ``metadata.<field>`` out of `bd show --json`."""
    return [b for b in blocks if f"metadata.{field}" in b and "jq" in b]


def _call_sites() -> list[tuple[str, list[str]]]:
    return [
        (".claude/skills/land/SKILL.md", LAND_SKILL_BLOCKS),
        (".claude/agents/code-reviewer.md", CODE_REVIEWER_AGENT_BLOCKS),
    ]


@pytest.mark.parametrize(
    ("name", "field"), [("land", "land_head"), ("cr", "review_head")]
)
def test_the_expected_read_site_still_exists(name: str, field: str) -> None:
    """Non-vacuity: if the read itself is renamed away, this whole gate would
    pass by finding nothing to check. Pins that each field is still read
    exactly once, in exactly one file."""
    found = [path for path, blocks in _call_sites() if _read_sites(blocks, field)]
    assert len(found) == 1, (
        f"expected exactly one file reading metadata.{field} in a fenced bash "
        f"block, found {found} -- if a read site was added or moved, extend "
        "this gate rather than deleting it"
    )


def test_every_drift_field_read_is_shape_checked_in_the_same_block() -> None:
    """The gate proper. A block that reads `land_head`/`review_head` must call
    the validator on it before that value can reach a drift comparison."""
    unguarded: list[str] = []
    for path, blocks in _call_sites():
        for field in DRIFT_FIELDS:
            for block in _read_sites(blocks, field):
                if VALIDATOR not in block:
                    unguarded.append(f"{path}: block reading metadata.{field}")
    assert not unguarded, (
        "these fenced bash blocks read a drift-detection SHA out of bd "
        f"metadata without calling {VALIDATOR} on it in the same block "
        f"(lode-xdg3): {unguarded}. A malformed value never equals a real "
        "branch tip, so it reads as drift and bounces a correct branch. Shell "
        "state does not survive between blocks (lode-sfnb), so the check must "
        "be in the SAME block as the read."
    )


def test_validator_is_called_on_the_value_that_was_read() -> None:
    """A call passing the wrong variable would satisfy the gate above while
    validating nothing. Pins that the field name and the shell variable the
    read assigned both appear on the validator's own line."""
    for path, blocks in _call_sites():
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
    import os

    script = REPO_ROOT / VALIDATOR
    assert script.exists(), f"{VALIDATOR} is missing"
    assert os.access(script, os.X_OK), f"{VALIDATOR} is not executable"
