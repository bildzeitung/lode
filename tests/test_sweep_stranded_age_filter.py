"""Regression pin for lode-42fh: `.claude/skills/sweep/SKILL.md` Section 2b's
`started_at` 24h age discriminator (added by lode-3k6x) had no test executing
the real fenced block -- its behaviour rested entirely on manual verification
against live `bd` (see lode-3k6x's technical review). This pins that arithmetic
mechanically instead.

WHAT THIS PINS, per lode-42fh's acceptance criteria:
- a ticket `started_at` just under 24h ago is NOT listed (excluded, live build);
- one just over 24h ago IS listed (stranded);
- a `null` `started_at` is treated as stranded (listed), not filtered out;
- an ABSENT `started_at` key is treated as stranded (listed), not filtered out;
- a healthy query exits 0 and writes real content, not the `SWEEP-QUERY-ERROR`
  sentinel.

Sabotage checks (comparison direction flipped; null branch dropped) are
included directly rather than via a mutated copy of the skill file, since the
property under test -- "a ticket at the boundary lands on the correct side" --
is exactly what a flipped `<`/`>` would invert, and dropping the null branch
would exclude the null-`started_at` row instead of listing it.

Same fake-`bd`-on-PATH-plus-real-fenced-block pattern as
tests/test_sweep_source_query_failure.py and tests/test_sweep_new_ids_ordering.py;
block extraction is tests/conftest.py::bash_fence_blocks (lode-kjei), execution
is tests/conftest.py::run_block (lode-n6q0).
"""

from __future__ import annotations

import json
import shutil
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import bash_fence_blocks, only_block_with, run_block

REPO_ROOT = Path(__file__).resolve().parent.parent
SWEEP_SKILL = REPO_ROOT / ".claude" / "skills" / "sweep" / "SKILL.md"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the skill's fenced blocks shell out to jq"
)

# IDs chosen to be distinguishable in assertions -- not real bd ids.
JUST_UNDER_ID = "lode-under23h"  # started_at ~23h ago -- must be EXCLUDED (live build)
JUST_OVER_ID = "lode-over33h"  # started_at ~33h ago -- must be LISTED (stranded)
NULL_ID = "lode-nullstart"  # started_at: null -- must be LISTED (no age evidence)
ABSENT_ID = "lode-noattr"  # no started_at key at all -- must be LISTED


def _iso(dt: datetime) -> str:
    """Plain ISO8601-Z, no fractional seconds -- the shape `bd` 1.1.0 emits and
    the shape jq's `fromdateiso8601` parses (lode-3k6x's review verified this
    against live bd)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _skill_blocks() -> list[str]:
    return bash_fence_blocks(SWEEP_SKILL.read_text(encoding="utf-8"))


def _section_2b_block() -> str:
    """Section 2b -- the stranded in_progress collection with the age filter.

    Located by text this ticket does not change (the `fromdateiso8601` select
    and the `STRANDED=` assignment), not by a text this test could itself be
    pinning -- a locator keyed on the exact select() clause would match zero
    blocks the moment the fix regressed, and this test would die inside
    `only_block_with` complaining about drift instead of reddening on its own
    terms.
    """
    return only_block_with(
        _skill_blocks(),
        "STRANDED=$(bd list --status in_progress",
        "started_at | fromdateiso8601",
        what="Section 2b's stranded-ticket collection",
    )


def _fake_bd(bin_dir: Path, rows: list[dict]) -> None:
    """A PATH dir holding a fake `bd` that answers `bd list --status
    in_progress ...` with a fixed JSON array, and everything else with `[]`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(rows)
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + textwrap.dedent(f"""\
            if [ "$1" = "list" ]; then
              cat <<'JSON'
{payload}
JSON
            else
              echo '[]'
            fi
        """)
    )
    fake_bd.chmod(0o755)


def _rows() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {
            "id": JUST_UNDER_ID,
            "title": "just under 23h",
            "started_at": _iso(now - timedelta(hours=23)),
        },
        {
            "id": JUST_OVER_ID,
            "title": "just over 33h",
            "started_at": _iso(now - timedelta(hours=33)),
        },
        {
            "id": NULL_ID,
            "title": "null started_at",
            "started_at": None,
        },
        {
            "id": ABSENT_ID,
            "title": "absent started_at key",
        },
    ]


def test_boundary_and_null_absent_rows(sweep_tmp: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, _rows())
    proc = run_block(_section_2b_block(), sweep_tmp, bin_dir)

    assert proc.returncode == 0, (
        f"a healthy §2b query exited non-zero (rc={proc.returncode}, "
        f"stderr={proc.stderr!r})"
    )

    stranded_path = sweep_tmp / "stranded"
    assert stranded_path.exists(), "§2b did not persist $SWEEP_TMP/stranded"
    content = stranded_path.read_text(encoding="utf-8")

    assert content != "SWEEP-QUERY-ERROR", (
        f"a healthy §2b query wrote the SWEEP-QUERY-ERROR sentinel instead of "
        f"real content (stderr={proc.stderr!r})"
    )

    assert JUST_UNDER_ID not in content, (
        f"a ticket claimed ~23h ago (under the 24h threshold) was listed as "
        f"stranded -- it is still a live build, not a stranding (lode-3k6x). "
        f"content={content!r}"
    )
    assert JUST_OVER_ID in content, (
        f"a ticket claimed ~33h ago (over the 24h threshold) was NOT listed as "
        f"stranded. content={content!r}"
    )
    assert NULL_ID in content, (
        f"a ticket with a null started_at (no age evidence) was NOT listed as "
        f"stranded -- it should be treated as stranded defensively, per §2b's "
        f"own comment. content={content!r}"
    )
    assert ABSENT_ID in content, (
        f"a ticket with no started_at key at all was NOT listed as stranded -- "
        f"same defensive branch as the null case. content={content!r}"
    )


def test_flipped_comparison_direction_would_be_caught(
    sweep_tmp: Path, tmp_path: Path
) -> None:
    """Sabotage check: reimplements §2b's block with the comparison direction
    flipped (`>` instead of `<`) and asserts the resulting selection is wrong
    on the very rows this ticket pins -- proving the real test above would
    catch a regression of exactly this shape."""
    block = _section_2b_block()
    sabotaged = block.replace(
        "(.started_at | fromdateiso8601) < (now - 86400)",
        "(.started_at | fromdateiso8601) > (now - 86400)",
    )
    assert sabotaged != block, "the select() clause text this test replaces has drifted"

    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, _rows())
    proc = run_block(sabotaged, sweep_tmp, bin_dir)
    assert proc.returncode == 0
    content = (sweep_tmp / "stranded").read_text(encoding="utf-8")

    # With the direction flipped, the just-under-threshold (live build) ticket
    # is now wrongly listed -- exactly the regression this test exists to catch.
    assert JUST_UNDER_ID in content, (
        "flipping the comparison direction did not change the outcome -- this "
        "sabotage check no longer proves the real assertions are sensitive to "
        "the comparison direction"
    )


def test_dropped_null_branch_would_be_caught(sweep_tmp: Path, tmp_path: Path) -> None:
    """Sabotage check: reimplements §2b's block with the `.started_at == null
    or` branch dropped, and asserts the null-started_at row is then wrongly
    excluded -- proving the real test above would catch a regression of
    exactly this shape."""
    block = _section_2b_block()
    sabotaged = block.replace(
        "select(.started_at == null or (.started_at | fromdateiso8601) < (now - 86400))",
        "select((.started_at | fromdateiso8601) < (now - 86400))",
    )
    assert sabotaged != block, "the select() clause text this test replaces has drifted"

    bin_dir = tmp_path / "fakebin"
    _fake_bd(bin_dir, _rows())
    proc = run_block(sabotaged, sweep_tmp, bin_dir)
    # A null started_at piped into fromdateiso8601 errors -- jq exits non-zero,
    # so this drop degrades to the SWEEP-QUERY-ERROR sentinel path rather than
    # a clean-but-wrong selection. Either symptom proves the branch mattered.
    if proc.returncode == 0:
        content = (sweep_tmp / "stranded").read_text(encoding="utf-8")
        assert content == "SWEEP-QUERY-ERROR" or NULL_ID not in content, (
            "dropping the null-started_at branch did not change the outcome "
            "-- this sabotage check no longer proves the real assertions are "
            "sensitive to that branch"
        )
