"""Tests for scripts/dep-churn-lib.sh -- update-deps.sh's VERSION DIFF and the skip
policy for its churn-evaluation stub ticket (lode-i642).

Written during the technical review of lode-i642, which shipped this logic with no
test at all and, as a direct consequence, shipped it DEAD: the raw change lines were
computed into a global from inside a function that update-deps.sh only ever calls as
``DIFF_TEXT="$(print_version_diff ...)"``. Command substitution runs in a subshell, so
the global never reached the parent scope, the "no version changes" guard was
unconditionally true, and the feature's whole point -- filing the stub ticket -- could
never happen on any input. Both quality gates were green over that. The standing rule
it cost (a sourceable shell lib returns on stdout and holds no cross-call state) is in
docs/stack.md; this file is where the incident itself is remembered.

So the bar here is not "the noise gate has a unit test". It is that the *decision* --
whether a given run files or skips, and in which order the skip reasons win -- is
exercised end to end through ``dep_stub_skip_reason``, the one function update-deps.sh
now consults. ``test_update_deps_never_assigns_changes_raw_in_a_function`` pins the
caller-side half that no pure-function test can reach.

Every test runs the ACTUAL shell via ``bash``; nothing is reimplemented in Python.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
from conftest import uv_lock_package

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "dep-churn-lib.sh"
UPDATE_DEPS = REPO_ROOT / "scripts" / "update-deps.sh"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'set -uo pipefail\n. "{LIB}"\n{script}'],
        capture_output=True,
        text=True,
        check=False,
    )


def _lock(tmp_path: Path, name: str, pins: dict[str, str]) -> Path:
    """A minimal uv.lock-shaped file: ``[[package]]`` tables plus hash noise."""
    path = tmp_path / name
    body = "".join(
        f'\n[[package]]\nname = "{pkg}"\nversion = "{ver}"\n'
        f'source = {{ registry = "https://pypi.org/simple" }}\n'
        f'wheels = [\n    {{ url = "https://example/{pkg}.whl", '
        f'hash = "sha256:{"0" * 64}", size = 1 }},\n]\n'
        for pkg, ver in pins.items()
    )
    path.write_text("version = 1\nrevision = 3\n" + body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# dep_changes_raw
# --------------------------------------------------------------------------


def test_changes_raw_reports_only_moved_packages(tmp_path: Path) -> None:
    old = _lock(tmp_path, "old.lock", {"alpha": "1.2.3", "beta": "2.0.0"})
    new = _lock(tmp_path, "new.lock", {"alpha": "1.2.4", "beta": "2.0.0"})
    got = _run(f'dep_changes_raw "{old}" "{new}"')
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "alpha\t1.2.3\t1.2.4"


def test_changes_raw_marks_additions_and_removals(tmp_path: Path) -> None:
    old = _lock(tmp_path, "old.lock", {"gone": "1.0.0"})
    new = _lock(tmp_path, "new.lock", {"fresh": "9.9.9"})
    lines = set(_run(f'dep_changes_raw "{old}" "{new}"').stdout.split("\n")) - {""}
    assert lines == {"fresh\t(none)\t9.9.9", "gone\t1.0.0\t(none)"}


def test_changes_raw_handles_multi_version_packages_and_ignores_hashes(
    tmp_path: Path,
) -> None:
    """One line per package even when uv.lock carries it at two versions, and a
    hash-only change never shows up as a version move."""
    table = '\n[[package]]\nname = "{n}"\nversion = "{v}"\nsdist = {{ hash = "sha256:{h}" }}\n'
    old = tmp_path / "old.lock"
    new = tmp_path / "new.lock"
    old.write_text(
        "version = 1\n"
        + table.format(n="multi", v="1.0", h="a" * 64)
        + table.format(n="multi", v="2.0", h="b" * 64)
        + table.format(n="same", v="3.0", h="c" * 64),
        encoding="utf-8",
    )
    new.write_text(
        "version = 1\n"
        + table.format(n="multi", v="1.0", h="a" * 64)
        + table.format(n="multi", v="2.1", h="d" * 64)
        + table.format(n="same", v="3.0", h="e" * 64),
        encoding="utf-8",
    )
    got = _run(f'dep_changes_raw "{old}" "{new}"')
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "multi\t1.0,2.0\t1.0,2.1"


def test_changes_raw_empty_when_identical(tmp_path: Path) -> None:
    old = _lock(tmp_path, "old.lock", {"alpha": "1.2.3"})
    new = _lock(tmp_path, "new.lock", {"alpha": "1.2.3"})
    assert _run(f'dep_changes_raw "{old}" "{new}"').stdout.strip() == ""


# --------------------------------------------------------------------------
# dep_version_diff_text -- the artifact that ends up in the ticket body
# --------------------------------------------------------------------------


def test_version_diff_text_renders_each_shape() -> None:
    raw = "alpha\t1.2.3\t1.3.0\nfresh\t(none)\t9.9.9\ngone\t1.0.0\t(none)"
    out = _run(f"dep_version_diff_text uv.lock {shlex.quote(raw)}").stdout
    assert out.startswith("VERSION DIFF (uv.lock -> candidate):")
    assert re.search(r"^ {4}alpha +1\.2\.3 -> 1\.3\.0$", out, re.MULTILINE)
    assert re.search(r"^ {2}\+ fresh +9\.9\.9$", out, re.MULTILINE)
    assert re.search(r"^ {2}- gone +1\.0\.0 \(removed\)$", out, re.MULTILINE)


def test_version_diff_text_says_no_change_on_empty_input() -> None:
    out = _run('dep_version_diff_text uv.lock ""').stdout
    assert out.strip() == (
        "VERSION DIFF: no change -- candidate lock is identical to uv.lock"
    )


# --------------------------------------------------------------------------
# dep_all_patch_level_only -- the noise gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "patch_only"),
    [
        # Plain patch bumps on every line -- the one case that suppresses filing.
        ("alpha\t1.2.3\t1.2.9", True),
        ("alpha\t1.2.3\t1.2.9\nbeta\t0.1.0\t0.1.1", True),
        # A single non-patch move poisons the whole set.
        ("alpha\t1.2.3\t1.2.9\nbeta\t0.1.0\t0.2.0", False),
        ("alpha\t1.2.3\t1.3.0", False),
        ("alpha\t1.2.3\t2.2.3", False),
        # Additions and removals are never patch-only.
        ("fresh\t(none)\t9.9.9", False),
        ("gone\t1.0.0\t(none)", False),
        # No changes at all -- false, so the skip-reason ordering owns that path.
        ("", False),
        # Numeric comparison, not a string-prefix comparison.
        ("alpha\t1.2.0\t1.20.0", False),
        ("alpha\t2.1.3\t2.10.0", False),
        # Unparseable leading major.minor is conservatively NOT patch-only, so an
        # unreadable version shape can only ever cost noise, never a missed eval.
        ("alpha\t1!2.3.4\t1!2.3.5", False),
        ("alpha\t2024\t2025", False),
        ("alpha\tunknown\t1.2.3", False),
        # A multi-version package (comma-joined by _dep_pins) is never
        # patch-only: a minor move in its second version, or a version added.
        ("multi\t1.0,2.0\t1.0,2.1", False),
        ("multi\t1.0\t1.0,2.0", False),
    ],
)
def test_noise_gate(raw: str, patch_only: bool) -> None:
    got = _run(f"dep_all_patch_level_only {shlex.quote(raw)}")
    assert (got.returncode == 0) is patch_only, got.stdout + got.stderr


# --------------------------------------------------------------------------
# dep_stub_skip_reason -- the filing decision update-deps.sh actually consults
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("no_file", "raw", "reconcile", "expect"),
    [
        # A real, non-patch move is the ONLY combination that files.
        (0, "alpha\t1.2.3\t1.3.0", 0, None),
        (0, "fresh\t(none)\t9.9.9", 0, None),
        # Each skip reason, and the order they win in.
        (0, "", 0, "no version changes"),
        (0, "alpha\t1.2.3\t1.2.4", 0, "patch-level only"),
        (1, "alpha\t1.2.3\t1.3.0", 0, "--no-file set"),
        (1, "", 0, "--no-file set"),  # --no-file outranks every later reason
        (1, "alpha\t1.2.3\t1.2.4", 0, "--no-file set"),
        (1, "fresh\t(none)\t9.9.9", 1, "--no-file set"),
        # Reconcile mode (lode-7qo1): pure additions and removals never file.
        (0, "fresh\t(none)\t9.9.9", 1, "no existing pin"),
        (0, "gone\t1.0.0\t(none)", 1, "no existing pin"),
        (0, "fresh\t(none)\t9.9.9\ngone\t1.0.0\t(none)", 1, "no existing pin"),
        (0, "", 1, "no existing pin"),
        # An existing pin moved within its patch component does not file.
        (0, "alpha\t1.2.3\t1.2.4\nfresh\t(none)\t9.9.9", 1, "patch-level only"),
        # An existing pin crossing a minor or major files, additions alongside or not.
        (0, "alpha\t1.2.3\t1.3.0", 1, None),
        (0, "alpha\t1.2.3\t2.0.0\nfresh\t(none)\t9.9.9", 1, None),
        # A package gaining or losing one of several resolved versions is an
        # existing pin moving, not an addition or a removal: it files.
        (0, "multi\t1.0\t1.0,2.0\nfresh\t(none)\t9.9.9", 1, None),
        (0, "multi\t1.0,2.0\t1.0", 1, None),
    ],
)
def test_stub_skip_reason(
    no_file: int, raw: str, reconcile: int, expect: str | None
) -> None:
    got = _run(f"dep_stub_skip_reason {no_file} {shlex.quote(raw)} {reconcile}")
    if expect is None:
        assert got.returncode != 0, f"expected FILE, got skip: {got.stdout!r}"
        assert got.stdout.strip() == ""
    else:
        assert got.returncode == 0, got.stderr
        assert expect in got.stdout


# --------------------------------------------------------------------------
# dep_cooldown_exemptions
# --------------------------------------------------------------------------

_CUTOFF = "2026-11-16T00:00:00.000Z"


def _exemptions(tmp_path: Path, *packages: str) -> list[str]:
    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\nrevision = 3\n" + "".join(packages), encoding="utf-8")
    got = _run(f'dep_cooldown_exemptions "{lock}" "{_CUTOFF}"')
    assert got.returncode == 0, got.stderr
    return got.stdout.splitlines()


@pytest.mark.parametrize(
    ("times", "newest"),
    [
        # uv.lock drops trailing zeros from the fraction, so the raw strings do
        # not sort by time: "Z" sorts above every digit and above ".".
        (("2026-11-20T10:00:02.83Z", "2026-11-20T10:00:02.835Z"), 1),
        (("2026-11-20T10:00:02Z", "2026-11-20T10:00:02.5Z"), 1),
        (("2026-11-20T10:00:02.999Z", "2026-11-20T10:00:03Z"), 1),
        (("2026-11-20T10:00:03Z", "2026-11-20T10:00:02.999Z"), 0),
    ],
)
def test_cooldown_exemption_is_the_newest_upload_time_of_the_locked_files(
    tmp_path: Path, times: tuple[str, str], newest: int
) -> None:
    """A cutoff earlier than any one file of the locked release makes uv move
    the pin back, so the time handed over must be the true newest."""
    got = _exemptions(tmp_path, uv_lock_package("pkg", "1.0", *times))
    assert got == [f"pkg={times[newest]}"]


def test_cooldown_exemptions_cover_only_registry_pins_younger_than_the_cutoff(
    tmp_path: Path,
) -> None:
    got = _exemptions(
        tmp_path,
        uv_lock_package("young", "2.0", "2026-11-18T09:00:00.1Z"),
        uv_lock_package("old", "1.0", "2026-11-15T23:59:59.999Z"),
        # Uploaded at the cutoff to the millisecond: the global cutoff keeps it.
        uv_lock_package("at-cutoff", "1.0", "2026-11-16T00:00:00Z"),
        # Locked at two versions: the package-wide cutoff is the newest of both.
        uv_lock_package("multi", "1.0", "2026-01-01T00:00:00.000Z"),
        uv_lock_package("multi", "2.0", "2026-11-19T00:00:00.000Z"),
        uv_lock_package("vcs", "1.0", "2026-11-20T00:00:00.000Z", source="git"),
        uv_lock_package("no-times", "1.0"),
    )
    assert got == [
        "multi=2026-11-19T00:00:00.000Z",
        "young=2026-11-18T09:00:00.1Z",
    ]


# --------------------------------------------------------------------------
# The caller wiring -- the actual lode-i642 defect
# --------------------------------------------------------------------------


def test_update_deps_never_assigns_changes_raw_in_a_function() -> None:
    """CHANGES_RAW must be assigned at top level, never from inside a function.

    This is the caller-side half of the shipped defect, and no test of the library
    can reach it: a function that sets CHANGES_RAW is fine in isolation and useless
    in update-deps.sh, because every call site is a command substitution running in
    a subshell. An indented assignment is the syntactic tell.
    """
    body = UPDATE_DEPS.read_text(encoding="utf-8")
    assigns = re.findall(r"^([ \t]*)CHANGES_RAW=", body, re.MULTILINE)
    assert assigns, "update-deps.sh no longer assigns CHANGES_RAW"
    assert all(indent == "" for indent in assigns), (
        "CHANGES_RAW is assigned from inside a block/function -- if that is a "
        "function body, the value dies in the command substitution's subshell"
    )


def test_update_deps_sources_the_library() -> None:
    body = UPDATE_DEPS.read_text(encoding="utf-8")
    assert "scripts/dep-churn-lib.sh" in body
    for fn in (
        "dep_changes_raw",
        "dep_version_diff_text",
        "dep_stub_skip_reason",
        "dep_cooldown_exemptions",
    ):
        assert fn in body, f"update-deps.sh stopped using {fn}"


# --------------------------------------------------------------------------
# dep_exempt_moves
# --------------------------------------------------------------------------


def test_exempt_moves_lists_only_moved_exempted_packages() -> None:
    exemptions = "young=2026-11-18T09:00:00.1Z\nother=2026-11-19T00:00:00.000Z"
    raw = "young\t1.0\t2.0\nfree\t1.0\t2.0\nother\t(none)\t1.0\ngone\t1.0\t(none)"
    # young: exempted and moved; free: moved, not exempted; other: an addition.
    got = _run(f"dep_exempt_moves '{exemptions}' '{raw}'")
    assert got.returncode == 0, got.stderr
    assert got.stdout.splitlines() == ["young\t1.0\t2.0"]


def test_exempt_moves_is_empty_without_exemptions_or_moves() -> None:
    assert _run("dep_exempt_moves '' $'a\\t1\\t2'").stdout == ""
    assert _run("dep_exempt_moves 'a=2026-11-18T09:00:00Z' ''").stdout == ""
