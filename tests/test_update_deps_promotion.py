"""Tests for scripts/update-deps.sh's promote-BEFORE-gating order (lode-2zi9).

update-deps.sh used to run its gates (``nox -t fix``, ``nox -s tests``) with the
CANDIDATE lock installed into ``./venv`` but the OLD ``requirements.lock`` still on
disk, promoting the candidate over the committed lock only after the gates passed.
Any test that reads the committed lock from disk (e.g.
``tests/test_build_docs_site.py::test_workflow_pins_match_their_sources``) therefore
gated against the pre-update pins and could pass inside the script while going red on
the very next run against the newly-promoted lock (observed 2026-09-08: a full-lock
recompile moved typer 0.27.1 -> 0.27.2 and shipped exactly this way).

The fix promotes the candidate over ``requirements.lock`` BEFORE the gates run, and
restores the saved committed lock byte-for-byte on any failure (a red gate, or a
failed candidate install). This module drives the ACTUAL script end to end -- not a
reimplementation of its logic -- against a throwaway fake repo whose compile/install/
gate steps are stubbed out, and asserts both directions: the candidate is on disk
while the gate runs, and the original is back on disk after a red gate.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_DEPS_SRC = REPO_ROOT / "scripts" / "update-deps.sh"
DEP_CHURN_LIB_SRC = REPO_ROOT / "scripts" / "dep-churn-lib.sh"

ORIGINAL_LOCK = "mypkg==1.0.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
CANDIDATE_LOCK = "mypkg==2.0.0 \\\n    --hash=sha256:" + "1" * 64 + "\n"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _build_fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo tree that runs the REAL update-deps.sh against stubbed
    compile/install/gate steps, so this test drives the actual promote-vs-gate
    ordering rather than reimplementing it.

    Returns ``(repo_dir, bin_dir)`` -- ``bin_dir`` must lead PATH so the stub
    ``python`` and ``nox`` executables shadow the real ones.
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (repo / "requirements.lock").write_text(ORIGINAL_LOCK, encoding="utf-8")

    # The real script under test, and its real (pure-parsing, side-effect-free)
    # dep-churn-lib.sh dependency, copied in unmodified.
    (scripts / "update-deps.sh").write_text(
        UPDATE_DEPS_SRC.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (scripts / "update-deps.sh").chmod(0o755)
    (scripts / "dep-churn-lib.sh").write_text(
        DEP_CHURN_LIB_SRC.read_text(encoding="utf-8"), encoding="utf-8"
    )

    # Stub compile-lock.sh: writes a fixed candidate lock to whatever -o PATH it
    # was given, instead of resolving anything from PyPI.
    _write_executable(
        scripts / "compile-lock.sh",
        f"""#!/bin/bash
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
cat > "$out" <<'LOCK_EOF'
{CANDIDATE_LOCK}LOCK_EOF
""",
    )

    # Stub venv-install.sh: no real installs -- always succeeds, since this
    # test's target is the lock-promotion ordering, not the install step.
    _write_executable(
        scripts / "venv-install.sh",
        """#!/bin/bash
install_locked_venv() {
  return 0
}
""",
    )

    # A pre-existing "venv" satisfying update-deps.sh's own precondition check.
    venv_bin = repo / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("deactivate() { :; }\n", encoding="utf-8")

    # PATH stubs: `python -m venv DIR` becomes a plain mkdir (never a real venv,
    # never a real install), and `nox` snapshots requirements.lock mid-gate, then
    # exits according to $NOX_TESTS_EXIT -- the stubbed gate result.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_executable(
        bindir / "python",
        """#!/bin/bash
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  printf 'deactivate() { :; }\\n' > "$3/bin/activate"
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        bindir / "nox",
        f"""#!/bin/bash
cp requirements.lock "{tmp_path / "lock_during_gate"}"
if [ "$1" = "-t" ] && [ "$2" = "fix" ]; then
  exit 0
fi
if [ "$1" = "-s" ] && [ "$2" = "tests" ]; then
  exit "${{NOX_TESTS_EXIT:-0}}"
fi
exit 0
""",
    )
    return repo, bindir


def _run_update_deps(
    repo: Path, bindir: Path, *, nox_tests_exit: int
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["NOX_TESTS_EXIT"] = str(nox_tests_exit)
    return subprocess.run(
        [str(repo / "scripts" / "update-deps.sh"), "--no-file"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_candidate_on_disk_during_gate_and_original_restored_after_red_gate(
    tmp_path: Path,
) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, nox_tests_exit=1)

    assert result.returncode == 1, result.stdout + result.stderr

    # (a) requirements.lock held the candidate while the gate ran -- captured by
    # the stub `nox` the moment it was invoked, which is AFTER the promote step
    # and BEFORE the (stubbed) red `nox -s tests` result.
    lock_during_gate = (tmp_path / "lock_during_gate").read_text(encoding="utf-8")
    assert lock_during_gate == CANDIDATE_LOCK

    # (b) the committed lock is back to the original, byte-for-byte, after the
    # red gate triggered the rollback.
    lock_after = (repo / "requirements.lock").read_text(encoding="utf-8")
    assert lock_after == ORIGINAL_LOCK

    assert "restored" in result.stdout


def test_lock_stays_promoted_after_a_green_gate(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, nox_tests_exit=0)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "requirements.lock").read_text(encoding="utf-8") == CANDIDATE_LOCK
