"""Rule-inventory gate for instruction files (lode-w18a.4, decision lode-9d9t).

tests/instruction_rules.json lists human-approved rules, each with one pinned
phrase. The gate fails when a pinned phrase is absent from its file, so a
dedupe cut cannot silently drop a rule. Per-file tickets add entries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = Path(__file__).resolve().parent / "instruction_rules.json"


def _norm(text: str) -> str:
    return " ".join(text.split())


def missing_rules(inventory: list[dict[str, str]], root: Path) -> list[str]:
    """Return 'file: rule' for every entry whose phrase is not in its file."""
    missing = []
    for entry in inventory:
        path = root / entry["file"]
        body = _norm(path.read_text()) if path.is_file() else ""
        if _norm(entry["phrase"]) not in body:
            missing.append(f"{entry['file']}: {entry['rule']}")
    return missing


def test_every_pinned_phrase_is_present() -> None:
    inventory = json.loads(INVENTORY.read_text())
    assert missing_rules(inventory, REPO_ROOT) == []


def test_gate_passes_when_present_and_fails_when_absent(tmp_path: Path) -> None:
    (tmp_path / "agent.md").write_text("Rule one:\nkeep   this phrase intact.\n")
    present = {"file": "agent.md", "rule": "r1", "phrase": "keep this phrase intact"}
    absent = {"file": "agent.md", "rule": "r2", "phrase": "a dropped rule"}
    gone = {"file": "nope.md", "rule": "r3", "phrase": "keep this phrase intact"}
    assert missing_rules([present], tmp_path) == []
    assert missing_rules([present, absent, gone], tmp_path) == [
        "agent.md: r2",
        "nope.md: r3",
    ]


@pytest.mark.parametrize("key", ["file", "rule", "phrase"])
def test_inventory_entries_are_well_formed(key: str) -> None:
    for entry in json.loads(INVENTORY.read_text()):
        assert entry.get(key), f"inventory entry missing {key!r}: {entry}"
