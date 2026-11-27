"""Tests for scripts/decisions_lookup_baseline.py (lode-ebayj.1), on synthetic transcripts."""

import json
from pathlib import Path

from conftest import load_module_from_path

REPO_ROOT = Path(__file__).resolve().parent.parent
_mod = load_module_from_path(
    "decisions_lookup_baseline", REPO_ROOT / "scripts" / "decisions_lookup_baseline.py"
)


def _use(tid: str, name: str, tool_input: dict) -> dict:
    return {
        "timestamp": "2026-11-27T10:00:00.000Z",
        "message": {
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input}
            ]
        },
    }


def _result(tid: str, text: str) -> dict:
    return {
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]
        }
    }


def test_scan_sizes_lookups_and_reads_of_decisions_md(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "s1.jsonl"
    transcript.parent.mkdir()
    entries = [
        _use("a", "Bash", {"command": "python scripts/docs_index_query.py 'x'"}),
        _result("a", "docs/decisions.md:10-20 snippet\n"),
        _use("b", "Read", {"file_path": "/r/docs/decisions.md"}),
        _result("b", "1\tone\n2\ttwo"),
        _use("c", "Read", {"file_path": "/r/docs/stack.md"}),
        _result("c", "ignored"),
        _use("d", "Bash", {"command": "python scripts/docs_index_query.py 'y'"}),
        _result("d", "docs/stack.md:1-2 not decisions"),
    ]
    transcript.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )

    rows = _mod.scan(tmp_path / "proj")

    assert sorted((r["kind"], r["lines"]) for r in rows) == [("lookup", 2), ("read", 2)]
    assert any(
        "lookups (index output citing it): 1" in line for line in _mod.summarize(rows)
    )
