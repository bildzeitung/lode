"""Gate: every site that reads or writes `land_head`/`review_head` guards the
value with the shape check (lode-xdg3, lode-uvjr, lode-qh1m).

`tests/test_validate_sha40.py` pins what `scripts/validate-sha40.sh` DOES.
Nothing there pins that anyone CALLS it -- and the callers are fenced bash
inside markdown instructions, which no other gate parses. Without this file,
deleting the validator call leaves the whole suite green while the pipeline
silently returns to the lode-r9z0 behaviour: a truncated SHA written to bd
metadata and later misread as drift, which in `/land` Section 2a bounces an
already-correct branch instead of escalating it.

Two shapes, two rules:

- **Main-session skills** (`.claude/skills/*/SKILL.md`) keep the SHA in a shell
  variable inside ONE fenced block. A read that compares the value against a
  branch tip, and a `--set-metadata <field>=` write, must each call
  `scripts/validate-sha40.sh <field> "$VAR"` in that same block, on the same
  variable (shell state does not survive between blocks, lode-sfnb).
- **Dispatched agents** (`.claude/agents/*.md`) cannot carry a value between
  commands at all -- the harness isolation classifier refuses a variable or
  `$(...)` (lode-fvg0) -- and copying a SHA from one call's output into the next
  let the validator check one paste while the other was written (lode-0a4n).
  So (lode-qh1m) an agent file may NOT write either field via `--set-metadata`
  (anywhere in the file, prose and inline code included) nor read it with `jq`;
  it calls `scripts/handoff-sha.sh` (derive + validate + write in one process)
  and `scripts/check-review-head.sh` (read + validate + ancestor check in one
  process). The rule is "the agent file calls the script", checked by exact
  path; that the scripts themselves validate before using the value is pinned
  by behaviour in `tests/test_handoff_scripts.py`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import markdown_corpus_blocks, markdown_corpus_text

REPO_ROOT = Path(__file__).resolve().parent.parent

VALIDATOR = "scripts/validate-sha40.sh"
HANDOFF = "scripts/handoff-sha.sh"
REVIEW_HEAD_CHECK = "scripts/check-review-head.sh"

#: The metadata fields whose value is compared against a real branch tip to
#: detect drift -- exactly the comparison a malformed value corrupts.
DRIFT_FIELDS = ("land_head", "review_head")

#: Markers that, co-occurring with a `metadata.<field>` read in the SAME
#: fenced block, mean that value is about to be compared against a real branch
#: tip. `.claude/skills/code/SKILL.md`'s `metadata.review_head` read is a
#: non-emptiness check only and carries none of these, so it is correctly
#: excluded. Prefer over-inclusion: a spurious match only demands a validator
#: call that is harmless to add, a missing one un-guards a real read.
TIP_COMPARISON_MARKERS = (
    "git rev-parse",
    "git ls-remote",
    "git merge-base",
    "origin/land",
)

WRITE_VALUE_PATTERNS = {
    field: re.compile(rf"--set-metadata\s+{re.escape(field)}=(\S+)")
    for field in DRIFT_FIELDS
}

#: A write value bound to a shell variable -- the one-block shape.
WRITE_VAR_VALUE = re.compile(r'"\$([A-Za-z_][A-Za-z0-9_]*)"')

#: (relative path, fenced-bash-blocks) for every file in the corpus. conftest's
#: cached :func:`markdown_corpus_blocks` reads and fence-parses it once per
#: session (lode-2evf).
CALL_SITES: list[tuple[str, list[str]]] = [
    (str(path.relative_to(REPO_ROOT)), list(blocks))
    for path, blocks in markdown_corpus_blocks()
]

AGENTS_PREFIX = ".claude/agents/"

AGENT_SITES = [(p, b) for p, b in CALL_SITES if p.startswith(AGENTS_PREFIX)]
SKILL_SITES = [(p, b) for p, b in CALL_SITES if not p.startswith(AGENTS_PREFIX)]

#: Raw text of every agent file: a hand-copied write is as dangerous in an
#: inline code span or a list item as in a fenced block, and the blocks view
#: cannot see those.
AGENT_TEXT = {
    rel: text
    for path, text in markdown_corpus_text()
    if (rel := str(path.relative_to(REPO_ROOT))).startswith(AGENTS_PREFIX)
}


def _validator_lines(block: str, field: str) -> list[str]:
    return [ln for ln in block.splitlines() if VALIDATOR in ln and field in ln]


def _write_site_indices(blocks: list[str], field: str) -> list[int]:
    return [i for i, b in enumerate(blocks) if WRITE_VALUE_PATTERNS[field].search(b)]


def _read_site_indices(blocks: list[str], field: str) -> list[int]:
    """Blocks that pull ``metadata.<field>`` via jq AND compare it to a tip."""
    return [
        i
        for i, b in enumerate(blocks)
        if f"metadata.{field}" in b
        and "jq" in b
        and any(marker in b for marker in TIP_COMPARISON_MARKERS)
    ]


def _calls(blocks: list[str], script: str, *needles: str) -> bool:
    """Some block has a line invoking ``script`` that mentions every needle."""
    return any(
        script in ln and all(n in ln for n in needles)
        for b in blocks
        for ln in b.splitlines()
    )


def test_skill_read_site_still_exists_for_land_head() -> None:
    """Non-vacuity for the one-block read gate: `/land` Section 2a still reads
    `land_head` and compares it to a tip. (`review_head` has no skill-side
    comparison -- the code-reviewer's check is a script, pinned below.)"""
    assert any(_read_site_indices(b, "land_head") for _, b in SKILL_SITES), (
        "expected a skill block reading metadata.land_head and comparing it to a "
        "branch tip, found none -- update this gate rather than deleting it"
    )


def test_every_skill_read_is_shape_checked_in_its_own_block() -> None:
    unguarded = [
        f"{path}: block reading metadata.{field}"
        for path, blocks in SKILL_SITES
        for field in DRIFT_FIELDS
        for i in _read_site_indices(blocks, field)
        if not any(
            f'"${field.upper()}"' in ln for ln in _validator_lines(blocks[i], field)
        )
    ]
    assert not unguarded, (
        f"these blocks read a drift SHA and compare it to a tip without "
        f'calling {VALIDATOR} <field> "$FIELD_UPPER" in the same block '
        f"(lode-xdg3, lode-sfnb): {unguarded}"
    )


def test_skill_write_site_still_exists() -> None:
    """Non-vacuity for the one-block write gate below: agent files may not
    write these fields at all, so the skills hold its only
    `--set-metadata <field>=` sites."""
    assert any(
        _write_site_indices(b, field) for _, b in SKILL_SITES for field in DRIFT_FIELDS
    ), (
        "expected a skill block writing a drift SHA via --set-metadata, found "
        "none -- update this gate rather than deleting it"
    )


def test_every_skill_write_is_shape_checked_on_the_same_variable() -> None:
    """A write must be `--set-metadata <field>="$VAR"` with the validator called
    on that same `"$VAR"` in the same block (lode-uvjr, lode-0a4n)."""
    problems: list[str] = []
    for path, blocks in SKILL_SITES:
        for field in DRIFT_FIELDS:
            for i in _write_site_indices(blocks, field):
                lines = _validator_lines(blocks[i], field)
                for raw in WRITE_VALUE_PATTERNS[field].findall(blocks[i]):
                    var = WRITE_VAR_VALUE.fullmatch(raw)
                    if not var or not any(f'"${var.group(1)}"' in ln for ln in lines):
                        problems.append(f"{path}: {field}={raw}")
    assert not problems, (
        f"skill writes of a drift SHA not validated on the same variable: {problems}"
    )


@pytest.mark.parametrize("field", DRIFT_FIELDS)
def test_no_agent_file_writes_or_reads_a_drift_field_by_hand(field: str) -> None:
    """Strict (lode-qh1m): agent files go through the scripts, never a
    hand-copied `--set-metadata <field>=` (anywhere in the file) or a `jq`
    read of the field."""
    offenders = [
        path
        for path, blocks in AGENT_SITES
        if WRITE_VALUE_PATTERNS[field].search(AGENT_TEXT[path])
        or any(f"metadata.{field}" in b and "jq" in b for b in blocks)
    ]
    assert not offenders, (
        f"{offenders} write or jq-read metadata.{field} directly; use {HANDOFF} / "
        f"{REVIEW_HEAD_CHECK}, which derive and validate in one process"
    )


@pytest.mark.parametrize(
    ("agent", "script", "needle"),
    [
        ("coding.md", HANDOFF, "review_head"),
        ("coding.md", HANDOFF, "land_head"),
        ("code-reviewer.md", HANDOFF, "land_head"),
        ("code-reviewer.md", REVIEW_HEAD_CHECK, ""),
    ],
)
def test_agent_files_still_call_the_hand_off_scripts(
    agent: str, script: str, needle: str
) -> None:
    """Non-vacuity for the strict gate above: it would pass vacuously if the
    agent simply stopped recording the hand-off."""
    blocks = dict(AGENT_SITES)[f".claude/agents/{agent}"]
    assert _calls(blocks, script, needle), f"{agent} no longer calls {script} {needle}"


def test_validator_script_is_executable() -> None:
    """Every caller invokes it as a bare path, not via `bash <path>`."""
    script = REPO_ROOT / VALIDATOR
    assert script.exists(), f"{VALIDATOR} is missing"
    assert os.access(script, os.X_OK), f"{VALIDATOR} is not executable"
