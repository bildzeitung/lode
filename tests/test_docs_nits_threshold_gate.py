"""Tests for scripts/docs-nits-threshold-gate.sh (lode-z1n5 part 2).

Shaped like scripts/epic-debate-gate.sh: an any-id gate /code's auto-select
step runs on every candidate that survives the human/epic filter. Prints
BUILD/SKIP; never a bd write.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fake_bd, fake_bin_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "docs-nits-threshold-gate.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the gate shells out to jq"
)


def _run(
    tmp_path: Path,
    id_: str,
    *,
    labels: list[str] | None,
    count_rows: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir()
    real_gate = scratch_scripts / "docs-nits-threshold-gate.sh"
    real_gate.write_text(SCRIPT.read_text())
    real_gate.chmod(0o755)
    # A stub bd-docs-nit.sh sibling -- the gate only ever runs `count` on it,
    # so it need not be the real script, just print the TSV rows a real
    # `count` would.
    stub_docs_nit = scratch_scripts / "bd-docs-nit.sh"
    stub_docs_nit.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{count_rows}EOF\n")
    stub_docs_nit.chmod(0o755)

    show_payload = tmp_path / "show.json"
    show_payload.write_text(json.dumps([{"id": id_, "labels": labels}]))

    bin_dir = tmp_path / "fakebin"
    fake_bd(
        bin_dir,
        {
            "show": f"cat {show_payload}",
        },
    )

    run_env = fake_bin_env(bin_dir)
    if env:
        run_env.update(env)

    return subprocess.run(
        [str(real_gate), id_],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=REPO_ROOT,
        check=False,
    )


def test_non_docs_nits_ticket_always_builds(tmp_path: Path) -> None:
    r = _run(tmp_path, "lode-abc1", labels=["bug"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "BUILD lode-abc1"


def test_docs_nits_ticket_no_labels_field_builds(tmp_path: Path) -> None:
    r = _run(tmp_path, "lode-abc1", labels=None)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "BUILD lode-abc1"


def test_docs_nits_ticket_below_threshold_skips(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "lode-59da",
        labels=["docs-nits", "chore"],
        count_rows="lode-59da\ttitle\t2\n",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "SKIP lode-59da docs-nits below threshold (2/3)"


def test_docs_nits_ticket_at_threshold_builds(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "lode-59da",
        labels=["docs-nits"],
        count_rows="lode-59da\ttitle\t3\n",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "BUILD lode-59da"


def test_docs_nits_ticket_missing_from_count_output_treated_as_zero(
    tmp_path: Path,
) -> None:
    r = _run(tmp_path, "lode-59da", labels=["docs-nits"], count_rows="")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "SKIP lode-59da docs-nits below threshold (0/3)"


def test_threshold_env_var_overrides_default(tmp_path: Path) -> None:
    r = _run(
        tmp_path,
        "lode-59da",
        labels=["docs-nits"],
        count_rows="lode-59da\ttitle\t1\n",
        env={"LODE_DOCS_NITS_THRESHOLD": "1"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "BUILD lode-59da"


def test_usage_with_no_id_is_a_nonzero_exit(tmp_path: Path) -> None:
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
    assert r.returncode != 0
