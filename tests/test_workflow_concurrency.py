"""Pins the deliberate byte-identical concurrency block across the three
push/pull_request-triggered CI workflows (lode-7hbu, lode-4lqx).

lode-7hbu's own framing: this must be a THREE-FILE change (build.yml,
tests.yml, coverage.yml) or none -- the byte-identical mirroring is
deliberate, and changing one alone breaks it. Nothing enforced that
mechanically; the reviewer had to verify it by hand with an ad-hoc scratch
script. This test makes that verification permanent.

Scope is deliberately hard-coded to these three named files, not a glob over
`.github/workflows/*.yml` -- release.yml is tag-triggered, not
push/pull_request-triggered, and does not carry (or need) this concurrency
block. A fourth push/PR-triggered workflow added later without wiring this
same block in will NOT be caught by this test automatically; whoever adds it
must add it to EXPECTED_WORKFLOWS below too, which is the point -- silently
extending the scope to "any yml file" would make the check meaningless the
moment a workflow legitimately doesn't need this pattern (e.g. a
tag-triggered release flow, as release.yml already is).
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The three push/pull_request-triggered CI workflows that must share this
# concurrency block (lode-7hbu). release.yml is tag-triggered and
# intentionally excluded.
EXPECTED_WORKFLOWS = ["build.yml", "tests.yml", "coverage.yml"]

EXPECTED_GROUP = "${{ github.workflow }}-${{ github.head_ref || github.ref_name }}"


def _load_concurrency(filename: str) -> dict:
    path = WORKFLOWS_DIR / filename
    with path.open() as f:
        data = yaml.safe_load(f)
    assert "concurrency" in data, f"{filename} has no top-level concurrency block"
    return data["concurrency"]


def test_concurrency_group_byte_identical_across_workflows() -> None:
    groups = {name: _load_concurrency(name)["group"] for name in EXPECTED_WORKFLOWS}
    distinct = set(groups.values())
    assert len(distinct) == 1, (
        "concurrency.group must be byte-identical across "
        f"{EXPECTED_WORKFLOWS}, got: {groups}"
    )


def test_concurrency_group_matches_expected_expression() -> None:
    for name in EXPECTED_WORKFLOWS:
        group = _load_concurrency(name)["group"]
        assert group == EXPECTED_GROUP, (
            f"{name}: concurrency.group expression drifted from the expected "
            f"form.\n  expected: {EXPECTED_GROUP}\n  actual:   {group}"
        )


def test_concurrency_group_uses_ref_name_not_ref() -> None:
    # The near-miss form `head_ref || ref` (NOT `ref_name`) still leaves the
    # push run in its own group and cancels nothing -- lode-7hbu exists to
    # prevent exactly this form being copied in. Assert the safe fallback is
    # present and the near-miss fallback is absent.
    for name in EXPECTED_WORKFLOWS:
        group = _load_concurrency(name)["group"]
        assert "github.ref_name" in group, (
            f"{name}: concurrency.group must fall back to github.ref_name, got: {group}"
        )
        assert "github.head_ref || github.ref }}" not in group, (
            f"{name}: concurrency.group uses the near-miss `head_ref || ref` "
            f"fallback (should be `head_ref || ref_name`): {group}"
        )


def test_cancel_in_progress_true_in_all_workflows() -> None:
    for name in EXPECTED_WORKFLOWS:
        concurrency = _load_concurrency(name)
        assert concurrency.get("cancel-in-progress") is True, (
            f"{name}: concurrency.cancel-in-progress must be true, "
            f"got: {concurrency.get('cancel-in-progress')!r}"
        )
