"""Rule-inventory gate for instruction files (lode-w18a.4, decision lode-9d9t).

tests/instruction_rules.json lists human-approved rules, each with one pinned
phrase. The gate fails when a pinned phrase does not match exactly once outside fenced
blocks in its file, so a dedupe cut cannot silently drop a rule (lode-4615). Per-file tickets add entries.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "tests" / "instruction_rules.json"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _outside_fences(text: str) -> str:
    """Drop fenced code blocks (``` or ~~~, possibly indented) from text."""
    kept = []
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if fence:
            if marker == fence:
                fence = ""
            continue
        if marker in ("```", "~~~"):
            fence = marker
            continue
        kept.append(line)
    return "\n".join(kept)


def missing_rules(inventory: list[dict[str, str]], root: Path) -> list[str]:
    """Return 'file: rule (N matches)' for every entry whose phrase does not
    match exactly once outside fenced blocks in its file."""
    problems = []
    for entry in inventory:
        path = root / entry["file"]
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        body = _norm(_outside_fences(text))
        phrase = _norm(entry["phrase"])
        # A blank phrase is a substring of every body, so it would pin nothing.
        count = body.count(phrase) if phrase else 0
        if count != 1:
            problems.append(f"{entry['file']}: {entry['rule']} ({count} matches)")
    return problems


def test_every_pinned_phrase_is_present() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert missing_rules(inventory, REPO_ROOT) == []


def test_gate_passes_when_present_and_fails_when_absent(tmp_path: Path) -> None:
    (tmp_path / "agent.md").write_text(
        "Rule one:\nkeep   this phrase intact.\n"
        "Twice here. Twice here.\n"
        "```\nfenced only phrase\nkeep this phrase intact\n```\n"
        "  ~~~\nindented fenced only\n  ~~~\n"
    )
    present = {"file": "agent.md", "rule": "r1", "phrase": "keep this phrase intact"}
    absent = {"file": "agent.md", "rule": "r2", "phrase": "a dropped rule"}
    gone = {"file": "nope.md", "rule": "r3", "phrase": "keep this phrase intact"}
    blank = {"file": "agent.md", "rule": "r4", "phrase": " \n"}
    twice = {"file": "agent.md", "rule": "r5", "phrase": "Twice here"}
    fenced = {"file": "agent.md", "rule": "r6", "phrase": "fenced only phrase"}
    fenced2 = {"file": "agent.md", "rule": "r7", "phrase": "indented fenced only"}
    assert missing_rules([present], tmp_path) == []
    assert missing_rules(
        [present, absent, gone, blank, twice, fenced, fenced2], tmp_path
    ) == [
        "agent.md: r2 (0 matches)",
        "nope.md: r3 (0 matches)",
        "agent.md: r4 (0 matches)",
        "agent.md: r5 (2 matches)",
        "agent.md: r6 (0 matches)",
        "agent.md: r7 (0 matches)",
    ]
