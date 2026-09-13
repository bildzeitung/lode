"""Tests for scripts/docs_index_usage_report.py -- the transcript-mining
baseline script (lode-dozi).

Uses SYNTHETIC transcript fixtures under tmp_path -- never a real
~/.claude/projects transcript -- both to keep the test hermetic and because
this script's whole point is to be pointed at a directory via
--projects-dir/`scan(projects_dir=...)`.
"""

import json
from pathlib import Path

from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent

_report_module = load_module_from_path(
    "docs_index_usage_report", REPO_ROOT / "scripts" / "docs_index_usage_report.py"
)
scan = _report_module.scan
summarize = _report_module.summarize
app = _report_module.app

runner = CliRunner()


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _tool_use_entry(
    *, timestamp: str, is_subagent: bool, name: str, tool_input: dict, cwd: str = ""
) -> dict:
    return {
        "timestamp": timestamp,
        "isSidechain": is_subagent,
        "cwd": cwd,
        "message": {
            "content": [
                {"type": "tool_use", "name": name, "input": tool_input},
            ]
        },
    }


def test_scan_classifies_index_invocation_via_bash(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={
                    "command": "python scripts/docs_index_query.py 'lode-nt98'"
                },
            )
        ],
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "index"
    assert rows[0]["is_subagent"] is False
    assert rows[0]["day"] == "2026-09-13"


def test_scan_classifies_direct_grep_over_docs(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=True,
                name="Bash",
                tool_input={"command": "grep -rn lode-nt98 docs/decisions.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "direct"
    assert rows[0]["is_subagent"] is True


def test_scan_classifies_read_tool_on_a_docs_file(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Read",
                tool_input={"file_path": "/repo/docs/design.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert rows[0]["kind"] == "direct"


def test_scan_ignores_unrelated_bash_commands(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "git status"},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_ignores_read_tool_on_a_non_docs_file(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Read",
                tool_input={"file_path": "/repo/src/lode/retrieval.py"},
            )
        ],
    )
    assert scan(tmp_path) == []


def test_scan_skips_non_message_lines_and_malformed_json(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        '{"type": "mode", "mode": "normal"}\n'
        "not json at all\n"
        + json.dumps(
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={"command": "cat docs/design.md"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    rows = scan(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "direct"


def test_summarize_splits_totals_by_day_and_role(tmp_path: Path) -> None:
    rows = [
        {"day": "2026-09-13", "session": "a", "is_subagent": False, "kind": "index"},
        {"day": "2026-09-13", "session": "a", "is_subagent": True, "kind": "direct"},
        {"day": "2026-09-14", "session": "b", "is_subagent": False, "kind": "direct"},
    ]
    result = summarize(rows)
    assert result["totals"] == {"index": 1, "direct": 2}
    assert result["per_day"]["2026-09-13"] == {"index": 1, "direct": 1}
    assert result["per_day"]["2026-09-14"] == {"direct": 1}
    assert result["per_role"]["main"] == {"index": 1, "direct": 1}
    assert result["per_role"]["subagent"] == {"direct": 1}


def test_report_cli_prints_totals_and_splits(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00.000Z",
                is_subagent=False,
                name="Bash",
                tool_input={
                    "command": "python scripts/docs_index_query.py 'lode-nt98'"
                },
            ),
            _tool_use_entry(
                timestamp="2026-09-13T11:00:00.000Z",
                is_subagent=True,
                name="Bash",
                tool_input={"command": "grep -n lode-nt98 docs/decisions.md"},
            ),
        ],
    )
    result = runner.invoke(app, ["--projects-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "index invocations: 1" in result.output
    assert "direct docs/*.md access: 1" in result.output
    assert "2026-09-13: index=1 direct=1" in result.output
    assert "main: index=1" in result.output
    assert "subagent: " in result.output and "direct=1" in result.output


def test_report_cli_handles_no_transcripts_found(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--projects-dir", str(tmp_path / "nonexistent")])
    assert result.exit_code == 0, result.output
    assert "index invocations: 0" in result.output


def test_scan_treats_a_launch_dir_cwd_as_a_subagent_when_issidechain_is_false(
    tmp_path: Path,
) -> None:
    """isSidechain reads False on every entry on Claude Code 2.1.270 even
    though subagents ran, so the cwd of the recording session is the fallback
    discriminator -- the same main-checkout-vs-producer proxy the invocation
    log uses. Added at technical review (lode-dozi)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                cwd="/repo/.claude/worktrees/agent-deadbeef",
                name="Read",
                tool_input={"file_path": "/repo/docs/design.md"},
            )
        ],
    )
    rows = scan(tmp_path)
    assert [r["is_subagent"] for r in rows] == [True]


def test_scan_buckets_an_unnarrowed_grep_as_ambiguous_not_direct(
    tmp_path: Path,
) -> None:
    """A Grep with neither path nor glob defaults to the whole repo, so it
    only MIGHT have read docs/ -- it must not inflate the headline
    index-vs-direct ratio. A docs-scoped Grep still counts as direct.
    Added at technical review (lode-dozi)."""
    transcript = tmp_path / "proj" / "s1.jsonl"
    _write_transcript(
        transcript,
        [
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                name="Grep",
                tool_input={"pattern": "anything"},
            ),
            _tool_use_entry(
                timestamp="2026-09-13T10:00:00Z",
                is_subagent=False,
                name="Grep",
                tool_input={"pattern": "anything", "path": "docs"},
            ),
        ],
    )
    assert summarize(scan(tmp_path))["totals"] == {"ambiguous": 1, "direct": 1}
