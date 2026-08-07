"""Gate: `/sweep` §2b's `--exclude-label` roster covers every pipeline-stage label this repo
applies to a ticket (lode-mm73 item 1; discovered while technically reviewing lode-ppki).

## The bug this closes

`.claude/skills/sweep/SKILL.md` §2b's `--exclude-label` list
(`ready-for-code-review,ready-for-land,needs-rebase,sweep-digest,land-escalated`) is the only place
in the repo that must enumerate ALL pipeline stage labels — every other call site names exactly one
(`.claude/skills/code/SKILL.md`, `.claude/skills/land/SKILL.md`,
`.claude/skills/epic-audit/SKILL.md`, `/sweep` §1/§2). It is therefore the only site that rots
silently when a new stage label is introduced: a ticket carrying that new label, while still
`in_progress`, would start reading as stranded, with no test failure and no grep that finds it.

The repo has already answered this exact class of problem twice: `lode-jhry` deleted a gate roster
from `agents-workflow.md` as the staleness anti-pattern, and `lode-200t` added
`tests/test_bd_list_limit_gate.py` precisely because a documented roster "is no longer what enforces
this". This gate applies the same remedy here: it FAILS when it finds a `--add-label`/`bd label add`
site anywhere in `.claude/skills/*/SKILL.md` or `.claude/agents/*.md` applying a label §2b's roster
does not cover — unless that label is documented in `EPIC_ONLY_LABELS` below as one that can never
reach §2b's query in the first place. DECIDED shape: `docs/decisions.md`, entry "`/sweep` §2b ...
hand-maintained pipeline-label roster is enforced by a GATE TEST".

## Why `EPIC_ONLY_LABELS` is a legitimate exemption, not a second hand-maintained roster

§2b's query is `bd list --status in_progress ...` — it can only ever surface a ticket sitting in
`status=in_progress`. `epic-debated` (`.claude/skills/challenge/SKILL.md`), `epic-ready-to-audit`
(`.claude/skills/land/SKILL.md`), and `epic-audited` (`.claude/skills/epic-audit/SKILL.md`) are
stamped exclusively onto `type: epic` issues, which this repo's own epic lifecycle keeps at
`status: open` throughout labeling (see `.claude/skills/epic-audit/SKILL.md`'s auditable-epic
definition: `issue_type == "epic" and status != "closed"`, never `in_progress`) — an epic never
transitions through `in_progress` the way a task/bug ticket does. So a label here can never make a
ticket §2b's query would otherwise see stranding, and adding it to §2b's `--exclude-label` list would
be a pure no-op against the real query, which is exactly the "roster grows regardless of whether it
does anything" shape `lode-ppki`'s design deliberately keeps narrow (see §2b's own "deliberately not
the fuller set §1 might suggest" note). `test_epic_only_labels_are_still_live` below keeps this list
itself from rotting the same way the roster it exempts from could.

## Scan surface and mechanics

Deliberately simpler than `test_bd_list_limit_gate.py`'s fence-aware scan: every `--add-label`/
`bd label add` site found in this repo's history is either inside a fenced ```bash block or an
inline single-backtick span in prose describing that exact same invocation (e.g.
`.claude/skills/challenge/SKILL.md`'s "I stamp it: `bd update <epic-id> --add-label epic-debated`"),
never inside a comment or a sentence describing an unrelated command — so a plain regex over the raw
file text finds every real site with no observed false positive, and adding fence/comment-awareness
would cost real complexity for zero measured benefit on this corpus. If a future false positive shows
up, add it to `NON_PIPELINE_LABELS` with a reason (mirroring `EPIC_ONLY_LABELS`'s shape) rather than
narrowing the regex — see `test_bd_list_limit_gate.py`'s own docstring for why an explicit, reasoned
skip beats a cleverer pattern.

`ADD_LABEL_RE` matches `--add-label` (or `--add-label=`) followed by one or more `-`/alphanumeric
label tokens, optionally wrapped in `<...>` and separated by `|` or `,` — the `<ready-for-code-review
|needs-rebase>` placeholder shape `.claude/skills/land/SKILL.md:2097` uses for "pick one of these two"
prose parses to both underlying names, not the literal placeholder text. `LABEL_ADD_CMD_RE` matches
the `bd label add <target> <label>` form (`.claude/skills/epic-audit/SKILL.md`,
`.claude/skills/land/SKILL.md`'s epic-completion loop) — a different bd subcommand than
`bd update --add-label`, used for a single ID instead of a variable ticket in a shared loop.

## The exclude-label roster is read from the shipped file, not re-typed here

`_sweep_exclude_labels()` parses §2b's actual `--exclude-label` line out of `sweep/SKILL.md` rather
than hand-copying the list into this test file a second time — the whole point of this gate is to
catch that list falling behind reality, so the gate must compare against the REAL, currently-shipped
roster, not a second copy of it that could itself drift unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
SWEEP_SKILL = SKILLS_DIR / "sweep" / "SKILL.md"

# Same scan surface as test_bd_list_limit_gate.py's MD_GLOBS: skills a Claude Code agent executes
# bash out of, and subagent definitions (.claude/agents/*.md), which carry just as many operative
# `bd update`/`bd label add` invocations as a SKILL.md.
SCAN_GLOBS = [(SKILLS_DIR, "*/SKILL.md"), (AGENTS_DIR, "*.md")]

_LABEL_TOKEN = r"[A-Za-z][A-Za-z0-9-]*"

# `--add-label foo`, `--add-label=foo`, `--add-label foo,bar`, or the placeholder
# `--add-label <foo|bar>` shape -- see the module docstring for why each is real.
ADD_LABEL_RE = re.compile(
    rf"--add-label[= ]+<?({_LABEL_TOKEN}(?:[|,]\s*{_LABEL_TOKEN})*)>?"
)

# `bd label add <target> <label>` -- a different bd subcommand than `bd update --add-label`.
LABEL_ADD_CMD_RE = re.compile(rf"\bbd label add\s+\S+\s+({_LABEL_TOKEN})")

# Labels stamped exclusively onto `type: epic` issues, which stay `status: open` throughout
# labeling and so can never reach §2b's `--status in_progress` query -- see the module docstring's
# "Why EPIC_ONLY_LABELS is a legitimate exemption" section for the full argument. Each entry names
# the site that applies it, so a reader can verify the claim without re-deriving it.
EPIC_ONLY_LABELS: dict[str, str] = {
    "epic-debated": (
        ".claude/skills/challenge/SKILL.md -- stamped on an epic after a /challenge pass; "
        "epics stay status=open, never in_progress."
    ),
    "epic-ready-to-audit": (
        ".claude/skills/land/SKILL.md -- stamped on an epic when /land closes its last "
        "parent-child child; epics stay status=open, never in_progress."
    ),
    "epic-audited": (
        ".claude/skills/epic-audit/SKILL.md -- stamped on an epic once /epic-audit reviews "
        "it; epics stay status=open, never in_progress."
    ),
}


def _discover_add_label_sites() -> set[str]:
    """Every label this repo applies via `--add-label`/`bd label add`, across the scan surface."""
    found: set[str] = set()
    for base, pattern in SCAN_GLOBS:
        for path in sorted(base.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            for m in ADD_LABEL_RE.finditer(text):
                for token in re.split(r"[|,]\s*", m.group(1)):
                    found.add(token)
            for m in LABEL_ADD_CMD_RE.finditer(text):
                found.add(m.group(1))
    return found


def _sweep_exclude_labels() -> set[str]:
    """The literal `--exclude-label` roster §2b's bash block passes to `bd list`, parsed straight
    from the shipped file -- see the module docstring's final section for why this must not be a
    second hand-copied list."""
    text = SWEEP_SKILL.read_text(encoding="utf-8")
    # §1's own `bd list --label land-escalated --exclude-label sweep-digest ...` (excluding only
    # its own digest issue) is a DIFFERENT, single-label --exclude-label site earlier in this same
    # file -- picking the match with the most comma-separated tokens is what selects §2b's roster
    # rather than §1's, without hard-coding a line number that would silently go stale if the file
    # is reordered.
    matches = re.findall(r"--exclude-label\s+([\w,-]+)", text)
    assert matches, (
        "could not find any --exclude-label list in .claude/skills/sweep/SKILL.md -- did "
        "the section move, get reworded, or lose its --exclude-label flag? Update this "
        "gate's parsing if the shape genuinely changed."
    )
    roster = max(matches, key=lambda m: m.count(","))
    return set(roster.split(","))


# =====================================================================================
# Unit tests -- the regex's own precision, against synthetic snippets.
# =====================================================================================


def test_bare_add_label_matches() -> None:
    assert ADD_LABEL_RE.search("bd update <id> --add-label needs-rebase")
    assert {
        tok
        for m in ADD_LABEL_RE.finditer("bd update <id> --add-label needs-rebase")
        for tok in re.split(r"[|,]\s*", m.group(1))
    } == {"needs-rebase"}


def test_multi_label_placeholder_yields_both_names() -> None:
    text = "bd update <id> --remove-label land-escalated --add-label <ready-for-code-review|needs-rebase>"
    labels = {
        tok
        for m in ADD_LABEL_RE.finditer(text)
        for tok in re.split(r"[|,]\s*", m.group(1))
    }
    assert labels == {"ready-for-code-review", "needs-rebase"}


def test_bd_label_add_command_form_matches() -> None:
    assert {
        m.group(1)
        for m in LABEL_ADD_CMD_RE.finditer('bd label add "$PARENT" epic-ready-to-audit')
    } == {"epic-ready-to-audit"}


def test_comma_separated_labels_both_captured() -> None:
    labels = {
        tok
        for m in ADD_LABEL_RE.finditer("bd update <id> --add-label foo,bar")
        for tok in re.split(r"[|,]\s*", m.group(1))
    }
    assert labels == {"foo", "bar"}


# =====================================================================================
# The gate itself, against the real, shipped files.
# =====================================================================================


def test_sweep_exclude_label_list_parses_the_shipped_roster() -> None:
    """Sanity check on the parser itself -- pins today's known-good roster so a parsing
    regression (not a real roster change) shows up here first, distinct from the gate below."""
    assert _sweep_exclude_labels() == {
        "ready-for-code-review",
        "ready-for-land",
        "needs-rebase",
        "sweep-digest",
        "land-escalated",
    }


def test_epic_only_labels_are_still_live() -> None:
    """Stale-entry guard, mirroring test_bd_list_limit_gate.py's `test_every_skip_entry_is_live_
    and_justified`: an EPIC_ONLY_LABELS entry that stops matching anything in the corpus is dead
    weight nobody can audit, not a routine no-op -- if a label's only call site is deleted or
    renamed, this entry must be removed too, not linger."""
    discovered = _discover_add_label_sites()
    stale = set(EPIC_ONLY_LABELS) - discovered
    assert not stale, (
        f"EPIC_ONLY_LABELS entries no longer found anywhere in the scanned corpus -- delete "
        f"them: {sorted(stale)}"
    )


def test_every_pipeline_label_is_covered_by_sweep_2b_or_documented_epic_only() -> None:
    """The actual gate (lode-mm73 item 1). Every label this repo applies to a TICKET via
    `--add-label`/`bd label add` must appear in §2b's live `--exclude-label` roster, or be a
    documented EPIC_ONLY_LABELS exemption -- otherwise a ticket carrying that label, while still
    in_progress, silently reads as stranded, exactly the class of drift §2b's roster exists to
    avoid."""
    discovered = _discover_add_label_sites()
    covered = _sweep_exclude_labels() | set(EPIC_ONLY_LABELS)
    uncovered = discovered - covered
    assert not uncovered, (
        f"new pipeline-stage label(s) found via --add-label/bd label add with no §2b "
        f"exclude-label coverage: {sorted(uncovered)}. If a ticket can carry this label "
        f"while still in_progress, add it to §2b's --exclude-label list in "
        f".claude/skills/sweep/SKILL.md. If it is stamped ONLY on epics (which never reach "
        f"status=in_progress), add it to EPIC_ONLY_LABELS in this file instead, with a "
        f"reason naming the site."
    )


def test_sabotage_new_uncovered_label_is_flagged() -> None:
    """Confirm the gate's own comparison actually fires on a genuinely new, uncovered label --
    guards against a vacuous pass the same way test_bd_list_limit_gate.py's sabotage tests do,
    without needing to edit a real shipped file to prove it (this gate's assertion is a pure set
    difference, not a text scan of a specific site, so a synthetic input suffices)."""
    covered = _sweep_exclude_labels() | set(EPIC_ONLY_LABELS)
    discovered = covered | {"totally-new-pipeline-label"}
    uncovered = discovered - covered
    assert uncovered == {"totally-new-pipeline-label"}
