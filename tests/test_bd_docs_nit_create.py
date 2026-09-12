"""Tests for scripts/bd-docs-nit-create.sh (lode-z1n5 part 1).

bd's on-create validation is warn-only (.beads/config.yaml
validation.on-create: warn) -- a `bd create` missing required sections STILL
CREATES and only warns; a caller reading a downstream pipe's non-zero exit as
"not created" will retry and duplicate (lode-wm4l). This script instead writes
--json to a file (never a pipe) and confirms with a follow-up `bd list`. Same
fake-`bd`-on-PATH pattern as the rest of this family.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fake_bd, fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bd-docs-nit-create.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)

STANDARD_TITLE = "Docs wording nits: batch fix in the next docs pass"


def _fake_bd(
    bin_dir: Path,
    *,
    create_id: str = "lode-newcol",
    create_ok: bool = True,
    create_json: str | None = None,
    confirm_rows: object = "__default__",
    confirm_exit: int = 0,
) -> Path:
    create_log = bin_dir / "create.log"
    create_exit = "0" if create_ok else "1"
    if create_json is None:
        create_json_val = json.dumps({"id": create_id})
    else:
        create_json_val = create_json
    payload = bin_dir / "create.json"
    payload.write_text(create_json_val)

    if confirm_rows == "__default__":
        confirm_rows = [{"id": create_id, "title": STANDARD_TITLE}]
    confirm_payload = bin_dir / "confirm.json"
    confirm_payload.write_text(
        json.dumps(confirm_rows) if confirm_rows is not None else "null"
    )

    fake_bd(
        bin_dir,
        {
            "create": f"""
                echo "$*" >> {create_log}
                if [ {create_exit} -ne 0 ]; then
                  exit {create_exit}
                fi
                cat {payload}
            """,
            "list": f"""
                if [ {confirm_exit} -ne 0 ]; then
                  exit {confirm_exit}
                fi
                cat {confirm_payload}
            """,
        },
    )
    return create_log


def _run(
    tmp_path: Path,
    *,
    create_id: str = "lode-newcol",
    create_ok: bool = True,
    create_json: str | None = None,
    confirm_rows: object = "__default__",
    confirm_exit: int = 0,
    dolt_ok: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir()
    script_copy = scratch_scripts / "bd-docs-nit-create.sh"
    script_copy.write_text(SCRIPT.read_text())
    (scratch_scripts / "docs-nits-constants.sh").write_text(
        (REPO_ROOT / "scripts" / "docs-nits-constants.sh").read_text()
    )
    script_copy.chmod(0o755)

    marker = scratch_scripts / "dolt_push_called"
    dolt_script = scratch_scripts / "bd-dolt-push.sh"
    dolt_exit = "0" if dolt_ok else "1"
    dolt_script.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit {dolt_exit}\n")
    dolt_script.chmod(0o755)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    create_log = _fake_bd(
        bin_dir,
        create_id=create_id,
        create_ok=create_ok,
        create_json=create_json,
        confirm_rows=confirm_rows,
        confirm_exit=confirm_exit,
    )

    result = subprocess.run(
        [str(script_copy)],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    return result, marker, create_log


def test_creates_standard_collector_and_prints_its_id(tmp_path: Path) -> None:
    r, dolt_marker, create_log = _run(tmp_path, create_id="lode-newcol")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "lode-newcol"
    assert dolt_marker.exists()

    call = create_log.read_text()
    assert "docs-nits" in call
    assert STANDARD_TITLE in call


def test_create_passes_description_and_acceptance_so_warn_only_never_fires(
    tmp_path: Path,
) -> None:
    _, _, create_log = _run(tmp_path)
    call = create_log.read_text()
    assert "--description" in call
    assert "--acceptance" in call


def test_bd_create_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker, _ = _run(tmp_path, create_ok=False)
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_unparseable_create_json_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker, _ = _run(tmp_path, create_json="not json")
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_confirm_query_finding_nothing_is_exit_2(tmp_path: Path) -> None:
    """bd's on-create validation is warn-only: `bd create`'s own exit status
    alone must never be trusted -- only the follow-up `bd list` confirms."""
    r, dolt_marker, _ = _run(tmp_path, confirm_rows=[])
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_confirm_query_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker, _ = _run(tmp_path, confirm_exit=3)
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_dolt_push_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker, _ = _run(tmp_path, dolt_ok=False)
    assert r.returncode == 2
    assert dolt_marker.exists(), "the push was attempted, just failed"
