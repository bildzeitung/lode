"""Tests for scripts/bd-docs-nit.sh (lode-y86u).

The docs-nits collector recipe -- locate the single bd issue carrying the
reserved `docs-nits` label, refuse unless exactly one exists, append a note in
the mandated patch shape with the `NIT` prefix the script owns, then push --
was inline, in slightly-drifting copies, in .claude/skills/land/SKILL.md,
.claude/agents/coding.md, and .claude/agents/code-reviewer.md, with the read
half duplicated again in .claude/skills/sweep/SKILL.md section 2d. Same
fake-`bd`-on-PATH pattern as tests/test_sweep_digest_id.py, the direct
precedent this script follows for its resolve-by-label + refusal contract.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from conftest import (
    CODE_REVIEWER_AGENT_TEXT,
    CODING_AGENT_TEXT,
    LAND_SKILL_TEXT,
    SWEEP_SKILL_BLOCKS,
    fake_bin_env,
    non_comment_fence_body,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bd-docs-nit.sh"
_SCRIPT_TEXT = SCRIPT.read_text()

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(
    bin_dir: Path, list_rows: object, *, update_ok: bool = True, list_exit: int = 0
) -> None:
    """A fake `bd` on PATH that serves `list_rows` as the JSON body of
    `bd list --label docs-nits --limit 0 --json` and records any
    `bd update <id> --append-notes <note>` invocation to `bin_dir/update.log`
    (one line: the id, then the raw note body) -- or fails it if `update_ok`
    is False, to exercise the machine-fault path."""
    payload = bin_dir / "rows.json"
    payload.write_text(json.dumps(list_rows) if list_rows is not None else "null")
    update_log = bin_dir / "update.log"
    update_exit = "0" if update_ok else "1"
    fake_bd = bin_dir / "bd"
    fake_bd.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "$1" = "list" ]; then
              if [ {list_exit} -ne 0 ]; then
                exit {list_exit}
              fi
              cat {payload}
              exit 0
            fi
            if [ "$1" = "update" ]; then
              id="$2"
              # --append-notes is the last argument in every call site.
              note="${{@: -1}}"
              printf '%s\\n---\\n%s\\n===\\n' "$id" "$note" >> {update_log}
              exit {update_exit}
            fi
            echo "unsupported: $*" >&2
            exit 1
        """)
    )
    fake_bd.chmod(0o755)


def _fake_dolt_push(bin_dir: Path, *, ok: bool = True) -> Path:
    """Stub bd-dolt-push.sh at the path the script under test resolves relative
    to its own directory -- a PATH entry would never be consulted. See `_run`."""
    marker = bin_dir / "dolt_push_called"
    script = bin_dir / "bd-dolt-push.sh"
    exit_code = "0" if ok else "1"
    script.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit {exit_code}\n")
    script.chmod(0o755)
    return marker


def _run(
    tmp_path: Path,
    args: list[str],
    list_rows: object,
    *,
    update_ok: bool = True,
    dolt_ok: bool = True,
    list_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run scripts/bd-docs-nit.sh from a scratch scripts/ directory containing
    a copy of the real script alongside fakes for `bd` (on PATH) and
    `bd-dolt-push.sh` (resolved by the script relative to its own dir)."""
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir()
    script_copy = scratch_scripts / "bd-docs-nit.sh"
    script_copy.write_text(_SCRIPT_TEXT)
    script_copy.chmod(0o755)
    dolt_marker = _fake_dolt_push(scratch_scripts, ok=dolt_ok)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_bd(bin_dir, list_rows, update_ok=update_ok, list_exit=list_exit)

    result = subprocess.run(
        [str(script_copy), *args],
        capture_output=True,
        text=True,
        env=fake_bin_env(bin_dir),
        cwd=REPO_ROOT,
        check=False,
    )
    return result, dolt_marker


_APPEND_ARGS = [
    "append",
    "--source",
    "test",
    "--file",
    "README.md",
    "--line",
    "42",
    "--anchor",
    "the old text",
    "--replacement",
    "the new text",
]


def test_append_with_one_collector_resolves_appends_and_pushes(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path, _APPEND_ARGS, [{"id": "lode-59da", "title": "Docs wording nits"}]
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "lode-59da"
    assert dolt_marker.exists(), "scripts/bd-dolt-push.sh was not called"

    log = (tmp_path / "fakebin" / "update.log").read_text()
    assert log.startswith("lode-59da\n---\n")
    assert "NIT (test): README.md:42" in log
    assert 'Anchor (verbatim): "the old text"' in log
    assert 'Replacement: "the new text"' in log


def test_append_note_is_prefixed_nit_literally(tmp_path: Path) -> None:
    """/sweep section 2d's `^NIT` scan depends on this exact literal -- the
    whole reason the prefix is owned by the script, not typed by each caller."""
    r, _ = _run(tmp_path, _APPEND_ARGS, [{"id": "lode-59da", "title": "x"}])
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "fakebin" / "update.log").read_text()
    note = log.split("---\n", 1)[1].split("\n===")[0]
    assert note.startswith("NIT (")


def test_append_with_what_appends_a_what_it_changes_line(tmp_path: Path) -> None:
    r, _ = _run(
        tmp_path,
        [*_APPEND_ARGS, "--what", "retargets a dangling cross-reference"],
        [{"id": "lode-59da", "title": "x"}],
    )
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "fakebin" / "update.log").read_text()
    assert "What it changes: retargets a dangling cross-reference" in log


def test_append_no_collector_refuses_with_exit_1_and_no_write(tmp_path: Path) -> None:
    r, dolt_marker = _run(tmp_path, _APPEND_ARGS, [])
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 0" in r.stderr
    assert not (tmp_path / "fakebin" / "update.log").exists()
    assert not dolt_marker.exists()


def test_append_duplicate_collectors_refuses_and_never_picks_one(
    tmp_path: Path,
) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        [
            {"id": "lode-dupA", "title": "collector A"},
            {"id": "lode-dupB", "title": "collector B"},
        ],
    )
    assert r.returncode == 1
    assert r.stdout == ""
    assert "found 2" in r.stderr
    assert "lode-dupA" in r.stderr
    assert "lode-dupB" in r.stderr
    assert not (tmp_path / "fakebin" / "update.log").exists()
    assert not dolt_marker.exists()


def test_append_missing_required_argument_is_exit_2(tmp_path: Path) -> None:
    args = ["append", "--source", "test", "--file", "README.md"]
    r, _ = _run(tmp_path, args, [{"id": "lode-59da", "title": "x"}])
    assert r.returncode == 2
    assert r.stdout == ""


def test_append_flag_with_no_value_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    """A trailing flag missing its value is a machine fault. Without the
    explicit arity check it dies on `set -u` with exit 1 -- the code reserved
    for "not exactly one collector", so a typo'd invocation would read to the
    caller as "no collector exists, fall back to reporting"."""
    r, dolt_marker = _run(
        tmp_path, ["append", "--source"], [{"id": "lode-59da", "title": "x"}]
    )
    assert r.returncode == 2
    assert r.stdout == ""
    assert "requires a value" in r.stderr
    assert not dolt_marker.exists()


def test_append_bd_update_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path, _APPEND_ARGS, [{"id": "lode-59da", "title": "x"}], update_ok=False
    )
    assert r.returncode == 2
    assert not dolt_marker.exists(), "must not push after a failed append"


def test_append_dolt_push_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path, _APPEND_ARGS, [{"id": "lode-59da", "title": "x"}], dolt_ok=False
    )
    assert r.returncode == 2
    assert dolt_marker.exists(), "the push was attempted, just failed"


def test_count_lists_every_open_collector_with_its_nit_count(tmp_path: Path) -> None:
    rows = [
        {
            "id": "lode-59da",
            "title": "Docs wording nits",
            "notes": "NIT (a): one\nsome body\nNIT (b): two\n",
        },
        {"id": "lode-dup2", "title": "second collector", "notes": None},
    ]
    r, _ = _run(tmp_path, ["count"], rows)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip("\n").split("\n")
    assert lines[0] == "lode-59da\tDocs wording nits\t2"
    assert lines[1] == "lode-dup2\tsecond collector\t0"


def test_count_on_empty_result_set_prints_nothing_not_an_error(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count"], [])
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_count_null_result_set_is_treated_as_empty(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count"], None)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_count_takes_no_arguments(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count", "unexpected"], [])
    assert r.returncode == 2
    assert r.stdout == ""


def test_unknown_subcommand_is_exit_2(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["bogus"], [])
    assert r.returncode == 2


def test_no_subcommand_is_exit_2(tmp_path: Path) -> None:
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
    assert r.returncode == 2


def test_bd_list_failure_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    """A machine fault must stay distinguishable from "no collector": exit 2
    vs 1 -- collapsing them would let a broken bd read as a clean empty state."""
    r, dolt_marker = _run(tmp_path, _APPEND_ARGS, [], list_exit=3)
    assert r.returncode == 2
    assert r.stdout == ""
    assert not dolt_marker.exists()


def _assert_delegates_to_the_script(text: str, *, label: str) -> None:
    """No call site may keep its own copy of the resolve query -- that drift is
    the whole reason scripts/bd-docs-nit.sh exists."""
    assert "bd list --label docs-nits" not in text, (
        f"{label}: still carries the inline docs-nits resolve recipe -- "
        "this is the drift scripts/bd-docs-nit.sh exists to remove"
    )
    assert "scripts/bd-docs-nit.sh" in text


def test_sweep_skill_count_call_site_uses_the_script() -> None:
    # Comment lines are stripped first: §2d's block cites the script's internals
    # in a comment, which is documentation, not a second copy of the query.
    body = non_comment_fence_body(SWEEP_SKILL_BLOCKS)
    _assert_delegates_to_the_script(body, label="sweep/SKILL.md")
    assert "scripts/bd-docs-nit.sh count" in body


@pytest.mark.parametrize(
    ("text", "label"),
    [
        # Every append call site states the recipe in plain prose with inline
        # backtick commands, not a fenced bash block -- so these scan the cached
        # raw text rather than the fence-parse locator §2d's test uses.
        (LAND_SKILL_TEXT, "land/SKILL.md"),
        (CODING_AGENT_TEXT, "coding.md"),
        (CODE_REVIEWER_AGENT_TEXT, "code-reviewer.md"),
    ],
)
def test_append_call_sites_use_the_script(text: str, label: str) -> None:
    _assert_delegates_to_the_script(text, label=label)
    assert "scripts/bd-docs-nit.sh append" in text
