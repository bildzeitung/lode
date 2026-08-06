"""Pins the deliberate byte-identical concurrency block across the three
branch-push/pull_request-triggered CI workflows (lode-7hbu, lode-4lqx).

lode-7hbu's own framing: this must be a THREE-FILE change (build.yml,
tests.yml, coverage.yml) or none -- the byte-identical mirroring is
deliberate, and changing one alone breaks it. Nothing enforced that
mechanically; the reviewer had to verify it by hand with an ad-hoc scratch
script. This test makes that verification permanent.

WHY THE DUPLICATION IS PINNED RATHER THAN REMOVED. The repo's usual answer to
"N byte-identical copies" is to extract the one source and test that
(scripts/gate-lib.sh, scripts/recycled-worktree-guard.sh, tests/_gitrepo.py).
That is unavailable here: GitHub Actions has no include/anchor mechanism for a
workflow's top-level `on:`/`concurrency:` keys, so three copies is the only
shape the platform allows. Pinning the duplication is the fallback, not a
departure from the house style.

Scope is deliberately the three named files, not a glob over the workflows
directory -- release.yml is tag-triggered and correctly carries no concurrency
block at all (cancelling a one-shot tag build would be wrong, not missing).
The hard-coded list is not a blind spot, though: a fourth branch-push or
pull_request-triggered workflow added later trips
`test_no_unlisted_workflow_is_branch_push_or_pr_triggered`, which points the
author at EXPECTED_WORKFLOWS.

Both halves of the invariant are pinned, because they fail differently:
the shared-block test compares the literal bytes the three files share
(comment prose included -- the `head_ref || ref` trap is documented ONLY in
that comment, and no parsed comparison can see a comment), and the parsed test
pins the semantics, surviving a reformat and naming the field that drifted.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The three branch-push/pull_request-triggered CI workflows that must share
# this concurrency block (lode-7hbu). release.yml is tag-triggered and
# intentionally excluded.
EXPECTED_WORKFLOWS = ["build.yml", "tests.yml", "coverage.yml"]

EXPECTED_GROUP = "${{ github.workflow }}-${{ github.head_ref || github.ref_name }}"

# Anchors delimiting the region the three files share verbatim. Both sit
# INSIDE that region on purpose: the blank-ish `# ` separator line just above
# BLOCK_START terminates a paragraph whose wording legitimately differs per
# file (build.yml's landing-loop note, coverage.yml's Codecov-badge
# digression), so anchoring any higher would make an unrelated reword of those
# paragraphs fail this test.
BLOCK_START = "# The group collapses"
BLOCK_END = "  cancel-in-progress: true\n"


def _shared_block(filename: str) -> str:
    """The literal comment+concurrency region of one workflow, verbatim."""
    text = (WORKFLOWS_DIR / filename).read_text()
    start = text.find(BLOCK_START)
    assert start != -1, f"{filename}: no line starting {BLOCK_START!r}"
    end = text.find(BLOCK_END, start)
    assert end != -1, f"{filename}: no {BLOCK_END!r} after {BLOCK_START!r}"
    return text[start : end + len(BLOCK_END)]


def _load_concurrency(filename: str) -> dict:
    data = yaml.safe_load((WORKFLOWS_DIR / filename).read_text())
    assert "concurrency" in data, f"{filename} has no top-level concurrency block"
    return data["concurrency"]


def _branch_or_pr_triggers(workflow_path: Path) -> set[str]:
    """The subset of {push, pull_request} that can fire on a BRANCH.

    A tag-only `push:` filter (release.yml) does not count: a tag is pushed
    once, so there is no superseded run to cancel and the concurrency block
    would be wrong there rather than missing. That distinction is the whole
    reason this predicate exists rather than a bare ``"push" in on``.
    """
    # pyyaml parses the `on:` key as the boolean True (YAML 1.1).
    triggers = yaml.safe_load(workflow_path.read_text())[True]
    # `on:` may be a mapping, a list, or a bare string -- normalise all three.
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = dict.fromkeys(triggers)

    found = {"pull_request"} & set(triggers)
    if "push" in triggers:
        filters = triggers["push"] if isinstance(triggers["push"], dict) else {}
        # No filter at all means every branch push; a tag-only filter means none.
        if {"branches", "branches-ignore"} & set(filters) or not (
            {"tags", "tags-ignore"} & set(filters)
        ):
            found.add("push")
    return found


def test_shared_block_is_byte_identical_across_workflows() -> None:
    # Compared mutually, against the first file rather than a golden copy
    # stored here: a golden would be a FOURTH copy to keep in sync, and would
    # turn every legitimate reword of the comment into a four-file edit.
    reference, *others = EXPECTED_WORKFLOWS
    expected = _shared_block(reference)
    for name in others:
        assert _shared_block(name) == expected, (
            f"{name}: the comment + concurrency block is no longer byte-identical "
            f"to {reference}. lode-7hbu's mirroring is deliberate -- change all "
            f"{len(EXPECTED_WORKFLOWS)} together, or none.\n"
            f"--- {reference} ---\n{expected}\n--- {name} ---\n{_shared_block(name)}"
        )


def test_parsed_concurrency_matches_expected() -> None:
    # Whole-dict equality, so this one assertion covers every semantic bullet
    # the ticket asks for: the group expression is identical across the three
    # AND is exactly EXPECTED_GROUP (hence the fallback is `github.ref_name`,
    # never the `head_ref || ref` near-miss lode-7hbu exists to prevent), and
    # cancel-in-progress is true. It also catches an unexpected extra key,
    # which per-field assertions would miss.
    for name in EXPECTED_WORKFLOWS:
        assert _load_concurrency(name) == {
            "group": EXPECTED_GROUP,
            "cancel-in-progress": True,
        }, f"{name}: concurrency block drifted from the expected form"


def test_no_unlisted_workflow_is_branch_push_or_pr_triggered() -> None:
    # Backstops the hard-coded EXPECTED_WORKFLOWS scope: a FOURTH branch-push
    # or pull_request-triggered workflow added later must carry the same
    # concurrency block, so it must be added to that list. `*.y*ml` because
    # GitHub accepts .yaml identically -- globbing only `*.yml` would let a
    # `nightly.yaml` slip past the very check that exists to catch it.
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        if path.name in EXPECTED_WORKFLOWS:
            continue
        found = _branch_or_pr_triggers(path)
        assert not found, (
            f"{path.name} is {sorted(found)}-triggered on a branch but is not "
            f"in EXPECTED_WORKFLOWS. Give it the same concurrency block as its "
            f"siblings and add it to that list."
        )


def test_listed_workflows_really_are_branch_push_or_pr_triggered() -> None:
    # Keeps the predicate above honest: if it ever stopped recognising the
    # trigger shape these three use, the backstop would silently pass on a
    # fourth file too. This is the positive control for that.
    for name in EXPECTED_WORKFLOWS:
        found = _branch_or_pr_triggers(WORKFLOWS_DIR / name)
        assert found == {"push", "pull_request"}, (
            f"{name}: expected both branch-push and pull_request triggers, "
            f"got {sorted(found)}"
        )
