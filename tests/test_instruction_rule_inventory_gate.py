"""Rule-inventory gate for instruction files (lode-w18a.4, decision lode-9d9t).

tests/instruction_rules.json lists human-approved rules, each with one pinned
phrase. The gate fails unless each pinned phrase matches exactly once in its
file outside fenced code blocks, so a dedupe cut cannot silently drop a rule
(lode-4615). Per-file tickets add entries.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import fence_scan

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "tests" / "instruction_rules.json"

# Stands in for a removed block so that a phrase cannot match across one.
_FENCE_GAP = "\0"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _outside_fences(text: str) -> str:
    """The lines of text outside fenced code blocks, as conftest's fence_scan
    partitions them (so less one leading blockquote marker each), with
    _FENCE_GAP where each block was."""
    kept = []
    blocks_seen = -1
    for _lineno, line, info, ordinal in fence_scan(text):
        if info is not None:
            continue
        if ordinal != blocks_seen:
            kept.append(_FENCE_GAP)
            blocks_seen = ordinal
        kept.append(line)
    return "\n".join(kept)


def rule_problems(inventory: list[dict[str, str]], root: Path) -> list[str]:
    """Return 'file: rule (N matches)' for every entry whose phrase does not
    match exactly once outside fenced blocks in its file."""
    problems = []
    bodies: dict[str, str] = {}
    for entry in inventory:
        file = entry["file"]
        if file not in bodies:
            path = root / file
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            bodies[file] = _norm(_outside_fences(text))
        phrase = _norm(entry["phrase"])
        # A blank phrase is a substring of every body, so it would pin nothing.
        count = bodies[file].count(phrase) if phrase else 0
        if count != 1:
            problems.append(f"{file}: {entry['rule']} ({count} matches)")
    return problems


def test_every_pinned_phrase_matches_exactly_once() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert rule_problems(inventory, REPO_ROOT) == []


def test_gate_fails_unless_exactly_one_match_outside_fences(tmp_path: Path) -> None:
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
    indented = {"file": "agent.md", "rule": "r7", "phrase": "indented fenced only"}
    assert rule_problems([present], tmp_path) == []
    assert rule_problems(
        [present, absent, gone, blank, twice, fenced, indented], tmp_path
    ) == [
        "agent.md: r2 (0 matches)",
        "nope.md: r3 (0 matches)",
        "agent.md: r4 (0 matches)",
        "agent.md: r5 (2 matches)",
        "agent.md: r6 (0 matches)",
        "agent.md: r7 (0 matches)",
    ]


def test_fence_boundaries(tmp_path: Path) -> None:
    (tmp_path / "agent.md").write_text(
        "````md\n```bash\nnested fenced only\n```\n````\n"
        "> ```\n> quoted fenced only\n> ```\n"
        "split across\n```\ncode\n```\na fence.\n"
        "prose after the blocks.\n"
        "~~~\nunclosed fenced only\n"
    )
    phrases = {
        "nested": "nested fenced only",
        "quoted": "quoted fenced only",
        "split": "split across a fence",
        "after": "prose after the blocks",
        "unclosed": "unclosed fenced only",
    }
    inventory = [
        {"file": "agent.md", "rule": rule, "phrase": phrase}
        for rule, phrase in phrases.items()
    ]
    assert rule_problems(inventory, tmp_path) == [
        "agent.md: nested (0 matches)",
        "agent.md: quoted (0 matches)",
        "agent.md: split (0 matches)",
        "agent.md: unclosed (0 matches)",
    ]


def test_prose_line_opening_with_triple_backtick_span_stays_prose(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent.md").write_text(
        "```inline``` code starts this line.\nprose pinned phrase\n"
        "```bash\nrm x\n```\nmore prose.\n"
    )
    inventory = [
        {"file": "agent.md", "rule": "prose", "phrase": "prose pinned phrase"},
        {"file": "agent.md", "rule": "fenced", "phrase": "rm x"},
    ]
    assert rule_problems(inventory, tmp_path) == ["agent.md: fenced (0 matches)"]
