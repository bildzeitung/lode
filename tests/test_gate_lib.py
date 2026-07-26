"""Tests for scripts/gate-lib.sh (lode-090f).

Shared `gate_could_not_run()` helper, factored out because the "exit 2 means
the GATE could not run, never that the CONTENT is bad" contract (lode-9i2p)
had reached three duplicated literal copies -- scripts/validate-mermaid.sh,
scripts/merge-precheck.sh, scripts/release-bump.sh -- free to drift, and had
already started to: validate-mermaid.sh's copy printed a two-line advisory
that merge-precheck.sh's stated differently, and release-bump.sh's carried no
advisory at all. Same "reaches three copies, extract" precedent as
scripts/epic-children-closed.sh (tests/test_epic_children_closed.py) and
scripts/recycled-worktree-guard.sh (tests/test_recycled_worktree_guard.py).

These tests exercise the library directly, under `bash -c '...'` sourcing it
the same way every real caller does (`. "$(dirname "$0")/gate-lib.sh"`),
rather than through any one of the three consuming scripts -- those each keep
their own existing regression tests (tests/test_validate_mermaid_gate.py,
tests/test_merge_precheck.py, tests/test_release_bump.py), which double as
this library's integration coverage and must stay green unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_LIB = REPO_ROOT / "scripts" / "gate-lib.sh"


def _run(script_body: str) -> subprocess.CompletedProcess:
    """Run `script_body` under `bash -uo pipefail -c`, after sourcing
    gate-lib.sh -- `-u` (nounset) matches how merge-precheck.sh/release-bump.sh
    actually run, and is the regime the unset-array bug below only reproduces
    under."""
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", f'. "{GATE_LIB}"\n{script_body}'],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_banner_and_cause_lines_go_to_stderr_with_exit_2():
    result = _run('gate_could_not_run "summary here" "cause line 1" "cause line 2"')

    assert result.returncode == 2
    assert result.stdout == ""
    assert "GATE COULD NOT RUN: summary here" in result.stderr
    assert "cause line 1" in result.stderr
    assert "cause line 2" in result.stderr


def test_no_gate_advisory_set_means_no_trailer_at_all():
    """release-bump.sh's shape: no GATE_ADVISORY set, so nothing beyond the
    caller's own cause lines is printed."""
    result = _run('gate_could_not_run "summary" "only cause line"')

    assert result.returncode == 2
    lines = [ln for ln in result.stderr.splitlines() if ln]
    assert lines == ["GATE COULD NOT RUN: summary", "only cause line"]


def test_gate_advisory_set_once_is_appended_after_every_calls_cause_lines():
    """merge-precheck.sh's / validate-mermaid.sh's shape: GATE_ADVISORY set
    once near the top of the sourcing script, then appended automatically on
    every call site -- never repeated per call."""
    result = _run(
        'GATE_ADVISORY=("advisory line one" "advisory line two")\n'
        'gate_could_not_run "summary" "cause line"'
    )

    assert result.returncode == 2
    lines = [ln for ln in result.stderr.splitlines() if ln]
    assert lines == [
        "GATE COULD NOT RUN: summary",
        "cause line",
        "advisory line one",
        "advisory line two",
    ]


def test_sourcing_under_nounset_does_not_error_on_unset_gate_advisory():
    """Regression: bash's `set -u` treats `${arr[@]}` on a never-declared
    array as an unbound-variable error (verified empirically, bash 5.2) --
    unlike a scalar's more forgiving `${var:-}` default. Every real caller of
    this library runs under `set -u` (merge-precheck.sh, release-bump.sh) or
    at least sources it (validate-mermaid.sh), so simply sourcing gate-lib.sh
    and calling gate_could_not_run with no GATE_ADVISORY ever set must not
    itself blow up with "unbound variable" before reaching the exit-2 path."""
    result = _run('gate_could_not_run "summary" "cause"')

    assert result.returncode == 2
    assert "unbound variable" not in result.stderr
