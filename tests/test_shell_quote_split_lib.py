"""scripts/shell-quote-split.sh -- the shared quote-aware split library (lode-dia6).

`scripts/gh-write-guard.sh` and `scripts/sha-fabrication-guard.sh` both source this library for
their segment split (`_split_unquoted`, lode-obox) and quoted-heredoc pre-pass
(`strip_quoted_heredoc_bodies`, lode-d5je). Extracting it removed a byte-identical duplicate --
and introduced a NEW hazard in its place: a second file that must be present AND loadable before
either default-deny guard can scan anything. Both guards must fail CLOSED on that path, because a
false ALLOW in either is unrecoverable (a `gh` write goes out under the user's public identity; a
fabricated SHA gets committed).

This file sweeps that property across every consumer, **discovered at runtime** rather than
listed. That mechanism is copied deliberately from `tests/test_gate_lib.py`, whose own docstring
records why a hard-coded list is the wrong shape: a test that enumerates its subjects IS the
enumeration, so a third guard that starts sourcing this library tomorrow would fail open silently
until someone remembered to hand-write a third near-identical test. Written during the technical
review of lode-dia6, replacing exactly that pair of hand-written per-guard copies.

Two failure modes, not one:

* **Absent** -- the library is missing/unreadable. Caught by each guard's `[ ! -r ]` check.
* **Broken** -- the library is present but does not define the functions (truncated write,
  partial checkout, bad merge, syntax error). `-r` passes, `source` "succeeds", and the guard
  then dies at the call site with rc=127 and no stdout, which the settings.json wrapper's
  trailing `exit 0` converts into a silent ALLOW. This was a live fail-OPEN found in review and
  is why each guard asserts the CONTRACT (`declare -F`) after sourcing, not just the file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB = SCRIPTS_DIR / "shell-quote-split.sh"
LIB_NAME = LIB.name

# A command that reaches the fail-closed block in each consumer. This mapping is unavoidably
# hand-written -- the block sits behind each guard's own cheap early-outs, so there is no single
# universal probe -- but it is NOT the consumer list: `test_every_consumer_has_a_probe` below
# fails loudly if a newly discovered consumer has no entry here, and
# `test_probe_reaches_past_the_block_when_the_library_is_present` pins that each probe really
# does get that far, so neither the sweep nor the mapping can go quietly vacuous.
PROBES = {
    "gh-write-guard.sh": "gh issue create --title x",
    "sha-fabrication-guard.sh": f"git show {'0' * 40}",
}


def _non_comment_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _consumers() -> list[Path]:
    """Every scripts/*.sh that sources shell-quote-split.sh -- discovered, never listed.

    Comment lines are skipped: both guards name the library in their header prose and in a
    `# shellcheck source=` directive, and this library's own header quotes its consumers, so a
    substring match over raw text would register documentation as a dependency.
    """
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.sh")
        if p.name != LIB_NAME
        and any(LIB_NAME in ln for ln in _non_comment_lines(p.read_text()))
    )


CONSUMERS = _consumers()


def test_the_library_has_consumers_at_all() -> None:
    """Non-vacuity for every sweep below: an over-tight discovery predicate would silently
    reduce them all to zero parametrizations, which pytest reports as passing."""
    assert CONSUMERS, (
        f"no scripts/*.sh discovered as sourcing {LIB_NAME} -- the discovery predicate in "
        "_consumers() has drifted, and every sweep in this file is now vacuous"
    )


def test_every_consumer_has_a_probe() -> None:
    """A newly discovered consumer with no PROBES entry must fail here, not be skipped."""
    missing = [p.name for p in CONSUMERS if p.name not in PROBES]
    assert not missing, (
        f"these scripts source {LIB_NAME} but have no PROBES entry, so the fail-closed sweeps "
        f"below never exercise them: {missing}"
    )


def _run_isolated(script: Path, command: str):
    """Run an isolated copy of a guard. cwd stays REPO_ROOT so the fabricated-SHA guard's
    "am I in a git work tree" early-out does not fire before the block under test."""
    return subprocess.run(
        ["bash", str(script), command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _isolate(tmp_path: Path, script: Path, lib_text: str | None) -> Path:
    """Copy ONLY this script into an isolated dir, optionally alongside a library of our
    choosing, so `dirname "${BASH_SOURCE[0]}"/shell-quote-split.sh` resolves there."""
    copied = tmp_path / script.name
    shutil.copy2(script, copied)
    if lib_text is not None:
        (tmp_path / LIB_NAME).write_text(lib_text)
    return copied


def _assert_denied_for_the_library(result, script_name: str) -> None:
    assert result.returncode == 0, (
        f"{script_name}: exited {result.returncode} -- a PreToolUse guard exiting non-zero "
        f"produces no decision, which the settings.json wrapper turns into a silent ALLOW: "
        f"{result.stderr}"
    )
    assert result.stdout.strip(), (
        f"{script_name}: emitted no decision, i.e. ALLOWED, with the shared library unusable"
    )
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert LIB_NAME in out["permissionDecisionReason"], (
        f"{script_name}: denied, but not for the reason under test -- the message must name "
        f"{LIB_NAME} so a human can act on it"
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_denies_when_the_library_is_absent(
    script: Path, tmp_path: Path
) -> None:
    """The `[ ! -r ]` path: no library next to the script at all."""
    copied = _isolate(tmp_path, script, lib_text=None)
    _assert_denied_for_the_library(
        _run_isolated(copied, PROBES[script.name]), script.name
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_denies_when_the_library_defines_nothing(
    script: Path, tmp_path: Path
) -> None:
    """The `declare -F` path: the library is present and READABLE -- so `-r` passes -- but
    defines neither function, as a truncated write or partial checkout would leave it.

    Without the contract check this is a silent ALLOW (rc=127, empty stdout), which is strictly
    worse than the absent case because `-r` gives a false sense of coverage.
    """
    copied = _isolate(
        tmp_path, script, lib_text="# truncated copy: no functions defined\n"
    )
    _assert_denied_for_the_library(
        _run_isolated(copied, PROBES[script.name]), script.name
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_denies_when_the_library_is_syntactically_broken(
    script: Path, tmp_path: Path
) -> None:
    """Same contract check, reached via a `source` that FAILS rather than one that succeeds
    hollowly -- which is why each guard sources under `|| true`: under `set -e` a non-zero
    `source` would abort the script before the check could deny."""
    copied = _isolate(
        tmp_path, script, lib_text="_split_unquoted() {\n  # unterminated function\n"
    )
    _assert_denied_for_the_library(
        _run_isolated(copied, PROBES[script.name]), script.name
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_probe_reaches_past_the_block_when_the_library_is_present(
    script: Path, tmp_path: Path
) -> None:
    """NON-VACUITY for the three sweeps above: with the REAL library alongside, the same probe
    must not produce the library-unusable deny.

    A probe that fell out of date -- one the guard's cheap early-outs now reject before the
    block is reached -- would leave the sweeps above asserting on a guard that never ran the
    code under test. This pins the other direction: with the library working, the probe must
    NOT report it as unusable.
    """
    copied = _isolate(tmp_path, script, lib_text=LIB.read_text())
    result = _run_isolated(copied, PROBES[script.name])
    assert result.returncode == 0, result.stderr
    if result.stdout.strip():
        out = json.loads(result.stdout)["hookSpecificOutput"]
        assert LIB_NAME not in out["permissionDecisionReason"], (
            f"{script.name}: still reports the library as unusable with the real library "
            f"present -- resolution is broken, not just the failure path"
        )


def test_library_is_not_marked_executable() -> None:
    """The library is SOURCED, never executed -- its own header says so. A `+x` bit would
    contradict that and make `./scripts/shell-quote-split.sh` a silent no-op instead of a loud
    failure. The shebang stays (shellcheck uses it for dialect detection); only the mode goes.
    """
    assert not LIB.stat().st_mode & 0o111, (
        f"{LIB_NAME} is marked executable, but it is a sourced library, not an entry point"
    )


def test_library_declares_no_shell_options() -> None:
    """`set -euo pipefail` here would leak into whichever guard sources the file, silently
    changing that guard's error semantics. Each caller owns its own options."""
    for line in _non_comment_lines(LIB.read_text()):
        assert not line.strip().startswith("set -"), (
            f"{LIB_NAME} sets shell options ({line.strip()!r}); they would leak into every "
            "guard that sources it"
        )


def test_split_does_not_leak_the_c_locale_to_its_caller() -> None:
    """`_split_unquoted` sets `LC_ALL=C` for byte-indexing performance (an O(n^2) -> O(n) fix
    on a hook that runs on every Bash call). It is declared `local` precisely so the callers'
    `grep -E` and `[[:space:]]` keep their normal locale semantics; pin that it stays local."""
    probe = (
        f'source "{LIB}"\n'
        '_split_unquoted "abc" >/dev/null\n'
        "x=$'\\u00e9\\u00e9\\u00e9'\n"
        'printf "LC_ALL=[%s] charlen=%s\\n" "${LC_ALL-unset}" "${#x}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "LC_ALL=[unset]" in result.stdout, (
        f"LC_ALL escaped _split_unquoted into the caller's environment: {result.stdout!r}"
    )
    assert "charlen=3" in result.stdout, (
        "the caller's locale is still byte-oriented after _split_unquoted returned -- three "
        f"accented characters counted as more than 3: {result.stdout!r}"
    )


def test_scan_length_cap_is_declared_once_and_shared() -> None:
    """lode-rjqm: `SHELL_QUOTE_SPLIT_MAX_LEN` is declared exactly once, here in the shared
    library, so both consumers cap at the same value by construction rather than each carrying
    its own (and eventually drifting) copy -- the same rationale the library itself exists for."""
    lib_text = LIB.read_text()
    assert "SHELL_QUOTE_SPLIT_MAX_LEN=" in lib_text, (
        f"{LIB_NAME} no longer declares the shared scan-length cap"
    )
    for path in CONSUMERS:
        text = path.read_text()
        assert "SHELL_QUOTE_SPLIT_MAX_LEN=" not in text, (
            f"{path.name} re-declares SHELL_QUOTE_SPLIT_MAX_LEN instead of using the shared "
            f"one from {LIB_NAME} -- this is exactly the drift risk the shared library exists "
            "to avoid (lode-dia6/lode-rjqm)"
        )
        assert "SHELL_QUOTE_SPLIT_MAX_LEN" in text, (
            f"{path.name} sources {LIB_NAME} but never checks the scan-length cap before "
            "calling _split_unquoted -- lode-rjqm's fail-closed cap is not wired in"
        )
