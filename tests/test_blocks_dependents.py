"""Tests for scripts/blocks-dependents.sh (lode-verb).

lode-v4rk found four sites in `.claude/skills/land/SKILL.md` and sibling
skills walking bd dependency arrays and fixed three of them (the epic-
completion checks in `/land`, `/epic-audit`, `/sweep`) by extracting to
`scripts/epic-children-closed.sh` + `scripts/epic-completion-check.sh`,
each with fixture-backed regression tests. It fixed the fourth -- `/land`'s
Bounce section, which re-points a superseded ticket's `blocks`-dependents
onto the rebuild ticket so they stay blocked rather than unblocking
prematurely -- correctly, but left it as an ungated inline jq snippet in
the markdown, with no test to catch a future regression.

That distinction matters more here than for the three extracted sites: a
dropped `--include-dependents` flag there fails silently SAFE (a missed
epic-completion flag reads identically to "not complete yet"). Here it
fails silently UNSAFE: `bd supersede` still closes the original ticket, so
a dropped re-point lets every `blocks`-dependent unblock immediately
against a rebuild that was never built -- and /code's fan-out can then
dispatch a builder onto it.

`test_old_dependents_derivation_without_flag_never_fires` is a pinned
demonstration of that failure mode: it embeds the pre-extraction jq snippet
(minus `--include-dependents`, i.e. what a regression would look like)
directly and asserts it silently yields nothing against a `bd show`-shaped
fixture that HAS blocks-dependents in the DB but omits `.dependents` from
the JSON -- exactly matching real `bd show --json` without the flag,
verified live against bd 1.1.0 (see lode-v4rk). It is not a regression
guard (it doesn't touch the actual script) -- the guards are the fake-`bd`
tests below, which exercise `scripts/blocks-dependents.sh` directly and
fail if the script's own `--include-dependents` flag is ever dropped.
Same fake-`bd`-on-PATH pattern as tests/test_epic_completion_check.py and
tests/test_epic_children_closed.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "blocks-dependents.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(tmp_path: Path, show_fixtures: dict[str, list[dict]]) -> Path:
    """A PATH dir holding a fake `bd` that serves `show_fixtures[<id>]` for
    `bd show <id> --json --include-dependents`. Fails loudly if the script
    calls `bd show` WITHOUT `--include-dependents` -- that is exactly the
    regression this harness exists to catch."""
    show_path = tmp_path / "show_fixtures.json"
    show_path.write_text(json.dumps(show_fixtures))

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()

    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake `bd show <id> --json --include-dependents` for testing
            # blocks-dependents.sh. Deliberately rejects any invocation that
            # drops --include-dependents, so a script regression that removes
            # the flag fails LOUDLY here instead of silently returning [].
            set -euo pipefail
            [ "$1" = "show" ] || {{ echo "unsupported: $*" >&2; exit 1; }}
            id="$2"
            shift 2
            case " $* " in
              *" --include-dependents "*) ;;
              *) echo "fake bd: refusing bd show without --include-dependents" >&2
                 exit 1 ;;
            esac
            jq -c --arg id "$id" \\
              '.[$id] // error("no show fixture for \\($id)")' "{show_path}"
            """)
    )
    fake_bd.chmod(0o755)
    return bin_dir


def _run(
    id_: str, tmp_path: Path, *, show_fixtures: dict[str, list[dict]]
) -> subprocess.CompletedProcess:
    bin_dir = _fake_bd(tmp_path, show_fixtures)
    return subprocess.run(
        ["bash", str(SCRIPT), id_],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def _dependent(id_: str, dependency_type: str) -> dict:
    return {"id": id_, "dependency_type": dependency_type}


def test_blocks_dependents_are_listed(tmp_path: Path) -> None:
    show_fixtures = {
        "lode-orig": [
            {
                "id": "lode-orig",
                "dependents": [
                    _dependent("lode-followup-a", "blocks"),
                    _dependent("lode-followup-b", "blocks"),
                ],
            }
        ]
    }
    result = _run("lode-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["lode-followup-a", "lode-followup-b"]


def test_non_blocks_dependents_are_excluded(tmp_path: Path) -> None:
    """`parent-child` and `discovered-from` dependents must NOT be re-pointed
    -- only `blocks` edges create the premature-unblock hazard."""
    show_fixtures = {
        "lode-orig": [
            {
                "id": "lode-orig",
                "dependents": [
                    _dependent("lode-child", "parent-child"),
                    _dependent("lode-spinoff", "discovered-from"),
                    _dependent("lode-related", "related"),
                    _dependent("lode-blocker-of-orig", "blocks"),
                ],
            }
        ]
    }
    result = _run("lode-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["lode-blocker-of-orig"]


def test_no_dependents_key_prints_nothing(tmp_path: Path) -> None:
    """The `dependents` key can be entirely absent (e.g. a fixture that never
    populated it) -- the script must not error, just print nothing."""
    show_fixtures = {"lode-solo": [{"id": "lode-solo"}]}
    result = _run("lode-solo", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_empty_dependents_array_prints_nothing(tmp_path: Path) -> None:
    show_fixtures = {"lode-solo": [{"id": "lode-solo", "dependents": []}]}
    result = _run("lode-solo", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_script_calls_bd_show_with_include_dependents_flag(tmp_path: Path) -> None:
    """Direct proof the script itself passes --include-dependents: the fake
    `bd` above refuses (exit 1) any `bd show` call missing that flag, so a
    regression that drops it turns this into a failure, not a silent []."""
    show_fixtures = {
        "lode-orig": [
            {"id": "lode-orig", "dependents": [_dependent("lode-dep", "blocks")]}
        ]
    }
    result = _run("lode-orig", tmp_path, show_fixtures=show_fixtures)
    assert result.returncode == 0, result.stderr
    assert "refusing bd show without --include-dependents" not in result.stderr


def test_old_dependents_derivation_without_flag_never_fires() -> None:
    """Pinned demonstration of the bug this script exists to avoid
    reintroducing: a `bd show --json` call WITHOUT `--include-dependents`
    never populates `.dependents` (bd 1.1.0), so a derivation built on it
    silently yields nothing -- even though the ticket genuinely has
    blocks-dependents in the DB (modeled here only via a non-zero
    `dependent_count`, since the payload itself cannot say so; that
    inability is exactly the bug).

    NOT a regression guard for scripts/blocks-dependents.sh -- it embeds the
    flag-less derivation inline and passes regardless of what the script
    does (verified: still passes with the script deleted). The regression
    guards are the fake-`bd` tests above, which fail if the real script's
    `--include-dependents` flag is ever dropped.
    """
    orig = {
        "id": "lode-orig",
        # deliberately no "dependents" key -- matches real `bd show --json`
        # without --include-dependents, despite a non-zero dependent_count.
        "dependent_count": 2,
    }
    old_snippet_without_flag = """
        .[0].dependents[]? | select(.dependency_type=="blocks") | .id
    """
    result = subprocess.run(
        ["jq", "-r", old_snippet_without_flag],
        input=json.dumps([orig]),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", (
        "the flag-less derivation printed dependents -- this fixture no "
        "longer reproduces the bug this script exists to avoid reintroducing"
    )
