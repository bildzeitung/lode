"""Tests for scripts/update-deps.sh's promote-BEFORE-gating order (lode-2zi9).

update-deps.sh used to run its gates (``nox -t fix``, ``nox -s tests``) with the
CANDIDATE lock installed into ``./.venv`` but the OLD committed lock still on
disk, promoting the candidate over the committed lock only after the gates passed.
Any test that reads the committed lock from disk (e.g.
``tests/test_build_docs_site.py::test_workflow_pins_match_their_sources``) therefore
gated against the pre-update pins and could pass inside the script while going red on
the very next run against the newly-promoted lock (observed 2026-09-08: a full-lock
recompile moved typer 0.27.1 -> 0.27.2 and shipped exactly this way).

The fix promotes the candidate over ``uv.lock`` BEFORE the gates run, and
restores the saved committed lock byte-for-byte on any failure. This module drives
the ACTUAL script end to end -- not a reimplementation of its logic -- against a
throwaway fake repo whose lock/install/gate steps are stubbed out, covering every
way out of the promote..verdict window: the candidate is on disk while the gate runs;
it stays there on a green gate; and the original comes back byte-for-byte on a red
gate, on a candidate that will not install, and on a signal that kills the script
mid-gate before it can reach its own failure path at all.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import fake_bd, fake_bin_env, uv_lock_package

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_DEPS_SRC = REPO_ROOT / "scripts" / "update-deps.sh"
DEP_CHURN_LIB_SRC = REPO_ROOT / "scripts" / "dep-churn-lib.sh"


def _lock_text(version: str, digit: str) -> str:
    return (
        f'[[package]]\nname = "mypkg"\nversion = "{version}"\n'
        f'sdist = {{ hash = "sha256:{digit * 64}" }}\n'
    )


ORIGINAL_LOCK = _lock_text("1.0.0", "0")
CANDIDATE_LOCK = _lock_text("2.0.0", "1")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _build_fake_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A throwaway repo tree that runs the REAL update-deps.sh against stubbed
    lock/install/gate steps, so this test drives the actual promote-vs-gate
    ordering rather than reimplementing it.

    Returns ``(repo_dir, bin_dir)`` -- ``bin_dir`` must lead PATH so the stub
    ``uv`` and ``nox`` executables shadow the real ones.
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    (repo / "uv.lock").write_text(ORIGINAL_LOCK, encoding="utf-8")

    # The real script under test, and its real (pure-parsing, side-effect-free)
    # dep-churn-lib.sh dependency, copied in unmodified -- copy2 so the copies
    # keep the originals' modes rather than a mode restated here.
    shutil.copy2(UPDATE_DEPS_SRC, scripts / "update-deps.sh")
    shutil.copy2(DEP_CHURN_LIB_SRC, scripts / "dep-churn-lib.sh")

    # PATH stubs: `uv lock ...` writes a fixed candidate lock over uv.lock instead
    # of resolving anything from PyPI, recording its arguments and the
    # UV_EXCLUDE_NEWER it was handed. It exits $UV_LOCK_EXIT, except that the
    # flagless `uv lock -q` exits $UV_PLAIN_LOCK_EXIT when set, so the
    # cutoff-normalising pass can fail after a successful upgrade pass.
    # `uv sync --frozen` becomes a plain mkdir that exits $INSTALL_EXIT (never a
    # real venv, never a real install; $INSTALL_EXIT makes the "failed candidate
    # install" arm of the rollback reachable as well as the red-gate arm -- both
    # restore the lock, through distinct branches), `uv run --frozen --no-sync
    # nox ...` hands off to the `nox` stub, and `nox` snapshots uv.lock mid-gate
    # before either exiting per $NOX_TESTS_EXIT (the stubbed gate result) or,
    # under $NOX_TESTS_SIGNAL_PARENT, killing the script where a Ctrl-C would.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "candidate.lock").write_text(CANDIDATE_LOCK, encoding="utf-8")
    _write_executable(
        bindir / "uv",
        f"""#!/bin/bash
if [ "$1" = "lock" ]; then
  echo "$*" >> "{tmp_path / "uv_lock_args"}"
  echo "${{UV_EXCLUDE_NEWER:-unset}}" >> "{tmp_path / "uv_lock_env"}"
  cp -f "{tmp_path / "candidate.lock"}" uv.lock
  if [ "$*" = "lock -q" ]; then
    exit "${{UV_PLAIN_LOCK_EXIT:-${{UV_LOCK_EXIT:-0}}}}"
  fi
  exit "${{UV_LOCK_EXIT:-0}}"
fi
if [ "$1 $2" = "sync --frozen" ]; then
  mkdir -p .venv/bin
  exit "${{INSTALL_EXIT:-0}}"
fi
if [ "$1 $2 $3 $4" = "run --frozen --no-sync nox" ]; then
  shift 4
  exec "$(dirname "$0")/nox" "$@"
fi
exit 1
""",
    )
    _write_executable(
        bindir / "nox",
        f"""#!/bin/bash
cp -f uv.lock "{tmp_path / "lock_during_gate"}"
if [ "$1" = "-t" ] && [ "$2" = "fix" ]; then
  exit 0
fi
if [ "$1" = "-s" ] && [ "$2" = "tests" ]; then
  if [ "${{NOX_TESTS_SIGNAL_PARENT:-0}}" = "1" ]; then
    # Stand in for a terminal Ctrl-C, which reaches BOTH the gate process and the
    # script waiting on it. Signalling only the parent is not enough: bash exits
    # on a deferred SIGINT only when its foreground child ALSO died from that
    # signal, so a stub that returns 0 here would have the shell swallow it and
    # carry on -- which is not the hazard being tested.
    kill -INT "$PPID"
    kill -INT $$
  fi
  exit "${{NOX_TESTS_EXIT:-0}}"
fi
exit 0
""",
    )
    return repo, bindir


def _lock_calls(tmp_path: Path) -> list[str]:
    """Every `uv lock` argument line the stub recorded, in call order."""
    return (tmp_path / "uv_lock_args").read_text(encoding="utf-8").splitlines()


def _run_update_deps(
    repo: Path,
    bindir: Path,
    *,
    nox_tests_exit: int = 0,
    install_exit: int = 0,
    signal_parent: bool = False,
    uv_lock_exit: int = 0,
    via_bash: bool = False,
    extra_args: tuple[str, ...] = ("--no-file",),
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = fake_bin_env(bindir)
    env.update(
        NOX_TESTS_EXIT=str(nox_tests_exit),
        INSTALL_EXIT=str(install_exit),
        NOX_TESTS_SIGNAL_PARENT="1" if signal_parent else "0",
        UV_LOCK_EXIT=str(uv_lock_exit),
        **(extra_env or {}),
    )
    # via_bash: `bash script` ignores the shebang's -e.
    prefix = ["bash"] if via_bash else []
    return subprocess.run(
        [*prefix, str(repo / "scripts" / "update-deps.sh"), *extra_args],
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

    # (a) uv.lock held the candidate while the gate ran -- captured by
    # the stub `nox` the moment it was invoked, which is AFTER the promote step
    # and BEFORE the (stubbed) red `nox -s tests` result.
    lock_during_gate = (tmp_path / "lock_during_gate").read_text(encoding="utf-8")
    assert lock_during_gate == CANDIDATE_LOCK

    # (b) the committed lock is back to the original, byte-for-byte, after the
    # red gate triggered the rollback.
    lock_after = (repo / "uv.lock").read_text(encoding="utf-8")
    assert lock_after == ORIGINAL_LOCK

    assert "restored" in result.stdout
    assert "full lock upgrade, 7 days release-age cooldown" in result.stdout


def test_lock_stays_promoted_after_a_green_gate(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, nox_tests_exit=0)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "uv.lock").read_text(encoding="utf-8") == CANDIDATE_LOCK


def test_failed_candidate_install_also_restores_the_lock(tmp_path: Path) -> None:
    """The other arm of acceptance criterion 2: a candidate that will not even
    install must restore the lock exactly as a red gate does. It reaches the
    rollback through ``rebuild_venv`` rather than through the gate branch -- and
    since this stub install fails every time, the rollback's own rebuild fails
    too, so this also pins that the report survives that second failure.
    """
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, install_exit=1)

    assert result.returncode == 1, result.stdout + result.stderr
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK
    assert "candidate install" in result.stdout
    assert "FAILURE REPORT" in result.stdout
    assert "ALSO failed" in result.stderr


def test_interrupt_between_promotion_and_verdict_restores_the_lock(
    tmp_path: Path,
) -> None:
    """A death in the promote..verdict window must not strand an UNGATED lock on
    disk looking exactly like a gated one -- the shipping-an-ungated-lock failure
    this whole ordering exists to prevent. The stub gate signals the script
    mid-gate, standing in for a Ctrl-C during the real multi-minute ``nox -s
    tests``; only the EXIT trap can cover this, since the handled-failure path
    never runs.
    """
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, signal_parent=True)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "FAILURE REPORT" not in result.stdout
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK


def test_prints_one_name_old_to_new_line_per_moved_package(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERSION DIFF" in result.stdout
    diff_lines = [
        ln for ln in result.stdout.splitlines() if ln.startswith("    ") and "->" in ln
    ]
    assert len(diff_lines) == 1
    assert diff_lines[0].split() == ["mypkg", "1.0.0", "->", "2.0.0"]
    assert _lock_calls(tmp_path) == [
        "lock -q --upgrade --exclude-newer 7 days",
        "lock -q",
    ]


def test_package_flag_moves_only_that_package(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, extra_args=("--package", "mypkg", "--no-file")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_calls(tmp_path) == [
        "lock -q --upgrade-package mypkg --exclude-newer 7 days",
        "lock -q",
    ]


def test_dry_run_prints_the_diff_and_leaves_the_lock_untouched(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, extra_args=("--dry-run",))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1.0.0 -> 2.0.0" in result.stdout
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK
    assert not (tmp_path / "lock_during_gate").exists()  # no gate ran


def test_failed_uv_lock_exits_nonzero_and_restores_the_lock(tmp_path: Path) -> None:
    """Run as ``bash update-deps.sh``, which drops the shebang's ``-e``: a failed
    ``uv lock`` must still stop the run red, not fall through to a green "update"
    -- and a lock it half-wrote is restored."""
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, uv_lock_exit=2, via_bash=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "uv lock failed" in result.stderr
    assert "--no-cooldown" in result.stderr  # the cutoff was in force: name the way out
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK
    assert not (tmp_path / "lock_during_gate").exists()  # no gate ran


def test_failed_cutoff_normalising_pass_restores_the_lock(tmp_path: Path) -> None:
    """The upgrade pass succeeded, so uv.lock holds a candidate that still records
    the cutoff; a failed second pass must not leave it there, ungated."""
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, via_bash=True, extra_env={"UV_PLAIN_LOCK_EXIT": "1"}
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cutoff-normalising pass" in result.stderr
    assert len(_lock_calls(tmp_path)) == 2
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK
    assert not (tmp_path / "lock_during_gate").exists()  # no gate ran


def test_reconcile_runs_plain_uv_lock_through_the_gate_path(tmp_path: Path) -> None:
    """--reconcile re-locks WITHOUT any upgrade flag, and still promotes the
    candidate before the gate and keeps it on green."""
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, extra_args=("--reconcile", "--no-file"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_calls(tmp_path) == ["lock -q"]
    assert (tmp_path / "lock_during_gate").read_text(encoding="utf-8") == CANDIDATE_LOCK
    assert (repo / "uv.lock").read_text(encoding="utf-8") == CANDIDATE_LOCK


def test_reconcile_restores_the_lock_after_a_red_gate(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, nox_tests_exit=1, extra_args=("--reconcile", "--no-file")
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "no-upgrade reconcile" in result.stdout
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK


# The original lock plus one new package: a pure addition, no existing pin moved.
ADDITION_LOCK = ORIGINAL_LOCK + ORIGINAL_LOCK.replace("mypkg", "fresh")


@pytest.mark.parametrize(
    ("args", "candidate", "files"),
    [
        (("--reconcile",), ADDITION_LOCK, False),
        (("--reconcile",), CANDIDATE_LOCK, True),  # mypkg 1.0.0 -> 2.0.0
        ((), ADDITION_LOCK, True),  # the upgrade modes still file an addition
    ],
)
def test_reconcile_files_the_churn_stub_only_for_a_moved_existing_pin(
    tmp_path: Path, args: tuple[str, ...], candidate: str, files: bool
) -> None:
    """The filing decision as update-deps.sh itself reaches it (lode-7qo1). The
    skip policy is unit-tested in test_dep_churn_lib.py; this covers the mode
    flag actually arriving there. `bd` is stubbed, so nothing is ever filed."""
    repo, bindir = _build_fake_repo(tmp_path)
    (tmp_path / "candidate.lock").write_text(candidate, encoding="utf-8")
    bd_calls = tmp_path / "bd_calls"
    fake_bd(bindir, {"create": "echo lode-stub"}, call_log=bd_calls)

    result = _run_update_deps(repo, bindir, extra_args=args)

    assert result.returncode == 0, result.stdout + result.stderr
    if files:
        assert bd_calls.read_text(encoding="utf-8").startswith("create --title=")
    else:
        assert not bd_calls.exists()
        assert "reconcile moved no existing pin" in result.stdout


def test_reconcile_and_package_are_mutually_exclusive(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, extra_args=("--reconcile", "--package", "x", "--no-file")
    )

    assert result.returncode == 1
    assert "mutually exclusive" in result.stderr
    assert not (tmp_path / "uv_lock_args").exists()
    assert (repo / "uv.lock").read_text(encoding="utf-8") == ORIGINAL_LOCK


def test_no_cooldown_drops_the_cutoff_from_an_upgrade(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(repo, bindir, extra_args=("--no-cooldown", "--no-file"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _lock_calls(tmp_path) == ["lock -q --upgrade"]


def test_reconcile_ignores_a_cutoff_in_the_environment(tmp_path: Path) -> None:
    """UV_EXCLUDE_NEWER would reach reconcile's plain `uv lock`; the script unsets it."""
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo,
        bindir,
        extra_args=("--reconcile", "--no-file"),
        extra_env={"UV_EXCLUDE_NEWER": "2020-01-01"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "uv_lock_env").read_text(encoding="utf-8").split() == ["unset"]


def test_no_cooldown_and_reconcile_are_mutually_exclusive(tmp_path: Path) -> None:
    repo, bindir = _build_fake_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, extra_args=("--reconcile", "--no-cooldown", "--no-file")
    )

    assert result.returncode == 1
    assert "--no-cooldown" in result.stderr
    assert not (tmp_path / "uv_lock_args").exists()


_YOUNG_PIN = uv_lock_package("young", "2.0.0", "2999-01-01T00:00:00.000Z")
_OLD_PIN = uv_lock_package("old", "1.0.0", "2000-01-01T00:00:00.000Z")


@pytest.mark.parametrize("extra_args", [(), ("--package", "old")])
def test_cooled_down_run_exempts_only_pins_younger_than_the_cutoff(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    """lode-hdob: a pin younger than the cutoff gets its own cutoff, the upload
    time of its locked release, so the global cutoff cannot move it back; an
    older pin gets none. Which pins qualify is tested in test_dep_churn_lib.py;
    this is the wiring, and the direction of the script's own cutoff."""
    repo, bindir = _build_fake_repo(tmp_path)
    (repo / "uv.lock").write_text(_YOUNG_PIN + _OLD_PIN, encoding="utf-8")

    result = _run_update_deps(repo, bindir, extra_args=(*extra_args, "--no-file"))

    assert result.returncode == 0, result.stdout + result.stderr
    first_call = _lock_calls(tmp_path)[0]
    assert "--exclude-newer 7 days" in first_call
    assert "--exclude-newer-package young=2999-01-01T00:00:00.000Z" in first_call
    assert first_call.count("--exclude-newer-package") == 1


@pytest.mark.parametrize("extra_args", [("--no-cooldown",), ("--reconcile",)])
def test_no_exemptions_without_a_cutoff(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    repo, bindir = _build_fake_repo(tmp_path)
    (repo / "uv.lock").write_text(_YOUNG_PIN, encoding="utf-8")

    result = _run_update_deps(repo, bindir, extra_args=(*extra_args, "--no-file"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--exclude-newer-package" not in " ".join(_lock_calls(tmp_path))


def test_a_run_that_cannot_read_its_exemptions_locks_nothing(tmp_path: Path) -> None:
    """Carrying on with no exemptions would move every young pin back and exit
    green, so the run stops before ``uv lock``."""
    repo, bindir = _build_fake_repo(tmp_path)
    (repo / "uv.lock").write_text(_YOUNG_PIN, encoding="utf-8")
    _write_executable(bindir / "awk", "#!/bin/bash\nexit 1\n")

    result = _run_update_deps(repo, bindir)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "could not read the pins younger than the cooldown cutoff" in result.stderr
    assert "died after moving the lock" not in result.stderr
    assert not (tmp_path / "uv_lock_args").exists()
    assert (repo / "uv.lock").read_text(encoding="utf-8") == _YOUNG_PIN


def _young_pin_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A fake repo whose locked ``mypkg`` is younger than the cutoff, so the
    cooldown exempts it; the stub ``uv lock`` then moves it 1.0.0 -> 2.0.0
    (lode-amyi)."""
    repo, bindir = _build_fake_repo(tmp_path)
    (repo / "uv.lock").write_text(
        uv_lock_package("mypkg", "1.0.0", "2999-01-01T00:00:00.000Z"),
        encoding="utf-8",
    )
    return repo, bindir


@pytest.mark.parametrize("extra_args", [("--no-file",), ("--dry-run",)])
def test_a_moved_exempted_pin_is_refused_and_the_lock_restored(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    repo, bindir = _young_pin_repo(tmp_path)
    original = (repo / "uv.lock").read_text(encoding="utf-8")

    result = _run_update_deps(repo, bindir, extra_args=extra_args)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "REFUSED" in result.stderr
    assert "mypkg  1.0.0 -> 2.0.0" in result.stderr
    assert "died after moving the lock" not in result.stderr
    assert (repo / "uv.lock").read_text(encoding="utf-8") == original
    assert not (repo / ".venv").exists()  # nothing was installed
    assert not (tmp_path / "lock_during_gate").exists()  # no gate ran


def test_allow_exempt_move_lets_the_run_proceed(tmp_path: Path) -> None:
    repo, bindir = _young_pin_repo(tmp_path)

    result = _run_update_deps(
        repo, bindir, extra_args=("--allow-exempt-move", "--no-file")
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stderr
    assert "mypkg  1.0.0 -> 2.0.0" in result.stderr  # named, not only in the diff
    assert (repo / "uv.lock").read_text(encoding="utf-8") == CANDIDATE_LOCK


def test_an_unmoved_exempted_pin_passes(tmp_path: Path) -> None:
    repo, bindir = _young_pin_repo(tmp_path)
    (tmp_path / "candidate.lock").write_text(
        (repo / "uv.lock").read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = _run_update_deps(repo, bindir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSED" not in result.stderr
    assert "--allow-exempt-move" not in result.stderr
