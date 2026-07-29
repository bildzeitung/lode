"""Tests for scripts/gate-lib.sh (lode-090f).

Why the shared `gate_could_not_run()` helper was extracted, and what the
GATE_ADVISORY contract is: see that script's own header. It is the single
source for both -- not restated here.

The tests in the first half exercise the library directly, under `bash -c
'...'` sourcing it the same way every real caller does
(`. "$(dirname "$0")/gate-lib.sh"`), rather than through any one of the
consuming scripts -- those each keep their own regression tests, which double
as this library's integration coverage.

The SWEEPS in the second half are the other half of the division of labour,
and they are deliberately not written per-consumer: they DISCOVER the consumer
set at runtime (the `grep -l gate-lib.sh scripts/*.sh` that gate-lib.sh's own
header names as the authoritative way to ask), so a script that starts
sourcing the library tomorrow is covered the day it lands rather than the day
someone remembers to write a sixth near-identical test. That distinction is
not academic here: land-merge-one.sh spent its entire life as a stranded
inline copy precisely because nothing enumerated the set, and it took a manual
audit (lode-bss5) to notice. A test that hard-codes the list IS that list.

Two invariants are swept, both of which were previously unenforceable
mechanically and left to per-consumer convention:

* every consumer GUARDS its source, so a missing/unreadable gate-lib.sh
  exits 2 rather than falling through to 0/1/127 (lode-bss5, Finding B);
* every consumer's source line supplies either its advisory lines or the
  literal `--no-advisory` sentinel -- never a bare source with zero trailing
  tokens (lode-ysr6, see below).

NON-VACUITY (acceptance criterion): sabotaging either gate -- reverting a
consumer's guard to the bare source it had before lode-bss5, or stripping a
no-advisory consumer's `--no-advisory` sentinel -- must make the sweep fail
and, for the second one, must reproduce the actual leak on the real script.
Both are proven below rather than asserted; a gate that cannot fail is not a
gate.

WHY THE OLD "GATE_ADVISORY ORDERING" SWEEP IS GONE (lode-ysr6): it compared
the line number of a consumer's separate `GATE_ADVISORY=(...)` statement
against its `gate_could_not_run` call sites. No consumer has such a statement
any more -- gate-lib.sh binds GATE_ADVISORY itself, at source time -- so the
sweep has nothing left to compare and was deleted rather than rewritten. The
hazard it swept for, and why it is now unrepresentable, is recorded once in
gate-lib.sh's own header. The narrower discipline that replaces it is swept
below.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
GATE_LIB = SCRIPTS_DIR / "gate-lib.sh"

# The fixed part of every consumer's guard -- the three lines it prints/does
# on a missing library, still byte-identical across all five consumers. What
# VARIES per consumer is only the source line's own trailing arguments
# (advisory strings, or the `--no-advisory` sentinel), so the guard is pinned
# as this literal plus the small argument grammar in GUARD_RE below, rather
# than as one verbatim constant covering the whole block.
GUARD_TAIL = (
    'echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2\n'
    '  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2\n'
    "  exit 2\n"
    "fi"
)

# Matches the whole guarded-source block, capturing (group 1) the inner
# `. "$(dirname "$0")/gate-lib.sh" ...` command -- including whatever
# trailing arguments (advisory strings, `--no-advisory`, or nothing) follow.
# Each argument is a double-quoted string or the literal sentinel, optionally
# preceded by a backslash line continuation, so the two formattings in use
# (one continued argument per line; a single argument on the source line) are
# the same rule rather than two. group(1) is what a BARE, unguarded source
# would have looked like before lode-bss5: dropping the `if ! `/`; then`/`fi`
# wrapper and the three echo/exit lines (GUARD_TAIL) around it, while
# preserving whatever the consumer passes gate-lib.sh.
GUARD_RE = re.compile(
    r'if ! (\. "\$\(dirname "\$0"\)/gate-lib\.sh"'
    r'(?:[ \t]*(?:\\\n[ \t]*)?(?:"[^"\n]*"|--no-advisory))*)'
    r"; then\n  " + re.escape(GUARD_TAIL),
)


def _consumers() -> list[Path]:
    """Every scripts/*.sh that sources gate-lib.sh -- discovered, never listed."""
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.sh")
        if p.name != "gate-lib.sh" and "gate-lib.sh" in p.read_text()
    )


CONSUMERS = _consumers()

# The subset that passes the `--no-advisory` sentinel rather than advisory
# strings. Only these can have the sentinel stripped, so only these can prove
# the leak it prevents -- see the non-vacuity test at the bottom of the file.
NO_ADVISORY_CONSUMERS = [p for p in CONSUMERS if "--no-advisory" in p.read_text()]


def _run_script(path: Path, *args: str) -> subprocess.CompletedProcess:
    """Execute directly (not `bash <path>`) so each script's own shebang flags
    are honoured -- validate-mermaid.sh's `#!/bin/bash -e` is dropped entirely
    under `bash <path>`, which would make this test run a shell that is not
    the one the script actually ships with."""
    return subprocess.run(
        [str(path), *args], capture_output=True, text=True, timeout=30, check=False
    )


def _guard_match(text: str) -> re.Match[str]:
    m = GUARD_RE.search(text)
    assert m is not None, (
        "no guarded gate-lib.sh source block found (or its shape has drifted "
        "from what this test expects -- see GUARD_RE)"
    )
    return m


def _run(
    script_body: str, *source_args: str, prelude: str = ""
) -> subprocess.CompletedProcess:
    """Run `script_body` under `bash -uo pipefail -c`, after sourcing
    gate-lib.sh with `source_args` on the source line -- `-u` (nounset)
    matches how merge-precheck.sh/release-bump.sh actually run, and is the
    regime the unbound-array bug below only reproduces under.

    `prelude` is emitted ABOVE the source line, which is the only way to give
    the sourcing shell its own positional parameters (`set -- ...`) before
    gate-lib.sh reads them. With no prelude and no source_args, $# is
    genuinely 0 at the top level and gate-lib.sh's source-time logic defaults
    GATE_ADVISORY to an empty array -- what every test that does not care
    about the source-time mechanism itself wants."""
    # Quoting is stripped by the shell at parse time, so a quoted
    # "--no-advisory" reaches gate-lib.sh as the same $1 the real consumers
    # pass unquoted -- no special case needed here (verified).
    args = "".join(f' "{a}"' for a in source_args)
    return subprocess.run(
        [
            "bash",
            "-uo",
            "pipefail",
            "-c",
            f'{prelude}. "{GATE_LIB}"{args}\n{script_body}',
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _stderr_lines(result: subprocess.CompletedProcess) -> list[str]:
    """Non-empty stderr lines -- gate_could_not_run writes the banner, the
    caller's cause lines and the advisory trailer there, one per line."""
    return [ln for ln in result.stderr.splitlines() if ln]


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
    assert _stderr_lines(result) == ["GATE COULD NOT RUN: summary", "only cause line"]


def test_gate_advisory_set_once_is_appended_after_every_calls_cause_lines():
    """merge-precheck.sh's / validate-mermaid.sh's shape: GATE_ADVISORY set
    once near the top of the sourcing script, then appended automatically on
    every call site -- never repeated per call. GATE_ADVISORY is a plain bash
    array regardless of how it was populated (source-time args, or a direct
    assignment as done here), so setting it by hand after sourcing is still a
    valid way to exercise gate_could_not_run's own print logic in isolation
    from the source-time mechanism, which is covered separately below."""
    result = _run(
        'GATE_ADVISORY=("advisory line one" "advisory line two")\n'
        'gate_could_not_run "summary" "cause line"'
    )

    assert result.returncode == 2
    assert _stderr_lines(result) == [
        "GATE COULD NOT RUN: summary",
        "cause line",
        "advisory line one",
        "advisory line two",
    ]


def test_sourcing_under_nounset_survives_an_empty_gate_advisory():
    """Every real caller runs under `set -u`, and bash's nounset treats a
    never-declared array as an unbound-variable error on `${arr[@]}` (unlike
    a scalar's more forgiving `${var:-}` default). gate-lib.sh therefore
    assigns GATE_ADVISORY unconditionally at source time -- from "$@", or to
    an empty array for the --no-advisory sentinel -- so the `for line in
    "${GATE_ADVISORY[@]}"` loop in gate_could_not_run is always safe. Sourcing
    and then calling with no advisory lines must reach the exit-2 path rather
    than blowing up on the way."""
    result = _run('gate_could_not_run "summary" "cause"')

    assert result.returncode == 2
    assert "unbound variable" not in result.stderr


def test_source_with_advisory_args_populates_gate_advisory_at_source_time():
    """The mechanism itself (lode-ysr6): positional arguments on the SOURCE
    line become GATE_ADVISORY before the sourcing script's own next line runs
    -- no separate assignment statement needed or possible."""
    result = _run('gate_could_not_run "summary" "cause"', "src-line-1", "src-line-2")

    assert result.returncode == 2
    assert _stderr_lines(result) == [
        "GATE COULD NOT RUN: summary",
        "cause",
        "src-line-1",
        "src-line-2",
    ]


def test_source_with_no_advisory_sentinel_gives_empty_gate_advisory():
    result = _run('gate_could_not_run "summary" "cause"', "--no-advisory")

    assert result.returncode == 2
    assert _stderr_lines(result) == ["GATE COULD NOT RUN: summary", "cause"]


def test_bare_source_with_no_args_leaks_the_callers_own_ambient_positional_params():
    """THE DANGER lode-ysr6's design note pins (verified empirically, bash
    5.2): `source file` with NO trailing tokens after the filename does NOT
    clear $@ inside file -- it inherits the CALLING script's CURRENT
    positional parameters unchanged. This is exactly why every real consumer
    must pass either advisory strings or the `--no-advisory` sentinel, never
    nothing: a caller that already has its own $1/$2 set (its own CLI
    arguments, in every real consumer) and then sources gate-lib.sh bare gets
    THOSE values silently folded into GATE_ADVISORY.

    This test is not testing a real consumer's behaviour (none of the five
    source bare -- see test_every_consumer_source_line_supplies_advisory_or_
    sentinel below) -- it pins the underlying bash mechanism the whole
    --no-advisory design depends on, so a bash version change or a
    misunderstanding of this behaviour is caught here directly rather than
    only as a symptom in some future consumer."""
    result = _run(
        'gate_could_not_run "summary" "cause"',
        prelude='set -- "callers-own-arg1" "callers-own-arg2"\n',
    )

    assert result.returncode == 2
    # The two leaked ambient args show up as if they were advisory lines --
    # this IS the bug the sentinel exists to prevent, reproduced directly.
    assert _stderr_lines(result) == [
        "GATE COULD NOT RUN: summary",
        "cause",
        "callers-own-arg1",
        "callers-own-arg2",
    ]


def test_positional_params_restored_after_source_with_advisory_args():
    """The other half of the mechanism this design depends on (verified,
    bash 5.2): `source file arg1 arg2` sets $1.. inside file for the
    duration of the source command, then restores the CALLER's own $1.. to
    exactly what they were the instant the source command returns. Every
    consumer's own arg-count check runs AFTER the source line, so this is
    what makes those checks see their own real CLI arguments, unaffected by
    the advisory strings just passed to gate-lib.sh."""
    result = _run(
        'printf "%s %s %s\\n" "$1" "$2" "$#"',
        "advisory-a",
        "advisory-b",
        prelude='set -- "own-arg1" "own-arg2"\n',
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "own-arg1 own-arg2 2"


def test_the_consumer_sweep_discovers_something():
    """Guards the sweeps below against silently passing on an empty set -- a
    glob that stops matching (renamed directory, changed suffix) would make
    every parametrized test vanish rather than fail."""
    assert CONSUMERS, "no gate-lib.sh consumers discovered under scripts/"


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_carries_the_guarded_source(script: Path):
    """lode-bss5, Finding B. This is the sweep that covers consumer #6: a new
    script sourcing gate-lib.sh bare, with no fail-closed guard at all, fails
    here the day it lands, rather than waiting for someone to notice (which
    for land-merge-one.sh took a manual audit and a live exit-code inversion
    in /land's own merge step)."""
    _guard_match(script.read_text())


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_source_line_supplies_advisory_or_sentinel(script: Path):
    """lode-ysr6's replacement discipline for the retired ordering sweep: a
    consumer's source line must supply EITHER its advisory strings OR the
    literal `--no-advisory` sentinel -- never a bare
    `. "$(dirname "$0")/gate-lib.sh"` with zero trailing tokens, which would
    silently fold the consumer's own CLI arguments into GATE_ADVISORY (see
    test_bare_source_with_no_args_leaks_the_callers_own_ambient_positional_
    params for why). A new consumer that forgets this is caught here the day
    it lands, the same enforcement shape as the guard sweep above."""
    inner_source = _guard_match(script.read_text()).group(1)
    bare = '. "$(dirname "$0")/gate-lib.sh"'
    assert inner_source != bare, (
        f"{script.name} sources gate-lib.sh with no trailing arguments -- "
        f"either its own advisory lines or the literal --no-advisory "
        f"sentinel is required (see gate-lib.sh's GATE_ADVISORY contract)."
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_exits_2_when_gate_lib_is_missing(script: Path, tmp_path: Path):
    """The guard's behaviour, not just its presence. Reproduced the way the
    defect was originally measured: copy ONLY this script (never gate-lib.sh)
    into an isolated directory, so `$(dirname "$0")/gate-lib.sh` resolves to a
    path that does not exist.

    Run with no arguments: in every consumer the guard sits above all argument
    parsing, so a bare invocation reaches it. That is itself worth pinning --
    a consumer that parsed arguments first could exit 1 (a live CONTENT verdict
    in merge-precheck.sh and validate-mermaid.sh) before ever reaching the
    guard."""
    copied = tmp_path / script.name
    shutil.copy2(script, copied)

    result = _run_script(copied)

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "GATE COULD NOT RUN" in result.stderr
    assert "gate-lib.sh is missing or unreadable" in result.stderr


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_missing_gate_lib_sweep_is_not_vacuous(script: Path, tmp_path: Path):
    """NON-VACUITY for the test above: reverting THIS consumer's guard to a
    bare source (same trailing arguments, no fail-closed wrapper) must stop
    producing exit 2. Pins each of the five copies separately -- the guard is
    duplicated per consumer, so a single sabotage sample would leave four
    copies unproven."""
    text = script.read_text()
    match = _guard_match(text)
    inner_source = match.group(1)
    # str.replace, not GUARD_RE.sub: the replacement is a chunk of shell that
    # can contain backslashes, and re.sub would interpret those as template
    # escapes rather than literal text.
    sabotaged = text.replace(match.group(0), inner_source, 1)
    assert sabotaged != text, "sabotage did not apply"

    copied = tmp_path / script.name
    copied.write_text(sabotaged)
    copied.chmod(0o755)

    result = _run_script(copied)

    assert result.returncode != 2, (
        f"{script.name} still exits 2 with the guard removed, so the sweep "
        f"above proves nothing about this consumer."
    )


def test_a_no_advisory_consumer_exists_to_sabotage():
    """Guards the non-vacuity proof below against silently reducing to zero
    cases -- the day both no-advisory consumers gain advisory trailers, that
    parametrized test would vanish rather than fail, taking the only proof
    that the sentinel sweep is non-vacuous with it. Same role
    test_the_consumer_sweep_discovers_something plays for CONSUMERS."""
    assert NO_ADVISORY_CONSUMERS, (
        "no --no-advisory consumer left to sabotage -- the non-vacuity proof "
        "below is now vacuous and needs a synthetic consumer instead."
    )


@pytest.mark.parametrize("script", NO_ADVISORY_CONSUMERS, ids=lambda p: p.name)
def test_stripping_the_no_advisory_sentinel_leaks_the_consumers_own_argv(
    script: Path, tmp_path: Path
):
    """NON-VACUITY for test_every_consumer_source_line_supplies_advisory_or_
    sentinel, proven on the REAL script rather than a synthetic one: stripping
    a no-advisory consumer's `--no-advisory` sentinel down to a bare source
    must make that consumer's own CLI argument leak into GATE_ADVISORY and
    show up on an exit-2 path -- the actual, observable consequence of the
    bug this sentinel exists to prevent, not just a changed source line.

    gate-lib.sh itself is copied alongside the sabotaged script (unlike the
    missing-library sweep above) -- the leak can only be observed if sourcing
    actually SUCCEEDS; a missing gate-lib.sh would hit the fail-closed guard
    first and mask the leak entirely.

    Note the deliberate argument on the invocation below: the leak only
    exists when the consumer is holding argv at the moment it sources, so a
    zero-argument run of the same sabotaged script would pass silently. That
    is exactly why the sweep this proves is STATIC rather than a runtime
    check -- see gate-lib.sh's GATE_ADVISORY contract."""
    text = script.read_text()
    inner_source = _guard_match(text).group(1)
    assert inner_source.endswith("--no-advisory")
    bare_inner = inner_source[: -len(" --no-advisory")]
    sabotaged = text.replace(inner_source, bare_inner, 1)
    assert sabotaged != text, "sabotage did not apply"

    copied = tmp_path / script.name
    copied.write_text(sabotaged)
    copied.chmod(0o755)
    shutil.copy2(GATE_LIB, tmp_path / "gate-lib.sh")

    marker = "leak-marker-xyz-123"
    result = _run_script(copied, marker)

    assert result.returncode == 2, result.stdout + result.stderr
    assert marker in result.stderr, (
        f"{script.name}: expected its own first CLI argument ({marker!r}) to "
        f"leak into the advisory trailer once --no-advisory is stripped, but "
        f"it did not appear in stderr:\n{result.stderr}"
    )
