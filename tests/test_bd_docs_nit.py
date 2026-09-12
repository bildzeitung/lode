"""Tests for scripts/bd-docs-nit.sh (lode-y86u; lifecycle policy lode-z1n5).

The docs-nits collector recipe -- locate the OPEN bd issue(s) carrying the
reserved `docs-nits` label, create one when none exists, converge when 2+
carry the standard title, append a note in the mandated patch shape with the
`NIT` prefix the script owns, then push -- was inline, in slightly-drifting
copies, in .claude/skills/land/SKILL.md, .claude/agents/coding.md, and
.claude/agents/code-reviewer.md, with the read half duplicated again in
.claude/skills/sweep/SKILL.md section 2d. Same fake-`bd`-on-PATH pattern as
tests/test_sweep_digest_id.py, the direct precedent this script follows for
its resolve-by-label + refusal contract.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import (
    CODE_REVIEWER_AGENT_TEXT,
    CODING_AGENT_TEXT,
    LAND_SKILL_TEXT,
    SWEEP_SKILL_BLOCKS,
    fake_bd,
    fake_bin_env,
    non_comment_fence_body,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bd-docs-nit.sh"
_SCRIPT_TEXT = SCRIPT.read_text()
CREATE_SCRIPT = REPO_ROOT / "scripts" / "bd-docs-nit-create.sh"
_CREATE_SCRIPT_TEXT = CREATE_SCRIPT.read_text()
_LABEL_HELPER = REPO_ROOT / "scripts" / "bd-label-single-id.sh"
_LABEL_HELPER_TEXT = _LABEL_HELPER.read_text()
_CONSTANTS_TEXT = (REPO_ROOT / "scripts" / "docs-nits-constants.sh").read_text()

STANDARD_TITLE = "Docs wording nits: batch fix in the next docs pass"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the script shells out to jq"
)


def _fake_bd(
    bin_dir: Path,
    *,
    open_rows: object,
    all_rows: object | None = None,
    show_fixtures: dict[str, object] | None = None,
    update_ok: bool = True,
    close_ok: bool = True,
    create_id: str = "lode-newcol",
    create_ok: bool = True,
    list_exit: int = 0,
) -> None:
    """A fake `bd` on PATH.

    `bd list --label docs-nits --status open --limit 0 --json` serves
    `open_rows`. `bd list --label docs-nits --limit 0 --json` (no --status,
    `count`'s own query) serves `all_rows` (defaults to `open_rows` when not
    given -- most tests don't exercise `count` and `append` in the same run).
    `bd list --label docs-nits --status open --id <id> --json` (the create
    script's confirm query) always reports exactly the id it was asked about
    as freshly created. `bd show <id> --json` serves `show_fixtures[<id>]`.
    `bd update`/`bd close`/`bd create` are logged to `update.log`/`close.log`
    and controlled by their own `_ok` flags.
    """
    if all_rows is None:
        all_rows = open_rows
    open_payload = bin_dir / "open_rows.json"
    open_payload.write_text(json.dumps(open_rows) if open_rows is not None else "null")
    all_payload = bin_dir / "all_rows.json"
    all_payload.write_text(json.dumps(all_rows) if all_rows is not None else "null")
    show_payload = bin_dir / "show_fixtures.json"
    show_payload.write_text(json.dumps(show_fixtures or {}))
    update_log = bin_dir / "update.log"
    close_log = bin_dir / "close.log"
    create_log = bin_dir / "create.log"
    update_exit = "0" if update_ok else "1"
    close_exit = "0" if close_ok else "1"
    create_exit = "0" if create_ok else "1"

    fake_bd(
        bin_dir,
        {
            "list": f"""
                if [ {list_exit} -ne 0 ]; then
                  exit {list_exit}
                fi
                args="$*"
                if [[ "$args" == *"--id "* ]]; then
                  id=""
                  prev=""
                  for a in "$@"; do
                    if [ "$prev" = "--id" ]; then id="$a"; fi
                    prev="$a"
                  done
                  jq -c --arg id "$id" '[{{id: $id, title: "{STANDARD_TITLE}"}}]' <<< '{{}}'
                elif [[ "$args" == *"--status open"* ]]; then
                  cat {open_payload}
                else
                  cat {all_payload}
                fi
            """,
            "show": f"""
                id="$2"
                jq -c --arg id "$id" '.[$id] // error("no show fixture for \\($id)")' {show_payload}
            """,
            "update": f"""
                id="$2"
                note="${{@: -1}}"
                printf '%s\\n---\\n%s\\n===\\n' "$id" "$note" >> {update_log}
                exit {update_exit}
            """,
            "close": f"""
                id="$2"
                printf '%s\\t%s\\n' "$id" "$*" >> {close_log}
                exit {close_exit}
            """,
            "create": f"""
                echo "$*" >> {create_log}
                if [ {create_exit} -ne 0 ]; then
                  exit {create_exit}
                fi
                jq -n --arg id "{create_id}" '{{id: $id}}'
            """,
        },
    )


def _fake_dolt_push(scratch_scripts: Path, *, ok: bool = True) -> Path:
    marker = scratch_scripts / "dolt_push_called"
    script = scratch_scripts / "bd-dolt-push.sh"
    exit_code = "0" if ok else "1"
    script.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit {exit_code}\n")
    script.chmod(0o755)
    return marker


def _run(
    tmp_path: Path,
    args: list[str],
    *,
    open_rows: object = None,
    all_rows: object | None = None,
    show_fixtures: dict[str, object] | None = None,
    update_ok: bool = True,
    close_ok: bool = True,
    create_id: str = "lode-newcol",
    create_ok: bool = True,
    dolt_ok: bool = True,
    list_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run scripts/bd-docs-nit.sh from a scratch scripts/ directory containing
    copies of the real script and its two sibling scripts (bd-label-single-id.sh,
    bd-docs-nit-create.sh), alongside fakes for `bd` (on PATH) and
    `bd-dolt-push.sh` (resolved by every script relative to its own dir)."""
    scratch_scripts = tmp_path / "scripts"
    scratch_scripts.mkdir()
    for name, text in (
        ("bd-docs-nit.sh", _SCRIPT_TEXT),
        ("bd-docs-nit-create.sh", _CREATE_SCRIPT_TEXT),
        ("bd-label-single-id.sh", _LABEL_HELPER_TEXT),
        ("docs-nits-constants.sh", _CONSTANTS_TEXT),
    ):
        copy = scratch_scripts / name
        copy.write_text(text)
        copy.chmod(0o755)
    dolt_marker = _fake_dolt_push(scratch_scripts, ok=dolt_ok)

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _fake_bd(
        bin_dir,
        open_rows=open_rows,
        all_rows=all_rows,
        show_fixtures=show_fixtures,
        update_ok=update_ok,
        close_ok=close_ok,
        create_id=create_id,
        create_ok=create_ok,
        list_exit=list_exit,
    )

    result = subprocess.run(
        [str(scratch_scripts / "bd-docs-nit.sh"), *args],
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


def test_append_with_one_open_collector_resolves_appends_and_pushes(
    tmp_path: Path,
) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[{"id": "lode-59da", "title": STANDARD_TITLE}],
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
    r, _ = _run(tmp_path, _APPEND_ARGS, open_rows=[{"id": "lode-59da", "title": "x"}])
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "fakebin" / "update.log").read_text()
    note = log.split("---\n", 1)[1].split("\n===")[0]
    assert note.startswith("NIT (")


def test_append_with_what_appends_a_what_it_changes_line(tmp_path: Path) -> None:
    r, _ = _run(
        tmp_path,
        [*_APPEND_ARGS, "--what", "retargets a dangling cross-reference"],
        open_rows=[{"id": "lode-59da", "title": "x"}],
    )
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "fakebin" / "update.log").read_text()
    assert "What it changes: retargets a dangling cross-reference" in log


def test_append_no_open_collector_creates_one_then_appends(tmp_path: Path) -> None:
    """lode-z1n5 part 1: reverses the old "a human opens it" rule -- append
    creates the collector itself when zero are open, rather than refusing."""
    r, dolt_marker = _run(tmp_path, _APPEND_ARGS, open_rows=[], create_id="lode-newcol")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "lode-newcol"
    assert dolt_marker.exists()

    create_log = (tmp_path / "fakebin" / "create.log").read_text()
    assert STANDARD_TITLE in create_log
    assert "docs-nits" in create_log

    log = (tmp_path / "fakebin" / "update.log").read_text()
    assert log.startswith("lode-newcol\n---\n")


def test_append_no_open_collector_create_failure_is_exit_2(tmp_path: Path) -> None:
    r, _dolt_marker = _run(tmp_path, _APPEND_ARGS, open_rows=[], create_ok=False)
    assert r.returncode == 2
    assert r.stdout == ""
    assert not (tmp_path / "fakebin" / "update.log").exists()


def test_append_two_standard_title_collectors_converges_on_smallest_id(
    tmp_path: Path,
) -> None:
    """lode-z1n5 part 1a: 2+ open collectors, all carrying the STANDARD title,
    converge without a human -- smallest id survives, every note migrates,
    losers close naming the survivor, then the nit lands on the survivor."""
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[
            {"id": "lode-bbbb", "title": STANDARD_TITLE},
            {"id": "lode-aaaa", "title": STANDARD_TITLE},
        ],
        show_fixtures={
            "lode-bbbb": [{"notes": "NIT (x): some/file.md:1\nAnchor...\n"}],
        },
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "lode-aaaa"
    assert dolt_marker.exists()

    close_log = (tmp_path / "fakebin" / "close.log").read_text()
    assert "lode-bbbb" in close_log
    assert "lode-aaaa" in close_log  # the survivor id named in the close reason

    update_log = (tmp_path / "fakebin" / "update.log").read_text()
    # the loser's migrated notes and the new nit both land on the survivor
    assert update_log.count("lode-aaaa\n---\n") == 2
    assert "NIT (x): some/file.md:1" in update_log
    assert "NIT (test): README.md:42" in update_log


def test_append_two_collectors_with_a_non_standard_title_refuses(
    tmp_path: Path,
) -> None:
    """A non-standard title is the signal a human opened a second collector on
    purpose -- convergence must not touch it."""
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[
            {"id": "lode-59da", "title": STANDARD_TITLE},
            {"id": "lode-human", "title": "A human-curated docs-nits batch"},
        ],
    )
    assert r.returncode == 1
    assert r.stdout == ""
    assert "lode-59da" in r.stderr
    assert "lode-human" in r.stderr
    assert not (tmp_path / "fakebin" / "update.log").exists()
    assert not (tmp_path / "fakebin" / "close.log").exists()
    assert not dolt_marker.exists()


def test_append_convergence_migrate_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[
            {"id": "lode-bbbb", "title": STANDARD_TITLE},
            {"id": "lode-aaaa", "title": STANDARD_TITLE},
        ],
        show_fixtures={"lode-bbbb": [{"notes": "NIT (x): f:1\n"}]},
        update_ok=False,
    )
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_append_convergence_close_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[
            {"id": "lode-bbbb", "title": STANDARD_TITLE},
            {"id": "lode-aaaa", "title": STANDARD_TITLE},
        ],
        show_fixtures={"lode-bbbb": [{"notes": "NIT (x): f:1\n"}]},
        close_ok=False,
    )
    assert r.returncode == 2
    assert not dolt_marker.exists()


def test_append_missing_required_argument_is_exit_2(tmp_path: Path) -> None:
    args = ["append", "--source", "test", "--file", "README.md"]
    r, _ = _run(tmp_path, args, open_rows=[{"id": "lode-59da", "title": "x"}])
    assert r.returncode == 2
    assert r.stdout == ""


def test_append_flag_with_no_value_is_exit_2_not_exit_1(tmp_path: Path) -> None:
    """A trailing flag missing its value is a machine fault. Without the
    explicit arity check it dies on `set -u` with exit 1 -- the code reserved
    for "2+ collectors, human must consolidate", so a typo'd invocation would
    misread as that state."""
    r, dolt_marker = _run(
        tmp_path, ["append", "--source"], open_rows=[{"id": "lode-59da", "title": "x"}]
    )
    assert r.returncode == 2
    assert r.stdout == ""
    assert "requires a value" in r.stderr
    assert not dolt_marker.exists()


def test_append_bd_update_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[{"id": "lode-59da", "title": "x"}],
        update_ok=False,
    )
    assert r.returncode == 2
    assert not dolt_marker.exists(), "must not push after a failed append"


def test_append_dolt_push_failure_is_exit_2(tmp_path: Path) -> None:
    r, dolt_marker = _run(
        tmp_path,
        _APPEND_ARGS,
        open_rows=[{"id": "lode-59da", "title": "x"}],
        dolt_ok=False,
    )
    assert r.returncode == 2
    assert dolt_marker.exists(), "the push was attempted, just failed"


def test_count_lists_every_non_closed_collector_with_its_nit_count(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "id": "lode-59da",
            "title": "Docs wording nits",
            "notes": "NIT (a): one\nsome body\nNIT (b): two\n",
        },
        {"id": "lode-dup2", "title": "second collector", "notes": None},
    ]
    r, _ = _run(tmp_path, ["count"], all_rows=rows)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip("\n").split("\n")
    assert lines[0] == "lode-59da\tDocs wording nits\t2"
    assert lines[1] == "lode-dup2\tsecond collector\t0"


def test_count_on_empty_result_set_prints_nothing_not_an_error(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count"], all_rows=[])
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_count_null_result_set_is_treated_as_empty(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count"], all_rows=None)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_count_takes_no_arguments(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["count", "unexpected"], all_rows=[])
    assert r.returncode == 2
    assert r.stdout == ""


def test_ensure_open_creates_when_a_docs_nits_ticket_closed_and_none_is_open(
    tmp_path: Path,
) -> None:
    """/land's whole reopen-on-close backstop (lode-z1n5 part 4) is this one call,
    so the branch is tested here rather than as prose in land/SKILL.md."""
    r, _ = _run(
        tmp_path,
        ["ensure-open", "lode-59da"],
        open_rows=[],
        show_fixtures={"lode-59da": [{"id": "lode-59da", "labels": ["docs-nits"]}]},
    )
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "fakebin" / "create.log").exists()


def test_ensure_open_is_a_noop_when_a_collector_is_already_open(tmp_path: Path) -> None:
    r, _ = _run(
        tmp_path,
        ["ensure-open", "lode-59da"],
        open_rows=[{"id": "lode-59da", "title": STANDARD_TITLE}],
        show_fixtures={"lode-59da": [{"id": "lode-59da", "labels": ["docs-nits"]}]},
    )
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "fakebin" / "create.log").exists()


def test_ensure_open_is_a_noop_when_no_closed_ticket_was_a_collector(
    tmp_path: Path,
) -> None:
    r, _ = _run(
        tmp_path,
        ["ensure-open", "lode-abcd"],
        open_rows=[],
        show_fixtures={"lode-abcd": [{"id": "lode-abcd", "labels": ["other"]}]},
    )
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "fakebin" / "create.log").exists()


def test_ensure_open_with_no_ids_is_a_noop(tmp_path: Path) -> None:
    """An empty $LANDED (a pass that closed nothing) is legitimate, not a fault."""
    r, _ = _run(tmp_path, ["ensure-open"], open_rows=[])
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "fakebin" / "create.log").exists()


def test_ensure_open_bd_failure_is_exit_2_and_creates_nothing(tmp_path: Path) -> None:
    """A broken `bd` must not read as "no open collector" -- that would mint a
    duplicate collector on every failing /land pass."""
    r, _ = _run(
        tmp_path,
        ["ensure-open", "lode-59da"],
        open_rows=[],
        show_fixtures={"lode-59da": [{"id": "lode-59da", "labels": ["docs-nits"]}]},
        list_exit=3,
    )
    assert r.returncode == 2
    assert not (tmp_path / "fakebin" / "create.log").exists()


def test_ensure_open_create_failure_is_exit_2(tmp_path: Path) -> None:
    r, _ = _run(
        tmp_path,
        ["ensure-open", "lode-59da"],
        open_rows=[],
        show_fixtures={"lode-59da": [{"id": "lode-59da", "labels": ["docs-nits"]}]},
        create_ok=False,
    )
    assert r.returncode == 2


def test_unknown_subcommand_is_exit_2(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, ["bogus"], all_rows=[])
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
    """A machine fault must stay distinguishable from a legitimate 0/2+ state:
    exit 2 vs 0/1 -- collapsing them would let a broken bd read as clean."""
    r, dolt_marker = _run(tmp_path, _APPEND_ARGS, open_rows=[], list_exit=3)
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
