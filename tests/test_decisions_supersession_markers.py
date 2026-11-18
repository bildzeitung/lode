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
design. A marker that predates its pattern, and cannot be normalized without
rewriting a committed line, is excused by name in ``_ALLOWLIST``. The excused
line itself stays invisible to the documented grep, so its entry carries an
appended ``Update (`` pointer that the grep does find (lode-4eyp).

*Correction lead-ins are matched more narrowly than the other keywords.*
"Correction" also opens prose in which an entry narrates its OWN history,
which the preamble exempts, so the bare keyword is not enough. It is flagged
only where it opens a bold span, blockquote or parenthetical AND is followed
-- through punctuation or ``by`` -- by an id, a date or
``maintainer decision``.

KNOWN MISSES in that narrower match, accepted (lode-4eyp):

- Prose between the keyword and its id (``**Correction (see lode-x)``,
  ``**Correction to the lode-x entry above``). By shape alone this is
  indistinguishable from own-history narration.
- A keyword whose id wraps onto the next line.

Accepted over-match, the reverse error: a date straight after the keyword goes
red even where the entry is narrating its own history. The way out is to
reword.

*A correcting entry's head is not a marker* (the preamble's lode-llxw
paragraph), so no pattern here targets one. Two limits follow:

- Nothing checks that a corrected entry actually received its appended
  marker. A missing marker leaves nothing for a scan to key on.
- The scan reads one line at a time and cannot tell an entry head from a
  marker, so a head that itself opens with a keyword in a gated shape
  (``- **Correction (lode-x) to the entry above``) still goes red. The way out
  is to reword the head. Whether heads should be exempt is lode-hp12.

*No wrapped marker.* A marker whose id wraps onto the next markdown line is
still invisible to the single-line grep the preamble documents: correct shape,
useless result. lode-ur6o hit exactly this on two of its own six
normalizations -- the fix for the ticket reintroducing the ticket's own
defect. The off-pattern scan cannot see it (nothing off-pattern is present),
so it is pinned separately.

Both scans are module-level helpers taking their lines as a parameter, so the
sabotage tests below can prove each actually fires on a violation rather than
passing vacuously -- same shape as tests/test_keybindings_doc.py.

FORMER KNOWN LIMITATION (lode-nlk6), now covered by a SEPARATE mechanism
(lode-rl6s): none of the checks in THIS module -- neither scan, nor the
preamble-states-the-rule check -- can detect a SILENT IN-PLACE REWRITE, the
exact failure the preamble's own sentence forbids. Every check here keys on
an artifact a *marker* leaves behind (an off-pattern keyword, a wrapped id,
the preamble's own wording); a silent rewrite is the ABSENCE of a correction,
so it leaves nothing for any of them to key on and every check here stays
green. That gap is real but is NOT closed in this module: it is closed by
`scripts/check-decisions-no-silent-rewrite.sh` +
`tests/test_decisions_no_silent_rewrite_guard.py`, which diff a SPECIFIC
base...head range of docs/decisions.md's git history and fail on any
pre-existing, non-blank line that vanished. That guard is scoped to one
branch's diff, deliberately and for reasons of its own -- see its header,
not a restatement here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DECISIONS = REPO_ROOT / "docs" / "decisions.md"

# Marker vocabulary, matched in TitleCase and ALL-CAPS but deliberately NOT
# lowercase: a case-insensitive group false-positives on ordinary parenthesised
# narrative such as "(superseded -- see below)", which
# test_off_pattern_scan_ignores_lowercase_narrative_prose pins. Both spellings
# are DERIVED from a keyword tuple rather than typed out twice, so the rule is
# stated once and a keyword cannot land half-applied -- which is exactly how
# lode-125q and lode-bv9o each arrived, one case variant at a time.
_MARKER_KEYWORDS = (
    "Superseded",
    "Falsified",
    "Obsolete",
    "Retracted",
    "Outdated",
    "Amendment",
)
# A tuple of their own because their lead-in pattern's suffix is narrower than
# the group above -- see the module docstring's Correction paragraph.
_CORRECTION_KEYWORDS = ("Correction", "Corrections", "Corrected")


def _both_cases(keywords: tuple[str, ...]) -> str:
    return "|".join(
        spelling for keyword in keywords for spelling in (keyword, keyword.upper())
    )


_DATE = r"\d{4}-\d{2}-\d{2}"
# The three things the preamble allows in a marker's id slot.
_REF = rf"(?:lode-|{_DATE}|maintainer decision)"
# \W+ rather than a list of connectors: every punctuation run between keyword
# and id is the same lead-in, closing asterisks and an id's backtick included.
_CORRECTION_TAIL = rf"(?:\W+|\s+(?:by|BY)\s+){_REF}"

# Where a marker lead-in can open: a bold span, a parenthetical, or a blockquote.
_OPENER = r"(?:\*\*|\(|^\s*>\s*\**)"

_OFF_PATTERNS: dict[str, re.Pattern[str]] = {
    "stale-flag keyword opening a bold span, blockquote, or parenthetical": re.compile(
        rf"{_OPENER}(?:{_both_cases(_MARKER_KEYWORDS)})\b"
    ),
    # Deliberately NOT anchored to a bold span/parenthetical: this one keyword
    # is banned outright, anywhere on a line. It therefore overlaps the group
    # above (a span-anchored occurrence matches both, and is reported twice) --
    # harmless, since every consumer asks only whether a line matched at all.
    "ALL-CAPS 'SUPERSEDED' marker keyword": re.compile(r"\bSUPERSEDED\b"),
    "'<claim> is falsified by <id>' sentence": re.compile(r"is falsified by lode-"),
    # Narrower than the bare keyword on purpose -- see the module docstring's
    # Correction paragraph (lode-zkki, lode-4eyp).
    "Correction-style lead-in opening a bold span, blockquote, or parenthetical": re.compile(
        rf"{_OPENER}(?:{_both_cases(_CORRECTION_KEYWORDS)}){_CORRECTION_TAIL}"
    ),
}

# Pre-existing off-pattern lead-ins that stay as written, keyed by a substring
# of the line and carrying the reason. Normalizing one in place deletes a
# committed line, which scripts/check-decisions-no-silent-rewrite.sh fails and
# the preamble's append-only rule bars -- so it is excused here instead.
_ALLOWLIST: dict[str, str] = {
    "**Correction (`lode-rtcx`) to the `lode-moq7` entry above": (
        "committed before the Correction pattern existed (lode-zkki)"
    ),
}


def _off_pattern_markers(
    lines: list[str], allowlist: Iterable[str] = _ALLOWLIST
) -> list[str]:
    """Lines carrying a stale-flag marker in a shape the documented grep misses.

    A line that already contains ``Update (`` is exempt whatever else it says:
    the property under guard is greppability, not phrasing purity. A line
    containing an ``allowlist`` key is excused whichever pattern it matches.
    """
    return [
        f"  L{lineno} ({description}): {line.strip()}"
        for lineno, line in enumerate(lines, start=1)
        for description, pattern in _OFF_PATTERNS.items()
        if pattern.search(line)
        and "Update (" not in line
        and not any(allowed in line for allowed in allowlist)
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


def test_every_allowlist_entry_excuses_exactly_one_live_marker() -> None:
    """An allowlist entry excuses ONE vetted line. A key matching nothing is a
    dead excuse; a key matching a second line excuses a marker nobody vetted."""
    lines = _decisions_lines()
    for allowed, reason in _ALLOWLIST.items():
        assert reason.strip(), f"allowlist entry {allowed!r} carries no reason"
        excused = [line for line in lines if allowed in line]
        assert len(excused) == 1, (
            f"allowlist entry {allowed!r} must match exactly one line of "
            f"docs/decisions.md, but matches {len(excused)}"
        )
        assert _off_pattern_markers(excused, allowlist=()), (
            f"allowlist entry {allowed!r} excuses a line the scan would not "
            "flag anyway -- drop the entry"
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
        "**Correction, 2026-11-16 (lode-zzzz)** to the lode-yyyy entry above.",
        "- **Correction (`lode-zzzz`) to the lode-yyyy entry above's sentence.**",
        "  > **CORRECTION (lode-zzzz, 2026-01-01) - the blockquote.**",
        "- **(Corrected, lode-zzzz: the parenthetical.)**",
        "- **Correction** (`lode-zzzz`): the closed-bold near miss.",
        "**Correction:** lode-zzzz changes the sentence above.",
        "**Correction \u2014 lode-zzzz, 2026-01-01** to the entry above.",
        "**Corrected by lode-zzzz** in the entry below.",
        "**Corrections (lode-zzzz)** to the two entries above.",
        "**Correction, maintainer decision:** the id slot filled with prose.",
        "**CORRECTIONS (lode-zzzz)** all-caps plural.",
        "**CORRECTED BY lode-zzzz** all-caps connector.",
        "**Correction** \u2014 `lode-zzzz`: closed bold, then a dash.",
    ]
    # Scanned one line at a time on purpose: a whole-list count can be met by
    # one line matching two patterns while another matches none, which would
    # silently mask a dropped keyword. Per-line, each shape must stand alone.
    missed = [line for line in reintroduced if not _off_pattern_markers([line])]

    assert not missed, (
        "the off-pattern scan no longer flags every off-pattern shape -- these "
        "went unnoticed:\n" + "\n".join(missed)
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
        "  - **Correction the original proposal did not anticipate, verified on bash:** x",
        "  **Correction to this entry's first draft:** it asserted the guard would be",
        "  **Correction, verified empirically on bash 5.2:** the guard did fire.",
        "  (Corrected, then re-verified.) The first run was misread.",
        "  the correction (see below) was applied; corrected in place by the author",
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
