"""Tests for scripts/bd-dolt-push-guard.sh and its integration into
scripts/bd-dolt-push.sh (lode-fzau).

Background: a code-reviewer/coding launch worktree was observed with a STRAY,
worktree-local bd DB -- bootstrap-hydrated from that branch's committed,
passively-lagging `.beads/issues.jsonl` -- instead of resolving to the ONE
shared main-checkout DB. A bd write against a ticket that happened to exist in
that stale snapshot would have succeeded SILENTLY against the stray DB, and
`bd-dolt-push.sh` would then have published that stale DB over
`refs/dolt/data`, reverting real cross-machine state.

A dispatched diagnostic could NOT reproduce the underlying stray-DB mechanism
(10 separate probes, including a live re-run of the ticket's own repro steps,
all resolved to the ONE shared, authoritative DB) -- so this is a BACKSTOP
against a mechanism that is real (it happened once) but not understood, not a
fix for a reproduced defect (human decision, recorded in lode-fzau's notes and
`--design`). It refuses a push when EITHER: (1) the resolved `.beads`
directory carries bd's own `.auto-import-issues.jsonl` marker (evidence the
local DB was hydrated from a passive jsonl snapshot), or (2) the current issue
count is wildly (default: >10%) below a local, per-DB-path high-water-mark
cache file that `bd-dolt-push.sh` itself writes after every successful push --
a network-free proxy for "wildly below the remote's count", since our own
last confirmed-pushed count can only be a floor on what the remote actually
has.

Both checks are designed to fail OPEN rather than block a legitimate push:
a fresh clone / `bd init` never touches this guard at all (`bd init` uses
`bd dolt pull`, not a jsonl import, and never calls `bd-dolt-push.sh`), and a
DB with no cache file yet (the common first-push case) has no baseline for
check 2, so it does not fire. `bd where`/`bd count` failing is itself treated
as "cannot assess, don't block" -- the real `bd dolt push` will surface any
genuine bd-level failure on its own.

The fake `bd` shim below drives `bd where --json` / `bd count --json` /
`bd dolt push` / `bd dolt pull` purely from environment variables (no fixture
files needed -- this guard's interface is much narrower than the epic-
completion scripts' `bd show`/`bd list` payloads).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = REPO_ROOT / "scripts" / "bd-dolt-push-guard.sh"
PUSH_SCRIPT = REPO_ROOT / "scripts" / "bd-dolt-push.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the guard shells out to jq"
)

FAKE_BD_SOURCE = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Fake `bd` for testing bd-dolt-push-guard.sh / bd-dolt-push.sh. Behavior
    # is driven entirely by env vars so tests need no fixture files.
    case "$1" in
      where)
        exit_code="${BD_FAKE_WHERE_EXIT:-0}"
        if [ "$exit_code" != "0" ]; then
          echo "fake bd: where forced failure" >&2
          exit "$exit_code"
        fi
        printf '%s' "$BD_FAKE_WHERE_JSON"
        ;;
      count)
        exit_code="${BD_FAKE_COUNT_EXIT:-0}"
        if [ "$exit_code" != "0" ]; then
          echo "fake bd: count forced failure" >&2
          exit "$exit_code"
        fi
        printf '%s' "$BD_FAKE_COUNT_JSON"
        ;;
      dolt)
        case "$2" in
          push)
            echo "push-called" >>"$BD_FAKE_CALL_LOG"
            exit "${BD_FAKE_PUSH_EXIT:-0}"
            ;;
          pull)
            echo "pull-called" >>"$BD_FAKE_CALL_LOG"
            exit "${BD_FAKE_PULL_EXIT:-0}"
            ;;
          *)
            echo "fake bd: unsupported dolt subcommand: $*" >&2
            exit 1
            ;;
        esac
        ;;
      *)
        echo "fake bd: unsupported: $*" >&2
        exit 1
        ;;
    esac
    """)


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(FAKE_BD_SOURCE)
    fake_bd.chmod(0o755)
    return bin_dir


def _db_dir(tmp_path: Path) -> Path:
    d = tmp_path / "beads"
    d.mkdir(exist_ok=True)
    return d


def _where_json(db_dir: Path) -> str:
    return json.dumps(
        {
            "database_path": str(db_dir / "embeddeddolt"),
            "path": str(db_dir),
            "prefix": "lode",
            "schema_version": 1,
        }
    )


def _count_json(count: int) -> str:
    return json.dumps({"count": count, "schema_version": 1})


def _run_guard(
    tmp_path: Path, *, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bin(tmp_path)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(GUARD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_push(
    tmp_path: Path, *, env_overrides: dict[str, str]
) -> tuple[subprocess.CompletedProcess, Path]:
    bin_dir = _fake_bin(tmp_path)
    call_log = tmp_path / "calls.log"
    call_log.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    env["BD_FAKE_CALL_LOG"] = str(call_log)
    # Keep the retry loop from actually sleeping/retrying in tests.
    env.setdefault("BD_DOLT_PUSH_MAX_ATTEMPTS", "1")
    env.update(env_overrides)
    result = subprocess.run(
        ["bash", str(PUSH_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, call_log


# --- Guard: the auto-import marker check -----------------------------------


def test_no_marker_no_cache_allows(tmp_path: Path) -> None:
    """A resolved DB with no marker and no prior high-water-mark cache is not
    suspicious -- the common case for every ordinary push."""
    db_dir = _db_dir(tmp_path)
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(407),
        },
    )
    assert result.returncode == 0, result.stderr


def test_auto_import_marker_present_refuses(tmp_path: Path) -> None:
    """bd's own `.auto-import-issues.jsonl` marker -- direct evidence this DB
    was hydrated from a passive jsonl snapshot -- refuses the push."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".auto-import-issues.jsonl").write_text('{"size": 815278}')
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(245),
        },
    )
    assert result.returncode != 0
    assert "auto-import-issues.jsonl" in result.stderr
    assert "REFUSING" in result.stderr


def test_marker_present_but_force_env_allows(tmp_path: Path) -> None:
    """BD_DOLT_PUSH_GUARD_FORCE bypasses the marker check for a deliberate
    override (e.g. disaster recovery)."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".auto-import-issues.jsonl").write_text('{"size": 815278}')
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(245),
            "BD_DOLT_PUSH_GUARD_FORCE": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "FORCE" in result.stderr


# --- Guard: the issue-count high-water-mark check ---------------------------


def test_count_wildly_below_cache_refuses(tmp_path: Path) -> None:
    """The real incident's shape: current count is far below what this same
    DB path had at its last successfully-recorded push."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".bd-dolt-push-guard-highwater").write_text("404")
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(245),
        },
    )
    assert result.returncode != 0
    assert "REFUSING" in result.stderr
    assert "245" in result.stderr
    assert "404" in result.stderr


def test_count_within_threshold_allows(tmp_path: Path) -> None:
    """A small, ordinary fluctuation (e.g. a handful of closes) must not
    trip the guard -- 380/400 = 95%, above the default 90% floor."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".bd-dolt-push-guard-highwater").write_text("400")
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(380),
        },
    )
    assert result.returncode == 0, result.stderr


def test_count_exactly_at_threshold_allows(tmp_path: Path) -> None:
    """Boundary: exactly the ratio floor must not refuse (strict '<', not
    '<=', per the guard's own comparison)."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".bd-dolt-push-guard-highwater").write_text("100")
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(90),
        },
    )
    assert result.returncode == 0, result.stderr


def test_no_cache_file_skips_count_check_even_when_low(tmp_path: Path) -> None:
    """Failure mode #1 from the ticket: a legitimately fresh/low-count DB
    (e.g. right after `bd init`) must not be blocked just because it has a
    small count -- there is no cache file yet, so there is no baseline to
    call it 'suspicious' against."""
    db_dir = _db_dir(tmp_path)
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(1),
        },
    )
    assert result.returncode == 0, result.stderr


def test_custom_ratio_threshold_env_var(tmp_path: Path) -> None:
    """BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT is honored: a drop that passes the
    default 90% floor can still be refused under a stricter custom floor."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".bd-dolt-push-guard-highwater").write_text("400")
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(380),  # 95% -- passes default 90%
            "BD_DOLT_PUSH_GUARD_MIN_RATIO_PCT": "99",
        },
    )
    assert result.returncode != 0
    assert "REFUSING" in result.stderr


def test_count_check_force_env_allows(tmp_path: Path) -> None:
    """BD_DOLT_PUSH_GUARD_FORCE also bypasses the count check."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".bd-dolt-push-guard-highwater").write_text("404")
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(1),
            "BD_DOLT_PUSH_GUARD_FORCE": "1",
        },
    )
    assert result.returncode == 0, result.stderr


# --- Guard: fail-open on bd introspection failure ---------------------------


def test_bd_where_failure_fails_open(tmp_path: Path) -> None:
    """If `bd where --json` itself cannot be read, the guard must not block
    -- the real `bd dolt push` will surface the underlying bd failure."""
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_EXIT": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "not blocking" in result.stderr


def test_bd_count_failure_fails_open(tmp_path: Path) -> None:
    """Same fail-open behavior when `bd count --json` fails, even though
    `bd where` succeeded and the marker check already passed."""
    db_dir = _db_dir(tmp_path)
    result = _run_guard(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_EXIT": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "not blocking" in result.stderr


# --- Integration: scripts/bd-dolt-push.sh calls the guard first ------------


def test_push_wrapper_blocks_when_guard_refuses(tmp_path: Path) -> None:
    """The wrapper must never call `bd dolt push` at all when the guard
    refuses -- this is the actual publish-time chokepoint the ticket is
    about, so a stray/suspicious DB's push attempt must be stopped before
    any real `bd dolt push` subprocess runs."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".auto-import-issues.jsonl").write_text('{"size": 1}')
    result, call_log = _run_push(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(245),
        },
    )
    assert result.returncode != 0
    assert call_log.read_text() == "", (
        "bd dolt push must never be invoked once the guard has refused"
    )


def test_push_wrapper_records_highwater_on_success(tmp_path: Path) -> None:
    """A real successful push must update the high-water-mark cache file at
    the resolved DB path, so the guard has a baseline for next time."""
    db_dir = _db_dir(tmp_path)
    result, call_log = _run_push(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(407),
        },
    )
    assert result.returncode == 0, result.stderr
    assert call_log.read_text().strip() == "push-called"
    cache_file = db_dir / ".bd-dolt-push-guard-highwater"
    assert cache_file.exists()
    assert cache_file.read_text().strip() == "407"


def test_push_wrapper_force_env_bypasses_guard(tmp_path: Path) -> None:
    """BD_DOLT_PUSH_GUARD_FORCE, set on the wrapper's own invocation,
    propagates to the guard subprocess and lets a real push through."""
    db_dir = _db_dir(tmp_path)
    (db_dir / ".auto-import-issues.jsonl").write_text('{"size": 1}')
    result, call_log = _run_push(
        tmp_path,
        env_overrides={
            "BD_FAKE_WHERE_JSON": _where_json(db_dir),
            "BD_FAKE_COUNT_JSON": _count_json(245),
            "BD_DOLT_PUSH_GUARD_FORCE": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert call_log.read_text().strip() == "push-called"
