"""Tests for scripts/decisions_lookup_baseline.py (lode-ebayj.1), on synthetic transcripts."""

import json
from pathlib import Path

import pytest
from conftest import load_module_from_path
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parent.parent
_mod = load_module_from_path(
    "decisions_lookup_baseline", REPO_ROOT / "scripts" / "decisions_lookup_baseline.py"
)

runner = CliRunner()

_INDEX = "uv run --frozen python scripts/docs_index_query.py"
_POINTER = "/r/docs/decisions.md:10-20  - **An entry**\n    snippet\n"


def _use(tid: str, name: str, tool_input: dict, day: str | None = "2026-11-27") -> dict:
    entry: dict = {
        "message": {
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
            ]
        },
    }
    if day is not None:
        entry["timestamp"] = f"{day}T10:00:00.000Z"
    return entry


def _result(tid: str, text: str) -> dict:
    return {
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]
        }
    }


def _bash(
    tid: str, command: str, output: str, day: str | None = "2026-11-27"
) -> list[dict]:
    return [_use(tid, "Bash", {"command": command}, day), _result(tid, output)]


def _read(tid: str, file_path: str, output: str = "1\tone\n2\ttwo") -> list[dict]:
    return [_use(tid, "Read", {"file_path": file_path}), _result(tid, output)]


def _write(path: Path, *calls: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e) + "\n" for call in calls for e in call), encoding="utf-8"
    )
    return path.parent


def _scan(tmp_path: Path, *calls: list[dict]) -> list[dict]:
    return _mod.scan(_write(tmp_path / "proj" / "s1.jsonl", *calls))


def test_scan_sizes_lookups_and_reads_of_decisions_md(tmp_path: Path) -> None:
    rows = _scan(
        tmp_path,
        _bash("a", f"{_INDEX} 'x'", _POINTER),
        _read("b", "/r/docs/decisions.md"),
        _read("c", "/r/docs/stack.md", "ignored"),
        _bash("d", f"{_INDEX} 'y'", "/r/docs/stack.md:1-2 not decisions\n"),
    )

    assert [(r["lookup"], r["read"], r["lines"], r["sole"]) for r in rows] == [
        (True, False, 2, True),
        (False, True, 2, True),
    ]
    assert rows[0]["bytes"] == len(_POINTER)


def test_a_lookup_needs_a_pointer_row_not_a_mention(tmp_path: Path) -> None:
    mention = (
        "/r/docs/agents-workflow.md:5-9  - **Recording.** A builder writing a "
        "ruling into `docs/decisions.md` writes the ruling.\n"
    )

    assert _scan(tmp_path, _bash("a", f"{_INDEX} 'x'", mention)) == []


def test_a_bash_read_is_the_segment_that_names_the_record(tmp_path: Path) -> None:
    rows = _scan(
        tmp_path,
        _bash("a", "sed -n 1,5p docs/stack.md; git log -- docs/decisions.md", "x\n"),
        _bash("b", "grep -n x docs/design.md | grep -v decisions.md", "x\n"),
        _bash("c", "sed -i 's/a/b/' docs/decisions.md", ""),
        _bash("d", "grep -n x docs/decisions.md | head -5", "12:x\n"),
        _bash("e", "git status --short; sed -n 1,5p docs/decisions.md", "a\nb\n"),
    )

    assert [(r["read"], r["sole"]) for r in rows] == [(True, True), (True, False)]


def test_filtering_the_index_output_by_the_record_is_not_a_read(tmp_path: Path) -> None:
    (row,) = _scan(
        tmp_path, _bash("a", f"{_INDEX} 'x' | grep docs/decisions.md", _POINTER)
    )

    assert (row["lookup"], row["read"], row["sole"]) == (True, False, True)


@pytest.mark.parametrize(
    ("tool_input", "read"),
    [
        ({"pattern": "x", "path": "docs/decisions.md"}, True),
        ({"pattern": "x", "path": "docs/decisions/"}, True),
        ({"pattern": "x", "path": "docs/", "glob": "decisions.md"}, True),
        ({"pattern": "x", "glob": "docs/decisions/*.md"}, True),
        ({"pattern": "x", "path": "docs"}, False),
    ],
)
def test_a_grep_tool_call_is_a_read_when_scoped_to_the_record(
    tmp_path: Path, tool_input: dict, read: bool
) -> None:
    rows = _scan(tmp_path, [_use("a", "Grep", tool_input), _result("a", "12:x\n")])

    assert [r["read"] for r in rows] == ([True] if read else [])


def test_a_call_that_looks_up_and_reads_is_both_and_not_sole(tmp_path: Path) -> None:
    (row,) = _scan(
        tmp_path,
        _bash("a", f"{_INDEX} 'x'; sed -n 10,20p docs/decisions.md", _POINTER),
    )

    assert (row["lookup"], row["read"], row["followup"], row["sole"]) == (
        True,
        True,
        True,
        False,
    )


def test_a_read_is_a_follow_up_only_after_a_lookup_in_its_session(
    tmp_path: Path,
) -> None:
    root = _write(
        tmp_path / "proj" / "s1.jsonl",
        _read("before", "/r/docs/decisions.md"),
        _bash("lookup", f"{_INDEX} 'x'", _POINTER),
        _read("after", "/r/docs/decisions.md"),
    )
    _write(tmp_path / "proj" / "s2.jsonl", _read("other", "/r/docs/decisions.md"))

    assert [r["followup"] for r in _mod.scan(root) if r["read"]] == [
        False,
        True,
        False,
    ]


def test_scan_measures_the_per_entry_layout_too(tmp_path: Path) -> None:
    rows = _scan(
        tmp_path,
        _bash("a", f"{_INDEX} 'x'", "/r/docs/decisions/ab12c-a-slug.md:1-9  entry\n"),
        _read("b", "/r/docs/decisions/ab12c-a-slug.md"),
        _bash("c", "sed -n 1,9p /r/docs/decisions/ab12c-a-slug.md", "x\n"),
        _bash("d", "grep -rn lode-abc docs/decisions/", "x\n"),
    )

    assert [(r["lookup"], r["read"]) for r in rows] == [
        (True, False),
        (False, True),
        (False, True),
        (False, True),
    ]


def test_scan_counts_a_tool_use_id_once_and_keeps_the_timestamped_day(
    tmp_path: Path,
) -> None:
    rows = _scan(
        tmp_path,
        _bash("a", f"{_INDEX} 'x'", _POINTER, day=None),
        _bash("a", f"{_INDEX} 'x'", _POINTER),
    )

    assert [r["day"] for r in rows] == ["2026-11-27"]


@pytest.mark.parametrize(
    ("values", "p90"),
    [(list(range(1, 11)), 9), (list(range(1, 21)), 18), ([7], 7), ([1, 2, 3], 3)],
)
def test_dist_p90_is_the_nearest_rank(values: list[int], p90: int) -> None:
    assert f" p90={p90} " in _mod._dist(values)


def test_summarize_counts_follow_ups_and_sizes_sole_calls_only() -> None:
    def row(session: str, bytes_: int, **flags: bool) -> dict:
        base = {"lookup": False, "read": False, "followup": False, "sole": True}
        return {"session": session, "bytes": bytes_, "lines": 1, **base, **flags}

    lines = _mod.summarize(
        [
            row("s1", 50, read=True),
            row("s1", 100, lookup=True),
            row("s1", 300, read=True, followup=True),
            row("s1", 9000, read=True, followup=True, sole=False),
            row("s2", 700, read=True),
        ]
    )

    assert lines == [
        "decisions lookups (index calls with a decisions pointer row): 1",
        "  output bytes per lookup, sole calls: n=1 mean=100 median=100 p90=100 max=100",
        "decisions reads: 4 (follow-up to a lookup: 2)",
        "  result bytes per read, sole calls: n=3 mean=350 median=300 p90=700 max=700",
        "  result lines per read, sole calls: n=3 mean=1 median=1 p90=1 max=1",
        "sessions with >=1 lookup: 1 (of 2 touching decisions)",
        "  lookups per such session: n=1 mean=1 median=1 p90=1 max=1",
        "  follow-up reads per such session: n=1 mean=2 median=2 p90=2 max=2",
        "  lookup + follow-up bytes per such session (upper bound): n=1 mean=9400 median=9400 p90=9400 max=9400",
        "bytes per lookup incl. follow-up reads (upper bound): 9400 (~2350 tokens)",
    ]


def test_report_cli_filters_by_since(tmp_path: Path) -> None:
    _write(
        tmp_path / "proj" / "s1.jsonl",
        _bash("old", f"{_INDEX} 'x'", _POINTER, day="2026-11-01"),
        _bash("new", f"{_INDEX} 'y'", _POINTER, day="2026-12-02"),
    )
    args = ["--projects-dir", str(tmp_path), "--all-projects"]

    everything = runner.invoke(_mod.app, args)
    recent = runner.invoke(_mod.app, [*args, "--since", "2026-12-01"])

    assert everything.exit_code == 0, everything.output
    assert "pointer row): 2" in everything.output
    assert recent.exit_code == 0, recent.output
    assert "pointer row): 1" in recent.output


def test_report_cli_says_so_when_the_project_has_no_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(_mod.app, ["--projects-dir", str(tmp_path / "projects")])

    assert result.exit_code == 0, result.output
    assert "no transcripts for this project" in result.output
    assert "pointer row): 0" in result.output
