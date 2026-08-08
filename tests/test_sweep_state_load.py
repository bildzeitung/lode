"""Pins lode-3oik: `.claude/skills/sweep/SKILL.md`'s `$SWEEP_TMP` load cluster
adopts `scripts/land-state-load.sh` (lode-dc4n) for its "default" policy sites
(missing fatal, empty OK), and the two `2>/dev/null` sites (deferred/stranded
in the report/publish section) are deliberately left alone -- their
missing-file behaviour is a non-fatal third state, which neither of
`land-state-load.sh`'s two policies expresses.

Same discipline as `tests/test_land_conflicts_state.py`'s exact call-site
pins: this asserts the literal call each retrofitted site makes, never a
character-proximity/distance check, so a future edit that quietly reverts a
site to a bare `cat` (silently flipping its policy back to a hand-rolled
spelling -- the exact hazard lode-dc4n's own rationale names) turns this file
red.

Pinned against the SHIPPED SKILL.md, not a reimplementation -- the bug class
this guards against lives in a markdown fence no other gate parses.
"""

from __future__ import annotations

from conftest import SWEEP_SKILL_BLOCKS, only_block_with


def _skill_blocks() -> list[str]:
    return SWEEP_SKILL_BLOCKS


def _only_block_with(*needles: str, what: str) -> str:
    return only_block_with(_skill_blocks(), *needles, what=what)


def _section_3_block() -> str:
    """§3 -- builds $CURRENT from $ESCALATED/$HUMAN/$CLOSABLE."""
    return _only_block_with(
        "CURRENT=$(printf",
        "sort -u -t$'\\t' -k1,1",
        what="§3's current-queue build",
    )


def _section_6_prep_block() -> str:
    """§6's precondition/prep block -- re-derives DIGEST_ID and $CURRENT."""
    return _only_block_with(
        "sweep-digest-id.sh",
        "LAST_BODY=$(bd show",
        what="§6's digest-rewrite precondition/prep",
    )


def _section_7_block() -> str:
    """§7 -- splits new ids into report-it vs push-it."""
    return _only_block_with(
        "new_annotated",
        "push_ids",
        "awk -F'\\t'",
        what="§7's new-ids split",
    )


def _section_8_block() -> str:
    """§8 -- deferred/stranded 3-valued state + publish."""
    return _only_block_with(
        "bd-dolt-push.sh",
        "STRANDED_STATE",
        what="§8's publish/report",
    )


# ---------------------------------------------------------------------------
# Retrofitted sites: exact call to scripts/land-state-load.sh, default policy
# (no --require-nonempty -- these five sites all had "missing fatal, empty
# OK" before the retrofit, never an emptiness check).
# ---------------------------------------------------------------------------


def test_section_3_escalated_uses_the_shared_loader_default_policy() -> None:
    site = _section_3_block()
    assert (
        'ESCALATED="$(scripts/land-state-load.sh "$SWEEP_TMP/escalated" -- \\' in site
    ), (
        "§3's $ESCALATED read no longer calls scripts/land-state-load.sh -- a "
        "reverted bare `cat` would silently drop the loud diagnostic (lode-3oik)"
    )
    assert "--require-nonempty" not in _only_line_for(site, "SWEEP_TMP/escalated"), (
        "§3's $ESCALATED read gained --require-nonempty -- it never had an "
        "emptiness check before the retrofit (lode-3oik policy-preservation)"
    )


def test_section_3_human_uses_the_shared_loader_default_policy() -> None:
    site = _section_3_block()
    assert 'HUMAN="$(scripts/land-state-load.sh "$SWEEP_TMP/human" -- \\' in site, (
        "§3's $HUMAN read no longer calls scripts/land-state-load.sh (lode-3oik)"
    )
    assert "--require-nonempty" not in _only_line_for(site, "SWEEP_TMP/human"), (
        "§3's $HUMAN read gained --require-nonempty -- it never had an "
        "emptiness check before the retrofit (lode-3oik policy-preservation)"
    )


def test_section_3_closable_uses_the_shared_loader_default_policy() -> None:
    site = _section_3_block()
    assert (
        'CLOSABLE="$(scripts/land-state-load.sh "$SWEEP_TMP/closable" -- \\' in site
    ), "§3's $CLOSABLE read no longer calls scripts/land-state-load.sh (lode-3oik)"
    assert "--require-nonempty" not in _only_line_for(site, "SWEEP_TMP/closable"), (
        "§3's $CLOSABLE read gained --require-nonempty -- it never had an "
        "emptiness check before the retrofit (lode-3oik policy-preservation)"
    )


def test_section_6_prep_current_uses_the_shared_loader_default_policy() -> None:
    site = _section_6_prep_block()
    assert 'CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \\' in site, (
        "§6 prep's $CURRENT read no longer calls scripts/land-state-load.sh (lode-3oik)"
    )
    assert "--require-nonempty" not in _only_line_for(site, "SWEEP_TMP/current"), (
        "§6 prep's $CURRENT read gained --require-nonempty -- it never had an "
        "emptiness check before the retrofit (lode-3oik policy-preservation)"
    )


def test_section_7_current_uses_the_shared_loader_default_policy() -> None:
    site = _section_7_block()
    assert 'CURRENT="$(scripts/land-state-load.sh "$SWEEP_TMP/current" -- \\' in site, (
        "§7's $CURRENT read no longer calls scripts/land-state-load.sh (lode-3oik)"
    )
    assert "--require-nonempty" not in _only_line_for(
        site, 'land-state-load.sh "$SWEEP_TMP/current"'
    ), (
        "§7's $CURRENT read gained --require-nonempty -- it never had an "
        "emptiness check before the retrofit (lode-3oik policy-preservation)"
    )


def test_section_7_new_ids_existence_check_is_left_alone() -> None:
    """§7's `[ -f "$SWEEP_TMP/new_ids" ]` is an EXISTENCE check, not a content
    load -- it never reads the file's bytes, only tests presence, and an
    empty-but-present file must run on through (lode-fm7t). Out of scope for
    the land-state-load.sh retrofit (not one of the ticket's listed sites);
    this test just pins that it is untouched."""
    site = _section_7_block()
    assert '[ -f "$SWEEP_TMP/new_ids" ] || {' in site, (
        "§7's $SWEEP_TMP/new_ids existence check changed shape -- re-verify by "
        "hand whether it should now be in scope for the loader retrofit"
    )


# ---------------------------------------------------------------------------
# Deliberately-not-retrofitted sites: §8's deferred/stranded reads. Pinned to
# stay on a bare `cat ... 2>/dev/null`, with an explicit note in the skill
# doc explaining why -- catches a future editor "helpfully" wiring these onto
# the shared loader's fatal-on-missing policy, which would flip a currently
# non-fatal, expected state (§2a/§2b never ran, or hasn't run yet) into a
# hard abort of §8's publish step.
# ---------------------------------------------------------------------------


def test_section_8_deferred_load_is_deliberately_not_retrofitted() -> None:
    site = _section_8_block()
    assert 'if DEFERRED="$(cat "$SWEEP_TMP/deferred" 2>/dev/null)"; then' in site, (
        "§8's $DEFERRED read changed shape -- if it now calls "
        "scripts/land-state-load.sh, re-verify DEFERRED_STATE=missing still "
        "means what this test's docstring says, and update the loader's own "
        "header comment (lode-3oik) to match"
    )
    assert "scripts/land-state-load.sh" not in _only_line_for(
        site, "SWEEP_TMP/deferred"
    ), (
        "§8's $DEFERRED read now calls scripts/land-state-load.sh, but that "
        "script's two policies both treat a missing file as FATAL -- §8 treats "
        "it as a non-fatal, expected DEFERRED_STATE=missing (lode-3oik)"
    )
    assert "lode-3oik" in site, (
        "§8's block lost the one-line note explaining why the deferred/"
        "stranded reads are deliberately not retrofitted onto "
        "scripts/land-state-load.sh (acceptance criterion, lode-3oik)"
    )


def test_section_8_stranded_load_is_deliberately_not_retrofitted() -> None:
    site = _section_8_block()
    assert 'if STRANDED="$(cat "$SWEEP_TMP/stranded" 2>/dev/null)"; then' in site, (
        "§8's $STRANDED read changed shape -- if it now calls "
        "scripts/land-state-load.sh, re-verify STRANDED_STATE=missing still "
        "means what this test's docstring says, and update the loader's own "
        "header comment (lode-3oik) to match"
    )
    assert "scripts/land-state-load.sh" not in _only_line_for(
        site, "SWEEP_TMP/stranded"
    ), (
        "§8's $STRANDED read now calls scripts/land-state-load.sh, but that "
        "script's two policies both treat a missing file as FATAL -- §8 treats "
        "it as a non-fatal, expected STRANDED_STATE=missing (lode-3oik)"
    )


def _only_line_for(block: str, needle: str) -> str:
    """The single call-site line/statement in ``block`` mentioning ``needle``,
    isolated so a --require-nonempty check on one $SWEEP_TMP path does not
    accidentally match a DIFFERENT path's flag a few lines away."""
    lines = [ln for ln in block.splitlines() if needle in ln]
    assert len(lines) == 1, (
        f"expected exactly 1 line mentioning {needle!r}, found {len(lines)} -- "
        "this test's locator has drifted; re-check by hand"
    )
    return lines[0]
