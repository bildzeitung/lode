"""Guards docs/decisions.md's supersession-marker convention (lode-ur6o).

docs/decisions.md is a dated, append-only log: an entry is never edited in
place when later work makes it stale -- the correction is a new entry, or a
marker appended to the existing one. That marker used to have FIVE different
shapes across the file (a bolded "Update (<id>, <date>) - ..." lead-in, an
inline "(Superseded by <id>, below: ...)" aside, a blockquoted "> **SUPERSEDED
(<id>, <date>) - ...**", a "<claim> is falsified by <id>" sentence, and a
"Superseded for the matching *shape* ... by <id>" sentence) -- and
"grep 'Update (lode-'", the way a reader (or an agent) actually locates stale
claims in this file, only ever found the first. lode-ur6o normalized every
marker to the single dominant shape and stated the convention once in the
file's own preamble; this test is the cheap regression guard the ticket asked
for, in the same spirit as tests/test_isolation_guard.py pinning the agent
frontmatter key -- a scan over the shipped file, not a reimplementation of
its content.

Deliberately narrow: it does not try to recognize every conceivable future
phrasing of "this is stale" (that would be unfalsifiable prose-matching). It
pins the exact off-pattern shapes lode-ur6o eliminated, so reintroducing any
of them -- or reverting one of that ticket's normalizations -- goes red.
Legitimate narrative use of the words "superseded" / "falsified" in ordinary,
lowercase, non-marker prose (there is plenty in this file, describing a
ticket's own history) is untouched by design: only the ALL-CAPS "SUPERSEDED"
marker keyword and the specific removed sentence shapes are checked.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "decisions.md"

# The exact off-pattern shapes lode-ur6o normalized away. Each is a narrow,
# literal signature of one of the five non-dominant marker shapes -- not a
# general "does this look stale" heuristic.
_OFF_PATTERNS: dict[str, re.Pattern[str]] = {
    "ALL-CAPS 'SUPERSEDED' marker (blockquoted or bolded)": re.compile(
        r"\bSUPERSEDED\b"
    ),
    "inline '(Superseded by <id>, below: ...)' aside": re.compile(r"\(Superseded by "),
    "'<claim> is falsified by <id>' sentence": re.compile(r"is falsified by lode-"),
    "'Superseded for the matching *shape* ... by <id>' sentence": re.compile(
        r"Superseded for the matching"
    ),
}


def test_decisions_file_exists() -> None:
    assert DECISIONS.is_file(), f"{DECISIONS} not found -- has docs/ moved?"


def test_preamble_states_the_supersession_convention_once() -> None:
    """Acceptance criterion: the convention is stated once, in the preamble
    -- not per-entry, and not left implicit in whichever entry happens to
    need it first."""
    text = DECISIONS.read_text(encoding="utf-8")
    # The preamble is everything before the log's first dated entry.
    preamble = text.split("\n- **", 1)[0]

    assert "Update (<id>" in preamble, (
        "docs/decisions.md's preamble no longer states the supersession-marker "
        "shape -- it must document 'Update (<id>[, <date>]) - ...' once, up "
        "front (lode-ur6o)."
    )
    assert "grep" in preamble.lower(), (
        "docs/decisions.md's preamble no longer tells a reader how to find "
        "every stale claim by grepping for the marker shape (lode-ur6o)."
    )


def test_no_off_pattern_supersession_markers() -> None:
    """Every marker that flags a claim elsewhere in this file as stale must
    use the single, greppable 'Update (<id>[, <date>]) - ...' shape -- the
    five shapes lode-ur6o normalized away must not come back."""
    lines = DECISIONS.read_text(encoding="utf-8").splitlines()

    offenders: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        for description, pattern in _OFF_PATTERNS.items():
            if pattern.search(line) and "Update (" not in line:
                offenders.append(f"  L{lineno} ({description}): {line.strip()}")

    assert not offenders, (
        "docs/decisions.md has supersession marker(s) that don't match the "
        "single dominant 'Update (<id>[, <date>]) - ...' shape, invisible to "
        "the grep everyone uses to find stale claims (lode-ur6o):\n"
        + "\n".join(offenders)
    )
