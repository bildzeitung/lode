r"""Guards docs/decisions.md's supersession-marker convention (lode-ur6o).

The convention itself is stated once, in that file's own preamble; this module
deliberately does not restate it, so the two cannot drift. It pins two
properties, because either alone leaves a hole:

*No off-pattern marker.* A stale-flag keyword opening a bold span, a
blockquote, or a parenthetical, with no ``Update (`` on the line to make it
greppable. The rule is structural rather than a list of the exact sentences
lode-ur6o rewrote, so a NEW shape (``**(Retracted, lode-x)**``) goes red too,
not only a verbatim revert of one of the six -- a denylist of removed
phrasings would have left the very hole the ticket was filed about. Ordinary
lowercase narrative use of "superseded"/"falsified" mid-sentence, of which
this file has plenty describing a ticket's own history, is untouched by
design.

*No wrapped marker.* A marker whose id wraps onto the next markdown line is
still invisible to the single-line grep the preamble documents: correct shape,
useless result. lode-ur6o hit exactly this on two of its own six
normalizations -- the fix for the ticket reintroducing the ticket's own
defect. The off-pattern scan cannot see it (nothing off-pattern is present),
so it is pinned separately.

Both scans are module-level helpers taking their lines as a parameter, so the
sabotage tests below can prove each actually fires on a violation rather than
passing vacuously -- same shape as tests/test_keybindings_doc.py.

KNOWN LIMITATION (lode-nlk6): no check here -- neither scan, nor the
preamble-states-the-rule check -- can detect a SILENT IN-PLACE REWRITE, the
exact failure the preamble's own sentence forbids. Every check keys on an
artifact a *marker* leaves behind (an off-pattern keyword, a wrapped id, the
preamble's own wording); a silent rewrite is the ABSENCE of a correction, so
it leaves nothing for any of them to key on and every check stays green. This
is a limit of THIS GATE, not a hole in the convention: the convention still
binds. Closing it would mean diffing an entry against its own git history, a
materially different and more expensive check than the text scans below.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "decisions.md"

_OFF_PATTERNS: dict[str, re.Pattern[str]] = {
    "stale-flag keyword opening a bold span, blockquote, or parenthetical": re.compile(
        r"(?:\*\*|\(|^\s*>\s*\**)"
        r"(?:Superseded|Falsified|FALSIFIED|Obsolete|OBSOLETE|Retracted|RETRACTED"
        r"|Outdated|OUTDATED|Amendment|AMENDMENT)\b"
    ),
    "ALL-CAPS 'SUPERSEDED' marker keyword": re.compile(r"\bSUPERSEDED\b"),
    "'<claim> is falsified by <id>' sentence": re.compile(r"is falsified by lode-"),
}


def _off_pattern_markers(lines: list[str]) -> list[str]:
    """Lines carrying a stale-flag marker in a shape the documented grep misses.

    A line that already contains ``Update (`` is exempt whatever else it says:
    the property under guard is greppability, not phrasing purity.
    """
    return [
        f"  L{lineno} ({description}): {line.strip()}"
        for lineno, line in enumerate(lines, start=1)
        for description, pattern in _OFF_PATTERNS.items()
        if pattern.search(line) and "Update (" not in line
    ]


def _wrapped_markers(lines: list[str]) -> list[str]:
    """Canonical markers whose ``(<id>...)`` does not close on the same line."""
    return [
        f"  L{lineno}: {line.strip()}"
        for lineno, line in enumerate(lines, start=1)
        if "**Update (" in line and ")" not in line.split("**Update (", 1)[1]
    ]


def _decisions_lines() -> list[str]:
    return DECISIONS.read_text(encoding="utf-8").splitlines()


def test_preamble_states_the_supersession_convention_once() -> None:
    """Acceptance criterion: the convention is stated once, in the preamble --
    not per-entry, and not left implicit in whichever entry happens to need it
    first."""
    text = DECISIONS.read_text(encoding="utf-8")
    # The preamble is everything before the log's first dated entry.
    preamble = text.split("\n- **", 1)[0]

    # The leading '**' is load-bearing: it is what the documented grep matches,
    # so a spec that omits it teaches a shape the grep cannot find.
    assert "**Update (<id>" in preamble, (
        "docs/decisions.md's preamble no longer states the supersession-marker "
        "shape -- it must document '**Update (<id>[, <date>])' once, up front, "
        "asterisks included (lode-ur6o)."
    )
    assert "grep" in preamble.lower(), (
        "docs/decisions.md's preamble no longer tells a reader how to find "
        "every stale claim by grepping for the marker shape (lode-ur6o)."
    )


def test_no_off_pattern_supersession_markers() -> None:
    """Every marker flagging a claim elsewhere in this file as stale must carry
    the greppable ``**Update (<id>...)`` lead-in."""
    offenders = _off_pattern_markers(_decisions_lines())

    assert not offenders, (
        "docs/decisions.md has supersession marker(s) with no '**Update "
        "(<id>...)' lead-in, so they are invisible to the grep everyone uses "
        "to find stale claims (lode-ur6o):\n" + "\n".join(offenders)
    )


def test_off_pattern_scan_catches_a_reintroduced_marker() -> None:
    """Non-vacuity: the scan above must fire on each shape lode-ur6o removed,
    and on a shape it never saw."""
    reintroduced = [
        "- **(Superseded by lode-zzzz, below: the inline aside.)**",
        "  > **SUPERSEDED (lode-zzzz, 2026-01-01) - the blockquote.**",
        "  the claim above is falsified by lode-zzzz, and the gap is closed.",
        "  **Superseded for the matching *shape* by the lode-zzzz entry below.**",
        "  *(Itself since SUPERSEDED by lode-zzzz -- the italic aside.)*",
        "- **(Retracted, lode-zzzz: a shape lode-ur6o never encountered.)**",
        "**AMENDMENT (`lode-zzzz`):** the exact lode-hg49 lead-in shape (lode-125q).",
        "**RETRACTED (lode-zzzz):** all-caps lead-in for the remaining keywords (lode-bv9o).",
        "**OBSOLETE (lode-zzzz):** all-caps lead-in for the remaining keywords (lode-bv9o).",
        "**FALSIFIED (lode-zzzz):** all-caps lead-in for the remaining keywords (lode-bv9o).",
        "**OUTDATED (lode-zzzz):** all-caps lead-in for the remaining keywords (lode-bv9o).",
    ]
    caught = _off_pattern_markers(reintroduced)

    assert len(caught) >= len(reintroduced), (
        "the off-pattern scan no longer flags every off-pattern shape -- it "
        f"caught {len(caught)} of {len(reintroduced)}:\n" + "\n".join(caught)
    )


def test_off_pattern_scan_ignores_lowercase_narrative_prose() -> None:
    """The other direction: describing a ticket's history in ordinary prose is
    not a marker, and must not go red."""
    narrative = [
        "  counter carried across each supersede, escalating to `land-escalated`",
        "  The first build attempt (superseded -- see below) wired the workflow",
        "  `lode-hwbm`'s reviewer falsified its ticket's entire premise with two",
        "  decision gets off, and is now **closed as superseded**, not built",
        "  behind by a *previous*, already-superseded dispatch (the guard runs)",
    ]

    assert not _off_pattern_markers(narrative), (
        "the off-pattern scan now false-positives on ordinary lowercase prose, "
        "which would push authors toward marking non-markers (lode-ur6o)."
    )


def test_every_canonical_marker_resolves_on_one_line() -> None:
    """A marker whose id wraps to the next line is still invisible to the
    single-line grep the preamble documents."""
    wrapped = _wrapped_markers(_decisions_lines())

    assert not wrapped, (
        "docs/decisions.md has supersession marker(s) whose '(<id>...)' wraps "
        "to the next line, so `grep -n '\\*\\*Update ('` finds the marker but "
        "not the ticket it points at -- keep '**Update (<id>...)' on one line "
        "(lode-ur6o):\n" + "\n".join(wrapped)
    )


def test_wrap_scan_catches_a_line_wrapped_marker() -> None:
    """Non-vacuity for the wrap scan, using the exact defect lode-ur6o's own
    first pass shipped."""
    assert _wrapped_markers(["  **Update (lode-", "  zzzz) - the wrapped id.**"])
    assert not _wrapped_markers(["  **Update (lode-zzzz) - the same id, intact.**"])
