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

**WRITE sites are a distinct hazard from READ sites, and are gated separately
(lode-uvjr).** The read-site gate above matches only a block that pulls
`metadata.<field>` out of `bd show --json` AND compares it to a branch tip
(:data:`TIP_COMPARISON_MARKERS`) -- a write block does neither: it never reads
`metadata.<field>` at all, it *sets* it via `--set-metadata <field>=`. Before
lode-uvjr, `.claude/agents/coding.md`'s three `--set-metadata
review_head=`/`--set-metadata land_head=` writes could each drop their
`scripts/validate-sha40.sh` guard with the whole suite green, and worse, a
reader of this file's docstring would reasonably conclude the corpus was fully
covered. :func:`_write_site_indices` and the three ``..._write...`` tests below
mirror the read-site shape (non-vacuity, guard-position gate, right-value check)
but keyed on `--set-metadata <field>=` instead of a read-and-compare.

Within each rostered file the gate is shaped around the *hazard*, not around
the current text: it finds any fenced bash block that reads one of these
metadata fields AND compares that value against a real branch tip, and
requires the validator call to sit where it can actually guard that value.
There are two shapes, and the position rule differs between them:

- **One block, a shell variable** (main-session skills). The read, the
  validator and the comparison share a block and a `"$VAR"`. The validator
  must be in THAT SAME BLOCK -- shell state does not survive between fenced
  blocks (lode-sfnb), so a check one block later would read an unset variable.
- **A plain-call sequence** (the dispatched-agent files, lode-fvg0). The
  harness's worktree-isolation classifier refuses a block that carries a
  derived value from one command into the next, so each command is its own
  block and the value is pasted from one call's output into the next. The
  validator must be in the block ADJACENT to the site: the one right after a
  read, the one right before a write. Adjacent, not "nearby" -- every block
  further away is room for an unrelated validator to satisfy the gate for a
  site whose own check was deleted.

The file roster itself is no longer hand-maintained (lode-rby4): it is a
hazard-keyed scan over the same corpus every other markdown gate in this repo
globs -- ``tests/test_skill_bash_state.py``,
``tests/test_no_hand_derived_skill_md_path.py``,
``tests/test_bd_list_limit_gate.py``, ``tests/test_isolation_guard.py`` all use
``sorted(SKILLS_DIR.glob('*/SKILL.md')) + sorted(AGENTS_DIR.glob('*.md'))``.
The read/compare predicate below (:func:`_read_site_indices`) is what keeps
that widening from producing a false positive: it excludes
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
import re
from pathlib import Path

import pytest
from conftest import markdown_corpus_blocks

REPO_ROOT = Path(__file__).resolve().parent.parent

VALIDATOR = "scripts/validate-sha40.sh"

#: The metadata fields whose value is compared against a real branch tip to
#: detect drift -- exactly the comparison a malformed value corrupts. Read as
#: `.[0].metadata.<field>` out of `bd show --json` at every site. Hand-
#: maintained, as is :data:`TIP_COMPARISON_MARKERS`; the *file roster* is not.
DRIFT_FIELDS = ("land_head", "review_head")

#: Markers that mean a value read in an EARLIER block of a plain-call sequence
#: is being compared against a real branch tip in this one. `git rev-parse` is
#: deliberately absent: in a neighbouring block it is as likely to be an
#: unrelated derive (a hand-off's `git rev-parse HEAD`) as a comparison.
LATER_BLOCK_COMPARISON_MARKERS = ("git ls-remote", "git merge-base", "origin/land")

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
TIP_COMPARISON_MARKERS = ("git rev-parse", *LATER_BLOCK_COMPARISON_MARKERS)

#: How many blocks past a plain-call read its tip comparison may sit: the read,
#: then the validator, then the comparison.
COMPARISON_LOOKAHEAD = 2

#: The value token of every `--set-metadata <field>=<value>` write, compiled
#: once at import rather than per site.
WRITE_VALUE_PATTERNS = {
    field: re.compile(rf"--set-metadata\s+{re.escape(field)}=(\S+)")
    for field in DRIFT_FIELDS
}

#: A write value bound to a shell variable -- the one-block shape.
WRITE_VAR_VALUE = re.compile(r'"\$([A-Za-z_][A-Za-z0-9_]*)"')


def _validator_lines(block: str, field: str) -> list[str]:
    """The lines of ``block`` that call the validator on ``field``."""
    return [ln for ln in block.splitlines() if VALIDATOR in ln and field in ln]


def _write_site_indices(blocks: list[str], field: str) -> list[int]:
    """Indices of blocks that write ``metadata.<field>`` via
    `bd update ... --set-metadata <field>=` -- the write-side hazard
    (lode-uvjr), distinct from :func:`_read_site_indices`: a write block never
    reads `metadata.<field>` at all, it sets it."""
    return [i for i, b in enumerate(blocks) if WRITE_VALUE_PATTERNS[field].search(b)]


def _compares_in_own_block(block: str) -> bool:
    return any(marker in block for marker in TIP_COMPARISON_MARKERS)


def _read_site_indices(blocks: list[str], field: str) -> list[int]:
    """Indices of blocks that pull ``metadata.<field>`` out of
    `bd show --json` AND whose value is compared against a branch tip: in that
    same block (:data:`TIP_COMPARISON_MARKERS`), or in one of the next
    :data:`COMPARISON_LOOKAHEAD` blocks of a plain-call sequence
    (:data:`LATER_BLOCK_COMPARISON_MARKERS`).

    A later block that WRITES the field is skipped: it re-derives the value
    (`git ls-remote ...` feeding `--set-metadata`), it does not compare the one
    that was read. Counting it would turn the non-emptiness read in
    ``.claude/skills/code/SKILL.md`` into a read site that passes only because
    the neighbouring write happens to validate its own, different value -- and
    that false site would then satisfy the non-vacuity test on its own."""
    writes = set(_write_site_indices(blocks, field))
    return [
        i
        for i, b in enumerate(blocks)
        if f"metadata.{field}" in b
        and "jq" in b
        and (
            _compares_in_own_block(b)
            or any(
                marker in blocks[j]
                for j in range(i + 1, min(i + 1 + COMPARISON_LOOKAHEAD, len(blocks)))
                if j not in writes
                for marker in LATER_BLOCK_COMPARISON_MARKERS
            )
        )
    ]


def _read_guard_index(blocks: list[str], i: int, field: str) -> int | None:
    """Index of the block whose validator call guards the read at ``i``, or
    ``None``. Its own block always counts. The next block counts only when the
    read block does not compare the value itself -- a validator that runs
    after the comparison guards nothing."""
    if _validator_lines(blocks[i], field):
        return i
    if (
        not _compares_in_own_block(blocks[i])
        and i + 1 < len(blocks)
        and _validator_lines(blocks[i + 1], field)
    ):
        return i + 1
    return None


def _write_is_guarded(blocks: list[str], i: int, field: str) -> bool:
    """The validator runs in the write's own block or the one right before."""
    return any(_validator_lines(blocks[j], field) for j in (i - 1, i) if j >= 0)


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
    :func:`test_every_drift_field_read_is_shape_checked`,
    which guards the whole corpus rather than merely reporting on it.
    """
    found = [path for path, blocks in CALL_SITES if _read_site_indices(blocks, field)]
    assert found, (
        f"expected at least one fenced bash block reading metadata.{field} "
        "AND comparing it to a branch tip, found none -- if the read site "
        "was renamed, moved, or removed, extend or update this gate rather "
        "than deleting it"
    )


def test_every_drift_field_read_is_shape_checked() -> None:
    """The gate proper. A block that reads `land_head`/`review_head` and
    compares it to a branch tip must call the validator on it first."""
    unguarded: list[str] = []
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for i in _read_site_indices(blocks, field):
                if _read_guard_index(blocks, i, field) is None:
                    unguarded.append(f"{path}: block reading metadata.{field}")
    assert not unguarded, (
        "these fenced bash blocks read a drift-detection SHA out of bd "
        f"metadata and compare it to a branch tip without calling {VALIDATOR} "
        f"on it first (lode-xdg3): {unguarded}. A malformed value "
        "never equals a real branch tip, so it reads as drift and bounces a "
        "correct branch. The check must be in the same block as the read "
        "(lode-sfnb), or -- when the read is a plain call of its own and the "
        "comparison comes in a later block -- in the block right after it "
        "(lode-fvg0)."
    )


def test_validator_is_called_on_the_value_that_was_read() -> None:
    """A call naming the field but passing nothing would satisfy the gate
    above while validating nothing. Pins that the validator's own line carries
    the field name AND a value argument after it: `"$FIELD_UPPER"` in a
    one-block site, any non-empty value (a pasted-from-output placeholder) in
    the plain-call sequence the agent files use (lode-fvg0)."""
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for i in _read_site_indices(blocks, field):
                j = _read_guard_index(blocks, i, field)
                assert j is not None, f"{path}: no {VALIDATOR} call on {field}"
                lines = _validator_lines(blocks[j], field)
                assert len(lines) == 1, f"{path}: expected one {VALIDATOR} call"
                value = lines[0].split(field, 1)[1].split("||")[0].strip()
                assert value, f"{path}: {VALIDATOR} call passes no value for {field}"
                if j == i:
                    assert f'"${field.upper()}"' in value, (
                        f"{path}: {VALIDATOR} is called on something other than "
                        f'"${field.upper()}", the variable the same block '
                        f"assigned from metadata.{field}"
                    )


def test_validator_script_is_executable() -> None:
    """Both call sites invoke it as a bare path, not via `bash <path>`."""
    script = REPO_ROOT / VALIDATOR
    assert script.exists(), f"{VALIDATOR} is missing"
    assert os.access(script, os.X_OK), f"{VALIDATOR} is not executable"


@pytest.mark.parametrize("field", DRIFT_FIELDS)
def test_the_expected_write_site_still_exists(field: str) -> None:
    """Non-vacuity for the write-side gate (lode-uvjr): pins that each field
    is still written via `--set-metadata <field>=` at least once somewhere in
    the corpus, so this gate can't pass merely by finding nothing to check."""
    found = [path for path, blocks in CALL_SITES if _write_site_indices(blocks, field)]
    assert found, (
        f"expected at least one fenced bash block writing metadata.{field} "
        "via --set-metadata, found none -- if the write site was renamed, "
        "moved, or removed, extend or update this gate rather than deleting "
        "it"
    )


def test_every_drift_field_write_is_shape_checked() -> None:
    """The write-side gate proper (lode-uvjr). A block that writes
    `land_head`/`review_head` via `--set-metadata` must have the validator
    called on that field in the same block or the one right before it,
    mirroring :func:`test_every_drift_field_read_is_shape_checked`."""
    unguarded: list[str] = []
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for i in _write_site_indices(blocks, field):
                if not _write_is_guarded(blocks, i, field):
                    unguarded.append(f"{path}: block writing metadata.{field}")
    assert not unguarded, (
        "these fenced bash blocks write a drift-detection SHA to bd metadata "
        f"via --set-metadata without calling {VALIDATOR} on it first "
        f"(lode-uvjr): {unguarded}. An unvalidated write can persist a "
        "malformed SHA that later reads as drift and bounces a correct "
        "branch. The check must be in the same block as the write (lode-sfnb) "
        "or, in a plain-call sequence, in the block right before it "
        "(lode-fvg0)."
    )


def test_validator_is_called_on_the_value_that_was_written() -> None:
    """A call passing the wrong variable would satisfy the gate above while
    validating nothing -- the write-side twin of
    :func:`test_validator_is_called_on_the_value_that_was_read`. Every written
    value must take one of two forms:

    - `"$VAR"` -- whatever that call site names it (`$HEAD_SHA`, `$SHA`,
      `$LAND_HEAD`, ...). The validator must be called on that same variable
      in the same block.
    - a `<placeholder>` -- the plain-call sequence of the dispatched-agent
      files (lode-fvg0), where the agent pastes the SHA it validated in the
      previous block. No variable exists to cross-check, so the adjacency gate
      above is all that can be pinned; that the second paste equals the first
      is the agent's to get right, and only the read-side checks catch a slip.

    Anything else is rejected: an inline `"$(git rev-parse ...)"`, an unquoted
    `$VAR`, a literal. A value that is neither bound to a variable nor pasted
    from a validated call cannot have been validated before it is written.

    Checks EVERY write in the block, not just the first: a block writing the
    same field twice from two different variables would otherwise pass on the
    strength of the first one's guard while the second went unvalidated.
    """
    for path, blocks in CALL_SITES:
        for field in DRIFT_FIELDS:
            for i in _write_site_indices(blocks, field):
                lines = _validator_lines(blocks[i], field)
                for raw in WRITE_VALUE_PATTERNS[field].findall(blocks[i]):
                    if raw.startswith("<"):
                        continue
                    var = WRITE_VAR_VALUE.fullmatch(raw)
                    assert var, (
                        f"{path}: --set-metadata {field}={raw} is neither a "
                        'plain "$VAR" nor a <placeholder> pasted from a '
                        "validated call -- write the SHA into a variable first, "
                        "or validate it in the previous plain call"
                    )
                    assert any(f'"${var.group(1)}"' in ln for ln in lines), (
                        f"{path}: no {VALIDATOR} call on {field} and "
                        f'"${var.group(1)}" -- the variable this same block '
                        f"writes via --set-metadata {field}="
                    )
