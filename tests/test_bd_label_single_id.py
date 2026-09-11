"""Tests for scripts/bd-label-single-id.sh (lode-ayfm).

The resolve-single-issue-by-reserved-label query and its exit 0/1/2 refusal
contract used to be a near line-for-line duplicate between
scripts/sweep-digest-id.sh (lode-x495) and scripts/bd-docs-nit.sh's
resolve_one_collector() (lode-y86u): same `bd list --label L --limit 0
--json`, same jq length, same -ne 1 refusal with a 0-vs-2+ diagnostic, same
`jq -r '.[0].id'`. This is the one script that now owns that recipe; both
callers wrap it with their own label, `--all`-or-not, and per-label advisory
wording. Same fake-`bd`-on-PATH pattern as tests/test_sweep_digest_id.py and
tests/test_bd_docs_nit.py, which now exercise this script indirectly through
their own callers -- this module pins the shared script's own contract
directly, including the pieces neither caller alone exercises (e.g. an
open-only query with no advisory at all).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bd-label-single-id.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _run(
    tmp_path: Path, args: list[str], rows: object, *, list_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run the script with a fake `bd` on PATH that serves `rows` as the JSON
    body of whatever `bd list --label ... --limit 0 --json [--all]` it runs,
    or fails the list with `list_exit` when nonzero."""
    payload = tmp_path / "rows.json"
    payload.write_text(json.dumps(rows) if rows is not None else "null")

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            [ "$1" = "list" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            if [ {list_exit} -ne 0 ]; then
              exit {list_exit}
            fi
            cat {payload}
        """)
    )
    fake_bd.chmod(0o755)

    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )


def test_one_match_prints_the_id(tmp_path: Path) -> None:
    r = _run(tmp_path, ["some-label"], [{"id": "lode-abc1", "title": "x"}])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "lode-abc1"


def test_no_match_refuses_with_exit_1_and_no_advisory_by_default(
    tmp_path: Path,
) -> None:
    r = _run(tmp_path, ["some-label"], [])
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 0" in r.stderr


def test_null_result_set_is_treated_as_empty(tmp_path: Path) -> None:
    r = _run(tmp_path, ["some-label"], None)
    assert r.returncode == 1, r.stderr
    assert "found 0" in r.stderr


def test_duplicate_matches_refuse_and_never_pick_one(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        ["some-label"],
        [
            {"id": "lode-dupA", "title": "one"},
            {"id": "lode-dupB", "title": "two"},
        ],
    )
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 2" in r.stderr
    assert "lode-dupA" in r.stderr
    assert "lode-dupB" in r.stderr


def test_zero_advisory_prints_on_the_n_eq_0_path_only(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        ["some-label", "--zero-advisory", "the zero case advisory line"],
        [],
    )
    assert r.returncode == 1
    assert "the zero case advisory line" in r.stderr


def test_dup_advisory_prints_before_the_id_list_on_n_gt_1(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        ["some-label", "--dup-advisory", "the dup case advisory line"],
        [{"id": "lode-dupA", "title": "one"}, {"id": "lode-dupB", "title": "two"}],
    )
    assert r.returncode == 1
    assert "the dup case advisory line" in r.stderr
    advisory_pos = r.stderr.index("the dup case advisory line")
    id_pos = r.stderr.index("lode-dupA")
    assert advisory_pos < id_pos


def test_zero_advisory_does_not_leak_onto_the_dup_path(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        ["some-label", "--zero-advisory", "zero-only text"],
        [{"id": "lode-dupA", "title": "one"}, {"id": "lode-dupB", "title": "two"}],
    )
    assert r.returncode == 1
    assert "zero-only text" not in r.stderr


def test_no_label_is_exit_2(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    r = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    assert r.returncode == 2
    assert r.stdout == ""


def test_unknown_argument_is_exit_2(tmp_path: Path) -> None:
    r = _run(tmp_path, ["some-label", "--bogus"], [])
    assert r.returncode == 2
    assert r.stdout == ""


def test_advisory_flag_with_no_value_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    """A trailing flag missing its value is a machine fault, not the `set -u`
    unbound-variable death that would otherwise exit 1 -- exit 1 is reserved
    for "not exactly one match", so a typo'd invocation must not read to the
    caller as a legitimate empty/duplicate state."""
    r = _run(tmp_path, ["some-label", "--zero-advisory"], [])
    assert r.returncode == 2
    assert r.stdout == ""
    assert "requires a value" in r.stderr


def test_bd_failure_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    r = _run(tmp_path, ["some-label"], [], list_exit=3)
    assert r.returncode == 2
    assert r.stdout == ""


def test_all_flag_is_forwarded_to_bd_list(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    call_log = bin_dir / "call.log"
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> {call_log}
            echo '[{{"id": "lode-abc1", "title": "x"}}]'
        """)
    )
    fake_bd.chmod(0o755)
    r = subprocess.run(
        [str(SCRIPT), "some-label", "--all"],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    call = call_log.read_text().strip()
    assert "--label some-label" in call
    assert "--limit 0" in call
    assert "--all" in call


def test_without_all_flag_bd_list_omits_all(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    call_log = bin_dir / "call.log"
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> {call_log}
            echo '[{{"id": "lode-abc1", "title": "x"}}]'
        """)
    )
    fake_bd.chmod(0o755)
    r = subprocess.run(
        [str(SCRIPT), "some-label"],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    call = call_log.read_text().strip()
    assert "--all" not in call
