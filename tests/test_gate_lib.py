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
set at runtime, so a script that starts sourcing the library tomorrow is
covered the day it lands rather than the day someone remembers to write a
sixth near-identical test. That distinction is not academic here:
land-merge-one.sh spent its entire life as a stranded inline copy precisely
because nothing enumerated the set, and it took a manual audit (lode-bss5) to
notice. A test that hard-codes the list IS that list.

What "discover" MEANS is itself load-bearing, and a whole-file
`grep -l gate-lib.sh scripts/*.sh` is not it (lode-pcee). That matches any
MENTION of the library -- including a comment explaining why a script
deliberately does not use it. scripts/assert-main-checkout.sh is exactly that
case: a precondition guard whose header documents the decision not to route
through gate-lib.sh. It was swept in as a phantom consumer and failed all
three parametrized sweeps over a library it never sources, and the only way to
stay green would have been to stop naming the library it was explaining it did
not use. `_consumers()` therefore anchors on a real, non-comment SOURCE line,
so what it computes matches the docstring it has always carried.

Two invariants are swept, both of which were previously unenforceable
mechanically and left to per-consumer convention:

* every consumer GUARDS its source, so a missing/unreadable gate-lib.sh
  exits 2 rather than falling through to 0/1/127 (lode-bss5, Finding B);
* every consumer that sets GATE_ADVISORY sets it ABOVE all of its own
  gate_could_not_run call sites -- the ordering hazard gate-lib.sh's header
  describes, which emits half the contract and which `set -u`, shellcheck and
  the library's own tests all structurally cannot see.

NON-VACUITY (acceptance criterion): sabotaging either gate -- reverting a
consumer's guard to the bare source it had before lode-bss5, or moving its
GATE_ADVISORY below a call site -- must make the sweep fail. Both are proven
below rather than asserted; a gate that cannot fail is not a gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
GATE_LIB = SCRIPTS_DIR / "gate-lib.sh"

# The guard every consumer must carry, verbatim. Pinning the exact text (not
# just "some guard exists") is what keeps the five copies from drifting -- the
# duplication is irreducible, since the guard is what LOADS the library and so
# cannot live inside it, but drift is not.
GUARDED_SOURCE = """if ! . "$(dirname "$0")/gate-lib.sh"; then
  echo "GATE COULD NOT RUN: scripts/gate-lib.sh is missing or unreadable" >&2
  echo "next to $0 -- this is a machine/checkout fault, not a branch verdict." >&2
  exit 2
fi"""

# What it looked like before lode-bss5, and what the sabotage below restores.
BARE_SOURCE = '. "$(dirname "$0")/gate-lib.sh"'


def _non_comment_lines(text: str) -> list[tuple[int, str]]:
    """`(1-based line number, line)` for every line that is not a comment.

    The single home for the skip-comments rule all three scans in this module
    share (`_sources_gate_lib`, `_line_of`, `_call_site_lines`). It is the
    load-bearing half of each of them -- it is what keeps
    `# shellcheck disable=SC2034 # read by gate_could_not_run()` from
    registering as a call site, and what keeps gate-lib.sh's quoted Usage block
    from registering as a source. Three copies in one file is exactly the bar
    the repo's other extractions fired at, and a refinement here (a trailing
    `#` on a real line, say) has to hold for all three at once or discovery and
    call-site accounting silently disagree.
    """
    return [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


def _sources_gate_lib(text: str) -> bool:
    """True if `text` carries a real, non-comment line that SOURCES gate-lib.sh.

    Anchored on BARE_SOURCE, never on GUARDED_SOURCE, and that is not a
    stylistic preference: BARE_SOURCE is a substring of GUARDED_SOURCE's first
    line, so this one predicate finds guarded and unguarded consumers alike.
    Discovering by GUARDED_SOURCE instead would make
    test_every_consumer_carries_the_verbatim_source_guard VACUOUS -- every
    discovered script would satisfy it by construction, and the single thing
    that sweep exists to catch (a NEW consumer sourcing the library bare) would
    become invisible to it, silently restoring the lode-bss5 defect.

    Comment lines are skipped for the same reason `_call_site_lines` skips
    them, and here it is load-bearing rather than defensive: gate-lib.sh's own
    header quotes the guard block verbatim as its Usage example, so a script
    that copies that documentation into its own header would otherwise register
    as a consumer of a library it never loads.
    """
    return any(BARE_SOURCE in line for _, line in _non_comment_lines(text))


def _consumers() -> list[Path]:
    """Every scripts/*.sh that sources gate-lib.sh -- discovered, never listed."""
    return sorted(
        p
        for p in SCRIPTS_DIR.glob("*.sh")
        if p.name != "gate-lib.sh" and _sources_gate_lib(p.read_text())
    )


CONSUMERS = _consumers()


def _run_script(path: Path) -> subprocess.CompletedProcess:
    """Execute directly (not `bash <path>`) so each script's own shebang flags
    are honoured -- validate-mermaid.sh's `#!/bin/bash -e` is dropped entirely
    under `bash <path>`, which would make this test run a shell that is not
    the one the script actually ships with."""
    return subprocess.run(
        [str(path)], capture_output=True, text=True, timeout=30, check=False
    )


def _line_of(text: str, needle: str) -> int:
    for n, line in _non_comment_lines(text):
        if needle in line:
            return n
    raise AssertionError(f"{needle!r} not found outside comments")


def _check_advisory_precedes_call_sites(name: str, text: str) -> None:
    """Raise AssertionError if any gate_could_not_run call sits above the
    GATE_ADVISORY assignment. Shared by the sweep and its non-vacuity proof so
    the proof exercises the real check rather than a copy of it."""
    advisory_line = _line_of(text, "GATE_ADVISORY=(")
    call_sites = _call_site_lines(text)
    assert call_sites, f"{name} sets GATE_ADVISORY but never calls the helper"

    assert min(call_sites) > advisory_line, (
        f"{name}: gate_could_not_run is called at line {min(call_sites)}, "
        f"above its GATE_ADVISORY assignment at line {advisory_line} -- that call "
        f"emits the banner and cause lines but NOT the advisory trailer."
    )


def _call_site_lines(text: str) -> list[int]:
    """1-based lines holding a real gate_could_not_run CALL. Comment lines are
    skipped, which is what keeps the `# shellcheck disable=SC2034 # read by
    gate_could_not_run()` note in three consumers from registering as a call."""
    return [n for n, line in _non_comment_lines(text) if "gate_could_not_run" in line]


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
    """Regression test for the `declare -p GATE_ADVISORY || GATE_ADVISORY=()`
    line in gate-lib.sh -- see that line's comment for why bash's nounset
    makes it necessary. Every real caller runs under `set -u`, so sourcing the
    library and calling gate_could_not_run with GATE_ADVISORY never set must
    not blow up with "unbound variable" before reaching the exit-2 path.

    Deliberately kept even though the exact-stderr assertion in
    test_no_gate_advisory_set_means_no_trailer_at_all would also fail if that
    line regressed: this one names the mechanism and fails with a diagnostic
    that points straight at it, rather than an opaque line-list mismatch."""
    result = _run('gate_could_not_run "summary" "cause"')

    assert result.returncode == 2
    assert "unbound variable" not in result.stderr


def test_the_consumer_sweep_discovers_something():
    """Guards the sweeps below against silently passing on an empty set -- a
    glob that stops matching (renamed directory, changed suffix) would make
    every parametrized test vanish rather than fail."""
    assert CONSUMERS, "no gate-lib.sh consumers discovered under scripts/"


def test_merely_naming_gate_lib_does_not_make_a_consumer():
    """lode-pcee. Discovery must match a real SOURCE line, not any mention.
    A script that names gate-lib.sh only to explain why it deliberately does
    NOT use it (scripts/assert-main-checkout.sh, the repo's first such case)
    was otherwise swept in as a phantom consumer and failed all three
    parametrized sweeps over a library it never loads.

    Exercises `_sources_gate_lib` on synthetic text rather than on the
    discovered set, so it keeps proving the predicate no matter how any real
    script's header is later reworded -- that comment belongs to the script,
    not to this test.
    """
    assert not _sources_gate_lib(
        "# Not sourced from `scripts/gate-lib.sh`: that helper's banner belongs\n"
        "# to the content gates, and this is a precondition guard.\n"
        "set -euo pipefail\n"
    )

    # gate-lib.sh's own header quotes the whole guard block as its Usage
    # example; a script copying that documentation must not become a consumer.
    commented_out = "\n".join(f"# {ln}" for ln in GUARDED_SOURCE.splitlines())
    assert not _sources_gate_lib(commented_out + "\n")


def test_an_unguarded_source_line_is_still_discovered():
    """The other direction, and the reason the predicate anchors on
    BARE_SOURCE rather than GUARDED_SOURCE: a consumer that sources the
    library BARE must still be discovered, or
    test_every_consumer_carries_the_verbatim_source_guard could never fail on
    the newcomer it exists to catch (lode-bss5, Finding B) -- tightening
    discovery onto the guard text would quietly vacate that sweep."""
    assert _sources_gate_lib(f"set -uo pipefail\n{BARE_SOURCE}\n")
    assert _sources_gate_lib(f"set -uo pipefail\n{GUARDED_SOURCE}\n")


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_every_consumer_carries_the_verbatim_source_guard(script: Path):
    """lode-bss5, Finding B. This is the sweep that covers consumer #6: a new
    script sourcing gate-lib.sh bare fails here the day it lands, rather than
    waiting for someone to notice (which for land-merge-one.sh took a manual
    audit and a live exit-code inversion in /land's own merge step)."""
    assert GUARDED_SOURCE in script.read_text(), (
        f"{script.name} sources gate-lib.sh without the verbatim fail-closed "
        f"guard. See gate-lib.sh's Usage section for the exact block."
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
    """NON-VACUITY for the test above: reverting THIS consumer's guard to the
    bare source it carried before lode-bss5 must stop producing exit 2. Pins
    each of the five copies separately -- the guard is duplicated per consumer,
    so a single sabotage sample would leave four copies unproven."""
    sabotaged = script.read_text().replace(GUARDED_SOURCE, BARE_SOURCE, 1)
    assert sabotaged != script.read_text(), "sabotage did not apply"

    copied = tmp_path / script.name
    copied.write_text(sabotaged)
    copied.chmod(0o755)

    result = _run_script(copied)

    assert result.returncode != 2, (
        f"{script.name} still exits 2 with the guard removed, so the sweep "
        f"above proves nothing about this consumer."
    )


@pytest.mark.parametrize("script", CONSUMERS, ids=lambda p: p.name)
def test_gate_advisory_is_set_above_every_call_site(script: Path):
    """The ordering hazard gate-lib.sh's header describes: a call site placed
    above its script's GATE_ADVISORY assignment still exits 2 with a correct
    banner, but silently emits HALF the contract -- and `set -u`, shellcheck
    and the library's own tests all structurally cannot see it.

    The header states that only each consumer's own advisory assertions catch
    it. They do, where they exist -- but that is opt-in per consumer, and the
    consumer that most needed one (land-merge-one.sh) is exactly the one that
    went unnoticed for a release. Enforced here for the whole discovered set
    instead. Consumers that set no GATE_ADVISORY (release-bump.sh's shape)
    have no contract to halve and are skipped."""
    text = script.read_text()
    if "GATE_ADVISORY=(" not in text:
        pytest.skip(f"{script.name} sets no GATE_ADVISORY (no-advisory shape)")
    _check_advisory_precedes_call_sites(script.name, text)


def test_gate_advisory_ordering_sweep_is_not_vacuous():
    """NON-VACUITY for the test above: moving a consumer's GATE_ADVISORY block
    below its own call sites must make the identical check raise. Exercises
    `_check_advisory_precedes_call_sites` itself, not a restatement of it --
    a non-vacuity proof that re-implements the assertion proves only that the
    copy works."""
    advisory_setters = [p for p in CONSUMERS if "GATE_ADVISORY=(" in p.read_text()]
    assert advisory_setters, "no advisory-setting consumer to sabotage"

    script = advisory_setters[0]
    text = script.read_text()
    start = text.index("GATE_ADVISORY=(")
    end = text.index(")\n", start) + 2
    moved = text[:start] + text[end:] + "\n" + text[start:end]

    with pytest.raises(AssertionError, match="above its GATE_ADVISORY"):
        _check_advisory_precedes_call_sites(script.name, moved)
