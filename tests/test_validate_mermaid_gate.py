"""Regression test for scripts/validate-mermaid.sh's engine-down handling
(lode-9i2p, lode-2vsc).

Root cause (lode-9i2p): the old guard was ``command -v docker`` — a PROXY that
only proves some binary named ``docker`` is on PATH. When Docker Desktop's
engine is stopped (Resource Saver mode, or WSL integration switched off for a
distro), the Windows shim left on PATH still satisfies that check, then fails
every ``docker run`` with "The command docker could not be found in this WSL 2
distro" — a message indistinguishable, to a caller, from real per-doc mermaid
syntax failures. A docs-only build (lode-tktc) hit exactly this and handed off
with the gate never having actually parsed the diagram it touched, because the
failure looked like "docker absent" rather than "gate broken."

The fix guards on the INVARIANT instead (``docker info``, i.e. can it reach a
running engine) and exits 2 — distinct from exit 1's "invalid mermaid" — with a
message that names the actual cause and never prints a per-doc ``FAIL`` line.

Both failure shapes are covered here, because ``docker info`` fails identically
whether the engine is stopped or docker was never installed, and blaming
Resource Saver in the second case would just relocate the original bug (a
confident, plausible, false machine-level story):

* engine down — a fake ``docker`` shim on PATH, mirroring the real Windows shim
  (present on PATH, fails at runtime) → exit 2, "docker engine unreachable"
* docker absent — nothing named ``docker`` on PATH at all → exit 2, "no docker
  on PATH", and explicitly *not* the Resource Saver story

Both assert the gate reports "could not run", never "invalid content". Each runs
with a PATH built entirely by the test, so the result never depends on whether
the machine running the suite has a real Docker — in either direction.

lode-2vsc closes the same invariant *inside* the per-doc loop, where the
pre-flight probe cannot reach: ``docker run`` can still fail for tool-level
reasons after the probe passed (image missing with no network, engine dies
mid-run), and that must not print a per-doc ``FAIL`` either. The gate now
partitions on mmdc's ONE content verdict rather than on docker's failure codes:
**exit 1 is the only exit allowed to print FAIL**; every other nonzero exit is
gate-could-not-run. The measured exit codes and the reasoning live in the
comment above the loop in ``scripts/validate-mermaid.sh`` — the one place a
reader debugging a gate failure will look. Not repeated here.

The tests below drive a fake docker whose ``info`` succeeds (so the pre-flight
probe passes) but whose ``run`` fails, across both sides of that partition; the
per-code rationale is inline in the parametrize list.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate-mermaid.sh"


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A PATH dir with no docker of any kind on it.

    It holds only ``dirname`` — the single external binary the script runs
    before it reaches the docker guard (to resolve its own repo root). That
    makes a PATH of exactly this one dir enough to reach the guard, and lets
    the docker-absent case be simulated by simply not putting a docker on it.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    real_dirname = shutil.which("dirname")
    assert real_dirname, "dirname not found — cannot build a hermetic PATH"
    (bin_dir / "dirname").symlink_to(real_dirname)
    return bin_dir


def _add_broken_docker(bin_dir: Path) -> None:
    """A fake docker that's present on PATH but can't reach an engine — the
    same shape as the real Windows shim once Docker Desktop's engine is
    stopped: `command -v docker` succeeds, every actual call fails."""
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'echo "The command docker could not be found in this WSL 2 distro" >&2\n'
        "exit 1\n"
    )
    shim.chmod(0o755)


def _add_docker_with_run_exit(bin_dir: Path, run_exit: int) -> None:
    """A fake docker whose `info` succeeds (engine reachable, so the pre-flight
    probe passes) but whose `run` always exits with a caller-chosen code —
    simulates a TOOL failure *inside* the per-doc loop (any nonzero that isn't
    1) vs mmdc's own content verdict (exactly 1) reaching the loop past the
    pre-flight guard."""
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "info" ]; then exit 0; fi\n'
        f'if [ "$1" = "run" ]; then exit {run_exit}; fi\n'
        "exit 0\n"
    )
    shim.chmod(0o755)


def _run_gate(
    path_dir: Path, *, inherit_path: bool = False
) -> subprocess.CompletedProcess:
    """Run the gate with ``path_dir`` as its PATH.

    Either way, which docker the script calls is decided entirely by what this
    test put in ``path_dir`` — never by whether the machine running the suite
    happens to have a real Docker. Verified in both directions: the suite passes
    with a live engine present AND with every real docker withheld from PATH.

    ``inherit_path=False`` (the pre-flight tests): PATH is *exactly* ``path_dir``,
    so a docker-absent PATH can be simulated by simply not putting one there.
    ``inherit_path=True`` (the in-loop tests): ``path_dir`` is *prepended* to the
    real PATH. Those tests run past the pre-flight guard and need real
    ``mktemp``/``chmod``/``grep``/``rm`` to reach the per-doc loop, which
    ``fake_bin`` (just ``dirname``) doesn't carry. Prepending does not give up
    control of any of them: whichever a test wants faked instead, it puts in
    ``path_dir``, which wins the PATH search because it comes first -- how the
    fake docker beats a real one here, and how the grep tests below beat a real
    grep.
    """
    path = f"{path_dir}:{os.environ.get('PATH', '')}" if inherit_path else str(path_dir)
    return subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        env={**os.environ, "PATH": path},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _assert_gate_could_not_run(
    result: subprocess.CompletedProcess, *, says: str
) -> None:
    """Assert the shared contract of every gate-could-not-run exit: code 2, so
    it can never be read as exit 1's "invalid mermaid"; the GATE COULD NOT RUN
    banner; this gate's own advisory trailer; and — the whole point — not one
    per-doc FAIL line, because a broken tool must never be mistakable for
    broken content.

    The advisory assertion is load-bearing since lode-090f moved that trailer
    out of the (structurally unskippable) function body and into a
    GATE_ADVISORY array, now bound at source time (see gate-lib.sh's
    GATE_ADVISORY contract). This assertion pins the advisory TEXT itself,
    which no structural sweep can see, and covers every one of this script's
    call sites -- stated without a count on purpose, since the count restales
    on every ticket that adds a guard (it read "three" while the script had
    four, then seven).

    REPO= is a call site like any other now (lode-dyq0): gate-lib.sh is
    sourced above it, so its failure routes through gate_could_not_run and
    carries the full advisory trailer same as every other guard in this file.
    The one caller that is NOT a call site is the source guard itself
    (missing/unreadable gate-lib.sh) -- it genuinely cannot call the helper it
    is checking for, so it hardcodes its own exit 2 with a DIFFERENT message
    ("not a branch verdict", not "not a mermaid syntax error"); nothing in
    this suite asserts that one against this helper."""
    assert result.returncode == 2, (
        f"expected exit 2 (gate could not run), got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "GATE COULD NOT RUN" in result.stderr
    assert says in result.stderr
    assert "not a mermaid syntax error" in result.stderr
    assert "FAIL" not in result.stdout
    assert "FAIL" not in result.stderr


def test_engine_down_reports_gate_could_not_run_not_content_failures(fake_bin):
    """docker on PATH but unreachable — the lode-9i2p case. The old
    `command -v docker` guard passed here, ran the loop, and reported every doc
    as FAIL."""
    _add_broken_docker(fake_bin)

    _assert_gate_could_not_run(_run_gate(fake_bin), says="docker engine unreachable")


def test_docker_absent_is_gate_could_not_run_but_says_so_honestly(fake_bin):
    """No docker at all. Still exit 2 — a missing tool is not broken content
    either — but the message must NOT blame Docker Desktop's Resource Saver or
    WSL integration. `docker info` fails identically whether the engine is
    stopped, docker was never installed, or the socket denies permission, so
    telling one confident story for all three would just relocate the bug this
    gate exists to kill."""
    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="no docker on PATH")
    assert "Resource Saver" not in result.stderr


@pytest.mark.parametrize(
    "run_exit",
    [
        125,  # docker couldn't start the container (image missing, no network)
        126,  # contained command not invocable
        127,  # contained command not found
        137,  # 128+SIGKILL: engine killed the container, or an OOM kill
        139,  # 128+SIGSEGV: chromium crashed inside the container
        143,  # 128+SIGTERM: engine asked the container to stop
        3,  # an exit code nobody anticipated — must still fail SAFE
    ],
)
def test_tool_failure_inside_loop_is_gate_could_not_run(fake_bin, run_exit):
    """`docker info` succeeds (pre-flight passes) but `docker run` fails partway
    through the per-doc loop with a TOOL-level code — the lode-2vsc case: the
    engine dies between the pre-flight probe and a later doc, or the image was
    never pulled and the network is down. None of these may be reported as a
    per-doc FAIL; each must abort with the same gate-could-not-run contract as
    the pre-flight guard.

    137/139/143 are the load-bearing cases. The engine dying *mid-run* kills a
    RUNNING container, which exits 128+signal — NOT one of docker's pre-start
    125-127 codes. An allowlist of 125-127 (this ticket's first pass) prints
    FAIL for exactly the flake the ticket was opened about. 3 pins the fail-safe
    direction: an unanticipated code escalates to a human rather than silently
    blaming an innocent doc."""
    _add_docker_with_run_exit(fake_bin, run_exit)

    result = _run_gate(fake_bin, inherit_path=True)

    _assert_gate_could_not_run(result, says="tool failure")
    assert f"exit {run_exit}" in result.stderr


def test_genuine_content_failure_inside_loop_still_reports_per_doc_fail(fake_bin):
    """`docker run` fails with mmdc's own exit code (1) — a real mermaid parse
    failure, not a tool one. This must stay on the exit-1, per-doc-FAIL path.

    This is the guard on the guard: the partition above treats every nonzero
    exit but 1 as a tool failure, so this test is what stops it widening into
    exit 1 and swallowing genuine content failures — which would silently pass
    broken diagrams, the mirror image of the bug this gate exists to kill."""
    _add_docker_with_run_exit(fake_bin, 1)

    result = _run_gate(fake_bin, inherit_path=True)

    assert result.returncode == 1, (
        f"expected exit 1 (invalid mermaid), got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "GATE COULD NOT RUN" not in result.stderr
    assert "FAIL" in result.stdout


# ---------------------------------------------------------------------------
# lode-3xqb: the non-`if` commands the shebang's `-e` could route to exit 1
# (this script's CONTENT verdict) on a pure machine fault, guarded one at a
# time. The mktemp -d guard above was already fixed in lode-bss5's review
# pass; four more are covered here (REPO=, the printf, both chmods), plus the
# EXIT trap, which lode-3xqb's own review found still able to overwrite all of
# them. The remaining deliberately-unguarded `echo`s, and whether -e should
# just be deleted, are argued in the AUDIT comment atop the script (lode-6znq).
# ---------------------------------------------------------------------------


def _add_broken_dirname(bin_dir: Path) -> None:
    """A fake ``dirname`` that behaves correctly on its FIRST call, then
    always reports a path with no existing parent on every call after that.

    gate-lib.sh is now sourced above REPO= (lode-dyq0), and both that source
    line and REPO= itself resolve their path via the identical
    ``dirname "$0"`` -- same binary, same argument -- so a dirname that is
    broken unconditionally would break the SOURCE first (a different failure
    entirely: "gate-lib.sh is missing or unreadable", not "could not resolve
    the repo root"). Answering correctly once, then breaking, isolates REPO='s
    own call: the source line is the first thing in the script that invokes
    dirname, so it gets the real answer and succeeds; REPO= is the second, and
    gets the bogus one."""
    counter = bin_dir / ".dirname-calls"
    real_dirname = shutil.which("dirname")
    assert real_dirname, "dirname not found — cannot build a hermetic PATH"
    shim = bin_dir / "dirname"
    shim.unlink()
    shim.write_text(
        "#!/bin/bash\n"
        f'COUNTER="{counter}"\n'
        # Builtins only -- PATH on this fake bin dir is exactly this one
        # directory (see fake_bin), so no external command (cat, expr, ...)
        # is guaranteed to exist here to read/write the counter.
        "n=0\n"
        '[ -f "$COUNTER" ] && read -r n < "$COUNTER"\n'
        'printf %s "$((n + 1))" > "$COUNTER"\n'
        'if [ "$n" -eq 0 ]; then\n'
        f'  exec "{real_dirname}" "$@"\n'
        "else\n"
        '  echo "/nonexistent-repo-root-for-lode-3xqb-test"\n'
        "fi\n"
    )
    shim.chmod(0o755)


def test_repo_root_resolution_failure_is_gate_could_not_run(fake_bin):
    """REPO= now runs AFTER gate-lib.sh is sourced (lode-dyq0), so its `cd`
    failure routes through gate_could_not_run like every other guard in this
    script, rather than carrying its own hardcoded fallback. Removing the
    guard entirely would let a failing `cd` fall through to -e's own abort
    with the FAILING COMMAND's exit status (bash's own 1 here), which in this
    script means "invalid mermaid" -- exactly the lode-9i2p inversion."""
    _add_broken_dirname(fake_bin)

    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="could not resolve the repo root")


def _add_real_rm(bin_dir: Path) -> None:
    """Symlinks the real ``rm`` into the fake PATH, so the script's own
    ``trap 'rm -rf "$CFG" || :' EXIT`` really removes $CFG and these tests
    don't litter /tmp. Not needed by the REPO= test above: that guard fires
    before the trap is ever registered.

    It used to be load-bearing for a second reason -- a `rm` missing from PATH
    made the trap itself fail, which under -e clobbered the exit status the
    guard had just set. That is now fixed IN THE SCRIPT (the trap's ``|| :``),
    and pinned by test_temp_dir_cleanup_failure_cannot_rewrite_the_exit_code
    below rather than worked around here."""
    real_rm = shutil.which("rm")
    assert real_rm, "rm not found — cannot build a hermetic PATH"
    (bin_dir / "rm").symlink_to(real_rm)


def _add_fixed_readonly_mktemp(bin_dir: Path, fixed_dir: Path) -> None:
    """A fake ``mktemp`` that returns a caller-prepared, already-created
    directory instead of creating a fresh one -- lets a test pre-chmod that
    directory read-only so the printf into it fails deterministically,
    without needing to predict mktemp's randomized suffix or race a
    permission change against script timing."""
    shim = bin_dir / "mktemp"
    shim.write_text(f'#!/bin/bash\necho "{fixed_dir}"\n')
    shim.chmod(0o755)


def test_puppeteer_config_write_failure_is_gate_could_not_run(fake_bin, tmp_path: Path):
    """The printf into $CFG/puppeteer.json: a write failure here (TMPDIR's
    filesystem full, or gone read-only after mktemp created $CFG) must not
    fall through to -e's own exit 1. Docker's pre-flight probe must pass
    (info succeeds) so the script actually reaches this line; `docker run`
    is never invoked since the failure happens before the per-doc loop."""
    _add_docker_with_run_exit(fake_bin, 0)
    _add_real_rm(fake_bin)
    cfg_dir = tmp_path / "readonly-cfg"
    cfg_dir.mkdir()
    cfg_dir.chmod(0o500)  # readable + traversable, not writable
    _add_fixed_readonly_mktemp(fake_bin, cfg_dir)

    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="could not write")
    assert "puppeteer.json" in result.stderr


def test_temp_dir_cleanup_failure_cannot_rewrite_the_exit_code(
    fake_bin, tmp_path: Path
):
    """The EXIT trap is the one command no guard below it can reach, and under
    -e it outranks all of them.

    MEASURED (bash 5.2): when a command fails inside an EXIT trap under -e, the
    shell exits with THAT command's status. So an unguarded ``rm -rf "$CFG"``
    silently rewrites every exit decided below it -- a guard's correct 2 and a
    clean run's 0 alike -- into rm's own 1, which in this script means "invalid
    mermaid". Guarding the individual commands is not the same as guarding the
    SHELL, and this is the route that survived both previous audit passes
    (lode-bss5, then lode-3xqb) because it is spelled as a trap, not a command.

    The fixture is the printf test's, plus one file inside $CFG: that makes
    ``rm -rf`` have something it must unlink from a directory it cannot write,
    so cleanup fails on the very fault the printf guard was written for ($CFG's
    filesystem read-only). Without the trap's ``|| :`` this test sees exit 1.
    """
    _add_docker_with_run_exit(fake_bin, 0)
    _add_real_rm(fake_bin)
    cfg_dir = tmp_path / "readonly-cfg"
    cfg_dir.mkdir()
    (cfg_dir / "occupant").touch()  # rm -rf must have something to fail on
    cfg_dir.chmod(0o500)  # readable + traversable, not writable
    _add_fixed_readonly_mktemp(fake_bin, cfg_dir)

    try:
        result = _run_gate(fake_bin)
    finally:
        cfg_dir.chmod(0o700)  # let pytest reclaim tmp_path

    # Self-check: if the fixture ever stops making cleanup fail, this test
    # would pass for a reason unrelated to the guard it exists to pin.
    assert "rm: cannot remove" in result.stderr, (
        f"fixture no longer makes the EXIT trap's rm fail; the assertion below "
        f"would be vacuous\nstderr={result.stderr!r}"
    )
    _assert_gate_could_not_run(result, says="could not write")


def _add_chmod_that_fails_on_mode(bin_dir: Path, fail_mode: str) -> None:
    """A fake ``chmod`` that behaves normally for every mode except
    ``fail_mode``, which it always fails -- lets a test target ONE of the
    script's two chmod calls (755 on $CFG, 644 on puppeteer.json) without the
    other one masking it: a blanket-broken chmod would fail on whichever call
    comes first regardless of which guard's test is running, proving nothing
    about the second one.

    Resolves the real chmod itself, like _add_real_rm/_add_real_mktemp do for
    theirs, so neither caller repeats the which()-plus-assert."""
    real_chmod = shutil.which("chmod")
    assert real_chmod, "chmod not found — cannot build a hermetic PATH"
    shim = bin_dir / "chmod"
    shim.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "{fail_mode}" ]; then\n'
        '  echo "chmod: fake failure for test" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{real_chmod}" "$@"\n'
    )
    shim.chmod(0o755)


def _add_real_mktemp(bin_dir: Path) -> None:
    """Symlinks the real ``mktemp`` into the fake PATH -- needed by the two
    chmod-failure tests below, which want $CFG created for real (so the
    printf and, where applicable, the first chmod call succeed exactly as in
    a normal run) and only the TARGETED chmod call to fail."""
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp, "mktemp not found — cannot build a hermetic PATH"
    (bin_dir / "mktemp").symlink_to(real_mktemp)


def test_cfg_dir_chmod_failure_is_gate_could_not_run(fake_bin):
    """chmod 755 "$CFG" -- the first of the two chmod calls."""
    _add_docker_with_run_exit(fake_bin, 0)
    _add_real_rm(fake_bin)
    _add_real_mktemp(fake_bin)
    _add_chmod_that_fails_on_mode(fake_bin, "755")

    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="could not chmod")
    assert "to 755" in result.stderr


def test_puppeteer_config_chmod_failure_is_gate_could_not_run(fake_bin):
    """chmod 644 "$CFG/puppeteer.json" -- the second chmod call, distinct
    from the 755 one above: the fake chmod here passes mode 755 through to
    the real binary (so $CFG's own chmod succeeds, same as a normal run) and
    only fails on 644, proving this is the SECOND guard firing, not the
    first one masking it."""
    _add_docker_with_run_exit(fake_bin, 0)
    _add_real_rm(fake_bin)
    _add_real_mktemp(fake_bin)
    _add_chmod_that_fails_on_mode(fake_bin, "644")

    result = _run_gate(fake_bin)

    _assert_gate_could_not_run(result, says="could not chmod")
    assert "to 644" in result.stderr


# ---------------------------------------------------------------------------
# lode-yoc3: the per-doc loop's grep, the mirror defect of the -e class above
# and worse -- it reported a clean PASS rather than a fabricated FAIL. Both
# sides of the new partition are covered here (grep's exit 1 = content, every
# other exit = machine); why that partition is drawn where it is, is argued at
# the loop in scripts/validate-mermaid.sh.
# ---------------------------------------------------------------------------


def _add_grep_with_exit(bin_dir: Path, exit_code: int) -> None:
    """A fake ``grep`` that always exits ``exit_code``, standing in for the
    script's ONE grep call -- scanning each doc in the per-doc loop for a
    mermaid fence. That is the only place validate-mermaid.sh invokes grep at
    all, so a blanket fake (ignoring its arguments entirely) is safe here
    without needing to also intercept some OTHER real usage. It fires on the
    very first doc in $REPO/docs/*.md, whichever that happens to be -- the
    guard being tested doesn't care which file, only which exit code."""
    shim = bin_dir / "grep"
    shim.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    shim.chmod(0o755)


def test_grep_scan_failure_is_gate_could_not_run(fake_bin):
    """grep exits 2 (or any code other than 0/1) scanning a doc for a mermaid
    fence -- an unreadable file, an I/O error, or $REPO/docs vanishing mid-run.
    This must not be silently treated the same as "no match" (exit 1): it is a
    machine fault, not a content answer, so it must escalate through
    gate_could_not_run rather than fall through the old bare `|| continue` and
    let the doc -- and potentially the whole gate -- report a false clean PASS.
    """
    _add_docker_with_run_exit(fake_bin, 0)
    _add_grep_with_exit(fake_bin, 2)

    result = _run_gate(fake_bin, inherit_path=True)

    _assert_gate_could_not_run(result, says="grep failed scanning")
    assert "exit 2" in result.stderr


def test_grep_no_match_still_skips_silently_without_tripping_guard(fake_bin):
    """grep exits 1 (genuine "no match") for every doc -- the unchanged,
    pre-existing behaviour: a doc with no mermaid block is skipped silently,
    `found` stays 0, and the gate reports its normal clean "nothing to
    validate" PASS. This is the guard on the guard above: it must not widen
    into treating exit 1 as a machine fault too, which would turn every
    ordinary diagram-free doc into a spurious GATE COULD NOT RUN.
    """
    _add_docker_with_run_exit(fake_bin, 0)
    _add_grep_with_exit(fake_bin, 1)

    result = _run_gate(fake_bin, inherit_path=True)

    assert result.returncode == 0, (
        f"expected exit 0 (nothing to validate), got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "no mermaid diagrams found in docs/" in result.stdout
    assert "GATE COULD NOT RUN" not in result.stderr
    assert "FAIL" not in result.stdout
    assert "FAIL" not in result.stderr
